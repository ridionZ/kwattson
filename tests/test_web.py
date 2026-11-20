"""HTTP security behaviour and the main API flows (demo site, no network)."""
import json
import time
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from kwattson.config import Runtime
from kwattson.web.app import create_app

ORIGIN = 'http://localhost:8080'
PASSWORD = 'a-long-test-password-123'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DEMO_MODE', 'true')
    monkeypatch.setenv('DEMO_PASSWORD', PASSWORD)
    app = create_app(Runtime(data_dir=tmp_path, app_url=ORIGIN, dev_http=True, worker=False))
    return TestClient(app, base_url=ORIGIN)


def login(client, password=PASSWORD, otp=''):
    r = client.post('/api/auth/login', json={'username': 'demo', 'password': password, 'otp': otp}, headers={'origin': ORIGIN})
    assert r.status_code == 200, r.text
    return {'x-csrf-token': r.json()['csrf'], 'origin': ORIGIN}


def test_security_headers(client):
    r = client.get('/')
    csp = r.headers['content-security-policy']
    assert "require-trusted-types-for 'script'" in csp and "frame-ancestors 'none'" in csp and 'nonce-' in csp
    assert 'unsafe-inline' not in csp
    assert r.headers['x-content-type-options'] == 'nosniff'
    assert 'nonce="' in r.text and '__CSP_NONCE__' not in r.text


def test_wrong_host_rejected(client):
    assert client.get('/api/auth/status', headers={'host': 'evil.example'}).status_code == 400


def test_api_requires_session(client):
    assert client.get('/api/status').status_code == 401


