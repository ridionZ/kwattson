"""Systems and vehicles that *push* data to the controller.

Any device, script, Node-RED flow or Home Assistant automation can POST
normalised readings to ``/api/ingest/system/<id>`` or ``/api/ingest/vehicle/<id>``
with an ingest-scoped API token. This is the zero-code way to support new
hardware. The plugin just returns the most recent pushed payload.

Payload (system), all optional except ts and soc: see docs/PLUGINS.md
    {"ts": "2026-11-17T01:30:00Z", "soc": 64, "pv_kw": 8.2, "load_kw": 1.1,
     "import_kw": 0, "export_kw": 0.4, "charge_kw": 6.7, "discharge_kw": 0}
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import Field

from .base import PluginConfig, PluginError, SystemPlugin, SystemReading, VehiclePlugin, VehicleReading
from ..validation import as_bool, as_number

SYSTEM_FIELDS = ('soc', 'pv_kw', 'charge_kw', 'discharge_kw', 'ac_output_kw', 'import_kw', 'export_kw', 'load_kw', 'battery_temp')


class PushConfig(PluginConfig):
    max_age_seconds: int = Field(900, ge=60, le=7200)


def _ts(payload) -> datetime:
    text = payload.get('ts')
    if not isinstance(text, str):
        raise PluginError('Pushed reading needs a "ts" timestamp')
    value = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise PluginError('Pushed timestamp needs a timezone offset')
    if value > datetime.now(timezone.utc) + timedelta(minutes=2):
        raise PluginError('Pushed timestamp is in the future')
    return value


def system_reading(payload: dict) -> SystemReading:
    values = {}
    for key in SYSTEM_FIELDS:
        if payload.get(key) is not None:
            low, high = (0, 100) if key == 'soc' else (-60, 150) if key == 'battery_temp' else (0, 500)
            values[key] = as_number(payload[key], key, low, high)
    unknown = set(payload) - set(SYSTEM_FIELDS) - {'ts', 'running_state', 'fault'}
    if unknown:
        raise PluginError(f'Unknown field(s): {", ".join(sorted(unknown))[:200]}')
    return SystemReading(ts=_ts(payload), time_verified=True, running_state=str(payload['running_state'])[:40] if payload.get('running_state') else None,
                         fault=as_bool(payload['fault'], 'fault') if 'fault' in payload else None, **values)


def vehicle_reading(payload: dict) -> VehicleReading:
    unknown = set(payload) - {'ts', 'soc', 'charging', 'plugged_in', 'at_home', 'charge_kw', 'range_km'}
    if unknown:
        raise PluginError(f'Unknown field(s): {", ".join(sorted(unknown))[:200]}')
    return VehicleReading(
        ts=_ts(payload), time_verified=True,
        soc=as_number(payload['soc'], 'soc', 0, 100) if payload.get('soc') is not None else None,
        charging=as_bool(payload['charging'], 'charging') if 'charging' in payload else None,
        plugged_in=as_bool(payload['plugged_in'], 'plugged_in') if 'plugged_in' in payload else None,
        at_home=as_bool(payload['at_home'], 'at_home') if 'at_home' in payload else None,
        charge_kw=as_number(payload['charge_kw'], 'charge_kw', 0, 400) if payload.get('charge_kw') is not None else None,
        range_km=as_number(payload['range_km'], 'range_km', 0, 3000) if payload.get('range_km') is not None else None)


class PushedSystem(SystemPlugin):
    name = 'push'
    title = 'Pushed readings (any device)'
    description = 'Your device or automation POSTs readings to /api/ingest/system/<system id> using an ingest token.'
    Config = PushConfig

    async def read(self) -> SystemReading:
        payload = self.context.pushed()
        if not payload:
            raise PluginError('No reading has been pushed yet')
        reading = system_reading(payload)
        if datetime.now(timezone.utc) - reading.ts > timedelta(seconds=self.config.max_age_seconds):
            raise PluginError('Latest pushed reading is too old')
        return reading


class PushedVehicle(VehiclePlugin):
    name = 'push_vehicle'
    title = 'Pushed vehicle readings'
    description = 'POST vehicle state to /api/ingest/vehicle/<vehicle id> using an ingest token.'
    Config = PushConfig

    async def read(self) -> VehicleReading | None:
        payload = self.context.pushed()
        return vehicle_reading(payload) if payload else None
