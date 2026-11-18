"""The planner must respect physics, limits and allowances, and use DC headroom to avoid clipping."""
from datetime import datetime, timedelta

import pytest

from kwattson import optimizer
from kwattson.config import BatterySpec, EnergySystem, Settings
from kwattson.dispatch import split_points

from conftest import BATTERY_KWH, ZONE, solarmax, sunny_points

START = datetime(2026, 12, 2, 4, 0, tzinfo=ZONE)


def run(db, settings, points, soc_pct=60, overrides=None):
    initial = {s.id: s.battery.capacity_kwh * soc_pct / 100 for s in settings.active_systems() if s.battery}
    return optimizer.plan(db, settings, 0, points, {'confidence': 'test'}, now=START, initial=initial, overrides=overrides or [], store=False)


def check_physics(settings, result, points):
    systems = {s.id: s for s in settings.active_systems()}
    for row, p in zip(result['intervals'], split_points(settings, points)):
        h = p['hours']
        supply = sum(d['pv_ac'] + d['dis'] - d['grid_bat'] for d in row['systems'].values()) + row['import_kwh']
        assert supply == pytest.approx(p['load_kwh'] + row['export_kwh'], abs=1e-4)
        assert row['import_kwh'] <= settings.site.grid_import_kw * h + 1e-6
        assert row['export_kwh'] <= settings.site.export_kw * h + 1e-6
        assert not (row['import_kwh'] > 1e-4 and row['export_kwh'] > 1e-4)
        for sid, d in row['systems'].items():
            s = systems[sid]
            assert d['pv_ac'] + d['pv_bat'] + d['curt'] == pytest.approx(p['solar_by_system'][sid], abs=1e-4)
            assert d['pv_ac'] + d['dis'] <= s.ac_kw * h + 1e-6          # AC output limit
            assert d['pv_bat'] + d['grid_bat'] <= s.battery.charge_kw * h + 1e-6
            assert not (d['dis'] > 1e-4 and d['pv_bat'] + d['grid_bat'] > 1e-4)
            assert s.battery.capacity_kwh * s.battery.reserve_pct / 100 - 1e-6 <= d['soc_kwh'] <= s.battery.capacity_kwh + 1e-6


def test_clipping_avoided_with_dc_headroom(db, settings):
    points = sunny_points(START, peak_kw=13.5)
    result = run(db, settings, points)
    check_physics(settings, result, points)
    summary = result['summary']
    assert summary['solver'] == 'HiGHS MILP'
    assert summary['baseline_curtailed_kwh'] > 10        # self-use fills the battery and then clips
    assert summary['curtailed_kwh'] < 0.5                # the plan keeps room for PV above 10.0 kW
    assert summary['modelled_saving_aud'] > 0
    assert any(r['action'] == 'export_priority' for r in result['intervals'])


def test_pv_above_ac_limit_charges_battery_directly(db, settings):
    points = sunny_points(START, peak_kw=13.5)
    result = run(db, settings, points, soc_pct=20)
    noon = max(result['intervals'], key=lambda r: r['solar_kwh'])
    d = noon['systems']['main']
    assert noon['solar_kwh'] / 0.5 > 10.0
    assert d['pv_bat'] > 0 and d['pv_ac'] <= 10.0 * 0.5 + 1e-6


def test_solarmax_premium_export_and_free_allowance(db, settings):
    s = solarmax(settings)
    points = sunny_points(START, peak_kw=4.0)  # cloudy: free-window grid charging becomes useful
    result = run(db, s, points, soc_pct=30)
    check_physics(s, result, points)
    rows = result['intervals']
    local_day = lambda r: datetime.fromisoformat(r['ts']).astimezone(ZONE).date()
    for day in {local_day(r) for r in rows}:
        free = sum(r['import_kwh'] for r in rows if local_day(r) == day and r['buy'] == 0)
        assert free <= 50 + 1e-4  # never models more free energy than the allowance
    assert any(r['action'] == 'force_charge' and r['buy'] == 0 for r in rows)
    assert all(r['action'] not in ('force_discharge', 'export_priority') and
               min(r['discharge_kwh'], r['export_kwh']) <= 1e-6 for r in rows if r['buy'] == 0)
    assert any(r['action'] == 'force_discharge' and r['sell'] == pytest.approx(0.15) for r in rows)


def test_allowance_already_used_today_is_respected(db, settings):
    s = solarmax(settings)
    points = sunny_points(START, peak_kw=2.0)
    # Pretend 45 kWh of today's free allowance is gone.
    original = optimizer.allowance_used
    day = START.date().isoformat()
    optimizer.allowance_used = lambda *a: {(day, 'covau-solarmax', 0): 45.0}
    try:
        result = run(db, s, points, soc_pct=20)
    finally:
        optimizer.allowance_used = original
    today_free = sum(r['import_kwh'] for r in result['intervals']
                     if r['buy'] == 0 and datetime.fromisoformat(r['ts']).astimezone(ZONE).date().isoformat() == day)
    assert today_free <= 5 + 1e-4


def test_low_initial_soc_not_invented(db, settings):
    points = sunny_points(START, peak_kw=0.0)
    result = run(db, settings, points, soc_pct=5)  # below the 15% reserve
    first = result['intervals'][0]['systems']['main']
    assert first['dis'] == pytest.approx(0, abs=1e-6)
    assert first['soc_kwh'] >= BATTERY_KWH * 0.05 - 1e-6


