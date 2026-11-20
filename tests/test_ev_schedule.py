import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from conftest import solarmax, ZONE
from kwattson import ev_charging, ev_schedule, forecast, optimizer
from kwattson.config import UTC, Vehicle, stamp
from kwattson.db import decode, encode
from kwattson.ingest import combine, store_site, store_system, store_vehicle
from kwattson.plugins.base import SystemReading, VehicleReading
from kwattson.web.api import ChargeOptions, charge_options

NOW = datetime(2026, 11, 19, 20, tzinfo=ZONE).astimezone(UTC)
CAR = Vehicle(id='car', name='Car', plugin='mg_ismart', capacity_kwh=60, charger_kw=6.4, charger_efficiency=.9,
              plugin_config={'allow_charge_schedule': True})
MIDNIGHT = NOW + timedelta(hours=4)
DEPART = NOW + timedelta(hours=14)


def reading(db, at, soc, charging, *, plugged=True):
    store_vehicle(db, CAR.id, CAR.plugin,
                  VehicleReading(ts=at, time_verified=False, soc=soc, charging=charging, plugged_in=plugged))


def test_clock_is_site_time_and_percentage_does_not_inherit_duration(settings):
    clock = ChargeOptions(charge_mode='time', end_at='2026-11-19T23:00')
    until, _ = charge_options(settings, CAR, NOW, clock)
    assert until - NOW == timedelta(hours=3)
    pct = ChargeOptions(charge_mode='percentage', target_pct=80, minutes=5, stop_at_limit=True)
    until, _ = charge_options(settings, CAR, NOW, pct)
    assert until - NOW == timedelta(days=14)


def test_grid_only_schedule_chooses_off_peak_and_finishes_early(db, settings):
    settings = solarmax(settings)
    settings.systems = []
    result = ev_schedule.preview(db, settings, CAR, NOW, DEPART, {'target_pct': 80, 'starting_soc': 42}, None, [])
    assert result['target_reachable'] is True
    assert result['end_soc'] == pytest.approx(80)
    assert result['kw'] == pytest.approx(5.12)
    assert result['windows'] == [{'start': stamp(MIDNIGHT), 'end': result['finish']}]
    assert datetime.fromisoformat(result['finish']) < MIDNIGHT + timedelta(hours=5)
    assert result['paid_grid_aud'] == pytest.approx(4.18)
    assert [(r['start'], r['rate']) for r in result['tariffs']] == [
        (stamp(NOW), .49555), (stamp(NOW + timedelta(hours=1)), .26906),
        (stamp(MIDNIGHT), .165), (stamp(MIDNIGHT + timedelta(hours=6)), .26906)]


def test_strict_tariff_shortfall_respects_buffer_and_allows_manual_no_grid(db, settings):
    settings = solarmax(settings)
    data = {'target_pct': 80, 'starting_soc': 12, 'grid_rates': [.165], 'auto_schedule': True}
    result = ev_schedule.preview(db, settings, CAR, NOW, DEPART, data, None, [])
    assert result['target_reachable'] is False
    assert 55 < result['end_soc'] < 60
    assert result['windows'][0]['start'] == stamp(MIDNIGHT + timedelta(minutes=5))
    assert result['windows'][0]['end'] == stamp(MIDNIGHT + timedelta(hours=6, minutes=-5))
    data['grid_rates'] = []
    assert ev_schedule.preview(db, settings, CAR, NOW, DEPART, data, None, [])['windows'] == []


def test_free_only_schedule_keeps_allowance_spill_out_of_auto_windows(db, settings):
    settings = solarmax(settings)
    now = NOW - timedelta(hours=9)
    settings.site.grid_import_kw = 30
    data = {'target_pct': 95, 'starting_soc': 12, 'grid_rates': [0], 'auto_schedule': True}
    result = ev_schedule.preview(db, settings, CAR, now, now + timedelta(hours=3), data, None, [])
    assert result['target_reachable'] is False
    assert result['windows'] == [{'start': stamp(now + timedelta(minutes=5)), 'end': stamp(now + timedelta(minutes=95))}]
    assert result['paid_grid_kwh'] == 0


