import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kwattson import advice, ev_charging, forecast, optimizer
from kwattson.config import UTC, Vehicle, stamp
from kwattson.db import decode, encode
from kwattson.ingest import store_vehicle
from kwattson.plugins.base import VehicleReading

NOW = datetime(2026, 11, 19, 4, tzinfo=UTC)
CAR = Vehicle(id='car', name='Car', plugin='mg_ismart', plugin_config={'allow_charge_stop': True},
              capacity_kwh=60, charger_kw=6, charger_efficiency=0.9)


def reading(db, at, soc, *, home=True, charging=True):
    store_vehicle(db, CAR.id, CAR.plugin,
                  VehicleReading(ts=at, time_verified=False, soc=soc, at_home=home, charging=charging))


def test_forecast_clips_at_battery_target_and_partial_time_limit(db, settings):
    settings.vehicles = [CAR]
    expires = NOW + timedelta(minutes=45)
    slots = [(NOW + timedelta(minutes=30 * i), 0.5) for i in range(4)]
    session = dict(id=1, kind='ev_charge_now', starts=stamp(NOW), expires=stamp(expires),
                   data={'vehicle_id': CAR.id, 'starting_soc': 47, 'target_pct': 50})
    energy, _ = forecast.ev_plan(db, settings, NOW, slots, [], [session])
    assert energy == pytest.approx([2, 0, 0, 0])
    session['data']['target_pct'] = 80
    energy, _ = forecast.ev_plan(db, settings, NOW, slots, [], [session])
    assert energy == pytest.approx([3, 1.5, 0, 0])


def test_charge_rate_uses_connected_sessions_and_ignores_away_readings(db):
    for home, day, samples in [(True, 2, [50, 51.5, 53]), (False, 1, [55, 56.5, 58.5])]:
        for i, soc in enumerate(samples):
            reading(db, NOW - timedelta(days=day) + timedelta(minutes=15 * i), soc, home=home)
    estimate, basis = ev_charging.charge_rate(db, CAR, NOW)
    assert estimate == pytest.approx(4)
    assert basis == 'learned'


@pytest.mark.parametrize('limit', ['target', 'duration'])
def test_stop_uses_the_first_observed_limit_and_confirms_from_the_car(db, settings, limit):
    settings.vehicles = [CAR]
    expires = NOW + timedelta(minutes=30)
    db.execute('INSERT INTO overrides(created_at, starts, expires, kind, data) VALUES(?,?,?,?,?)',
               (stamp(NOW), stamp(NOW), stamp(expires), 'ev_charge_now', encode({
                   'vehicle_id': CAR.id, 'target_pct': 80, 'stop_at_limit': True})))
    at = NOW + timedelta(minutes=20) if limit == 'target' else expires
    reading(db, at, 80 if limit == 'target' else 70)
    stopper = AsyncMock()
    devices = SimpleNamespace(vehicle=lambda *_: SimpleNamespace(stop_charging=stopper))
    notify = SimpleNamespace(emit=Mock())

    async def run():
        await ev_charging.monitor(db, settings, devices, notify, at)
        await ev_charging.monitor(db, settings, devices, notify, at + timedelta(seconds=20))
        pending = decode(db.one('SELECT data FROM overrides')['data'])
        assert pending['state'] == 'stopping'
        assert pending['stop_reason'] == ('80% target' if limit == 'target' else 'time limit')
        stopper.assert_awaited_once_with()
        notify.emit.assert_not_called()
        reading(db, at + timedelta(minutes=10), 81, charging=False)
        await ev_charging.monitor(db, settings, devices, notify, at + timedelta(minutes=10))
        assert decode(db.one('SELECT data FROM overrides')['data'])['state'] == 'confirmed'
        notify.emit.assert_called_once()
        assert 'confirmed it' in notify.emit.call_args.args[1]['reason']

    asyncio.run(run())


@pytest.mark.parametrize('stop_at_limit, at_home, age, state', [
    (False, True, 0, 'finished'),
    (True, True, 26, 'unconfirmed'),
    (True, False, 0, 'finished'),
])
def test_planning_only_stale_and_away_sessions_never_send_a_stop(db, settings, stop_at_limit, at_home, age, state):
    settings.vehicles = [CAR]
    expires = NOW + timedelta(hours=1)
    db.execute('INSERT INTO overrides(created_at, starts, expires, kind, data) VALUES(?,?,?,?,?)',
               (stamp(NOW), stamp(NOW), stamp(expires), 'ev_charge_now',
                encode({'vehicle_id': CAR.id, 'stop_at_limit': stop_at_limit})))
    reading(db, expires - timedelta(minutes=age), 60, home=at_home)
    devices = SimpleNamespace(vehicle=Mock())
    notify = SimpleNamespace(emit=Mock())
    asyncio.run(ev_charging.monitor(db, settings, devices, notify, expires))
    devices.vehicle.assert_not_called()
    assert decode(db.one('SELECT data FROM overrides')['data'])['state'] == state
    notify.emit.assert_called_once()


def test_cost_preview_uses_surplus_and_grid_allowance_and_requires_coverage(db, settings, monkeypatch):
    rows = [dict(ts=stamp(NOW), hours=0.5, import_kwh=0, curtailed_kwh=0.5, export_kwh=0.5),
            dict(ts=stamp(NOW + timedelta(minutes=30)), hours=0.5, import_kwh=1, curtailed_kwh=0, export_kwh=0)]
    monkeypatch.setattr(advice, 'plan_intervals', lambda *_: rows)
    policy = optimizer.dispatch_policy(settings)
    group = ('2026-11-19', 'test', 0)
    monkeypatch.setattr(advice, 'price_points', lambda *_: [
        {'imports': [{'group': None, 'rate': 0}]},
        {'imports': [{'group': group, 'rate': 0, 'limit': 5}, {'group': None, 'rate': 0.30}]}])
    monkeypatch.setattr(advice, 'allowance_used', lambda *_: {group: 3})
    decision = dict(id=1, created_at=stamp(NOW), summary=dict(dispatch_policy=policy,
                    allowance_remaining=[dict(day=group[0], plan=group[1], index=group[2], kwh=4)]))
    cost = advice.charging_preview(db, settings, CAR, NOW, NOW + timedelta(hours=1), {}, stamp(NOW), decision)
    assert cost['paid_grid_kwh'] == 2
    assert cost['paid_grid_aud'] == 0.60
    missing = advice.charging_preview(db, settings, CAR, NOW, NOW + timedelta(hours=2), {}, stamp(NOW), decision)
    assert missing['paid_grid_aud'] is None
