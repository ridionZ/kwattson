"""Tests for importing AER CDR plan data into TariffPlan (kwattson.tariff_import)."""
import asyncio
import copy
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pytest

from kwattson.tariff_import import fetch_cdr, from_cdr, load_file
from kwattson.tariffs import TariffPlan

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / 'fixtures' / 'cdr-covau-solarmax-COV1117614MRE3.json'
TARIFFS_DIR = HERE.parent / 'tariffs'
PLANS_DIR = HERE.parent.parent / 'plans'  # optional local corpus of Energy Made Easy downloads
ON_DATE = date(2026, 11, 17)


def at(plan: TariffPlan, hhmm: str, weekday: int = 0):
    """Period in force at a local time on a given weekday (0 = Monday)."""
    hour, minute = map(int, hhmm.split(':'))
    return plan.period_at(datetime(2024, 1, 1 + weekday, hour, minute))  # 2024-01-01 is a Monday


def cdr(blocks, *, feed_in=None, tariff_extra=None, periods=None) -> dict:
    """Minimal CDR plan-detail payload for synthetic tests."""
    tariff = {'startDate': '01-01', 'endDate': '12-31', 'displayName': 'Test', 'dailySupplyCharge': '1.00',
              'rateBlockUType': 'timeOfUseRates', 'timeOfUseRates': blocks, **(tariff_extra or {})}
    return {'data': {'planId': 'TST1@EME', 'brand': 'test', 'brandName': 'Test', 'displayName': 'Test plan',
                     'electricityContract': {'tariffPeriod': periods or [tariff],
                                             'solarFeedInTariff': feed_in or []}}}


def block(name, price, windows, *, kind='PEAK', rates=None, period='P1Y'):
    return {'type': kind, 'displayName': name, 'period': period,
            'rates': rates or [{'unitPrice': str(price)}],
            'timeOfUse': [{'days': days, 'startTime': start, 'endTime': end} for days, start, end in windows]}


ALL = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']


# --------------------------------------------------------------------------- CovaU SolarMax


@pytest.fixture(scope='module')
def solarmax() -> TariffPlan:
    return load_file(FIXTURE, on_date=ON_DATE)


def test_solarmax_matches_fact_sheet(solarmax):
    """Rates from plan-comparison-2026-11-16.md, GST inclusive."""
    assert solarmax.status == 'pending' and solarmax.active_from is None
    assert solarmax.plan_code == 'COV1117614MRE3'
    assert solarmax.supply_daily == pytest.approx(1.52, abs=0.001)
    expected = {  # time: (import incl GST, export)
        '00:00': (0.165, 0.05), '05:55': (0.165, 0.05),
        '06:00': (0.2691, 0.05), '10:55': (0.2691, 0.05),
        '11:00': (0.0, 0.05), '13:55': (0.0, 0.05),
        '14:00': (0.2691, 0.05),
        '15:00': (0.4955, 0.05), '17:55': (0.4955, 0.05),
        '18:00': (0.4955, 0.15), '20:55': (0.4955, 0.15),
        '21:00': (0.2691, 0.05), '23:55': (0.2691, 0.05),
    }
    for weekday in (0, 6):
        for time, (buy, sell) in expected.items():
            period = at(solarmax, time, weekday)
            assert period.import_rate == pytest.approx(buy, abs=0.001), time
            assert period.export_rate == pytest.approx(sell, abs=0.001), time


def test_solarmax_allowances(solarmax):
    free, export = (next(a for a in solarmax.allowances if a.kind == kind) for kind in ('import', 'export'))
    assert free.kwh_per_day == 50 and free.rate_after == pytest.approx(0.2691, abs=0.001)
    assert [at(solarmax, '12:00').name] == free.periods
    assert export.kwh_per_day == 30 and export.rate_after == pytest.approx(0.05, abs=0.001)
    # The export tier applies only 18-21, not to the whole 15-21 import peak.
    assert export.periods == [at(solarmax, '18:00').name]
    assert at(solarmax, '15:00').name not in export.periods
    assert len(solarmax.allowances) == 2


def test_solarmax_period_names(solarmax):
    names = [p.name for p in solarmax.periods]
    assert len(names) == len(set(names))
    assert at(solarmax, '15:00').name == 'peak' and at(solarmax, '18:00').name == 'peak_fit_peak'


def test_from_cdr_accepts_data_object():
    payload = json.loads(FIXTURE.read_text())
    assert from_cdr(payload['data'], on_date=ON_DATE) == from_cdr(payload, on_date=ON_DATE)


# --------------------------------------------------------------------------- native files


@pytest.mark.parametrize('path', sorted(p for p in TARIFFS_DIR.glob('*.json') if p.name != 'tariff.schema.json'),
                         ids=lambda p: p.name)
