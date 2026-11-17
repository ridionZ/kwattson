"""FoxESS Cloud Open API (hybrid inverters such as H1/H3/KH series).

Reads real-time data every poll. Control uses the v2 scheduler: one short time
group per command, replaced on every cycle and disabled on release.

Control caveats (also shown in the UI):
- Scheduler groups repeat daily until replaced. If the controller stops running
  while a group is enabled, that group recurs at the same time each day. Groups
  are kept short (<=30 min) and `release()` disables the scheduler.
- ForceCharge power may be limited by the inverter's own configuration.
- Write support has NOT been verified on hardware by this project yet.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field

from .base import BatteryCommand, CommandResult, PluginConfig, PluginError, SecretField, SystemPlugin, SystemReading

HOSTS = {'global': 'https://www.foxesscloud.com'}
READ = {'/op/v0/device/list', '/op/v1/device/real/query', '/op/v0/device/history/query', '/op/v1/device/detail',
        '/op/v2/device/scheduler/get', '/op/v1/device/scheduler/get/flag'}
WRITE = {'/op/v2/device/scheduler/enable', '/op/v1/device/scheduler/set/flag'}
VARIABLES = ['pvPower', 'loadsPower', 'feedinPower', 'gridConsumptionPower', 'batChargePower', 'batDischargePower',
             'generationPower', 'SoC', 'batTemperature', 'runningState']
FAULT_STATES = {'165', '166'}
MODES = {'self_use': 'SelfUse', 'hold': 'SelfUse', 'export_priority': 'Feedin',
         'force_charge': 'ForceCharge', 'force_discharge': 'ForceDischarge'}


class FoxConfig(PluginConfig):
    serial: str = Field('', max_length=40, pattern=r'^[A-Za-z0-9]*$', description='Leave blank to auto-detect a single inverter')
    region: str = Field('global', pattern='^global$')


def measurement_time(value, zone: str) -> datetime:
    """FoxESS returns e.g. '2026-11-15 11:09:14 AEST+1000' or epoch ms."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / (1000 if value > 1e11 else 1), timezone.utc)
    if isinstance(value, str):
        match = re.match(r'(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)(?:\s*[A-Z]*)?([+-]\d{2}:?\d{2})?', value)
        if not match:
            raise PluginError('Unrecognised FoxESS timestamp')
        naive = datetime.fromisoformat(match[1].replace(' ', 'T'))
        if match[2]:
            offset = match[2].replace(':', '')
            delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
            return naive.replace(tzinfo=timezone(delta if offset[0] == '+' else -delta))
        return naive.replace(tzinfo=ZoneInfo(zone))
    raise PluginError('FoxESS reading has no timestamp')


def _number(value):
    if value is None or value == '':
        return None
    result = float(value)
    return result if math.isfinite(result) else None