def test_csrf_and_origin_required(client):
    headers = login(client)
    assert client.post('/api/plan', headers={'origin': ORIGIN}).status_code == 403
    assert client.post('/api/plan', headers={**headers, 'origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/plan', headers={**headers, 'sec-fetch-site': 'cross-site'}).status_code == 403
    assert client.post('/api/plan', headers=headers).status_code == 200


def test_login_rate_limit(client):
    for _ in range(5):
        assert client.post('/api/auth/login', json={'username': 'demo', 'password': 'wrong-password-xx'}).status_code == 401
    assert client.post('/api/auth/login', json={'username': 'demo', 'password': PASSWORD}).status_code == 429


def test_oversized_body_rejected(client):
    headers = login(client)
    r = client.put('/api/settings', content=b'{' + b' ' * 2_100_000 + b'}', headers={**headers, 'content-type': 'application/json'})
    assert r.status_code == 413


def test_mfa_enrol_and_replay_protection(client):
    headers = login(client)
    seed = client.post('/api/auth/mfa/start', json={'password': PASSWORD}, headers=headers).json()['secret']
    totp = pyotp.TOTP(seed)
    assert client.post('/api/auth/mfa/confirm', json={'password': PASSWORD, 'otp': totp.now()}, headers=headers).json()['enabled']
    client.cookies.clear()
    assert client.post('/api/auth/login', json={'username': 'demo', 'password': PASSWORD}).status_code == 401
    code = totp.at(time.time() + 30)  # next window (the current one was consumed by enrolment)
    assert client.post('/api/auth/login', json={'username': 'demo', 'password': PASSWORD, 'otp': code}).status_code == 200
    client.cookies.clear()
    assert client.post('/api/auth/login', json={'username': 'demo', 'password': PASSWORD, 'otp': code}).status_code == 401  # replay


def test_settings_validation_and_secrets_never_returned(client):
    headers = login(client)
    data = client.get('/api/settings').json()['settings']
    data['site']['export_kw'] = -1
    r = client.put('/api/settings', json=data, headers=headers)
    assert r.status_code == 422 and 'export_kw' in r.json()['detail']
    r = client.put('/api/secrets/system:main', json={'values': {'api_key': 'x'}}, headers=headers)
    assert r.status_code == 422  # the simulator has no secrets
    r = client.post('/api/channels', json={'name': 'n', 'kind': 'ntfy', 'config': {'topic': 'secret-topic-abc', 'token': 'tk_SECRET'}}, headers=headers)
    assert r.status_code == 200
    listing = client.get('/api/channels').text
    assert 'tk_SECRET' not in listing and 'token' in json.loads(listing)['channels'][0]['configured']


def test_export_redacts(client):
    headers = login(client)
    client.post('/api/channels', json={'name': 'n', 'kind': 'ntfy', 'config': {'topic': 'topic123', 'token': 'tk_SECRET'}}, headers=headers)
    client.post('/api/plan', headers=headers)
    text = client.get('/api/export?days=30').text
    assert 'tk_SECRET' not in text and 'password_hash' not in text and 'token_hash' not in text and 'plugin_config":{' not in text
    assert json.loads(text.splitlines()[0])['type'] == 'manifest'
    assert any(json.loads(line)['type'] == 'decision_intervals' for line in text.splitlines())


def test_ingest_token_scopes(client):
    headers = login(client)
    data = client.get('/api/settings').json()['settings']
    data['systems'].append({'id': 'pushed', 'name': 'Pushed', 'plugin': 'push', 'ac_kw': 5, 'pv_kwp': 3})
    assert client.put('/api/settings', json=data, headers=headers).status_code == 200
    read = client.post('/api/tokens', json={'password': PASSWORD, 'name': 'ha', 'scope': 'read'}, headers=headers).json()['token']
    ingest = client.post('/api/tokens', json={'password': PASSWORD, 'name': 'dev', 'scope': 'ingest'}, headers=headers).json()['token']
    client.cookies.clear()
    payload = {'ts': '2026-11-10T00:00:00Z', 'soc': 50, 'pv_kw': 1}
    assert client.post('/api/ingest/system/pushed', json=payload, headers={'authorization': 'Bearer ' + read}).status_code == 401
    assert client.post('/api/ingest/system/pushed', json=payload, headers={'authorization': 'Bearer ' + ingest}).status_code == 200
    assert client.get('/api/ha/state', headers={'authorization': 'Bearer ' + read}).status_code == 200
    assert client.get('/api/status', headers={'authorization': 'Bearer ' + read}).status_code == 401


def test_control_enable_blocked_by_guards(client):
    headers = login(client)
    r = client.post('/api/control/enable', json={'password': PASSWORD, 'confirmation': 'ENABLE BATTERY CONTROL', 'acknowledge': True}, headers=headers)
    assert r.status_code == 409 and 'Not enabled' in r.json()['detail']
    assert client.get('/api/settings').json()['settings']['mode'] == 'monitor'


def test_tariff_activation_requires_password(client):
    headers = login(client)
    plan = json.loads((Path(__file__).parents[1] / 'tariffs' / 'covau-solarmax-qld.json').read_text())
    plan['id'] = 'covau-solarmax'
    assert client.post('/api/tariffs/import', json=plan, headers=headers).status_code == 200
    r = client.post('/api/tariffs/covau-solarmax/activate', json={'password': 'wrong-password-zz', 'active_from': '2026-11-20T00:00'}, headers=headers)
    assert r.status_code == 403
    r = client.post('/api/tariffs/covau-solarmax/activate', json={'password': PASSWORD, 'active_from': '2026-11-20T00:00'}, headers=headers)
    assert r.status_code == 200 and r.json()['active_from'] == '2026-11-19T14:00:00+00:00'



def test_demo_rejects_live_device_sources(client):
    headers = login(client)
    catalogue = client.get('/api/settings').json()['plugins']
    assert 'foxess' not in {p['name'] for p in catalogue}
    spec = {'id': 'new', 'name': 'New', 'plugin': 'foxess', 'ac_kw': 5}
    path = '/api/devices/system'
    assert client.put(path, json={'spec': spec}, headers=headers).status_code == 422
    response = client.post('/api/test-draft/system', json={'spec': spec}, headers=headers)
    assert 'in the demo' in response.json()['detail']


def test_wizard_saves_credentials_atomically_and_detects_conflicts(tmp_path, monkeypatch):
    monkeypatch.setenv('DEMO_MODE', 'false')
    runtime = Runtime(data_dir=tmp_path, app_url=ORIGIN, dev_http=True, worker=False)
    app = create_app(runtime)
    app.state.security.create_owner('demo', PASSWORD)
    client = TestClient(app, base_url=ORIGIN)
    headers = login(client)
    spec = {'id': 'meter', 'name': 'Sample meter', 'plugin': 'foxess', 'ac_kw': 5}
    url = '/api/devices/system'
    assert client.put(url, json={'spec': spec}, headers=headers).status_code == 422
    assert app.state.db.settings().systems == []
    sample_key = 'synthetic-fixture-value'
    payload = {'spec': spec, 'secrets': {'api_key': sample_key}}
    assert client.put(url, json=payload, headers=headers).status_code == 200
    assert client.put(url, json=payload, headers=headers).status_code == 409
    assert app.state.security.secret('system:meter:api_key') == sample_key
    assert sample_key not in client.get('/api/settings').text
    assert sample_key not in client.get('/api/audit').text
    original = app.state.db.settings().systems[0].model_dump()
    edit = {'original': original, 'spec': {**original, 'name': 'Renamed'}}
    assert client.put(url, json=edit, headers=headers).status_code == 200
    assert app.state.security.secret('system:meter:api_key') == sample_key
    assert client.put(url, json=edit, headers=headers).status_code == 409
    # A failure after encryption must roll back the new credential as well.
    def fail_write(*args):
        raise ValueError('Fixture write failure')
    monkeypatch.setattr(app.state.db, 'save_settings_in', fail_write)
    edit['original'] = edit['spec']
    edit['secrets'] = {'api_key': 'different-fixture-value'}
    assert client.put(url, json=edit, headers=headers).status_code == 422
    assert app.state.security.secret('system:meter:api_key') == sample_key


def test_vehicle_controls_require_plugged_start_and_confirm_new_physical_state(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from kwattson.config import Vehicle, stamp, utcnow
    from kwattson.db import encode
    from kwattson.ingest import store_vehicle
    from kwattson.plugins.base import VehicleReading
    from kwattson.web import api

    monkeypatch.setenv('DEMO_MODE', 'false')
    app = create_app(Runtime(data_dir=tmp_path, app_url=ORIGIN, dev_http=True, worker=False))
    app.state.security.create_owner('demo', PASSWORD)
    db, engine = app.state.db, app.state.engine
    settings = db.settings()
    car = Vehicle(id='car', name='Car', plugin='mg_ismart', capacity_kwh=60,
                  plugin_config={'allow_charge_schedule': True})
    settings.vehicles = [car]
    db.save_settings(settings)
    plugin = SimpleNamespace(start_charging=AsyncMock(), stop_charging=AsyncMock())
    monkeypatch.setattr(engine.devices, 'vehicle', lambda *_: plugin)
    now = utcnow().replace(microsecond=0)
    monkeypatch.setattr(api, 'utcnow', lambda: now)
    def reading(at, charging, plugged):
        store_vehicle(db, car.id, car.plugin, VehicleReading(ts=at, time_verified=False, soc=60,
                                                            charging=charging, plugged_in=plugged))
    client = TestClient(app, base_url=ORIGIN)
    headers = login(client)
    reading(now, False, False)
    assert client.post('/api/vehicles/car/charge', json={'action': 'start'}, headers=headers).status_code == 409
    plugin.start_charging.assert_not_called()
    auto = db.execute('INSERT INTO overrides(created_at,starts,expires,kind,data) VALUES(?,?,?,?,?)',
                      (stamp(now), stamp(now), stamp(now + timedelta(hours=1)), 'ev_charge_now',
                       encode({'vehicle_id': car.id, 'auto_schedule': True})))
    reading(now, False, True)
    response = client.post('/api/vehicles/car/charge', json={'action': 'start'}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'pending'
    assert db.one('SELECT cancelled_at FROM overrides WHERE id=?', (auto,))['cancelled_at']
    plugin.start_charging.assert_awaited_once()
    assert client.get('/api/status').json()['vehicles'][0]['pending_charge']['action'] == 'start'
    refreshed_at = now + timedelta(seconds=1)
    async def refresh(*_):
        reading(refreshed_at, True, True)
    monkeypatch.setattr(engine.devices, 'refresh_vehicle', refresh)
    monkeypatch.setattr(api, 'utcnow', lambda: now + timedelta(seconds=2))
    assert client.post('/api/vehicles/car/refresh', headers=headers).status_code == 200
    assert db.one('SELECT status FROM commands')['status'] == 'verified'
    # Stale/missing telemetry must not prevent an explicit stop request.
    db.execute('DELETE FROM vehicle_readings')
    response = client.post('/api/vehicles/car/charge', json={'action': 'stop'}, headers=headers)
    assert response.status_code == 200, response.text
    plugin.stop_charging.assert_awaited_once()


def test_timed_battery_modes_use_commissioned_controller(tmp_path, monkeypatch, settings):
    from test_device_controls import ready_controller
    monkeypatch.setenv('DEMO_MODE', 'false')
    app = create_app(Runtime(data_dir=tmp_path, app_url=ORIGIN, dev_http=True, worker=False))
    app.state.security.create_owner('demo', PASSWORD)
    _, controller, device = ready_controller(app.state.db, settings, tmp_path)
    app.state.engine.controller = controller
    app.state.runtime.worker = True  # TestClient does not start the background lifespan here.
    client = TestClient(app, base_url=ORIGIN)
    headers = login(client)
    rejected = client.post('/api/systems/main/mode', json={'mode': 'force_charge', 'power_kw': 100}, headers=headers)
    assert rejected.status_code == 422
    device.apply_schedule.assert_not_called()
    response = client.post('/api/systems/main/mode', json={'mode': 'hold', 'minutes': 15}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['control']['executed']
    assert client.get('/api/status').json()['systems'][0]['manual_mode']['mode'] == 'hold'
    response = client.post('/api/systems/main/mode', json={'mode': 'automatic'}, headers=headers)
    assert response.status_code == 200, response.text
    assert not app.state.db.meta('battery_manual')


def test_climate_api_preserves_charge_plan_and_verifies_new_status(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from kwattson.config import Vehicle, utcnow
    from kwattson.db import encode
    from kwattson.plugins.base import VehicleClimate
    monkeypatch.setenv('DEMO_MODE', 'false')
    app = create_app(Runtime(data_dir=tmp_path, app_url=ORIGIN, dev_http=True, worker=False))
    app.state.security.create_owner('demo', PASSWORD)
    app.state.runtime.worker = True
    db, engine = app.state.db, app.state.engine
    settings = db.settings()
    settings.vehicles = [Vehicle(id='car', name='Car', plugin='mg_ismart', capacity_kwh=50,
                                plugin_config={'allow_climate_control': True})]
    db.save_settings(settings)
    now = utcnow()
    device = SimpleNamespace(read_climate=AsyncMock(return_value=VehicleClimate(now, True, True, 'off')),
                             set_climate=AsyncMock())
    monkeypatch.setattr(engine.devices, 'vehicle', lambda *_: device)
    session = db.execute('INSERT INTO overrides(created_at,starts,expires,kind,data) VALUES(?,?,?,?,?)',
               (now.isoformat(), now.isoformat(), (now + timedelta(hours=12)).isoformat(), 'ev_charge_now',
                encode({'vehicle_id': 'car', 'auto_schedule': True})))
    client = TestClient(app, base_url=ORIGIN)
    headers = login(client)
    response = client.post('/api/vehicles/car/climate', json={'action': 'start', 'temperature_c': 22}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'pending'
    assert db.one('SELECT cancelled_at FROM overrides WHERE id=?', (session,))['cancelled_at'] is None
    device.read_climate.return_value = VehicleClimate(now + timedelta(seconds=1), True, True, 'on')
    response = client.post('/api/vehicles/car/climate/refresh', headers=headers)
    assert response.status_code == 200, response.text
    assert db.one('SELECT status FROM commands')['status'] == 'verified'


@pytest.mark.parametrize('start_demo', [False, True])
def test_data_directory_cannot_switch_between_demo_and_normal(tmp_path, monkeypatch, start_demo):
    monkeypatch.setenv('DEMO_MODE', str(start_demo))
    monkeypatch.setenv('DEMO_PASSWORD', PASSWORD)
    app = create_app(Runtime(data_dir=tmp_path, app_url=ORIGIN, dev_http=True, worker=False))
    before = app.state.db.settings()
    # Exercise recognition of databases created by the previous version.
    app.state.db.execute("DELETE FROM meta WHERE key='demo_mode'")
    monkeypatch.setenv('DEMO_MODE', str(not start_demo))
    with pytest.raises(RuntimeError, match='separate data directories'):
        create_app(Runtime(data_dir=tmp_path, app_url=ORIGIN, dev_http=True, worker=False))
    assert app.state.db.settings() == before