def test_example_tariff_files_validate(path):
    plan = load_file(path)
    assert plan.status == ('active' if plan.id == 'flat-example' else 'pending')
    if plan.status == 'active':
        assert plan.active_from is None


def test_example_solarmax_file_matches_import(solarmax):
    native = load_file(TARIFFS_DIR / 'covau-solarmax-qld.json')
    assert native.periods == solarmax.periods and native.allowances == solarmax.allowances


# --------------------------------------------------------------------------- parsing rules


def test_inclusive_end_and_midnight_wrap():
    plan = from_cdr(cdr([
        block('Night', 0.10, [(ALL, '2200', '0659')], kind='OFF_PEAK'),       # HHMM, wraps midnight
        block('Day', 0.20, [(ALL, '07:00', '21:59')], kind='SHOULDER'),
    ]), on_date=ON_DATE)
    assert at(plan, '06:55').import_rate == pytest.approx(0.11)
    assert at(plan, '07:00').import_rate == pytest.approx(0.22)
    assert at(plan, '21:55').import_rate == pytest.approx(0.22)
    assert at(plan, '22:00').import_rate == pytest.approx(0.11)
    assert at(plan, '23:55').import_rate == pytest.approx(0.11)
    assert plan.supply_daily == pytest.approx(1.10)


def test_end_2359_and_0000_mean_midnight():
    for end in ('23:59', '00:00'):
        plan = from_cdr(cdr([block('Day', 0.20, [(ALL, '00:00', end)], kind='SHOULDER')]), on_date=ON_DATE)
        assert [(p.start, p.end) for p in plan.periods] == [('00:00', '24:00')]


def test_day_groups_and_gap_fill():
    plan = from_cdr(cdr([
        block('Peak', 0.40, [(['BUSINESS_DAYS'], '16:00', '20:59')], kind='PEAK'),
        block('Weekend', 0.25, [(['WEEKENDS'], '16:00', '20:59')], kind='SHOULDER'),
        block('Off peak', 0.20, [(ALL, '21:00', '15:59')], kind='OFF_PEAK'),
    ]), on_date=ON_DATE)
    assert at(plan, '17:00', 4).import_rate == pytest.approx(0.44)
    assert at(plan, '17:00', 5).import_rate == pytest.approx(0.275)

    gappy = from_cdr(cdr([
        block('Peak', 0.40, [(['MON', 'TUE', 'WED', 'THU', 'FRI'], '16:00', '20:59')], kind='PEAK'),
        block('Off peak', 0.20, [(ALL, '21:00', '15:59')], kind='OFF_PEAK'),
    ]), on_date=ON_DATE)
    assert at(gappy, '17:00', 6).import_rate == pytest.approx(0.22)  # weekend gap filled with off-peak
    assert 'not covered' in gappy.notes


def test_ambiguous_gap_raises():
    with pytest.raises(ValueError, match='uncovered'):
        from_cdr(cdr([block('Peak', 0.40, [(ALL, '16:00', '20:59')], kind='PEAK')]), on_date=ON_DATE)


def test_overlap_with_different_rates_raises():
    with pytest.raises(ValueError, match='overlap'):
        from_cdr(cdr([
            block('A', 0.10, [(ALL, '00:00', '23:59')], kind='OFF_PEAK'),
            block('B', 0.20, [(ALL, '10:00', '11:59')], kind='PEAK'),
        ]), on_date=ON_DATE)


def test_single_rate_and_demand_charge_noted():
    tariff = {'startDate': '01-01', 'endDate': '12-31', 'dailySupplyCharge': '1', 'rateBlockUType': 'singleRate',
              'singleRate': {'displayName': 'Anytime', 'period': 'P1Y', 'rates': [{'unitPrice': '0.3'}]},
              'demandCharges': [{'displayName': 'Peak Demand', 'amount': '0.2', 'measureUnit': 'KW', 'chargePeriod': 'DAY'}]}
    plan = from_cdr(cdr([], periods=[tariff]), on_date=ON_DATE)
    assert [(p.start, p.end, p.import_rate) for p in plan.periods] == [('00:00', '24:00', pytest.approx(0.33))]
    assert 'Demand charges not modelled' in plan.notes


def test_non_daily_tier_uses_first_rate_with_note():
    rates = [{'unitPrice': '0.00', 'volume': 24}, {'unitPrice': '0.10'}]
    plan = from_cdr(cdr([block('Free', 0, [(ALL, '00:00', '23:59')], kind='OFF_PEAK', rates=rates, period='P1Y')]),
                    on_date=ON_DATE)
    assert not plan.allowances and plan.periods[0].import_rate == 0
    assert 'not modelled' in plan.notes

    daily = from_cdr(cdr([block('Free', 0, [(ALL, '00:00', '23:59')], kind='OFF_PEAK', rates=rates, period='P1D')]),
                     on_date=ON_DATE)
    assert daily.allowances[0].kwh_per_day == 24 and daily.allowances[0].rate_after == pytest.approx(0.11)