def test_battery_only_schedule_uses_available_energy_without_expected_grid(db, settings):
    system = settings.systems[0]
    system.pv_kwp = 0
    store_system(db, system.id, system.plugin, SystemReading(ts=NOW, time_verified=True, soc=90))
    flows = {system.id: {'soc_kwh': 34}}
    rows = [dict(ts=stamp(NOW), hours=3, systems=flows, action='self_use', import_kwh=0, export_kwh=0, discharge_kwh=0)]
    decision = {'summary': {'dispatch_policy': optimizer.dispatch_policy(settings), 'terminal_value_per_kwh': .10},
                'created_at': stamp(NOW)}
    data = {'target_pct': 50, 'starting_soc': 42, 'grid_rates': []}
    result = ev_schedule.preview(db, settings, CAR, NOW, NOW + timedelta(hours=3), data, decision, rows)
    assert result['target_reachable'] is True
    assert result['grid_kwh'] == 0
    assert result['battery_kwh'] == 5.33
    assert result['solar_kwh'] == 0


def test_automatic_free_grid_only_schedule_can_use_home_battery_at_paid_rates(db, settings):
    system = settings.systems[0]
    system.pv_kwp = 0
    store_system(db, system.id, system.plugin, SystemReading(ts=NOW, time_verified=True, soc=90))
    flows = {system.id: {'soc_kwh': 34}}
    rows = [dict(ts=stamp(NOW), hours=3, systems=flows, action='self_use', import_kwh=0, export_kwh=0, discharge_kwh=0)]
    decision = {'summary': {'dispatch_policy': optimizer.dispatch_policy(settings), 'terminal_value_per_kwh': .10},
                'created_at': stamp(NOW)}
    data = {'target_pct': 50, 'starting_soc': 42, 'grid_rates': [0], 'auto_schedule': True}
    result = ev_schedule.preview(db, settings, CAR, NOW, NOW + timedelta(hours=3), data, decision, rows)
    assert result['target_reachable']
    assert result['paid_grid_aud'] == result['paid_grid_kwh'] == result['grid_kwh'] == 0
    assert result['battery_kwh'] == 5.33
    assert all(w['surplus_only'] for w in result['windows'])


def test_car_can_redirect_optional_exports_without_spending_house_reserve(db, settings):
    system = settings.systems[0]
    system.controllable = True
    system.pv_kwp = 0
    store_system(db, system.id, system.plugin, SystemReading(ts=NOW, time_verified=True, soc=90))
    rows = [dict(ts=stamp(NOW), hours=1, systems={system.id: {'soc_kwh': 20, 'dis': 5}},
                 action='force_discharge', import_kwh=0, export_kwh=4, discharge_kwh=5),
            dict(ts=stamp(NOW + timedelta(hours=1)), hours=1, systems={system.id: {'soc_kwh': 6, 'dis': 0}},
                 action='self_use', import_kwh=0, export_kwh=0, discharge_kwh=0)]
    deadline = NOW + timedelta(hours=2)
    budget = sum(kwh for _, kwh in ev_schedule.home_battery(db, settings, NOW, deadline, rows))
    assert budget == pytest.approx(3.2)  # 4 kWh of optional export with the 20% safety margin
    system.controllable = False
    assert ev_schedule.home_battery(db, settings, NOW, deadline, rows) == []