def test_hold_override_stops_discharge(db, settings):
    points = sunny_points(START, peak_kw=0.0)
    result = run(db, settings, points, soc_pct=80, overrides=[{'kind': 'hold', 'data': '{}'}])
    assert all(r['discharge_kwh'] < 1e-6 for r in result['intervals'])


def test_reserve_override(db, settings):
    points = sunny_points(START, peak_kw=0.0)
    result = run(db, settings, points, soc_pct=80, overrides=[{'kind': 'reserve', 'data': '{"pct": 60}'}])
    assert min(r['systems']['main']['soc_pct'] for r in result['intervals']) >= 60 - 1e-4


def test_two_systems_different_vendors(db, settings):
    data = settings.model_dump()
    data['systems'].append(EnergySystem(id='shed', name='AC battery', plugin='simulator', coupling='ac', ac_kw=5, pv_kwp=0,
                                        battery=BatterySpec(capacity_kwh=13.5, charge_kw=5, discharge_kw=5)).model_dump())
    s = Settings.model_validate(data)
    points = sunny_points(START, peak_kw=13.5)
    for p in points:
        p['solar_by_system']['shed'] = 0.0
    result = run(db, s, points)
    check_physics(s, result, points)
    assert result['summary']['curtailed_kwh'] < result['summary']['baseline_curtailed_kwh']


def test_baseline_self_use_clips(settings):
    points = sunny_points(START, peak_kw=13.5)
    prices = optimizer.price_points(settings, points)
    base = optimizer.self_use(settings, points, prices, {'main': BATTERY_KWH})
    assert sum(b['curtailed'] for b in base) > 10


def test_dispatch_buffers_both_edges_of_free_and_premium_windows(db, settings):
    s = solarmax(settings)
    points = sunny_points(START, peak_kw=4.0)
    result = run(db, s, points, soc_pct=60)
    rows = result['intervals']

    def minute(row):
        at = datetime.fromisoformat(row['ts']).astimezone(ZONE)
        return at.hour * 60 + at.minute

    buffered_charge = [r for r in rows if minute(r) in (660, 835)]
    buffered_export = [r for r in rows if minute(r) in (1080, 1255)]
    assert len(buffered_charge) == len(buffered_export) == 4
    for row in buffered_charge:
        assert row['buy'] == 0
        assert row['hours'] == pytest.approx(5 / 60)
        assert row['grid_charge_kwh'] == pytest.approx(0)
    for row in buffered_export:
        assert row['sell'] == pytest.approx(0.15)
        assert row['hours'] == pytest.approx(5 / 60)
        assert min(row['export_kwh'], row['discharge_kwh']) == pytest.approx(0)

    charge = [r for r in rows if r['action'] == 'force_charge' and r['buy'] == 0]
    export = [r for r in rows if r['action'] == 'force_discharge' and r['sell'] == pytest.approx(0.15)]
    assert charge and export
    assert all(665 <= minute(r) and minute(r) + r['hours'] * 60 <= 835 for r in charge)
    assert all(1085 <= minute(r) and minute(r) + r['hours'] * 60 <= 1255 for r in export)

    s.forecast.tariff_buffer_minutes = 0
    assert len(split_points(s, points)) == len(points)
    s.forecast.tariff_buffer_minutes = 7
    starts = {minute(r) for r in split_points(s, points)}
    assert {667, 833, 1087, 1253} <= starts


def test_export_reserve_includes_demand_and_adapts_to_overnight_spread(db, settings):
    s = solarmax(settings)
    s.forecast.terminal_value_factor = 0
    start = START.replace(hour=18)
    points = sunny_points(start, hours=12, peak_kw=0)
    eta = s.systems[0].battery.round_trip_efficiency ** 0.5
    for p in points:
        hour = datetime.fromisoformat(p['ts']).astimezone(ZONE).hour
        expected = 0 if hour >= 21 or hour < 6 else 0.15
        if hour >= 21 or hour < 6:
            expected = 8 * eta * p['hours'] / 9
        p.update(house_kwh=expected, house_p90=expected, load_kwh=expected)
    result = run(db, s, points, soc_pct=70)
    last = next(r for r in result['intervals'] if datetime.fromisoformat(r['ts']).astimezone(ZONE).strftime('%H:%M') == '20:30')
    # 15% minimum + 20% for 21:00–06:00 + 5 minutes of expected load before 21:00.
    expected_floor = 35 + 0.15 / 6 / eta / BATTERY_KWH * 100
    assert last['systems']['main']['export_floor_pct'] == pytest.approx(expected_floor)
    for p in points:
        p['house_p90'] += p['hours'] * 1.0
    noisy = run(db, s, points, soc_pct=70)
    last = next(r for r in noisy['intervals'] if r['ts'] == last['ts'])
    assert last['systems']['main']['export_floor_pct'] == pytest.approx(expected_floor + 4)
    sales = [row for row in noisy['intervals'] if row['action'] == 'force_discharge']
    assert sales
    for row in sales:
        d = row['systems']['main']
        assert d['soc_pct'] >= d['export_floor_pct'] - 1e-5
        assert row['commands']['main']['target_soc'] >= d['export_floor_pct']
    assert noisy['intervals'][-1]['soc_pct'] < expected_floor
