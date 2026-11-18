"""Exercise FoxESS verification against synthetic scheduler replies."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from kwattson.config import BatterySpec, utcnow
from kwattson.plugins import foxess
from kwattson.plugins.base import BatteryCommand
from kwattson.plugins.foxess import FoxConfig, FoxESSCloud


@pytest.mark.parametrize('failure', [None, 'disabled', 'missing_cutoff', 'wrong_cutoff', 'slow_readback'])
def test_scheduler_waits_for_matching_enabled_group_without_rewriting(monkeypatch, failure):
    battery = BatterySpec(capacity_kwh=40, charge_kw=5, discharge_kw=5)
    fox = FoxESSCloud(SimpleNamespace(timezone='Australia/Brisbane'), FoxConfig(serial='example'),
                      SimpleNamespace(battery=battery))
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
