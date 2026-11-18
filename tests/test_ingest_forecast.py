from datetime import datetime, timedelta, timezone

import pytest

from kwattson import forecast
from kwattson.config import stamp
from kwattson.ingest import combine, store_site
from kwattson.plugins.base import SystemReading
from kwattson.plugins.push import system_reading
from kwattson.validation import as_bool

from conftest import ZONE

T0 = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)


def reading(minutes, load=2.0, solar=0.0, imp=2.0, exp=0.0, soc=50.0):
    return SystemReading(ts=T0 + timedelta(minutes=minutes), time_verified=True, soc=soc, pv_kw=solar, charge_kw=0, discharge_kw=0,
                         import_kw=imp, export_kw=exp, load_kw=load)


def ingest_all(db, settings, minutes_list):
    for m in minutes_list:
        store_site(db, combine(settings, {'main': reading(m)}))


def intervals(db):
    return {r['ts']: r for r in db.rows("SELECT * FROM intervals WHERE source='telemetry'")}


def test_constant_power_integrates_exactly(db, settings):
    ingest_all(db, settings, range(0, 65, 5))
    rows = intervals(db)
    assert rows[stamp(T0)]['load_kwh'] == pytest.approx(1.0)       # 2 kW × 0.5 h
    assert rows[stamp(T0)]['coverage_s'] == 1800


def test_out_of_order_readings_do_not_double_count(db, settings):
    ordered = list(range(0, 35, 5))
    shuffled = [0, 30, 10, 5, 25, 15, 20, 10, 30]
    ingest_all(db, settings, ordered)
    first = intervals(db)[stamp(T0)]['load_kwh']
    db2 = type(db)(db.path.with_name('other.sqlite3'))
    ingest_all(db2, settings, shuffled)
    assert intervals(db2)[stamp(T0)]['load_kwh'] == pytest.approx(first)


def test_gaps_are_not_filled(db, settings):
    ingest_all(db, settings, [0, 5, 40, 45])  # 35-minute gap
    rows = intervals(db)
    assert rows[stamp(T0)]['coverage_s'] == pytest.approx(300)
    assert rows[stamp(T0)]['quality'] == 'partial'


def test_site_meter_required(settings):
    assert combine(settings, {'main': SystemReading(ts=T0, time_verified=True, soc=50, pv_kw=1)}) is None


def test_load_derived_when_not_reported(settings):
    r = SystemReading(ts=T0, time_verified=True, soc=50, pv_kw=5, charge_kw=2, discharge_kw=0, import_kw=0, export_kw=1)
    assert combine(settings, {'main': r})['load_kw'] == pytest.approx(2)


def test_strict_booleans():
    assert as_bool('false') is False and as_bool('on') is True and as_bool(1) is True
    with pytest.raises(ValueError):
        as_bool('maybe')


def test_pushed_reading_validation():
    ok = system_reading({'ts': '2026-11-10T00:00:00Z', 'soc': 50, 'pv_kw': 3})
    assert ok.pv_kw == 3
    for bad in ({'ts': '2026-11-10T00:00:00', 'soc': 50}, {'ts': '2026-11-10T00:00:00Z', 'soc': 150},
                {'ts': '2026-11-10T00:00:00Z', 'soc': 'NaN'}, {'ts': '2026-11-10T00:00:00Z', 'bogus': 1}):
        with pytest.raises(Exception):
            system_reading(bad)


