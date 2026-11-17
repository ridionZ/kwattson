from datetime import datetime

import pytest
from pydantic import ValidationError

from kwattson.config import Settings
from kwattson.tariffs import Allowance, Period, TariffPlan, bill, plan_at, rate_at

from conftest import ZONE, solarmax


def utc(local):
    return local.replace(tzinfo=ZONE)


def test_pending_plan_is_never_used(settings):
    assert plan_at(settings.tariffs, utc(datetime(2026, 12, 1, 12))).id == 'flat'


def test_solarmax_periods(settings):
    s = solarmax(settings)
    expect = {0: 0.165, 7: 0.2691, 12: 0.0, 14: 0.2691, 16: 0.4955, 19: 0.4955, 22: 0.2691}
    for hour, rate in expect.items():
        assert rate_at(s.tariffs, utc(datetime(2026, 12, 1, hour, 10)), ZONE)['buy'] == pytest.approx(rate, abs=1e-3)
    assert rate_at(s.tariffs, utc(datetime(2026, 12, 1, 19)), ZONE)['sell'] == pytest.approx(0.15)
    assert rate_at(s.tariffs, utc(datetime(2026, 12, 1, 16)), ZONE)['sell'] == pytest.approx(0.05)


def test_daily_allowances_are_applied_in_order(settings):
    s = solarmax(settings)
    rows = []
    # 60 kWh imported in the free window: 50 free, 10 at 26.91c.
    for slot in range(6):
        rows.append(dict(ts=utc(datetime(2026, 12, 1, 11 + slot // 2, 30 * (slot % 2))).isoformat(), import_kwh=10.0, export_kwh=0.0))
    # 36 kWh exported 18–21: 30 at 15c, 6 at 5c.
    for slot in range(6):
        rows.append(dict(ts=utc(datetime(2026, 12, 1, 18 + slot // 2, 30 * (slot % 2))).isoformat(), import_kwh=0.0, export_kwh=6.0))
    day = bill(s.tariffs, rows, ZONE)['2026-12-01']
    assert day['import_cost'] == pytest.approx(10 * 0.2691, abs=1e-2)
    assert day['export_credit'] == pytest.approx(30 * 0.15 + 6 * 0.05, abs=1e-2)
    assert day['net'] == pytest.approx(1.52 + 10 * 0.2691 - 30 * 0.15 - 6 * 0.05, abs=1e-2)


def test_allowance_resets_each_local_day(settings):
    s = solarmax(settings)
    rows = [dict(ts=utc(datetime(2026, 12, d, 12)).isoformat(), import_kwh=40.0, export_kwh=0.0) for d in (1, 2)]
    days = bill(s.tariffs, rows, ZONE)
    assert days['2026-12-01']['import_cost'] == 0 and days['2026-12-02']['import_cost'] == 0


def test_gaps_in_coverage_rejected():
    with pytest.raises(ValidationError, match='no rate covers'):
        TariffPlan(id='gap', name='Gap', supply_daily=1, periods=[Period(name='day', start='06:00', end='18:00', import_rate=0.3, export_rate=0.05)])


def test_inverted_allowance_rejected():
    with pytest.raises(ValidationError, match='must not exceed'):
        TariffPlan(id='bad', name='Bad', supply_daily=1,
                   periods=[Period(name='all', start='00:00', end='24:00', import_rate=0.4, export_rate=0.05)],
                   allowances=[Allowance(kind='import', periods=['all'], kwh_per_day=10, rate_after=0.2)])


def test_non_finite_numbers_rejected(settings):
    data = settings.model_dump()
    data['site']['export_kw'] = float('nan')
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_needs_base_plan(settings):
    data = settings.model_dump()
    data['tariffs'] = [data['tariffs'][1]]
    with pytest.raises(ValidationError, match='base plan'):
        Settings.model_validate(data)
