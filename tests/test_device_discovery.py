"""Read-only installer probes using vendor-shaped replies and no network."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kwattson.plugins.base import PluginError, SystemReading
from kwattson.plugins.foxess import FoxConfig, FoxESSCloud
from kwattson.plugins.mg_ismart import MGConfig, MGiSmart
from kwattson.config import utcnow


@pytest.mark.parametrize('email', [True, False])
def test_mg_account_and_battery(monkeypatch, email):
    saic = pytest.importorskip('saic_ismart_client_ng')
    from saic_ismart_client_ng.api.vehicle.schema import VehicleListResp, VinInfo
    from saic_ismart_client_ng.api.vehicle_charging.schema import ChrgMgmtDataResp, ChrgMgmtData
    username = 'test@example.invalid' if email else '000000000'
    secrets = {'username': username, 'password': 'synthetic'}
    api = AsyncMock()
    api.vehicle_list.return_value = VehicleListResp(vinList=[VinInfo(vin='test-car')])
    api.get_vehicle_charging_management_data.return_value = ChrgMgmtDataResp(chrgMgmtData=ChrgMgmtData(bmsPackSOCDsp=678, bmsChrgSts=1))
    factory = Mock(return_value=api)
    monkeypatch.setattr(saic, 'SaicApi', factory)
    plugin = MGiSmart(SimpleNamespace(secret=secrets.get, record_raw=Mock()), MGConfig(), None)
    reading = asyncio.run(plugin.read())
    assert reading.soc == 67.8 and reading.charging and not reading.time_verified
    config = factory.call_args.args[0]
    assert config.username_is_email == email and config.phone_country_code == (None if email else '61')
    api.get_vehicle_status_cached.assert_not_called()


def test_mg_remote_stop_requires_opt_in():
    plugin = MGiSmart(None, MGConfig(), None)
    plugin.api = AsyncMock()
    plugin.vin = 'example'

    async def run():
        with pytest.raises(PluginError):
            await plugin.stop_charging()
        plugin.api.control_charging.assert_not_awaited()
        plugin.config.allow_charge_stop = True
        await plugin.stop_charging()
        plugin.api.control_charging.assert_awaited_once_with('example', stop_charging=True)

    asyncio.run(run())


@pytest.mark.parametrize('stage', ['sign-in', 'vehicle discovery', 'battery read'])
def test_mg_failures_identify_stage_without_credentials(monkeypatch, stage):
    saic = pytest.importorskip('saic_ismart_client_ng')
    from saic_ismart_client_ng.exceptions import SaicApiException
    api = AsyncMock()
    api.vehicle_list.return_value = SimpleNamespace(vinList=[SimpleNamespace(vin='private')])
    methods = {'sign-in': api.login, 'vehicle discovery': api.vehicle_list, 'battery read': api.get_vehicle_charging_management_data}
    methods[stage].side_effect = SaicApiException('personal information', 6)
    monkeypatch.setattr(saic, 'SaicApi', Mock(return_value=api))
    plugin = MGiSmart(SimpleNamespace(secret={'username': 'a@example.invalid', 'password': 'private'}.get), MGConfig(), None)
    with pytest.raises(PluginError) as failure:
        asyncio.run(plugin.read())
    assert stage in str(failure.value) and 'service code 6' in str(failure.value)
    assert 'personal information' not in str(failure.value)
    api.close.assert_awaited_once()


@pytest.mark.parametrize('reserve_available', [True, False])
def test_fox_discovery_only_prefills_reported_ratings(reserve_available):
    plugin = FoxESSCloud(None, FoxConfig(serial='example'), None)
    plugin.read = AsyncMock(return_value=SystemReading(ts=utcnow(), time_verified=True, soc=80))
    async def request(path, payload):
        if path == '/op/v1/device/detail':
            return {'result': {'capacity': 5}}
        if path == '/op/v0/device/battery/soc/get' and reserve_available:
            return {'result': {'minSocOnGrid': 18}}
        raise PluginError('unsupported')
    plugin._request = AsyncMock(side_effect=request)
    result = asyncio.run(plugin.test())
    assert result['reading']['soc'] == 80
    assert result['hardware'] == ({'ac_kw': 5, 'reserve_pct': 18, 'daily_schedule_supported': False} if reserve_available else {'ac_kw': 5, 'daily_schedule_supported': False})
    assert result['warnings']


def test_fox_discovery_separates_dc_battery_capacity_from_scheduler_ac_power():
    plugin = FoxESSCloud(None, FoxConfig(serial='example'), SimpleNamespace(ac_kw=1))
    plugin.read = AsyncMock(return_value=SystemReading(ts=utcnow(), time_verified=True, soc=40))
    async def request(path, payload):
        if path == '/op/v1/device/detail':
            return {'result': {'capacity': 9.9, 'batteryDesignCapacity': 41.93}}
        if path == '/op/v0/device/battery/soc/get':
            return {'result': {'minSoc': 10}}
        return {'result': {'maxGroupCount': 96, 'properties': {'fdpwr': {'unit': 'W', 'range': {'max': 10500}}}}}
    plugin._request = AsyncMock(side_effect=request)
    result = asyncio.run(plugin.test())
    assert result['hardware']['capacity_kwh'] == 41.93
    assert result['hardware']['scheduler_limit_kw'] == 10.5
    assert result['hardware']['min_soc_pct'] == 10
    assert result['hardware']['daily_schedule_supported']
    assert 'charge_kw' not in result['hardware']
    assert 'reserve_pct' not in result['hardware']
    assert result['connection']['serial'] == 'example'
    assert sum(call.args[0] == '/op/v1/device/detail' for call in plugin._request.call_args_list) == 1


def test_fox_solar_only_can_connect_without_battery_data():
    plugin = FoxESSCloud(SimpleNamespace(timezone='UTC'), FoxConfig(), SimpleNamespace(battery=None))
    assert plugin._parse([{'variable': 'pvPower', 'value': 2.5}], None).pv_kw == 2.5
    with pytest.raises(PluginError):
        plugin._parse([], None)


def test_foxess_maps_cumulative_energy_without_altering_instantaneous_power():
    plugin = FoxESSCloud(SimpleNamespace(timezone='UTC'), FoxConfig(), SimpleNamespace(battery=None))
    parsed = plugin._parse([{'variable': 'loadsPower', 'value': 0.75},
                            {'variable': 'loads', 'value': 5234.1},
                            {'variable': 'gridConsumption', 'value': 400},
                            {'variable': 'feedin', 'value': 'unavailable'}], '2026-11-19 00:05:00 UTC+0000')
    assert parsed.load_kw == 0.75
    assert parsed.extra['energy_counters_kwh'] == {'load_kwh': 5234.1, 'import_kwh': 400}
