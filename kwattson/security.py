"""Owner authentication, encrypted secrets, rate limiting and outbound URL checks.

- Single owner account; Argon2id password hashes; optional TOTP with replay protection.
- Sessions: random token in an HttpOnly, Secure, SameSite=Strict __Host- cookie;
  only its SHA-256 is stored. Separate CSRF token required on unsafe requests.
- Secrets (API keys, channel credentials) are Fernet-encrypted with a key file in
  the data directory (``master.key``). Back that file up with the database.
- Rate limits count failures per key in a sliding 15-minute window.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from .config import Runtime, stamp, utcnow
from .db import Database

PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
SESSION_HOURS = 12
IDLE_MINUTES = 60
WINDOW = 900
MAX_FAILURES = 5
LOCK_SECONDS = 900
ROLES = ('admin', 'member', 'viewer')


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Security:
    def __init__(self, db: Database, runtime: Runtime):
        self.db, self.runtime = db, runtime
        key_file = runtime.data_dir / 'master.key'
        if not key_file.exists():
            if db.one('SELECT 1 FROM secrets LIMIT 1'):
                raise RuntimeError('Encrypted secrets exist but master.key is missing; restore it from your backup')
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        self.fernet = Fernet(key_file.read_bytes())
        self.bootstrap = runtime.data_dir / 'setup-token'
        if not self.owner_exists() and not self.bootstrap.exists():
            fd = os.open(self.bootstrap, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(secrets.token_urlsafe(32))

    # Secrets -------------------------------------------------------------
    def secret(self, name: str) -> str | None:
        row = self.db.one('SELECT ciphertext FROM secrets WHERE name=?', (name,))
        if not row:
            return None
        try:
            return self.fernet.decrypt(row['ciphertext'].encode()).decode()
        except InvalidToken:
            raise RuntimeError('A stored secret cannot be decrypted; master.key does not match this database') from None

    def save_secret(self, name: str, value: str | None):
        with self.db.connect() as c:
            self.save_secret_in(c, name, value)

    def save_secret_in(self, c, name, value):
        if not value:
            c.execute('DELETE FROM secrets WHERE name=?', (name,))
            return
        c.execute('INSERT INTO secrets VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET ciphertext=excluded.ciphertext, '
                        'updated_at=excluded.updated_at', (name, self.fernet.encrypt(value.encode()).decode(), stamp()))

    def secret_names(self, prefix: str) -> list[str]:
        return [r['name'][len(prefix):] for r in self.db.rows('SELECT name FROM secrets WHERE substr(name,1,?)=?', (len(prefix), prefix))]

    # Rate limiting -------------------------------------------------------
    def check_limit(self, key: str):
        row = self.db.one('SELECT * FROM rate_limits WHERE key=?', (digest(key),))
        if row and row['locked_until'] > time.time():
            raise HTTPException(429, 'Too many attempts; try again in 15 minutes')

    def fail(self, key: str):
        hashed, now = digest(key), time.time()
        with self.db.connect() as c:
            row = c.execute('SELECT * FROM rate_limits WHERE key=?', (hashed,)).fetchone()
            failures, start = (row['failures'], row['window_start']) if row and now - row['window_start'] < WINDOW else (0, now)
            failures += 1
            locked = now + LOCK_SECONDS if failures >= MAX_FAILURES else 0
            c.execute('INSERT OR REPLACE INTO rate_limits VALUES(?,?,?,?)', (hashed, failures, start, locked))

    def clear_limit(self, key: str):
        self.db.execute('DELETE FROM rate_limits WHERE key=?', (digest(key),))

    def throttle(self, key: str, per_window: int):
        """Count every call (not just failures), e.g. for unauthenticated report endpoints."""
        self.check_limit(key)
        hashed, now = digest(key), time.time()
        with self.db.connect() as c:
            row = c.execute('SELECT * FROM rate_limits WHERE key=?', (hashed,)).fetchone()
            count, start = (row['failures'], row['window_start']) if row and now - row['window_start'] < WINDOW else (0, now)
            count += 1
            c.execute('INSERT OR REPLACE INTO rate_limits VALUES(?,?,?,?)', (hashed, count, start, now + LOCK_SECONDS if count > per_window else 0))

    # Owner ----------------------------------------------------------------
    def owner_exists(self) -> bool:
        return self.db.one('SELECT id FROM users') is not None

    @staticmethod
    def validate_password(password: str):
        if not 14 <= len(password) <= 256:
            raise HTTPException(422, 'Use a password or passphrase of 14–256 characters')

    def setup(self, token: str, username: str, password: str, client: str):
        key = 'setup:' + client
        self.check_limit(key)
        if self.owner_exists():
            raise HTTPException(409, 'Owner already exists')
        expected = self.bootstrap.read_text().strip() if self.bootstrap.exists() else ''
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            self.fail(key)
            raise HTTPException(403, 'Invalid setup token')
        self.validate_password(password)
        if not 3 <= len(username) <= 80:
            raise HTTPException(422, 'Username must be 3–80 characters')
        self.create_owner(username, password)

    def create_owner(self, username: str, password: str):
        """First account: always an admin."""
        self.create_user(username, password, 'admin', actor='owner')
        self.bootstrap.unlink(missing_ok=True)

    def create_user(self, username: str, password: str, role: str, actor: str = 'owner') -> int:
        if role not in ROLES:
            raise HTTPException(422, 'Role must be admin, member or viewer')
        if not 3 <= len(username) <= 80 or not username.replace('_', '').replace('-', '').replace('.', '').replace('@', '').isalnum():
            raise HTTPException(422, 'Usernames are 3–80 letters, numbers or . _ - @')
        self.validate_password(password)
        if self.db.one('SELECT 1 FROM users WHERE username=?', (username,)):
            raise HTTPException(409, 'That username is taken')
        user_id = self.db.execute('INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)',
                                  (username, PASSWORDS.hash(password), role, stamp()))
        self.db.audit('user_created', {'user_id': user_id, 'role': role}, actor=actor)
        return user_id

    def admins(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM users WHERE role='admin'")['n']

    def reset_user(self, username: str, password: str, disable_mfa: bool, make_admin: bool = False):
        """Local-console recovery (CLI only): new password, that user's sessions revoked."""
        self.validate_password(password)
        user = self.db.one('SELECT id FROM users WHERE username=?', (username,))
        if not user:
            raise ValueError(f'No user called {username}')
        with self.db.connect() as c:
            c.execute('UPDATE users SET password_hash=? WHERE id=?', (PASSWORDS.hash(password), user['id']))
            if disable_mfa:
                c.execute('UPDATE users SET totp_cipher=NULL, totp_last=-1 WHERE id=?', (user['id'],))
            if make_admin:
                c.execute("UPDATE users SET role='admin' WHERE id=?", (user['id'],))
            c.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
            c.execute('DELETE FROM rate_limits')
        self.db.audit('user_recovered', {'user_id': user['id'], 'mfa_disabled': disable_mfa, 'made_admin': make_admin}, actor='console')

    def _password_ok(self, user, password) -> bool:
        try:
            return PASSWORDS.verify(user['password_hash'], password)
        except (VerificationError, InvalidHashError):
            return False

    def _totp_ok(self, user, code: str) -> bool:
        if not user.get('totp_cipher'):
            return True
        if not code or not code.isdigit():
            return False
        seed = self.fernet.decrypt(user['totp_cipher'].encode()).decode()
        otp = pyotp.TOTP(seed)
        counter = int(time.time()) // 30
        for count in (counter - 1, counter, counter + 1):
            if count > user['totp_last'] and hmac.compare_digest(otp.at(count * 30), code):
                with self.db.connect() as c:
                    # Atomic: a code can be used once, even by concurrent requests.
                    return c.execute('UPDATE users SET totp_last=? WHERE id=? AND totp_last<?', (count, user['id'], count)).rowcount == 1
        return False

    def login(self, username: str, password: str, otp: str, client: str) -> tuple[str, str]:
        key = 'login:' + client
        self.check_limit(key)
        self.check_limit('login:any')
        user = self.db.one('SELECT * FROM users WHERE username=?', (username,))
        ok = bool(user) and self._password_ok(user, password) and self._totp_ok(user, otp)
        if not user:
            PASSWORDS.hash(password[:256])  # similar timing for unknown usernames
        if not ok:
            self.fail(key)
            self.fail('login:any')  # global brake against distributed guessing
            self.db.audit('login_failed', {}, actor='anonymous')
            raise HTTPException(401, 'Invalid username, password or authenticator code')
        self.clear_limit(key)
        self.db.execute('UPDATE users SET last_login=? WHERE id=?', (stamp(), user['id']))
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        self.db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?)', (digest(token), user['id'], csrf, stamp(), stamp(),
                                                                       stamp(utcnow() + timedelta(hours=SESSION_HOURS)), stamp()))
        self.db.audit('login', actor='owner')
        return token, csrf

    def authenticate(self, token: str) -> dict:
        row = self.db.one('SELECT s.*, u.username, u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE token_hash=?', (digest(token or ''),))
        now = utcnow()
        if (not row or datetime.fromisoformat(row['expires']) < now
                or datetime.fromisoformat(row['last_seen']) < now - timedelta(minutes=IDLE_MINUTES)):
            raise HTTPException(401, 'Sign in required')
        if datetime.fromisoformat(row['last_seen']) < now - timedelta(minutes=1):
            self.db.execute('UPDATE sessions SET last_seen=? WHERE token_hash=?', (stamp(now), row['token_hash']))
        return row

    def check_csrf(self, session: dict, header: str):
        if not hmac.compare_digest((header or '').encode(), session['csrf'].encode()):
            raise HTTPException(403, 'Security token missing or stale; refresh the page')

    def reauthenticate(self, user_id: int, password: str, otp: str = ''):
        """Confirm the signed-in user's password (and code) before sensitive actions."""
        key = f'reauth:{user_id}'
        self.check_limit(key)
        user = self.db.one('SELECT * FROM users WHERE id=?', (user_id,))
        if not user or not self._password_ok(user, password) or not self._totp_ok(user, otp):
            self.fail(key)
            raise HTTPException(403, 'Password or authenticator code incorrect')
        self.clear_limit(key)
        return user


def safe_destination(url: str, expected_hosts: set[str] | None = None) -> str:
    """Allow only HTTPS URLs to public addresses, unless the host is explicitly allowed.

    Private/LAN destinations (e.g. Home Assistant on 192.168.x.x) must be listed in
    OUTBOUND_ALLOW_HOSTS. Redirects are never followed by callers.
    """
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError('Use an https:// URL without credentials or #fragment')
    host = parts.hostname.lower()
    if expected_hosts and host not in expected_hosts:
        raise ValueError('Unexpected destination host')
    allowed = {h.strip().lower() for h in os.getenv('OUTBOUND_ALLOW_HOSTS', '').split(',') if h.strip()}
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        raise ValueError('Destination host cannot be resolved') from None
    if host not in allowed and any(not ipaddress.ip_address(a.split('%')[0]).is_global for a in addresses):
        raise ValueError(f'{host} is a private address; add it to OUTBOUND_ALLOW_HOSTS to allow it')
    return url
