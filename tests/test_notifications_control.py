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
