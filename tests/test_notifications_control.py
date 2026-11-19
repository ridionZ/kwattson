import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kwattson.config import Runtime
from kwattson.control import Controller, commissioning_hash
from kwattson.demo import demo_settings
from kwattson.devices import Devices
from kwattson.notifications import Notifications, check_template, render
from kwattson.plugins import registry
from kwattson.security import Security

from conftest import ZONE, make_test_site


@pytest.fixture
def security(db, tmp_path):
    return Security(db, Runtime(data_dir=tmp_path, app_url='http://localhost:8080', dev_http=True, worker=False))


def test_quiet_hours_delay_delivery(db, security):
    db.save_settings(make_test_site())  # Brisbane time zone, 22:00–06:00 quiet hours
    n = Notifications(db, security)
    channel = db.execute("INSERT INTO channels(name, kind, enabled, created_at) VALUES('x','ntfy',1,'now')")
    db.execute('UPDATE notification_rules SET data=? WHERE event=?', ('{"channels":[%d]}' % channel, 'low_battery'))
    late = datetime(2026, 12, 1, 23, 30, tzinfo=ZONE)
    assert n.emit('low_battery', {'soc': 10, 'reason': 'r'}, now=late)
    due = datetime.fromisoformat(db.one('SELECT next_at FROM deliveries')['next_at']).astimezone(ZONE)
    assert (due.day, due.hour) == (2, 6)


def test_cooldown_and_dedupe(db, security):
    n = Notifications(db, security)
    t = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)
    assert n.emit('high_price', {'reason': 'a'}, now=t)
    assert n.emit('high_price', {'reason': 'b'}, now=t + timedelta(minutes=5)) is None
    assert n.emit('daily_summary', {'reason': 'x'}, key='daily:1', now=t)
    assert n.emit('daily_summary', {'reason': 'x'}, key='daily:1', now=t) is None


def test_templates_are_restricted():
    check_template('Battery {soc}% {reason}')
    for bad in ('{0}', '{reason.__class__}', '{reason!r}', '{soc:>10}'):
        with pytest.raises(ValueError):
            check_template(bad)
    assert render('{missing} ok', {}) == ' ok'


def test_registry_lists_builtins():
    names = {p['name'] for p in registry.describe()}
    assert {'foxess', 'simulator', 'homeassistant', 'push', 'manual'} <= names


def test_guards_and_failsafe_release(db, security, tmp_path):
    settings = demo_settings()
    settings.tariffs = [t for t in settings.tariffs if t.status != 'pending']
    db.save_settings(settings)
    runtime = Runtime(data_dir=tmp_path, app_url='http://localhost:8080', dev_http=True, worker=False)
    devices = Devices(db, security)
    controller = Controller(db, devices, Notifications(db, security), runtime)
    reasons = controller.guards(settings)
    assert any('commissioned' in r for r in reasons) and any('monitor' in r for r in reasons)
    settings.mode = 'automatic'
    db.save_settings(settings)
    db.set_meta('control_commissioned', {'hash': commissioning_hash(settings)})
    db.set_meta('control_active', {'main': {'mode': 'force_charge', 'start': '2026-01-01T00:00:00+00:00', 'end': '2026-01-01T00:20:00+00:00'}})
    result = asyncio.run(controller.cycle(settings))  # no readings, no plan → guards fail → device released
    assert result['executed'] is False
    assert db.meta('control_active') == {}
    assert db.one("SELECT * FROM commands WHERE action='release'")['status'] == 'released'


