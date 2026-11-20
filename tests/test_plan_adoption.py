from datetime import datetime, timedelta
from copy import deepcopy

from kwattson import optimizer, plans, schedules
from kwattson.config import stamp
from kwattson.db import encode
from conftest import ZONE


NOW = datetime(2026, 11, 20, 6, tzinfo=ZONE)


def rows(settings, charge_start=11, charge_end=14, power=4):
    system = settings.systems[0]
    result = []
    for i in range(36):
        at = NOW + timedelta(minutes=i * 30)
        charge = charge_start <= at.hour + at.minute / 60 < charge_end
        result.append(dict(ts=stamp(at), hours=0.5, cost=0, buy_rate=0 if charge else 0.3, sell_rate=0.05,
                           grid_to_battery_kwh=power / 2 if charge else 0, discharge_kwh=0, reason='synthetic',
                           systems={system.id: {'command': {'mode': 'force_charge' if charge else 'self_use',
                                                            'power_kw': power if charge else 0, 'target_soc': 80}}}))
    return result


def test_small_changes_keep_the_adopted_plan(settings):
    before = rows(settings)
    after = rows(settings, power=4.1)
    result = plans.compare(settings, before, after, NOW)
    assert not result['significant']
    assert result['energy_kwh'] < 1


def test_large_energy_and_cost_changes_adopt(settings):
    before = rows(settings)
    assert plans.compare(settings, before, rows(settings, power=5), NOW)['significant']
    after = deepcopy(before)
    after[0]['cost'] = 0.6
    assert plans.compare(settings, before, after, NOW)['reason'] == 'Modelled cost changed'


def test_timing_threshold_is_strict_and_future_only(settings):
    settings.forecast.plan_change_kwh = 20
    before = rows(settings)
    assert not plans.compare(settings, before, rows(settings, charge_start=11.5), NOW)['significant']
    assert plans.compare(settings, before, rows(settings, charge_start=12), NOW)['reason'] == 'Schedule timing changed'
    # A completed change must not cause another schedule write.
    assert not plans.compare(settings, before, rows(settings, charge_start=12), NOW.replace(hour=15))['significant']


def test_short_forecast_horizon_does_not_publish_every_extension(settings):
    before = rows(settings)[:24]
    after = rows(settings)[:25]
    assert not plans.compare(settings, before, after, NOW)['significant']
    assert plans.compare(settings, before, after, NOW.replace(hour=17, minute=30))['significant']


def test_daily_schedule_merges_charge_window_and_preserves_tariff_end(settings):
    commands, expiry = schedules.build(settings, settings.systems[0], rows(settings), NOW)
    charges = [c for c in commands if c.mode == 'force_charge']
    assert len(charges) == 1
    assert charges[0].start.astimezone(ZONE).hour == 11
    assert charges[0].end.astimezone(ZONE).hour == 14
    assert expiry.astimezone(ZONE).hour == 0
    assert commands[-1].end.astimezone(ZONE).strftime('%H:%M') == '23:59'


def test_daily_schedule_does_not_lose_hold_or_discharge_cutoff(settings):
    source = rows(settings)
    for row in source[:2]:
        row['systems']['main']['command'] = dict(mode='hold', power_kw=0, target_soc=55)
    source[2]['systems']['main']['command'] = dict(mode='force_discharge', power_kw=3, target_soc=45)
    source[3]['systems']['main']['command'] = dict(mode='force_discharge', power_kw=3, target_soc=42)
    source[3]['systems']['main']['export_floor_pct'] = 44
    commands, _ = schedules.build(settings, settings.systems[0], source, NOW, live_soc=60)
    assert commands[0].min_soc == 60
    discharge = next(c for c in commands if c.mode == 'force_discharge')
    assert discharge.target_soc == 44


def test_candidate_is_retained_without_replacing_active_plan(db, settings):
    from conftest import sunny_points
    from kwattson.ingest import store_system
    from kwattson.plugins.base import SystemReading
    settings.mode = 'automatic'
    override = {'vehicle_id': 'car', 'charge_mode': 'ready_by', 'target_pct': 100, 'auto_schedule': True,
                'charge_windows': [{'start': stamp(NOW), 'end': stamp(NOW + timedelta(hours=2))}]}
    override_id = db.execute('INSERT INTO overrides(created_at,starts,expires,kind,data) VALUES(?,?,?,?,?)',
        (stamp(NOW), stamp(NOW), stamp(NOW + timedelta(hours=3)), 'ev_charge_now', encode(override)))
    store_system(db, 'main', 'simulator', SystemReading(ts=NOW, time_verified=True, soc=50))
    points = sunny_points(NOW, hours=48, peak_kw=0)
    def candidate(at):
        result = optimizer.plan(db, settings, 0, points, {}, now=at)
        row = db.one('SELECT * FROM decisions WHERE id=?', (result['id'],))
        row['summary'] = result['summary']
        return row
    first = candidate(NOW)
    adopted = plans.consider(db, settings, None, first, NOW)
    override.update(surplus_guard={'good_last': stamp(NOW), 'good_samples': 1}, observed_plug=True,
                    charge_command={'action': 'start', 'requested_at': stamp(NOW)}, native_start_warned_for=stamp(NOW))
    db.execute('UPDATE overrides SET data=? WHERE id=?', (encode(override), override_id))
    second = candidate(NOW + timedelta(minutes=1))
    chosen = plans.consider(db, settings, adopted, second, NOW + timedelta(minutes=1))
    assert chosen['id'] == adopted['id']
    assert plans.latest(db)['id'] == adopted['id']
    assert db.one('SELECT COUNT(*) AS n FROM decisions')['n'] == 2
    assert db.meta('active_plan')['candidate_id'] == second['id']


