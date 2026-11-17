"""Home Assistant as a data source for any inverter, battery or EV it already knows.

Map Home Assistant entity ids to the readings the controller needs. Power
entities may be W or kW (``unit_of_measurement`` is read from HA and converted).
For grid power you can map separate import/export sensors, or one signed sensor
(``grid_power``) where positive means importing (flip with ``grid_power_positive_is_export``).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
from pydantic import Field, field_validator

from .base import PluginConfig, PluginError, SecretField, SystemPlugin, SystemReading, VehiclePlugin, VehicleReading
from ..validation import as_bool, as_number

ENTITY = r'^[a-z_]+\.[a-z0-9_]+$'


def _entity(value):
    if value and not re.fullmatch(ENTITY, value):
        raise ValueError('Entity ids look like sensor.inverter_pv_power')
    return value


class HAConnection(PluginConfig):
    url: str = Field('', max_length=200, description='e.g. https://homeassistant.local:8123')


class HASystemConfig(HAConnection):
    soc: str = Field('', description='Battery state of charge (%)')
    pv_power: str = ''
    battery_charge_power: str = ''
    battery_discharge_power: str = ''
    battery_power: str = Field('', description='Signed battery power, positive = discharging (alternative to the two above)')
    grid_import_power: str = ''
    grid_export_power: str = ''
    grid_power: str = Field('', description='Signed grid power (alternative to import/export)')
    grid_power_positive_is_export: bool = False
    load_power: str = ''
    fault: str = Field('', description='Binary sensor that is on during a fault')

    @field_validator('*')
    @classmethod
    def entities(cls, value, info):
        if info.field_name in ('url', 'grid_power_positive_is_export'):
            return value
        return _entity(value)


class HAVehicleConfig(HAConnection):
    soc: str = ''
    charging: str = ''
    plugged_in: str = ''
    at_home: str = Field('', description='device_tracker or binary_sensor that is "home"/"on" when the car is home')
    charge_power: str = ''
    range: str = ''

    @field_validator('*')
    @classmethod
    def entities(cls, value, info):
        return value if info.field_name == 'url' else _entity(value)


class _Client:
    secrets = (SecretField('token', 'Long-lived access token', 'Home Assistant → your profile → Security → Long-lived access tokens'),)

    async def _state(self, client, entity):
        response = await client.get(self.config.url.rstrip('/') + '/api/states/' + entity)
        response.raise_for_status()
        data = response.json()
        state = data.get('state')
        if state in (None, 'unknown', 'unavailable', ''):
            return None, None, None
        return state, (data.get('attributes') or {}).get('unit_of_measurement'), data.get('last_updated')

    def _client(self):
        if not self.config.url:
            raise PluginError('Home Assistant URL is not configured')
        self.context.check_url(self.config.url)
        token = self.context.secret('token')
        if not token:
            raise PluginError('Home Assistant token is not configured')
        return httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False, headers={'Authorization': 'Bearer ' + token})

    @staticmethod
    def _kw(state, unit, name):
        value = as_number(state, name, -1e6, 1e6)
        unit = (unit or 'kW').strip()
        if unit == 'W':
            return value / 1000
        if unit == 'kW':
            return value
        if unit == 'MW':
            return value * 1000
        raise PluginError(f'{name}: unsupported unit {unit!r}; use W or kW')


class HomeAssistantSystem(_Client, SystemPlugin):
    name = 'homeassistant'
    title = 'Home Assistant entities'
    description = 'Read any inverter/battery that Home Assistant already integrates by mapping its sensors.'
    Config = HASystemConfig

    async def read(self) -> SystemReading:
        c = self.config
        values, newest = {}, None
        async with self._client() as client:
            for key in ('soc', 'pv_power', 'battery_charge_power', 'battery_discharge_power', 'battery_power',
                        'grid_import_power', 'grid_export_power', 'grid_power', 'load_power', 'fault'):
                entity = getattr(c, key)
                if not entity:
                    continue
                state, unit, updated = await self._state(client, entity)
                if state is None:
                    continue
                values[key] = (state, unit)
                if updated:
                    when = datetime.fromisoformat(updated.replace('Z', '+00:00'))
                    newest = max(newest, when) if newest else when
        if 'soc' not in values and self.system.battery:
            raise PluginError('Battery state of charge entity is missing or unavailable')
        kw = lambda k: self._kw(*values[k], k) if k in values else None
        charge, discharge = kw('battery_charge_power'), kw('battery_discharge_power')
        if 'battery_power' in values:
            signed = kw('battery_power')
            discharge, charge = max(0.0, signed), max(0.0, -signed)
        imp, exp = kw('grid_import_power'), kw('grid_export_power')
        if 'grid_power' in values:
            signed = kw('grid_power') * (-1 if c.grid_power_positive_is_export else 1)
            imp, exp = max(0.0, signed), max(0.0, -signed)
        return SystemReading(
            ts=datetime.now(timezone.utc), time_verified=False,
            soc=as_number(values['soc'][0], 'soc', 0, 100) if 'soc' in values else None,
            pv_kw=kw('pv_power'), charge_kw=charge, discharge_kw=discharge, import_kw=imp, export_kw=exp,
            load_kw=kw('load_power'), fault=as_bool(values['fault'][0], 'fault') if 'fault' in values else None,
            extra={'newest_entity_update': newest.isoformat() if newest else None})


class HomeAssistantVehicle(_Client, VehiclePlugin):
    name = 'homeassistant_vehicle'
    title = 'Home Assistant vehicle entities'
    description = 'Read EV state of charge / charging / location from Home Assistant.'
    Config = HAVehicleConfig

    async def read(self) -> VehicleReading | None:
        c = self.config
        out = {}
        async with self._client() as client:
            for key in ('soc', 'charging', 'plugged_in', 'at_home', 'charge_power', 'range'):
                entity = getattr(c, key)
                if entity:
                    state, unit, _ = await self._state(client, entity)
                    if state is not None:
                        out[key] = (state, unit)
        if not out:
            return None
        flag = lambda k: as_bool(out[k][0], k) if k in out else None
        return VehicleReading(
            ts=datetime.now(timezone.utc), time_verified=False,
            soc=as_number(out['soc'][0], 'soc', 0, 100) if 'soc' in out else None,
            charging=flag('charging'), plugged_in=flag('plugged_in'), at_home=flag('at_home'),
            charge_kw=self._kw(*out['charge_power'], 'charge_power') if 'charge_power' in out else None,
            range_km=as_number(out['range'][0], 'range', 0, 5000) if 'range' in out else None)