def test_surplus_schedule_stops_on_paid_import_and_respects_cooldown(db, settings):
    settings.vehicles = [CAR]
    system = settings.systems[0]
    identity, data = save_schedule(db, settings)
    data.update(grid_rates=[0], charge_windows=[dict(start=stamp(NOW), end=stamp(NOW + timedelta(hours=2)), surplus_only=True)])
    db.execute('UPDATE overrides SET data=? WHERE id=?', (encode(data), identity))
    plugin = SimpleNamespace(start_charging=AsyncMock(), stop_charging=AsyncMock())
    devices = SimpleNamespace(vehicle=lambda *_: plugin)
    notify = SimpleNamespace(emit=Mock())
    def meter(at, imp):
        measured = SystemReading(ts=at, time_verified=True, soc=80, import_kw=imp, export_kw=0, load_kw=2,
                                 pv_kw=0, charge_kw=0, discharge_kw=1)
        store_system(db, system.id, system.plugin, measured)
        store_site(db, combine(settings, {system.id: measured}))
    async def run():
        reading(db, NOW, 45, False)
        meter(NOW, 0)
        await ev_charging.monitor(db, settings, devices, notify, NOW)
        plugin.start_charging.assert_awaited_once()
        at = NOW + timedelta(minutes=1)
        meter(at, .5)
        reading(db, at, 45, True)
        await ev_charging.monitor(db, settings, devices, notify, at)
        plugin.stop_charging.assert_awaited_once()
        at += timedelta(minutes=1)
        meter(at, 0)
        reading(db, at, 45, False)
        await ev_charging.monitor(db, settings, devices, notify, at)
        plugin.start_charging.assert_awaited_once()
        assert decode(db.one('SELECT data FROM overrides WHERE id=?', (identity,))['data'])['surplus_paused_until'] > stamp(at)
        assert ev_schedule.home_energy_available(db, settings, CAR, at + timedelta(minutes=5))
        assert not ev_schedule.home_energy_available(db, settings, CAR, at + timedelta(minutes=8))
        low = SystemReading(ts=at, time_verified=True, soc=16, import_kw=0, export_kw=0, load_kw=1,
                            pv_kw=0, charge_kw=0, discharge_kw=1)
        store_system(db, system.id, system.plugin, low)
        store_site(db, combine(settings, {system.id: low}))
        reading(db, at, 45, True)
        assert not ev_schedule.home_energy_available(db, settings, CAR, at)
    asyncio.run(run())


def save_schedule(db, settings):
    data = dict(vehicle_id=CAR.id, charge_mode='ready_by', auto_schedule=True,
                dispatch_policy=optimizer.dispatch_policy(settings), target_pct=80,
                charge_windows=[dict(start=stamp(MIDNIGHT), end=stamp(MIDNIGHT + timedelta(hours=1)))],
                schedule_kw=5.12, scheduled_end_soc=80)
    identity = db.execute('INSERT INTO overrides(created_at, starts, expires, kind, data) VALUES(?,?,?,?,?)',
                          (stamp(NOW), stamp(NOW), stamp(DEPART), 'ev_charge_now', encode(data)))
    return identity, data


def test_schedule_forecast_places_only_the_saved_window(db, settings):
    settings.vehicles = [CAR]
    identity, data = save_schedule(db, settings)
    session = dict(id=identity, kind='ev_charge_now', starts=stamp(NOW), expires=stamp(DEPART), data=data)
    slots = [(MIDNIGHT + timedelta(minutes=30*i), .5) for i in range(-1, 3)]
    energy, _, _ = forecast.ev_plan(db, settings, NOW, slots, [], [session])
    assert energy == pytest.approx([0, 2.56, 2.56, 0])


def test_manual_schedule_does_not_hide_observed_charging_or_confirm_a_stop(db, settings):
    settings.vehicles = [CAR]
    identity, data = save_schedule(db, settings)
    data.update(auto_schedule=False, target_pct=50)
    db.execute('UPDATE overrides SET data=? WHERE id=?', (encode(data), identity))
    reading(db, NOW, 65, True)
    session = dict(id=identity, starts=stamp(NOW), expires=stamp(DEPART), kind='ev_charge_now', data=data)
    energy, _, _ = forecast.ev_plan(db, settings, NOW, [(NOW, .5), (NOW + timedelta(minutes=30), .5)], [], [session])
    assert energy == pytest.approx([3.2, 3.2])
    devices = SimpleNamespace(vehicle=Mock())
    notify = SimpleNamespace(emit=Mock())
    asyncio.run(ev_charging.monitor(db, settings, devices, notify, NOW))
    devices.vehicle.assert_not_called()
    assert decode(db.one('SELECT data FROM overrides WHERE id=?', (identity,))['data'])['state'] == 'finished'
    assert 'did not stop' in notify.emit.call_args.args[1]['reason']


