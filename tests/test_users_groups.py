"""Multiple users with roles, alert groups, schema migration and the design-variant route."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from kwattson.config import Runtime
from kwattson.db import Database
from kwattson.web.app import create_app
from kwattson.web.permissions import allowed, required_role

ORIGIN = 'http://localhost:8080'
ADMIN_PW = 'a-long-test-password-123'
MEMBER_PW = 'member-password-123456'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DEMO_MODE', 'true')
    monkeypatch.setenv('DEMO_PASSWORD', ADMIN_PW)
    app = create_app(Runtime(data_dir=tmp_path, app_url=ORIGIN, dev_http=True, worker=False))
    return TestClient(app, base_url=ORIGIN)


def login(client, username='demo', password=ADMIN_PW):
    client.cookies.clear()
    r = client.post('/api/auth/login', json={'username': username, 'password': password}, headers={'origin': ORIGIN})
    assert r.status_code == 200, r.text
    return {'x-csrf-token': r.json()['csrf'], 'origin': ORIGIN}


def add_user(client, headers, username, role, password=MEMBER_PW):
    r = client.post('/api/users', json={'username': username, 'role': role, 'initial_password': password, 'password': ADMIN_PW},
                    headers=headers)
    assert r.status_code == 200, r.text
    return r.json()['id']


def test_permission_table():
    assert required_role('GET', '/api/status') == 'viewer'
    assert required_role('POST', '/api/overrides') == 'member'
    assert required_role('PUT', '/api/settings') == 'admin'
    assert required_role('GET', '/api/settings') == 'admin'
    assert required_role('POST', '/api/unknown-new-endpoint') == 'admin'  # default deny
    assert allowed('member', 'POST', '/api/control/stop') and not allowed('viewer', 'POST', '/api/control/stop')
    assert allowed('member', 'POST', '/api/vehicles/car/charge')
    assert not allowed('viewer', 'POST', '/api/vehicles/car/charge')
    assert not allowed('viewer', 'POST', '/api/vehicles/car/refresh')
    assert not allowed('viewer', 'POST', '/api/vehicles/car/climate')
    assert allowed('member', 'POST', '/api/systems/main/mode')
    assert not allowed('viewer', 'POST', '/api/systems/main/mode')


def test_roles_are_enforced(client):
    admin = login(client)
    add_user(client, admin, 'helper', 'member')
    add_user(client, admin, 'guest', 'viewer')

    member = login(client, 'helper', MEMBER_PW)
    assert client.get('/api/status').status_code == 200
    assert client.post('/api/overrides', json={'kind': 'hold', 'minutes': 30}, headers=member).status_code == 200
    assert client.get('/api/settings').status_code == 403
    assert client.post('/api/users', json={'username': 'x' * 5, 'role': 'admin', 'initial_password': 'p' * 20,
                                           'password': MEMBER_PW}, headers=member).status_code == 403

    viewer = login(client, 'guest', MEMBER_PW)
    assert client.get('/api/plan').status_code == 200
    assert client.post('/api/overrides', json={'kind': 'hold', 'minutes': 30}, headers=viewer).status_code == 403
    assert client.get('/api/overrides').status_code == 200 and client.get('/api/holidays').status_code == 200
    session = client.get('/api/auth/session').json()
    assert session['role'] == 'viewer' and session['username'] == 'guest'


def test_last_admin_protected_and_role_change(client):
    admin = login(client)
    users = client.get('/api/users').json()
    me = next(u for u in users if u['you'])
    r = client.put(f"/api/users/{me['id']}", json={'role': 'member', 'password': ADMIN_PW}, headers=admin)
    assert r.status_code == 409
    other = add_user(client, admin, 'second-admin', 'admin')
    assert client.post(f'/api/users/{me["id"]}/delete', json={'password': ADMIN_PW}, headers=admin).status_code == 409  # not yourself
    assert client.put(f'/api/users/{other}', json={'role': 'viewer', 'password': ADMIN_PW}, headers=admin).status_code == 200
    assert client.post(f'/api/users/{other}/delete', json={'password': ADMIN_PW}, headers=admin).status_code == 200


def test_user_actions_need_password(client):
    admin = login(client)
    r = client.post('/api/users', json={'username': 'nope', 'role': 'viewer', 'initial_password': MEMBER_PW,
                                        'password': 'wrong-password-xxxx'}, headers=admin)
    assert r.status_code == 403


def test_alert_groups(client):
    admin = login(client)
    groups = client.get('/api/notification-groups').json()
    car = next(g for g in groups if g['id'] == 'car')
    assert {e['event'] for e in car['events']} == {'ev_charge_opportunity', 'ev_charge_reminder', 'ev_charge_session', 'vehicle_climate'}
    channel = client.post('/api/channels', json={'name': 'Phone', 'kind': 'ntfy', 'config': {'topic': 'abc-123'}}, headers=admin).json()['id']
    r = client.put('/api/notification-groups/car', json={'events': {'ev_charge_reminder': False}, 'channels': [channel],
                                                        'cooldown_minutes': 90, 'respect_quiet': True}, headers=admin)
    assert r.status_code == 200
    car = next(g for g in client.get('/api/notification-groups').json() if g['id'] == 'car')
    assert car['channels'] == [channel] and car['cooldown_minutes'] == 90
    assert {e['event']: e['enabled'] for e in car['events']} == {
        'ev_charge_opportunity': True, 'ev_charge_reminder': False, 'ev_charge_session': True, 'vehicle_climate': True,
    }
    bad = client.put('/api/notification-groups/car', json={'events': {'high_price': True}}, headers=admin)
    assert bad.status_code == 422


def test_critical_alerts_ignore_quiet_hours(client):
    admin = login(client)
    client.put('/api/notification-groups/system', json={'respect_quiet': True, 'cooldown_minutes': 30}, headers=admin)
    events = {r['event']: r for r in client.get('/api/notification-rules').json()}
    assert events['system_fault']['respect_quiet'] is False      # critical
    assert events['telemetry_stale']['respect_quiet'] is True


def test_v1_database_migrates(tmp_path):
    path = tmp_path / 'old.sqlite3'
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES('schema_version', '1');
        CREATE TABLE users(id INTEGER PRIMARY KEY CHECK(id=1), username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            totp_cipher TEXT, totp_last INTEGER NOT NULL DEFAULT -1, created_at TEXT NOT NULL);
        INSERT INTO users(id, username, password_hash, created_at) VALUES(1, 'owner', 'hash', '2026-01-01');
    """)
    c.commit()
    c.close()
    db = Database(path)
    assert db.one("SELECT value FROM meta WHERE key='schema_version'")['value'] == '2'
    with sqlite3.connect(path.with_suffix('.before-v1-to-v2.sqlite3')) as snapshot:
        assert snapshot.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ('1',)
        assert snapshot.execute('SELECT username FROM users').fetchone() == ('owner',)
    assert db.one('SELECT username, role FROM users') == {'username': 'owner', 'role': 'admin'}
    db.execute("INSERT INTO users(username, password_hash, role, created_at) VALUES('second', 'h', 'viewer', 'now')")  # CHECK(id=1) gone