def test_dispatch_command_ends_at_buffered_step(db, security, tmp_path):
    from kwattson.config import stamp

    settings = demo_settings()
    runtime = Runtime(data_dir=tmp_path, app_url='http://localhost:8080', dev_http=True, worker=False)
    controller = Controller(db, Devices(db, security), Notifications(db, security), runtime)
    now = datetime(2026, 12, 1, 20, 52, 10, tzinfo=ZONE)
    step = now.replace(minute=30, second=0)
    end = now.replace(minute=55, second=0)
    command = {'mode': 'force_discharge', 'power_kw': 3, 'target_soc': 39}
    result = asyncio.run(controller.send(settings, settings.systems[0], command, 1, now, 'Test reserve', step, end))
    assert result['status'] == 'verified'
    active = db.meta('control_active')['main']
    assert active['end'] == stamp(end)
    assert active['target_soc'] == 39

    changed = {**command, 'target_soc': 42}
    result = asyncio.run(controller.send(settings, settings.systems[0], changed, 2, now + timedelta(minutes=1),
                                         'Test higher reserve', step, end))
    assert result['status'] == 'verified'
    assert db.meta('control_active')['main']['target_soc'] == 42


@pytest.mark.parametrize('failure', [None, 'planning', 'verification'])
def test_enabling_control_builds_and_applies_first_step(db, security, tmp_path, monkeypatch, failure):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from kwattson import optimizer
    from kwattson.config import stamp
    from kwattson.engine import Engine
    from kwattson.ingest import store_system
    from kwattson.plugins.base import CommandResult, SystemReading
    from kwattson.plugins.simulator import SimulatedHybrid
    from kwattson.tariffs import Period, TariffPlan
    from kwattson.web.api import EnableControl, enable_control

    from conftest import sunny_points

    now = datetime(2026, 12, 1, 11, 10, tzinfo=ZONE)
    settings = demo_settings()
    settings.systems[0].pv_kwp = 0
    settings.tariffs = [TariffPlan(id='test', name='Free then peak', supply_daily=1, periods=[
        Period(name='free', start='00:00', end='12:00', import_rate=0, export_rate=0),
        Period(name='peak', start='12:00', end='24:00', import_rate=0.75, export_rate=0),
    ])]
    db.save_settings(settings)
    system = settings.systems[0]
    store_system(db, system.id, system.plugin, SystemReading(ts=now, time_verified=True, soc=20))
    runtime = Runtime(data_dir=tmp_path, app_url='http://localhost:8080', dev_http=True, worker=True)
    engine = Engine(db, security, runtime)
    monkeypatch.setattr(security, 'reauthenticate', lambda *_: None)
    monkeypatch.setattr('kwattson.control.utcnow', lambda: now)

    async def replan(proposed):
        assert proposed.mode == 'automatic'
        assert db.settings().mode == 'monitor'
        if failure == 'planning':
            raise RuntimeError('synthetic planner error')
        return optimizer.plan(db, proposed, 0, sunny_points(now, hours=24, peak_kw=0), {}, now=now)

    monkeypatch.setattr(engine, 'replan', replan)
    if failure == 'verification':
        async def unconfirmed(self, command):
            return CommandResult(accepted=True, verified=False, detail={})
        monkeypatch.setattr(SimulatedHybrid, 'apply', unconfirmed)

    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=db, security=security, engine=engine, runtime=runtime)),
        state=SimpleNamespace(user={'user_id': 1, 'username': 'fixture'}),
    )
    body = EnableControl(password='synthetic', confirmation='ENABLE BATTERY CONTROL', acknowledge=True)
    assert engine.latest_decision() is None
    if failure == 'planning':
        with pytest.raises(HTTPException) as error:
            asyncio.run(enable_control(body, req))
        assert error.value.status_code == 503
        assert db.settings().mode == 'monitor'
        assert not db.meta('control_commissioned')
        assert db.one('SELECT COUNT(*) AS count FROM commands')['count'] == 0
        return

    result = asyncio.run(enable_control(body, req))
    assert result['mode'] == db.settings().mode == 'automatic'
    assert result['decision_id'] == engine.latest_decision()['id']
    if failure == 'verification':
        assert not result['initial_control']['executed']
        assert db.one("SELECT status FROM commands WHERE action='force_charge'")['status'] == 'unverified'
        assert db.one("SELECT status FROM commands WHERE action='release'")['status'] == 'released'
        assert engine.controller.recovery_reasons(now)
    else:
        assert db.one("SELECT status FROM commands WHERE action='force_charge'")['status'] == 'verified'
        assert db.meta('control_active')[system.id]['start'] == stamp(now)
        from kwattson.web.api import stop_control
        asyncio.run(stop_control(req))
        assert db.settings().mode == 'monitor'
        def unexpected_reauth(*_):
            pytest.fail('a commissioned system should not request the password again')
        monkeypatch.setattr(security, 'reauthenticate', unexpected_reauth)
        assert asyncio.run(enable_control(EnableControl(), req))['mode'] == 'automatic'