def test_remaining_projection_uses_new_soc_and_only_future_windows(db, settings):
    identity, data = save_schedule(db, settings)
    data.update(auto_schedule=False, target_pct=100, charge_estimate={'end_soc': 55, 'paid_grid_aud': 12})
    session = dict(id=identity, starts=stamp(NOW), expires=stamp(DEPART), data=data)
    reading(db, NOW, 65, True)
    estimate = ev_schedule.remaining_estimate(db, CAR, NOW, session)
    assert estimate['soc'] == 65
    assert estimate['end_soc'] == pytest.approx(72.68)
    assert estimate['continuous_end_soc'] == 100
    assert estimate['charging_outside_window']
    assert estimate['paid_grid_aud'] is None
    at = MIDNIGHT + timedelta(minutes=30)
    reading(db, at, 70, True)
    estimate = ev_schedule.remaining_estimate(db, CAR, at, session)
    assert estimate['energy_kwh'] == pytest.approx(2.56)
    assert estimate['end_soc'] == pytest.approx(73.84)
    reading(db, DEPART, 77, False)
    estimate = ev_schedule.remaining_estimate(db, CAR, DEPART, session)
    assert estimate['end_soc'] == 77
    assert estimate['energy_kwh'] == 0


def test_saved_window_forecast_never_charges_past_target(db, settings):
    settings.vehicles = [CAR]
    identity, data = save_schedule(db, settings)
    reading(db, MIDNIGHT, 79, True)
    session = dict(id=identity, kind='ev_charge_now', starts=stamp(NOW), expires=stamp(DEPART), data=data)
    energy, _, _ = forecast.ev_plan(db, settings, MIDNIGHT, [(MIDNIGHT, .5), (MIDNIGHT + timedelta(minutes=30), .5)], [], [session])
    assert sum(energy) == pytest.approx(60 / .9 / 100)


def test_adopted_forecast_can_supply_context_when_recently_reviewed(db, settings):
    decision = dict(id=4, created_at=stamp(NOW - timedelta(hours=8)),
                    summary={'dispatch_policy': optimizer.dispatch_policy(settings)})
    db.set_meta('active_plan', dict(decision_id=4, reviewed_at=stamp(NOW), valid=True))
    from kwattson import plans
    rows = [{'example': True}]
    assert ev_schedule.forecast_rows(decision, settings, NOW, rows, plans.reviewed_at(db, decision)) == rows
    db.set_meta('active_plan', dict(decision_id=4, reviewed_at=stamp(NOW), valid=False))
    assert ev_schedule.forecast_rows(decision, settings, NOW, rows, plans.reviewed_at(db, decision)) == []


def test_automatic_schedule_starts_and_stops_once_and_waits_for_confirmation(db, settings):
    settings.vehicles = [CAR]
    identity, _ = save_schedule(db, settings)
    plugin = SimpleNamespace(start_charging=AsyncMock(), stop_charging=AsyncMock())
    devices = SimpleNamespace(vehicle=lambda *_: plugin)
    notify = SimpleNamespace(emit=Mock())

    async def run():
        reading(db, MIDNIGHT - timedelta(minutes=5), 73, False)
        await ev_charging.monitor(db, settings, devices, notify, MIDNIGHT - timedelta(minutes=5))
        plugin.start_charging.assert_not_called()
        await ev_charging.monitor(db, settings, devices, notify, MIDNIGHT)
        await ev_charging.monitor(db, settings, devices, notify, MIDNIGHT + timedelta(seconds=20))
        plugin.start_charging.assert_awaited_once()
        reading(db, MIDNIGHT + timedelta(minutes=10), 74, True)
        await ev_charging.monitor(db, settings, devices, notify, MIDNIGHT + timedelta(minutes=10))
        plugin.stop_charging.assert_not_called()
        reading(db, MIDNIGHT + timedelta(minutes=50), 78, True)
        await ev_charging.monitor(db, settings, devices, notify, MIDNIGHT + timedelta(hours=1))
        plugin.stop_charging.assert_awaited_once()
        reading(db, MIDNIGHT + timedelta(minutes=61), 78, False)
        await ev_charging.monitor(db, settings, devices, notify, MIDNIGHT + timedelta(minutes=61))
        assert 'charge_command' not in decode(db.one('SELECT data FROM overrides WHERE id=?', (identity,))['data'])
        assert any('may only reach' in c.args[1]['reason'] for c in notify.emit.call_args_list)

    asyncio.run(run())


