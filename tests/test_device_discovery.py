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
    plugin._request = AsyncMock(side_effect=[{'result': {'capacity': 5}}, {'result': {'minSocOnGrid': 18}} if reserve_available else PluginError('unsupported')])
    result = asyncio.run(plugin.test())
    assert result['reading']['soc'] == 80
    assert result['hardware'] == ({'ac_kw': 5, 'reserve_pct': 18} if reserve_available else {'ac_kw': 5})


def test_fox_solar_only_can_connect_without_battery_data():
    plugin = FoxESSCloud(SimpleNamespace(timezone='UTC'), FoxConfig(), SimpleNamespace(battery=None))
    assert plugin._parse([{'variable': 'pvPower', 'value': 2.5}], None).pv_kw == 2.5
    with pytest.raises(PluginError):
        plugin._parse([], None)