class FoxESSCloud(SystemPlugin):
    name = 'foxess'
    title = 'FoxESS Cloud'
    description = 'FoxESS hybrid inverters via the FoxESS Cloud Open API (API key from foxesscloud.com → User Profile → API Management).'
    Config = FoxConfig
    secrets = (SecretField('api_key', 'API key', 'Generated in FoxESS Cloud under User Profile → API Management'),)
    can_control = True
    supported_modes = tuple(MODES)
    control_notes = ('Uses FoxESS cloud scheduler time groups. Groups repeat daily until replaced; the controller replaces '
                     'them every cycle and disables the scheduler when control stops. Not yet verified on hardware.')

    _lock = asyncio.Lock()   # FoxESS allows ~1 request/second per key
    _last = 0.0

    async def _request(self, path, payload=None, write=False):
        if path not in (WRITE if write else READ):
            raise PluginError('FoxESS endpoint not allowed')
        key = self.context.secret('api_key')
        if not key:
            raise PluginError('FoxESS API key is not configured')
        async with FoxESSCloud._lock:
            await asyncio.sleep(max(0.0, 1.1 - (time.monotonic() - FoxESSCloud._last)))
            FoxESSCloud._last = time.monotonic()
            timestamp = str(round(time.time() * 1000))
            # FoxESS signs with literal backslash-r-backslash-n separators.
            signature = hashlib.md5((path + r'\r\n' + key + r'\r\n' + timestamp).encode()).hexdigest()
            headers = {'token': key, 'signature': signature, 'timestamp': timestamp, 'lang': 'en',
                       'User-Agent': 'kWattson', 'Content-Type': 'application/json'}
            async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
                response = await client.post(HOSTS[self.config.region] + path, json=payload or {}, headers=headers)
            response.raise_for_status()
            data = response.json()
        if data.get('errno') != 0:
            raise PluginError(f'FoxESS rejected the request (code {data.get("errno")})')
        return data

    async def serial(self) -> str:
        if self.config.serial:
            return self.config.serial
        known = self.context.recall('serial')
        if known:
            return known
        data = await self._request('/op/v0/device/list', {'currentPage': 1, 'pageSize': 25})
        devices = data['result']['data']
        if len(devices) != 1:
            raise PluginError('Several inverters on this account: enter the serial in the system settings')
        self.context.remember('serial', devices[0]['deviceSN'])
        return devices[0]['deviceSN']

    def _parse(self, fields: list[dict], measured) -> SystemReading:
        values = {f.get('variable'): f.get('value') for f in fields}
        times = [f.get('time') for f in fields if f.get('time')]
        stamp = measured or (times[0] if times else None)
        state = values.get('runningState')
        state = str(int(float(state))) if state not in (None, '') else None
        reading = SystemReading(
            ts=measurement_time(stamp, self.context.timezone) if stamp else datetime.now(timezone.utc),
            time_verified=bool(stamp),
            soc=_number(values.get('SoC')), pv_kw=_number(values.get('pvPower')),
            charge_kw=_number(values.get('batChargePower')), discharge_kw=_number(values.get('batDischargePower')),
            ac_output_kw=_number(values.get('generationPower')), import_kw=_number(values.get('gridConsumptionPower')),
            export_kw=_number(values.get('feedinPower')), load_kw=_number(values.get('loadsPower')),
            battery_temp=_number(values.get('batTemperature')), running_state=state,
            fault=state in FAULT_STATES if state else None)
        if reading.soc is None or not 0 <= reading.soc <= 100:
            raise PluginError('FoxESS returned an invalid battery state of charge')
        return reading

    async def read(self) -> SystemReading:
        payload = await self._request('/op/v1/device/real/query', {'sns': [await self.serial()], 'variables': VARIABLES})
        self.context.record_raw(payload)
        device = payload['result'][0] if isinstance(payload['result'], list) else payload['result']
        return self._parse(device.get('datas', []), device.get('time'))

    async def history(self, start: datetime, end: datetime) -> list[SystemReading]:
        """5-minute history, at most 24 h per request."""
        out = []
        cursor = start
        while cursor < end:
            stop = min(end, cursor + timedelta(hours=24))
            payload = await self._request('/op/v0/device/history/query', {
                'sn': await self.serial(), 'variables': VARIABLES,
                'begin': int(cursor.timestamp() * 1000), 'end': int(stop.timestamp() * 1000)})
            out.extend(parse_history(payload, self.context.timezone))
            cursor = stop
        return out

    async def test(self) -> dict:
        reading = await self.read()
        sn = await self.serial()
        groups = await self._request('/op/v2/device/scheduler/get', {'deviceSN': sn})
        flag = await self._request('/op/v1/device/scheduler/get/flag', {'deviceSN': sn})
        return {'reading': reading.as_dict(), 'scheduler_supported': flag.get('result', {}).get('support'),
                'scheduler_enabled': flag.get('result', {}).get('enable'),
                'scheduler_groups': len((groups.get('result') or {}).get('groups') or []),
                'note': 'Read-only test. No settings were changed.'}

    def _group(self, command: BatteryCommand) -> dict:
        zone = ZoneInfo(self.context.timezone)
        start, end = command.start.astimezone(zone), command.end.astimezone(zone)
        if start.date() != end.date() and not (end.hour == 0 and end.minute == 0):
            raise PluginError('A FoxESS time group cannot cross midnight')
        if end.hour == 0 and end.minute == 0:
            end = end - timedelta(minutes=1)
        reserve = math.ceil(self.system.battery.reserve_pct)
        extra = {'minSocOnGrid': reserve, 'fdSoc': reserve, 'fdPwr': 0, 'maxSoc': math.floor(self.system.battery.max_soc_pct)}
        if command.mode == 'hold':
            extra['minSocOnGrid'] = max(reserve, min(100, math.ceil(command.min_soc or reserve)))
        elif command.mode == 'force_charge':
            extra['maxSoc'] = min(math.floor(self.system.battery.max_soc_pct), math.ceil(command.target_soc or 100))
            extra['fdPwr'] = round(min(command.power_kw, self.system.battery.charge_kw) * 1000)
        elif command.mode == 'force_discharge':
            extra['fdSoc'] = max(reserve, math.ceil(command.target_soc or reserve))
            extra['fdPwr'] = round(min(command.power_kw, self.system.battery.discharge_kw) * 1000)
        return {'enable': 1, 'startHour': start.hour, 'startMinute': start.minute, 'endHour': end.hour,
                'endMinute': end.minute, 'workMode': MODES[command.mode], 'extraParam': extra}

    async def apply(self, command: BatteryCommand) -> CommandResult:
        if (command.end - command.start) > timedelta(minutes=30):
            raise PluginError('Commands are limited to 30 minutes')
        sn = await self.serial()
        group = self._group(command)
        await self._request('/op/v2/device/scheduler/enable', {'deviceSN': sn, 'groups': [group]}, write=True)
        await self._request('/op/v1/device/scheduler/set/flag', {'deviceSN': sn, 'enable': 1}, write=True)
        back = await self._request('/op/v2/device/scheduler/get', {'deviceSN': sn})
        groups = (back.get('result') or {}).get('groups') or []
        verified = any(_same_group(group, g) for g in groups)
        return CommandResult(accepted=True, verified=verified, detail={'group': group, 'read_back_groups': len(groups)})

    async def release(self) -> CommandResult:
        sn = await self.serial()
        await self._request('/op/v1/device/scheduler/set/flag', {'deviceSN': sn, 'enable': 0}, write=True)
        flag = await self._request('/op/v1/device/scheduler/get/flag', {'deviceSN': sn})
        disabled = (flag.get('result') or {}).get('enable') in (0, False)
        return CommandResult(accepted=True, verified=disabled, detail={'scheduler_enabled': not disabled})