def test_percentage_session_never_stops_at_monitoring_expiry(db, settings):
    settings.vehicles = [CAR]
    db.execute('INSERT INTO overrides(created_at, starts, expires, kind, data) VALUES(?,?,?,?,?)',
               (stamp(NOW), stamp(NOW), stamp(MIDNIGHT), 'ev_charge_now',
                encode({'vehicle_id': CAR.id, 'charge_mode': 'percentage', 'target_pct': 80, 'stop_at_limit': True})))
    reading(db, MIDNIGHT, 50, True)
    devices = SimpleNamespace(vehicle=Mock())
    asyncio.run(ev_charging.monitor(db, settings, devices, SimpleNamespace(emit=Mock()), MIDNIGHT))
    devices.vehicle.assert_not_called()


def test_future_charges_enter_forecast_only_at_saved_times_and_can_be_cancelled(db, settings):
    settings.vehicles = [CAR]
    friday = NOW + timedelta(days=1, hours=2, minutes=15)
    sunday = NOW + timedelta(days=3, hours=2)
    ids = []
    for start in (friday, sunday):
        ids.append(db.execute('INSERT INTO overrides(created_at, starts, expires, kind, data) VALUES(?,?,?,?,?)',
                   (stamp(NOW), stamp(start), stamp(start + timedelta(hours=2)), 'ev_charge_planned',
                    encode({'vehicle_id': CAR.id}))))
    _, points, model = forecast.build(db, settings, NOW)
    assert model['vehicles'][CAR.id]['planned_charges'] == [ids[0]]
    expected = [r for r in points if r['ev_kwh'] > 0]
    assert expected[0]['ts'] == stamp(friday - timedelta(minutes=15))
    assert [r['ev_kwh'] for r in expected] == pytest.approx([1.6, 3.2, 3.2, 3.2, 1.6])
    assert sum(r['planned_ev_kwh'] for r in points) == pytest.approx(12.8)
    assert not ev_charging.sessions(db, friday)
    db.execute('UPDATE overrides SET cancelled_at=? WHERE id=?', (stamp(NOW), ids[0]))
    _, points, _ = forecast.build(db, settings, NOW)
    assert sum(r['ev_kwh'] for r in points) == 0


def test_pinned_ev_charge_replaces_regular_load_same_evening_and_lifts_weekly_projection(db, settings):
    from kwattson.advice import ev_advice

    car = CAR.model_copy(update={'manual_soc': 42, 'weekday_km': [0]*7})
    car.schedule.days = [4]
    car.schedule.start_hour = 20
    settings.vehicles = [car]
    start = NOW + timedelta(days=1, hours=2)
    end = start + timedelta(hours=2)
    row = dict(id=1, kind='ev_charge_planned', starts=stamp(start), expires=stamp(end), data={'vehicle_id': car.id})
    slots = [(start - timedelta(hours=2) + timedelta(minutes=30*i), .5) for i in range(10)]
    energy, planned, _ = forecast.ev_plan(db, settings, NOW, slots, [], [row])
    assert energy == pytest.approx(planned)
    assert energy[:4] == [0] * 4
    assert sum(energy) == pytest.approx(12.8)
    outlook = ev_advice(db, settings, [], car, NOW, [], [row])
    assert outlook['projection'][1]['soc_pct'] == pytest.approx(61.2)
    assert outlook['projection'][1]['planned_charge_pct'] == pytest.approx(19.2)