def test_forecast_learns_weekday_profile_and_respects_exclusions_and_holidays(db, settings):
    # Two weeks of data: 1 kW on weekdays, 3 kW on weekends; one excluded day at 50 kW.
    start = datetime(2026, 11, 2, 0, 0, tzinfo=ZONE).astimezone(timezone.utc)  # Monday
    rows = []
    for i in range(14 * 48):
        at = start + timedelta(minutes=30 * i)
        kw = 3.0 if at.astimezone(ZONE).weekday() >= 5 else 1.0
        if at.astimezone(ZONE).date().day == 4:
            kw = 50.0
        rows.append((stamp(at), 'telemetry', 0, kw / 2, 0, kw / 2, 0, 0, 0, 50, 0, 1800, 'observed'))
    with db.connect() as c:
        c.executemany('INSERT INTO intervals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
        c.execute('INSERT INTO exclusions(start, end, reason) VALUES(?,?,?)',
                  (stamp(datetime(2026, 11, 4, tzinfo=ZONE)), stamp(datetime(2026, 11, 5, tzinfo=ZONE)), 'test'))
        c.execute('INSERT INTO holidays(name, start, end, load_factor, ev_away, created_at) VALUES(?,?,?,?,?,?)',
                  ('Away', stamp(datetime(2026, 11, 17, 12, tzinfo=ZONE)), stamp(datetime(2026, 11, 18, tzinfo=ZONE)), None, 1, stamp()))
    now = datetime(2026, 11, 16, 0, 0, tzinfo=ZONE).astimezone(timezone.utc)  # Monday
    settings.vehicles = []
    fid, points, model = forecast.build(db, settings, now)
    by_local = {datetime.fromisoformat(p['ts']).astimezone(ZONE): p for p in points}
    monday = by_local[datetime(2026, 11, 16, 10, 0, tzinfo=ZONE)]
    holiday = by_local[datetime(2026, 11, 17, 14, 0, tzinfo=ZONE)]
    assert monday['house_kwh'] == pytest.approx(0.5, rel=0.05)   # excluded 50 kW day ignored
    assert holiday['house_kwh'] == pytest.approx(settings.forecast.holiday_base_load_kw / 2, rel=0.01)
    assert db.one('SELECT COUNT(*) AS n FROM forecast_points WHERE forecast_id=?', (fid,))['n'] == len(points)


def test_early_accuracy_and_partial_solar_windows(db, settings):
    run, _, _ = forecast.build(db, settings, now=T0)
    ingest_all(db, settings, range(0, 121, 5))
    assert forecast.score_run(db, run, T0 + timedelta(minutes=29))['samples'] == 0
    assert forecast.score_run(db, run, T0 + timedelta(minutes=91))['samples'] == 3
    assert db.one('SELECT 1 FROM evaluations') is None
    db.execute('UPDATE forecast_points SET solar_kwh=hours*2 WHERE forecast_id=?', (run,))
    now = T0 + timedelta(minutes=17)
    est = forecast.solar_snapshot(db, settings, now)
    midnight = (now.astimezone(settings.zone) + timedelta(days=1)).replace(hour=0, minute=0)
    assert est['next_hour'] == 2
    assert est['rest_today'] == pytest.approx((midnight-now).total_seconds()/1800, abs=0.01)
    assert forecast.solar_snapshot(db, settings, now + timedelta(hours=2)) == {}
    assert forecast.evaluate(db, T0 + timedelta(hours=26)) == 1


def test_comparison_matches_saved_plan_and_elapsed_meter_intervals(db, settings):
    from kwattson import optimizer

    at = T0 + timedelta(minutes=7)
    original, points, model = forecast.build(db, settings, now=at)
    plan = optimizer.plan(db, settings, original, points, model, now=at, initial={'main': 4.0})
    next_run, _, _ = forecast.build(db, settings, now=at + timedelta(minutes=15))
    ingest_all(db, settings, range(30, 65, 5))
    db.execute('UPDATE forecast_points SET house_kwh=1, ev_kwh=0.5 WHERE forecast_id=?', (original,))
    now = T0 + timedelta(hours=1, minutes=45)
    measured, gap = forecast.comparison(db, original, now)['points']
    assert measured['end'] == stamp(T0 + timedelta(hours=1))
    assert measured['actual_solar'] == 0 and measured['actual_load'] == pytest.approx(1)
    assert measured['forecast_load'] == 1.5 and measured['actual_soc'] == 50
    assert measured['planned_soc'] == pytest.approx(plan['intervals'][1]['soc_pct'])
    assert gap['actual_load'] is None and gap['actual_solar'] is None and gap['actual_soc'] is None
    assert gap['planned_soc'] is not None
    assert all(p['planned_soc'] is None for p in forecast.comparison(db, next_run, now)['points'])


def test_today_comparison_uses_latest_available_forecast_without_lookahead(db, settings):
    start = T0.replace(hour=0)
    first, _, _ = forecast.build(db, settings, start - timedelta(hours=1))
    newer, _, _ = forecast.build(db, settings, start - timedelta(minutes=10))
    future, _, _ = forecast.build(db, settings, start + timedelta(minutes=5))
    for fid, solar in ((first, 0), (newer, 1.75), (future, 2.5)):
        db.execute('UPDATE forecast_points SET solar_kwh=? WHERE forecast_id=?', (solar, fid))
    ingest_all(db, settings, range(0, 35, 5))

    points = forecast.today_comparison(db, settings, start + timedelta(hours=1, minutes=10))['points']
    first_slot, second_slot = points[-2:]
    assert first_slot['end'] == stamp(start + timedelta(minutes=30))
    assert first_slot['forecast_solar'] == 1.75
    assert first_slot['actual_load'] == pytest.approx(1.0)
    assert second_slot['forecast_solar'] == 2.5
    assert second_slot['actual_load'] is None
    assert points[0]['forecast_solar'] is None


def test_money_periods_use_local_week_boundaries_supply_and_partial_coverage(db, settings):
    from kwattson.outcomes import money_periods

    today = datetime(2026, 11, 18, 6, tzinfo=ZONE)
    for days_ago, imported, exported in ((7, 2, 0), (2, 1, 0), (1, 0, 60), (0, 1, 0)):
        at = today - timedelta(days=days_ago, hours=1)
        db.execute(
            'INSERT INTO intervals (ts, source, solar_kwh, load_kwh, ev_kwh, import_kwh, export_kwh, '
            "charge_kwh, discharge_kwh, coverage_s, quality) VALUES (?, 'telemetry', 0, 0, 0, ?, ?, 0, 0, 1800, '{}')",
            (stamp(at), imported, exported),
        )
    periods = money_periods(db, settings, today)
    assert periods['today']['net_aud'] == pytest.approx(1.30)
    assert periods['today']['coverage'] == pytest.approx(0.5 / 6)
    assert periods['yesterday']['net_aud'] == pytest.approx(-2)
    assert periods['last_week']['net_aud'] == pytest.approx(1.60)
    assert periods['this_week']['net_aud'] == pytest.approx(1.30 - 2 + 1.30)


def test_older_weather_remains_usable_with_cautious_planning(db, settings):
    fetched = T0 - timedelta(hours=12)
    for hour in range(2):
        at = T0 + timedelta(hours=hour)
        db.execute('INSERT INTO weather_hours VALUES(?,?,?,?,?,?,?)',
                   (stamp(at), 27, 27, 600, 0, stamp(fetched), 12 + hour))
    _, points, model = forecast.build(db, settings, T0)
    assert not model['weather_fresh'] and model['weather_usable']
    assert points[0]['radiation'] == 600
    assert points[0]['load_kwh'] > points[0]['house_kwh']
    assert sum(points[0]['solar_by_system'].values()) < points[0]['solar_kwh']
    assert 'cautious planning' in points[0]['solar_basis']


def test_projected_today_prorates_remaining_plan_and_keeps_supply_once(settings):
    from types import SimpleNamespace

    from kwattson.outcomes import projected_today

    now = datetime(2026, 11, 18, 23, 15, tzinfo=ZONE)
    decision = {'id': 7, 'created_at': stamp(now - timedelta(minutes=15))}
    rows = [
        {'ts': stamp(now - timedelta(minutes=15)), 'hours': 0.5, 'cost': 0.4},
        {'ts': stamp(now + timedelta(minutes=15)), 'hours': 1, 'cost': -0.4},
    ]
    assert projected_today(SimpleNamespace(rows=lambda *_: rows), settings, now, decision, {'net_aud': 1.35}) == 1.35
    assert projected_today(SimpleNamespace(rows=lambda *_: rows[:1]), settings, now, decision, {'net_aud': 1.35}) is None
