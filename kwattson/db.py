"""SQLite storage (WAL). See docs/DATA_DICTIONARY.md for every table and column.

Conventions
- Every timestamp is UTC ISO-8601 text (``2026-11-17T01:30:00+00:00``).
- Half-hour data is keyed by the *start* of the interval.
- Energy columns are kWh, power columns kW, rates AUD/kWh, money AUD.
- Rows written by the planner are append-only; nothing rewrites a past decision.
- JSON columns hold structured detail; the columns most useful for analysis are
  also stored as plain columns so agents can query them with SQL.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import Settings, stamp

SCHEMA_VERSION = 2


def encode(value) -> str:
    return json.dumps(value, separators=(',', ':'), sort_keys=True, allow_nan=False, default=str)


def decode(value, default=None):
    return default if value is None else json.loads(value)


SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings_history(id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, changed TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS secrets(name TEXT PRIMARY KEY, ciphertext TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE, password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin' CHECK(role IN ('admin','member','viewer')), totp_cipher TEXT, totp_last INTEGER NOT NULL DEFAULT -1,
    created_at TEXT NOT NULL, last_login TEXT);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, csrf TEXT NOT NULL, created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL, expires TEXT NOT NULL, reauth_at TEXT);
CREATE TABLE IF NOT EXISTS rate_limits(key TEXT PRIMARY KEY, failures INTEGER NOT NULL, window_start REAL NOT NULL, locked_until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS raw_readings(id INTEGER PRIMARY KEY, at TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS system_readings(ts TEXT NOT NULL, system_id TEXT NOT NULL, plugin TEXT NOT NULL, soc REAL, pv_kw REAL,
    charge_kw REAL, discharge_kw REAL, ac_output_kw REAL, import_kw REAL, export_kw REAL, load_kw REAL, battery_temp REAL,
    running_state TEXT, fault INTEGER, time_verified INTEGER NOT NULL, extra TEXT NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY(system_id, ts));
CREATE TABLE IF NOT EXISTS site_readings(ts TEXT PRIMARY KEY, soc REAL, stored_kwh REAL, capacity_kwh REAL, solar_kw REAL NOT NULL,
    load_kw REAL NOT NULL, import_kw REAL NOT NULL, export_kw REAL NOT NULL, charge_kw REAL NOT NULL, discharge_kw REAL NOT NULL,
    ev_kw REAL NOT NULL DEFAULT 0, ev_estimated INTEGER NOT NULL DEFAULT 0, output_limited INTEGER NOT NULL DEFAULT 0, fault INTEGER NOT NULL DEFAULT 0,
    time_verified INTEGER NOT NULL, systems TEXT NOT NULL, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vehicle_readings(ts TEXT NOT NULL, vehicle_id TEXT NOT NULL, plugin TEXT NOT NULL, soc REAL, charging INTEGER,
    plugged_in INTEGER, at_home INTEGER, charge_kw REAL, range_km REAL, time_verified INTEGER NOT NULL, received_at TEXT NOT NULL,
    PRIMARY KEY(vehicle_id, ts));
CREATE TABLE IF NOT EXISTS pushed_readings(target TEXT PRIMARY KEY, received_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intervals(ts TEXT NOT NULL, source TEXT NOT NULL, solar_kwh REAL NOT NULL, load_kwh REAL NOT NULL, ev_kwh REAL NOT NULL,
    import_kwh REAL NOT NULL, export_kwh REAL NOT NULL, charge_kwh REAL NOT NULL, discharge_kwh REAL NOT NULL, soc_end REAL,
    limited_s REAL NOT NULL DEFAULT 0, coverage_s REAL NOT NULL, quality TEXT NOT NULL, PRIMARY KEY(ts, source));
CREATE TABLE IF NOT EXISTS daily_history(day TEXT NOT NULL, source TEXT NOT NULL, data TEXT NOT NULL, quality TEXT NOT NULL, PRIMARY KEY(day, source));
CREATE TABLE IF NOT EXISTS exclusions(id INTEGER PRIMARY KEY, start TEXT NOT NULL, end TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS holidays(id INTEGER PRIMARY KEY, name TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
    load_factor REAL, ev_away INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS annotations(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, start TEXT NOT NULL, end TEXT, kind TEXT NOT NULL, text TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS weather_hours(ts TEXT PRIMARY KEY, temperature REAL, apparent_temperature REAL, radiation REAL, cloud_cover REAL,
    fetched_at TEXT NOT NULL, lead_hours REAL NOT NULL);
CREATE TABLE IF NOT EXISTS forecasts(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, version TEXT NOT NULL, model TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecast_points(forecast_id INTEGER NOT NULL, ts TEXT NOT NULL, lead_minutes INTEGER NOT NULL, hours REAL NOT NULL,
    house_kwh REAL NOT NULL, house_p10 REAL NOT NULL, house_p90 REAL NOT NULL, ev_kwh REAL NOT NULL,
    solar_kwh REAL NOT NULL, solar_p10 REAL NOT NULL, solar_p90 REAL NOT NULL, temperature REAL, radiation REAL,
    load_basis TEXT NOT NULL, solar_basis TEXT NOT NULL, samples INTEGER NOT NULL, solar_by_system TEXT NOT NULL, PRIMARY KEY(forecast_id, ts));
CREATE TABLE IF NOT EXISTS evaluations(forecast_id INTEGER PRIMARY KEY, evaluated_at TEXT NOT NULL, metrics TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_params(name TEXT PRIMARY KEY, version TEXT NOT NULL, fitted_at TEXT NOT NULL, data TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, version TEXT NOT NULL, forecast_id INTEGER NOT NULL,
    mode TEXT NOT NULL, tariff_id TEXT NOT NULL, input_hash TEXT NOT NULL, inputs TEXT NOT NULL, summary TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_intervals(decision_id INTEGER NOT NULL, ts TEXT NOT NULL, hours REAL NOT NULL, action TEXT NOT NULL,
    fox_mode TEXT NOT NULL, load_kwh REAL NOT NULL, solar_kwh REAL NOT NULL, pv_to_ac_kwh REAL NOT NULL, pv_to_battery_kwh REAL NOT NULL,
    grid_to_battery_kwh REAL NOT NULL, discharge_kwh REAL NOT NULL, import_kwh REAL NOT NULL, export_kwh REAL NOT NULL,
    curtailed_kwh REAL NOT NULL, soc_pct REAL NOT NULL, buy_rate REAL NOT NULL, sell_rate REAL NOT NULL, cost REAL NOT NULL,
    baseline_soc_pct REAL NOT NULL, baseline_cost REAL NOT NULL, baseline_curtailed_kwh REAL NOT NULL, reason TEXT NOT NULL, systems TEXT NOT NULL,
    PRIMARY KEY(decision_id, ts));
CREATE TABLE IF NOT EXISTS commands(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, decision_id INTEGER, action TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL, result TEXT NOT NULL, verified_at TEXT, idempotency_key TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS overrides(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, starts TEXT NOT NULL, expires TEXT NOT NULL,
    kind TEXT NOT NULL, data TEXT NOT NULL, cancelled_at TEXT);
CREATE TABLE IF NOT EXISTS daily_outcomes(day TEXT PRIMARY KEY, computed_at TEXT NOT NULL, coverage REAL NOT NULL, data TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, enabled INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notification_rules(event TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, event TEXT NOT NULL, severity TEXT NOT NULL,
    title TEXT NOT NULL, body TEXT NOT NULL, data TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0, dedupe TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY, notification_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT NOT NULL, result TEXT NOT NULL, UNIQUE(notification_id, channel_id));
CREATE TABLE IF NOT EXISTS api_tokens(id INTEGER PRIMARY KEY, name TEXT NOT NULL, hash TEXT UNIQUE NOT NULL, scope TEXT NOT NULL,
    created_at TEXT NOT NULL, last_used TEXT);

CREATE INDEX IF NOT EXISTS idx_system_ts ON system_readings(ts);
CREATE INDEX IF NOT EXISTS idx_intervals_ts ON intervals(ts);
CREATE INDEX IF NOT EXISTS idx_points_ts ON forecast_points(ts);
CREATE INDEX IF NOT EXISTS idx_forecasts_at ON forecasts(created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_at ON decisions(created_at);
CREATE INDEX IF NOT EXISTS idx_decision_intervals_ts ON decision_intervals(ts);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at);
CREATE INDEX IF NOT EXISTS idx_notifications_at ON notifications(created_at);
CREATE INDEX IF NOT EXISTS idx_raw_at ON raw_readings(at);
'''

# Periods that must never train the models start empty; owners add their own (e.g. a fault week).
DEFAULT_EXCLUSIONS: list[tuple[str, str, str]] = []


def migrate(c, version: int):
    """Upgrade an older database in place. Each step is idempotent and runs in the open transaction."""
    if version < 2:
        # v2: multiple users with roles (v1 allowed a single owner with id=1).
        columns = {r['name'] for r in c.execute('PRAGMA table_info(users)')}
        if 'role' not in columns:
            c.execute('ALTER TABLE users RENAME TO users_v1')
            c.execute(SCHEMA[SCHEMA.index('CREATE TABLE IF NOT EXISTS users('):SCHEMA.index('CREATE TABLE IF NOT EXISTS sessions(')])
            c.execute("INSERT INTO users(id, username, password_hash, role, totp_cipher, totp_last, created_at) "
                      "SELECT id, username, password_hash, 'admin', totp_cipher, totp_last, created_at FROM users_v1")
            c.execute('DROP TABLE users_v1')
    c.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))


class Database:
    def __init__(self, path, *, demo: bool | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as c:
            c.executescript(SCHEMA)
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if demo is not None:
                marker = c.execute("SELECT value FROM meta WHERE key='demo_mode'").fetchone()
                legacy_demo = c.execute("SELECT 1 FROM meta WHERE key='demo_seeded'").fetchone()
                if row and (decode(marker['value']) if marker else bool(legacy_demo)) != demo:
                    raise RuntimeError('Demo and normal mode need separate data directories. Choose another DATA_DIR.')
                c.execute("INSERT OR IGNORE INTO meta VALUES('demo_mode', ?)", (encode(demo),))
            if row is None:
                c.execute("INSERT INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
                if DEFAULT_EXCLUSIONS:
                    c.executemany('INSERT INTO exclusions(start, end, reason) VALUES(?,?,?)', DEFAULT_EXCLUSIONS)
            elif int(row['value']) > SCHEMA_VERSION:
                raise RuntimeError('Database was created by a newer version of this app')
            else:
                migrate(c, int(row['value']))
            c.execute('INSERT OR IGNORE INTO settings VALUES(1, ?, ?)', (encode(Settings().model_dump()), stamp()))
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA busy_timeout=30000')
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def rows(self, query, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(query, args)]

    def one(self, query, args=()):
        with self.connect() as c:
            row = c.execute(query, args).fetchone()
            return dict(row) if row else None

    def execute(self, query, args=()):
        with self.connect() as c:
            return c.execute(query, args).lastrowid

    # Settings -----------------------------------------------------------
    def settings(self) -> Settings:
        return Settings.model_validate(decode(self.one('SELECT data FROM settings WHERE id=1')['data']))

    def save_settings(self, value: Settings, actor='system'):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            return self.save_settings_in(c, value, actor)

    @staticmethod
    def save_settings_in(c, value: Settings, actor):
        old = decode(c.execute('SELECT data FROM settings WHERE id=1').fetchone()['data'])
        new = value.model_dump()
        changed = sorted(k for k in new if old.get(k) != new[k])
        c.execute('UPDATE settings SET data=?, updated_at=? WHERE id=1', (encode(new), stamp()))
        if changed:
            c.execute('INSERT INTO settings_history(at, actor, changed, data) VALUES(?,?,?,?)',
                      (stamp(), actor, encode(changed), encode(new)))
        return changed

    # Meta ---------------------------------------------------------------
    def meta(self, key, default=None):
        row = self.one('SELECT value FROM meta WHERE key=?', (key,))
        return decode(row['value']) if row else default

    def set_meta(self, key, value):
        self.execute('INSERT INTO meta VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, encode(value)))

    # Records ------------------------------------------------------------
    def audit(self, event, data=None, actor='system'):
        return self.execute('INSERT INTO audit(at, actor, event, data) VALUES(?,?,?,?)', (stamp(), actor, event, encode(data or {})))

    def raw(self, source, payload, at=None):
        text = encode(payload)
        digest = hashlib.sha256((source + text).encode()).hexdigest()
        self.execute('INSERT OR IGNORE INTO raw_readings(at, source, payload, digest) VALUES(?,?,?,?)', (at or stamp(), source, text, digest))

    def latest_site(self):
        row = self.one('SELECT * FROM site_readings ORDER BY ts DESC LIMIT 1')
        if row:
            row['systems'] = decode(row['systems'])
        return row

    def latest_system(self, system_id):
        row = self.one('SELECT * FROM system_readings WHERE system_id=? ORDER BY ts DESC LIMIT 1', (system_id,))
        if row:
            row['extra'] = decode(row['extra'])
        return row

    def latest_vehicle(self, vehicle_id):
        return self.one('SELECT * FROM vehicle_readings WHERE vehicle_id=? ORDER BY ts DESC LIMIT 1', (vehicle_id,))

    def backup(self, target):
        target = Path(target)
        with self.connect() as c:
            dest = sqlite3.connect(target)
            try:
                c.backup(dest)
            finally:
                dest.close()
        os.chmod(target, 0o600)
        return target
