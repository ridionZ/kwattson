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