def test_seasonal_tariff_period_selection():
    summer = {'startDate': '11-01', 'endDate': '03-31', 'displayName': 'Summer', 'dailySupplyCharge': '1',
              'rateBlockUType': 'singleRate', 'singleRate': {'rates': [{'unitPrice': '0.5'}]}}
    winter = {**summer, 'startDate': '04-01', 'endDate': '10-31', 'displayName': 'Winter',
              'singleRate': {'rates': [{'unitPrice': '0.2'}]}}
    payload = cdr([], periods=[summer, winter])
    assert from_cdr(payload, on_date=date(2026, 1, 15)).periods[0].import_rate == pytest.approx(0.55)
    july = from_cdr(payload, on_date=date(2026, 7, 15))
    assert july.periods[0].import_rate == pytest.approx(0.22)
    assert 'Summer' in july.notes


def test_feed_in_singletariff_tiers_and_government_scheme():
    feed_in = [
        {'payerType': 'RETAILER', 'scheme': 'OTHER', 'tariffUType': 'singleTariff', 'description': 'first 8kWh/day',
         'singleTariff': {'period': 'P1D', 'rates': [{'unitPrice': '0.088', 'volume': 8}]}},
        {'payerType': 'RETAILER', 'scheme': 'OTHER', 'tariffUType': 'singleTariff', 'description': 'after',
         'singleTariff': {'period': 'P1D', 'rates': [{'unitPrice': '0.00'}]}},
        {'payerType': 'GOVERNMENT', 'scheme': 'PREMIUM', 'tariffUType': 'singleTariff', 'description': 'bonus',
         'singleTariff': {'period': 'P1Y', 'rates': [{'unitPrice': '0.44'}]}},
    ]
    plan = from_cdr(cdr([block('Any', 0.3, [(ALL, '00:00', '23:59')], kind='OFF_PEAK')], feed_in=feed_in),
                    on_date=ON_DATE)
    assert plan.periods[0].export_rate == pytest.approx(0.088)  # not GST-adjusted
    assert plan.allowances[0].kind == 'export' and plan.allowances[0].kwh_per_day == 8
    assert 'government/premium' in plan.notes


def test_network_tariff_index_out_of_range():
    with pytest.raises(ValueError, match='network_tariff_index'):
        from_cdr(json.loads(FIXTURE.read_text()), network_tariff_index=1, on_date=ON_DATE)


def test_controlled_load_listed_when_requested(solarmax):
    payload = copy.deepcopy(json.loads(FIXTURE.read_text()))
    payload['data']['electricityContract']['controlledLoad'] = [
        {'displayName': 'Controlled Load 1', 'singleRate': {'rates': [{'unitPrice': '0.146'}]}}]
    assert 'ignored' in from_cdr(payload, on_date=ON_DATE).notes
    assert '16.06c' in from_cdr(payload, include_controlled_load=True, on_date=ON_DATE).notes


@pytest.mark.parametrize('plan_id, brand', [
    ('../etc', 'covau'), ('COV1/x', 'covau'), ('a' * 81, 'covau'), ('', 'covau'),
    ('COV1117614MRE3@EME', 'CovaU'), ('COV1117614MRE3@EME', 'cov/au'), ('COV1117614MRE3@EME', ''),
])
def test_fetch_cdr_rejects_unsafe_identifiers(plan_id, brand):
    with pytest.raises(ValueError):
        asyncio.run(fetch_cdr(plan_id, brand))  # validation happens before any network access


# --------------------------------------------------------------------------- corpus


@pytest.mark.skipif(not PLANS_DIR.is_dir(), reason='plans corpus not available')
def test_bulk_corpus_conversion():
    """Convert every downloaded Energy Made Easy plan; report success and failure reasons."""
    ok, failures, seen = 0, Counter(), 0
    for path in sorted(PLANS_DIR.glob('detail-*.json')):
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
            continue  # e.g. detail-errors.json
        contract = payload['data'].get('electricityContract') or {}
        if payload['data'].get('customerType', 'RESIDENTIAL') != 'RESIDENTIAL':
            continue
        if not {t.get('rateBlockUType') for t in contract.get('tariffPeriod', [])} <= {'singleRate', 'timeOfUseRates'}:
            continue
        seen += 1
        try:
            from_cdr(payload, on_date=ON_DATE)
            ok += 1
        except ValueError as error:
            failures[str(error).splitlines()[0][:80]] += 1
    print(f'\nCDR corpus: {ok}/{seen} converted; failures: {dict(failures.most_common(10))}')
    assert seen and ok / seen >= 0.8
