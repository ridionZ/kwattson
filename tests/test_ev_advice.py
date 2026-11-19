from datetime import datetime, timedelta

import pytest

from kwattson.advice import ev_advice
from kwattson.config import UTC, Vehicle, stamp
from kwattson.ingest import store_vehicle
from kwattson.plugins.base import VehicleReading
from kwattson.vehicle_usage import daily_drain, fit_cycle, phase, project

from conftest import ZONE


NOW = datetime(2026, 11, 19, 14, tzinfo=ZONE).astimezone(UTC)  # Thursday afternoon
CAR = Vehicle(id='car', name='Car', plugin='push_vehicle', capacity_kwh=60,
              charger_kw=7.2, reminder_soc_pct=30)


def reading(db, at, soc, *, home=True, charging=False):
    store_vehicle(db, CAR.id, CAR.plugin, VehicleReading(ts=at, time_verified=True, soc=soc, at_home=home, charging=charging))


def learn(db, weeks):
    thursday = NOW.astimezone(ZONE).replace(hour=0)
    for week in range(1, weeks + 1):
        start = thursday - timedelta(weeks=week)
        for hours, soc, charging in (
            (0, 72, False), (6, 72, False), (12, 60, False), (18, 60, False),
            (24, 60, False), (30, 80, True), (36, 80, False), (42, 58, False), (48, 58, False),
        ):
            reading(db, start + timedelta(hours=hours), soc, charging=charging)


def plan():
    rows = []
    for i in range(96):
        at = NOW + timedelta(minutes=30 * i)
        rows.append(dict(ts=stamp(at), hours=0.5, curtailed_kwh=3.6 if i < 8 else 0,
                         export_kwh=0, buy_rate=0.30, sell_rate=0, radiation=100,
                         baseline_curtailed_kwh=0, solar_kwh=0, action='self_use',
                         fox_mode='SelfUse', soc_pct=50, reason='Test plan'))
    return rows


def test_weekday_advice_charges_before_low_solar_day(db, settings):
    learn(db, 3)
    weekly, period, _ = fit_cycle(daily_drain(db, CAR, NOW, settings.zone))
    assert weekly == {3: 12, 4: 22}
    assert period == 1
    reading(db, NOW, 48)
    info = ev_advice(db, settings, plan(), CAR, NOW, [])
    assert info['projection'][0]['soc_pct'] == 43
    assert info['projection'][1]['soc_pct'] == 21
    rec = info['recommendation']
    assert rec['start'] == stamp(NOW)
    assert 'little spare solar' in rec['text']
    assert rec['energy_kwh'] == pytest.approx(21.8)
    assert rec['cost_aud'] == 0

    reading(db, NOW, 70)
    assert ev_advice(db, settings, plan(), CAR, NOW, [])['recommendation'] is None


def test_insufficient_history_and_away_or_charging_suppress_advice(db, settings):
    learn(db, 2)
    reading(db, NOW, 48)
    info = ev_advice(db, settings, plan(), CAR, NOW, [])
    assert info['projection'][0]['soc_pct'] is None
    assert info['recommendation'] is None

    reading(db, NOW, 28)
    assert ev_advice(db, settings, plan(), CAR, NOW, [])['recommendation']
    future = NOW + timedelta(hours=1)
    assert ev_advice(db, settings, plan(), CAR, NOW, [(NOW, future)])['recommendation'] is None
    reading(db, NOW, 28, home=False)
    assert ev_advice(db, settings, plan(), CAR, NOW, [])['recommendation'] is None
    reading(db, NOW, 28, charging=True)
    assert ev_advice(db, settings, plan(), CAR, NOW, [])['recommendation'] is None


def test_charge_notifications_require_fresh_home_presence(db, settings, monkeypatch):
    from types import SimpleNamespace

    from kwattson import advice, alerts

    monkeypatch.setattr(advice, 'plan_intervals', lambda *_: plan())
    settings.vehicles = [CAR]
    sent = []
    notify = SimpleNamespace(emit=lambda event, *args, **kwargs: sent.append(event))
    for home in (None, False):
        reading(db, NOW, 28, home=home)
        alerts.check(db, settings, notify, {}, [], 1, NOW)
        assert not any(event.startswith('ev_charge') for event in sent)

    reading(db, NOW, 28)
    alerts.check(db, settings, notify, {}, [], 1, NOW)
    assert [event for event in sent if event.startswith('ev_charge')] == ['ev_charge_opportunity']

    sent.clear()
    alerts.check(db, settings, notify, {}, [], 1, NOW + timedelta(minutes=31))
    assert not any(event.startswith('ev_charge') for event in sent)



def test_kilometre_estimates_cover_known_days_and_give_way_to_history(db, settings):
    car = CAR.model_copy(update={'weekday_km': [None, None, None, None, 100, None, 0]})
    reading(db, NOW, 48)
    info = ev_advice(db, settings, plan(), car, NOW, [])
    assert info['projection'][0]['source'] is None
    friday = info['projection'][1]
    assert friday['source'] == 'estimate'
    assert friday['soc_pct'] is None
    assert friday['soc_upper_pct'] == pytest.approx(48 - 100 * 14 / 60)
    assert info['projection'][3]['drain_pct'] == 0
    assert 'Could reach 25% or lower' in info['recommendation']['text']

    learn(db, 3)
    info = ev_advice(db, settings, plan(), car, NOW, [])
    assert info['projection'][1]['source'] == 'observed'
    assert info['projection'][1]['drain_pct'] == 22


def test_three_week_roster_improves_held_out_predictions(settings):
    today = NOW.astimezone(ZONE).date()
    daily = {}
    for days_ago in range(1, 85):
        day = today - timedelta(days=days_ago)
        daily[day] = 0 if phase(day, 3) == 0 else 20
    weekly, period, _ = fit_cycle(daily)
    assert period == 3
    assert weekly[today.weekday()] == 20
    points = project(75, NOW, settings.zone, CAR, daily)
    for point in points[1:]:
        day = datetime.fromisoformat(point['day']).date()
        assert point['drain_pct'] == daily[day - timedelta(weeks=3)]


def test_charging_day_suggestion_can_extend_beyond_the_home_plan(db, settings):
    car = CAR.model_copy(update={'manual_soc': 85, 'weekday_km': [100] * 7})
    info = ev_advice(db, settings, [], car, NOW, [])
    assert info['suggested_day'] == '2026-11-21'
    assert info['recommendation'] is None