@pytest.mark.parametrize('manual', [False, True])
def test_unverified_release_is_retried_before_dispatch_resumes(db, security, tmp_path, monkeypatch, manual):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kwattson.plugins.base import CommandResult

    settings = demo_settings()
    settings.mode = 'automatic'
    db.save_settings(settings)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    acknowledged = CommandResult(accepted=True, verified=True, detail={})
    unconfirmed = CommandResult(accepted=True, verified=False, detail={})
    device = SimpleNamespace(apply=AsyncMock(side_effect=[unconfirmed, acknowledged]),
                             release=AsyncMock(side_effect=[unconfirmed, acknowledged]))
    controller = Controller(db, SimpleNamespace(system=lambda *_: device), Notifications(db, security),
                            Runtime(data_dir=tmp_path, app_url='http://localhost:8080', dev_http=True, worker=False))
    command = {'mode': 'force_discharge', 'power_kw': 2, 'target_soc': 25}
    row = {'ts': now.isoformat(), 'hours': 0.5, 'reason': 'synthetic'}
    monkeypatch.setattr(controller, 'guards', lambda *_: [])
    monkeypatch.setattr(controller, 'latest_decision', lambda: {'id': 1})
    monkeypatch.setattr(controller, 'current_commands', lambda *_: ({'main': {'command': command}}, row))
    monkeypatch.setattr('kwattson.control.utcnow', lambda: now)

    assert not asyncio.run(controller.cycle(settings, now))['executed']
    assert db.settings().mode == 'automatic'
    from kwattson.config import stamp
    assert controller.recovery_status()['phase'] == 'release'
    assert controller.recovery_status()['retry_at'] == stamp(now + timedelta(seconds=30))
    assert not asyncio.run(controller.cycle(settings, now + timedelta(seconds=10)))['executed']
    assert device.apply.await_count == 1 and device.release.await_count == 1
    if not manual:
        assert not asyncio.run(controller.cycle(settings, now + timedelta(seconds=30)))['executed']
        assert device.apply.await_count == 1 and device.release.await_count == 2
        assert controller.recovery_status()['retry_at'] == stamp(now + timedelta(minutes=1))
    delay = timedelta(seconds=20) if manual else timedelta(minutes=1)
    assert asyncio.run(controller.cycle(settings, now + delay, retry_now=manual))['executed']
    assert device.apply.await_count == 2 and device.release.await_count == 2
    assert not db.meta('control_release_pending')
    assert db.meta('control_recovery') is None


def test_remembered_sign_in_survives_idle_limit(db, security):
    from fastapi import HTTPException

    from kwattson.config import stamp, utcnow

    password = 'synthetic-owner-passphrase'
    security.create_owner('fixture-user', password)
    ordinary, _ = security.login('fixture-user', password, '', 'fixture')
    remembered, _ = security.login('fixture-user', password, '', 'fixture', remember=True)
    db.execute('UPDATE sessions SET last_seen=?', (stamp(utcnow() - timedelta(hours=2)),))
    with pytest.raises(HTTPException):
        security.authenticate(ordinary)
    session = security.authenticate(remembered)
    assert datetime.fromisoformat(session['expires']) - datetime.fromisoformat(session['created_at']) == timedelta(days=30)