def test_ev_goals_and_control_changes_still_invalidate_plan_policy(db, settings):
    data = dict(vehicle_id='car', charge_mode='ready_by', target_pct=80, auto_schedule=True,
                charge_windows=[dict(start=stamp(NOW), end=stamp(NOW + timedelta(hours=2)))])
    identity = db.execute('INSERT INTO overrides(created_at,starts,expires,kind,data) VALUES(?,?,?,?,?)',
        (stamp(NOW), stamp(NOW), stamp(NOW + timedelta(hours=3)), 'ev_charge_now', encode(data)))
    before = plans.policy(db, settings, NOW)
    changes = [dict(target_pct=100), dict(auto_schedule=False), dict(cancel_requested=True),
               dict(state='finished'), dict(charge_windows=[dict(start=stamp(NOW), end=stamp(NOW + timedelta(hours=1)))])]
    for change in changes:
        db.execute('UPDATE overrides SET data=? WHERE id=?', (encode({**data, **change}), identity))
        assert plans.policy(db, settings, NOW) != before


def test_manual_override_and_new_day_bypass_thresholds(db, settings):
    previous = {'id': 1, 'version': 'test', 'summary': {'solver': 'HiGHS MILP', 'telemetry_fresh': True}}
    # consider() can adopt a safety change without attempting an energy comparison.
    db.set_meta('active_plan', dict(decision_id=1, day='2026-11-20', policy=plans.policy(db, settings, NOW)))
    db.execute('INSERT INTO overrides(created_at,starts,expires,kind,data) VALUES(?,?,?,?,?)',
               (stamp(NOW), stamp(NOW), stamp(NOW + timedelta(hours=1)), 'hold', encode({})))
    proposed = {**previous, 'id': 2}
    chosen = plans.consider(db, settings, previous, proposed, NOW)
    assert chosen['id'] == 2
    assert chosen['summary']['adopted']


def test_failed_candidate_blocks_control_without_erasing_adopted_plan(db, settings):
    previous = {'id': 1, 'version': 'test', 'summary': {'solver': 'HiGHS MILP', 'telemetry_fresh': True}}
    db.set_meta('active_plan', dict(decision_id=1, day='2026-11-20', policy=plans.policy(db, settings, NOW)))
    candidate = {'id': 2, 'version': 'test', 'summary': {'solver': 'fallback', 'telemetry_fresh': False}}
    # A fallback is never used to replace a working plan or to bless its freshness.
    chosen = plans.consider(db, settings, previous, candidate, NOW + timedelta(days=1))
    assert chosen['id'] == 1
    assert not db.meta('active_plan')['valid']


def test_new_day_requires_a_new_adopted_plan_even_without_energy_changes(db, settings):
    previous = {'id': 1, 'version': 'test', 'summary': {'solver': 'HiGHS MILP', 'telemetry_fresh': True}}
    db.set_meta('active_plan', dict(decision_id=1, day='2026-11-19', policy=plans.policy(db, settings, NOW)))
    chosen = plans.consider(db, settings, previous, {**previous, 'id': 2}, NOW)
    assert chosen['id'] == 2
    assert db.meta('active_plan')['day'] == '2026-11-20'


def test_six_am_checkpoint_survives_background_runs(db, settings):
    from types import SimpleNamespace
    from kwattson import forecast
    from kwattson.web.api import forecast_comparison
    settings.site.weather_enabled = False
    db.save_settings(settings)
    first, _, _ = forecast.build(db, settings, NOW)
    plans.checkpoint(db, first, settings, NOW)
    later, _, _ = forecast.build(db, settings, NOW + timedelta(minutes=15))
    plans.checkpoint(db, later, settings, NOW + timedelta(minutes=15))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, security=None, engine=None, runtime=None)))
    result = forecast_comparison(req, today=True)
    assert [run['id'] for run in result['runs']] == [first]
    every_run = forecast_comparison(req, today=True, all_runs=True)
    assert len(every_run['runs']) == 2