def _same_group(sent: dict, got: dict) -> bool:
    keys = ('startHour', 'startMinute', 'endHour', 'endMinute', 'workMode')
    if any(sent[k] != got.get(k) for k in keys) or int(got.get('enable', 0)) != 1:
        return False
    extra = got.get('extraParam') or {}
    return all(abs(float(extra.get(k, -1)) - float(v)) < 1 for k, v in sent['extraParam'].items() if k in extra)


def parse_history(payload: dict, zone: str) -> list[SystemReading]:
    """Convert a /op/v0/device/history/query response into readings (5-min samples)."""
    device = payload['result'][0] if isinstance(payload['result'], list) else payload['result']
    series = {s['variable']: s.get('data', []) for s in device.get('datas', [])}
    by_time: dict[str, dict] = {}
    for variable, points in series.items():
        for point in points:
            by_time.setdefault(point['time'], {})[variable] = point.get('value')
    readings = []
    for when, values in sorted(by_time.items()):
        soc = _number(values.get('SoC'))
        if soc is None:
            continue
        readings.append(SystemReading(
            ts=measurement_time(when, zone), time_verified=True, soc=soc, pv_kw=_number(values.get('pvPower')),
            charge_kw=_number(values.get('batChargePower')), discharge_kw=_number(values.get('batDischargePower')),
            import_kw=_number(values.get('gridConsumptionPower')), export_kw=_number(values.get('feedinPower')),
            load_kw=_number(values.get('loadsPower')), ac_output_kw=_number(values.get('generationPower'))))
    return readings