def test_dashboard_build_policy(tmp_path, monkeypatch):
    """static/csp.json may request a style nonce; unknown Trusted Types names are ignored."""
    import kwattson.web.app as web
    static = tmp_path / 'static'
    static.mkdir()
    (static / 'index.html').write_text('<meta name="csp-nonce" content="__CSP_NONCE__"><script nonce="__CSP_NONCE__" src="/static/a.js"></script>')
    (static / 'csp.json').write_text('{"style_nonce": true, "trusted_types": ["default", "lit-html"]}')
    monkeypatch.setattr(web, 'STATIC', static)
    monkeypatch.setenv('DEMO_MODE', 'true')
    monkeypatch.setenv('DEMO_PASSWORD', ADMIN_PW)
    app = create_app(Runtime(data_dir=tmp_path / 'data', app_url=ORIGIN, dev_http=True, worker=False))
    r = TestClient(app, base_url=ORIGIN).get('/')
    csp = r.headers['content-security-policy']
    assert '__CSP_NONCE__' not in r.text
    assert "style-src 'self' 'nonce-" in csp and 'trusted-types lit-html;' in csp and 'default' not in csp.split('trusted-types')[1].split(';')[0]
    api = TestClient(app, base_url=ORIGIN).get('/api/auth/status').headers['content-security-policy']
    assert "style-src 'self';" in api  # nonce only on the page that needs it
