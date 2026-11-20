"""Exercise FoxESS verification against synthetic scheduler replies."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from kwattson.config import BatterySpec, utcnow
from kwattson.plugins import foxess
from kwattson.plugins.base import BatteryCommand, PluginError
from kwattson.plugins.foxess import FoxConfig, FoxESSCloud


@pytest.mark.parametrize('failure', [None, 'disabled', 'missing_cutoff', 'wrong_cutoff', 'slow_readback'])
def test_scheduler_waits_for_matching_enabled_group_without_rewriting(monkeypatch, failure):
    battery = BatterySpec(capacity_kwh=40, charge_kw=5, discharge_kw=5)
    fox = FoxESSCloud(SimpleNamespace(timezone='Australia/Brisbane'), FoxConfig(serial='example'),
                      SimpleNamespace(battery=battery, ac_kw=5))
    start = utcnow().replace(second=0, microsecond=0)
    command = BatteryCommand(mode='force_discharge', start=start, end=start + timedelta(minutes=20),
                             power_kw=4.48, target_soc=93.2)
    calls = []
    reads = 0

    async def request(path, payload, write=False):
        nonlocal reads
        calls.append((path, write))
        if write:
            return {'errno': 0}
        if not any(writes for _, writes in calls):
            return {'result': {'properties': {'fdpwr': {'unit': 'W', 'range': {'max': 5000}}}}}
        reads += 1
        if failure == 'slow_readback' and reads in (2, 3):
            raise httpx.ReadTimeout('synthetic read timeout')
        group = fox._group(command)
        group['startHour'] = str(group['startHour'])
        if reads == 1 or (failure == 'slow_readback' and reads < 5):
            group['workMode'] = 'SelfUse'
        if failure == 'missing_cutoff':
            group['extraParam'].pop('fdSoc')
        elif failure == 'wrong_cutoff':
            group['extraParam']['fdSoc'] -= 1
        return {'result': {'enable': failure != 'disabled', 'groups': [group]}}

    monkeypatch.setattr(foxess, 'SCHEDULER_READ_PAUSE', 0)
    monkeypatch.setattr(fox, '_request', request)
    result = asyncio.run(fox.apply(command))
    assert result.verified is (failure in (None, 'slow_readback'))
    assert reads == (2 if failure is None else 5)
    assert [path for path, writes in calls if writes] == [
        '/op/v2/device/scheduler/enable', '/op/v1/device/scheduler/set/flag',
    ]
    if failure is None:
        assert result.detail['read_back_checks'] == [
            {'enabled': True, 'groups': 1, 'matched': False},
            {'enabled': True, 'groups': 1, 'matched': True},
        ]


@pytest.mark.parametrize('mode', ['force_charge', 'force_discharge'])
@pytest.mark.parametrize('properties, expected_w', [
    ({'fdpwr': {'unit': 'W', 'range': {'max': 10500}}}, 10500),
    ({'fdPwr': {'unit': 'W', 'range': {'max': '8000'}}}, 8000),
    ({}, 9900),
])
def test_scheduler_caps_power_at_device_limit_without_changing_dc_rating(monkeypatch, mode, properties, expected_w):
    battery = BatterySpec(capacity_kwh=40, charge_kw=13, discharge_kw=13)
    fox = FoxESSCloud(SimpleNamespace(timezone='Australia/Brisbane'), FoxConfig(serial='example'),
                      SimpleNamespace(battery=battery, ac_kw=9.9))
    start = utcnow().replace(second=0, microsecond=0)
    command = BatteryCommand(mode=mode, start=start, end=start + timedelta(minutes=20),
                             power_kw=13, target_soc=40)
    sent = None
    calls = []

    async def request(path, payload, write=False):
        nonlocal sent
        calls.append((path, write))
        if path == '/op/v2/device/scheduler/enable':
            sent = payload['groups'][0]
            # Reproduce the inverter: acknowledge but retain the old schedule
            # if the requested power is above the supported AC range.
            return {'errno': 0}
        if write:
            return {'errno': 0}
        if sent is None:
            return {'result': {'properties': properties}}
        groups = [sent] if sent['extraParam']['fdPwr'] <= expected_w else []
        return {'result': {'enable': 1, 'groups': groups}}

    monkeypatch.setattr(fox, '_request', request)
    result = asyncio.run(fox.apply(command))
    assert result.verified
    assert sent['extraParam']['fdPwr'] == expected_w
    assert result.detail['scheduler_power_limit_w'] == expected_w
    assert sent['extraParam']['minSocOnGrid'] == 15
    assert battery.charge_kw == 13
    assert calls[0] == ('/op/v2/device/scheduler/get', False)


@pytest.mark.parametrize('maximum', [None, 0, -1, 'invalid', float('nan'), float('inf')])
def test_invalid_advertised_power_limit_sends_no_commands(monkeypatch, maximum):
    fox = FoxESSCloud(SimpleNamespace(timezone='UTC'), FoxConfig(serial='example'),
                      SimpleNamespace(battery=BatterySpec(capacity_kwh=40, charge_kw=13, discharge_kw=10), ac_kw=9.9))
    start = utcnow()
    command = BatteryCommand(mode='force_charge', start=start, end=start + timedelta(minutes=20), power_kw=13)
    writes = []

    async def request(path, payload, write=False):
        if write:
            writes.append(path)
        return {'result': {'properties': {'fdpwr': {'unit': 'W', 'range': {'max': maximum}}}}}

    monkeypatch.setattr(fox, '_request', request)
    with pytest.raises(PluginError, match='invalid scheduler power limit'):
        asyncio.run(fox.apply(command))
    assert not writes


def test_release_checks_flag_again_after_delayed_readback(monkeypatch):
    fox = FoxESSCloud(SimpleNamespace(timezone='Australia/Brisbane'), FoxConfig(serial='example'), None)
    responses = iter([{'result': {'enable': True}}, {'result': {'enable': False}}])
    writes = []

    async def request(path, payload, write=False):
        if write:
            writes.append(payload['enable'])
            return {'errno': 0}
        return next(responses)

    monkeypatch.setattr(fox, '_request', request)
    result = asyncio.run(fox.release())
    assert result.verified
    assert result.detail['read_back_attempts'] == 2
    assert writes == [0]


def test_daily_schedule_is_written_once_then_only_read(monkeypatch):
    from copy import deepcopy
    fox = FoxESSCloud(SimpleNamespace(timezone='Australia/Brisbane'), FoxConfig(serial='example'),
                      SimpleNamespace(battery=BatterySpec(capacity_kwh=40, charge_kw=13, discharge_kw=10), ac_kw=9.9))
    start = utcnow().replace(second=0, microsecond=0)
    # Keep the synthetic test away from local midnight.
    start = start.astimezone(foxess.ZoneInfo('Australia/Brisbane')).replace(hour=11)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return start.astimezone(tz)
    monkeypatch.setattr(foxess, 'datetime', Clock)
    commands = [BatteryCommand('force_charge', start, start + timedelta(hours=2), 13, 80),
                BatteryCommand('self_use', start + timedelta(hours=2), start + timedelta(hours=3))]
    state = {'enable': 0, 'groups': [], 'maxGroupCount': 96,
             'properties': {'fdpwr': {'unit': 'W', 'range': {'min': 0, 'max': 10500}}}}
    writes = []
    async def request(path, payload, write=False):
        if write:
            writes.append(path)
            if path.endswith('/scheduler/enable'):
                state['groups'] = deepcopy(payload['groups'])
            else:
                state['enable'] = payload['enable']
            return {'errno': 0}
        return {'result': deepcopy(state)}
    monkeypatch.setattr(fox, '_request', request)
    async def run():
        first = await fox.apply_schedule(commands)
        assert first.verified
        assert first.detail['groups'][0]['extraParam']['fdPwr'] == 10500
        assert 'enable' not in first.detail['groups'][0]
        assert (await fox.verify_schedule(commands)).verified
        assert (await fox.apply_schedule(commands)).detail['reused']
        state['groups'][0]['extraParam']['maxSoc'] = 100
        assert not (await fox.verify_schedule(commands)).verified
    asyncio.run(run())
    assert writes == ['/op/v3/device/scheduler/enable', '/op/v1/device/scheduler/set/flag']


def test_daily_schedule_rejects_unsupported_reserve_and_group_capacity(monkeypatch):
    fox = FoxESSCloud(SimpleNamespace(timezone='UTC'), FoxConfig(serial='example'),
                      SimpleNamespace(battery=BatterySpec(capacity_kwh=40, charge_kw=5, discharge_kw=5, reserve_pct=5), ac_kw=5))
    start = utcnow().replace(hour=11)
    command = BatteryCommand('hold', start, start + timedelta(hours=1), min_soc=5)
    metadata = {'maxGroupCount': 96, 'properties': {'minsocongrid': {'range': {'min': 10, 'max': 100}}}}
    with pytest.raises(PluginError, match='minSocOnGrid'):
        fox._schedule_groups([command], metadata)
    with pytest.raises(PluginError, match='cannot hold'):
        fox._schedule_groups([command], {'maxGroupCount': 0})


def test_release_allows_slow_cloud_flag_without_resending_disable(monkeypatch):
    fox = FoxESSCloud(None, FoxConfig(serial='example'), None)
    reads, writes = [], []
    async def request(path, payload, write=False):
        if write:
            writes.append(path)
            return {'errno': 0}
        reads.append(path)
        return {'result': {'enable': len(reads) < 5}}
    monkeypatch.setattr(foxess, 'SCHEDULER_READ_PAUSE', 0)
    monkeypatch.setattr(fox, '_request', request)
    assert asyncio.run(fox.release()).verified
    assert len(reads) == 5 and len(writes) == 1


def test_daily_schedule_ignores_completed_prefix_but_not_future_changes():
    start = utcnow().replace(hour=11, minute=0, second=0, microsecond=0)
    fox = FoxESSCloud(SimpleNamespace(timezone='UTC'), FoxConfig(serial='example'),
                      SimpleNamespace(battery=BatterySpec(capacity_kwh=40, charge_kw=5, discharge_kw=5), ac_kw=5))
    command = BatteryCommand('force_charge', start, start + timedelta(hours=3), 4, 80)
    sent = fox._group(command)
    sent.pop('enable')
    got = {**sent, 'startHour': 10}
    assert foxess._same_schedule([sent], {'enable': 1, 'groups': [got]}, start + timedelta(hours=1))
    assert not foxess._same_schedule([sent], {'enable': 1, 'groups': [{**got, 'endHour': 15}]}, start + timedelta(hours=1))
    assert not foxess._same_schedule([sent], {'enable': 1, 'groups': [{**got, 'enable': 0}]}, start + timedelta(hours=1))
