"""FoxESS Cloud Open API (hybrid inverters such as H1/H3/KH series).

Reads real-time data every poll. Daily control uses the v3 scheduler and only
replaces the day's schedule when an adopted plan materially changes it. The
v2 short-command path remains available for older firmware.

Control caveats (also shown in the UI):
- Scheduler groups repeat daily until replaced. If the controller stops running,
  the stored daily schedule can repeat. `release()` disables the scheduler.
- ForceCharge power may be limited by the inverter's own configuration.
- Scheduler power limits describe AC power and can be lower than the battery's
  DC charging limit. Read the inverter's advertised limit before each command.
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
READ = {'/op/v0/device/list', '/op/v1/device/real/query', '/op/v0/device/history/query', '/op/v1/device/detail', '/op/v0/device/battery/soc/get',
        '/op/v2/device/scheduler/get', '/op/v3/device/scheduler/get', '/op/v1/device/scheduler/get/flag'}
WRITE = {'/op/v2/device/scheduler/enable', '/op/v3/device/scheduler/enable', '/op/v1/device/scheduler/set/flag'}
COUNTERS = {'loads': 'load_kwh', 'gridConsumption': 'import_kwh', 'feedin': 'export_kwh', 'PVEnergyTotal': 'solar_kwh'}
VARIABLES = ['pvPower', 'loadsPower', 'feedinPower', 'gridConsumptionPower', 'batChargePower', 'batDischargePower',
             'generationPower', 'SoC', 'batTemperature', 'runningState', *COUNTERS]
FAULT_STATES = {'165', '166'}
SCHEDULER_READ_ATTEMPTS = 5
SCHEDULER_READ_PAUSE = 2
SCHEDULER_READ_TIMEOUT = 15
SCHEDULER_REQUEST_TIMEOUT = 3
MODES = {'self_use': 'SelfUse', 'hold': 'SelfUse', 'export_priority': 'Feedin',
         'force_charge': 'ForceCharge', 'force_discharge': 'ForceDischarge'}


class FoxConfig(PluginConfig):
    serial: str = Field('', max_length=40, pattern=r'^[A-Za-z0-9]*$', description='Leave blank to auto-detect a single inverter')
    region: str = Field('global', pattern='^global$')
    daily_schedule: bool = Field(True, description='Confirm one schedule for each day; replace it only when the adopted plan changes significantly')


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


def energy_counters(values: dict) -> dict:
    counters = {}
    for variable, metric in COUNTERS.items():
        try:
            value = _number(values.get(variable))
        except (TypeError, ValueError):
            continue
        if value is not None and value >= 0:
            counters[metric] = value
    return {'energy_counters_kwh': counters} if counters else {}


class FoxESSCloud(SystemPlugin):
    name = 'foxess'
    title = 'FoxESS Cloud'
    description = 'FoxESS hybrid inverters via the FoxESS Cloud Open API (API key from foxesscloud.com → User Profile → API Management).'
    Config = FoxConfig
    secrets = (SecretField('api_key', 'API key', 'Generated in FoxESS Cloud under User Profile → API Management'),)
    can_control = True
    supported_modes = tuple(MODES)
    control_notes = ('Confirms one schedule for the rest of each day and changes it when the adopted plan changes materially. '
                     'Groups repeat daily if the controller is offline; stop and safety guards disable the scheduler while online. '
                     'Scheduler power is capped at the reported AC limit; battery charge power is a separate DC limit.')

    _lock = asyncio.Lock()   # FoxESS allows ~1 request/second per key
    _last = 0.0

    def __init__(self, *args):
        super().__init__(*args)
        self._serial_key = None
        self._serial = None

    @property
    def supports_battery_schedule(self):
        return self.config.daily_schedule

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
            metadata = path in ('/op/v1/device/detail', '/op/v0/device/battery/soc/get')
            async with httpx.AsyncClient(timeout=8 if metadata else 30, follow_redirects=False, trust_env=False) as client:
                url = HOSTS[self.config.region] + path
                if metadata:
                    response = await client.get(url, params=payload, headers=headers)
                else:
                    response = await client.post(url, json=payload or {}, headers=headers)
            response.raise_for_status()
            data = response.json()
        if data.get('errno') != 0:
            raise PluginError(f'FoxESS rejected the request (code {data.get("errno")})')
        return data

    async def serial(self) -> str:
        if self.config.serial:
            return self.config.serial
        key = self.context.secret('api_key')
        if self._serial and self._serial_key == key:
            return self._serial
        data = await self._request('/op/v0/device/list', {'currentPage': 1, 'pageSize': 25})
        devices = data['result']['data']
        if not devices:
            raise PluginError('FoxESS returned no inverters for this account')
        if len(devices) != 1:
            raise PluginError('Several inverters on this account: enter the serial in the system settings')
        self._serial_key, self._serial = key, devices[0]['deviceSN']
        return self._serial

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
            fault=state in FAULT_STATES if state else None, extra=energy_counters(values))
        if reading.soc is None and self.system.battery is not None:
            raise PluginError('FoxESS did not report a battery state of charge')
        if reading.soc is None and all(value is None for value in (reading.pv_kw, reading.ac_output_kw, reading.load_kw)):
            raise PluginError('FoxESS did not return power or battery readings')
        if reading.soc is not None and not 0 <= reading.soc <= 100:
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
        hardware = {}
        warnings = []
        metadata = {}
        for path, source, target, minimum, maximum in (
            ('/op/v1/device/detail', 'capacity', 'ac_kw', 0.001, 100),
            ('/op/v1/device/detail', 'batteryDesignCapacity', 'capacity_kwh', 0.5, 500),
            ('/op/v0/device/battery/soc/get', 'minSocOnGrid', 'reserve_pct', 0, 90),
            ('/op/v0/device/battery/soc/get', 'minSoc', 'min_soc_pct', 0, 90),
        ):
            try:
                if path not in metadata:
                    metadata[path] = await self._request(path, {'sn': sn})
                detail = metadata[path]
                value = _number((detail.get('result') or {}).get(source))
                if value is not None and minimum <= value <= maximum:
                    hardware[target] = value
            except (httpx.HTTPError, PluginError, TypeError, ValueError, AttributeError):
                continue  # Optional on older inverters.
        try:
            schedule = await self._request('/op/v3/device/scheduler/get', {'deviceSN': sn})
            result = schedule.get('result') or {}
            properties = result.get('properties') or {}
            if any(k.lower() == 'fdpwr' for k in properties):
                hardware['scheduler_limit_kw'] = _scheduler_power_limit(result, hardware.get('ac_kw', getattr(self.system, 'ac_kw', 1))) / 1000
            hardware['schedule_groups'] = result.get('maxGroupCount')
            hardware['work_modes'] = (result.get('properties', {}).get('workmode') or {}).get('enumList', [])
            hardware['daily_schedule_supported'] = bool(result.get('maxGroupCount'))
        except (httpx.HTTPError, PluginError, TypeError, ValueError, AttributeError):
            hardware['daily_schedule_supported'] = False
            warnings.append('Daily scheduling could not be confirmed. Disable Daily schedule to use short commands instead.')
        return {'reading': reading.as_dict(), 'hardware': hardware, 'connection': {'serial': sn}, 'warnings': warnings}

    def _group(self, command: BatteryCommand, power_limit_w: int | None = None) -> dict:
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
        if power_limit_w is not None:
            extra['fdPwr'] = min(extra['fdPwr'], power_limit_w)
        return {'enable': 1, 'startHour': start.hour, 'startMinute': start.minute, 'endHour': end.hour,
                'endMinute': end.minute, 'workMode': MODES[command.mode], 'extraParam': extra}

    async def apply(self, command: BatteryCommand) -> CommandResult:
        if (command.end - command.start) > timedelta(minutes=30):
            raise PluginError('Commands are limited to 30 minutes')
        sn = await self.serial()
        # FoxESS may acknowledge an out-of-range write while leaving the old
        # schedule in place. BatterySpec.charge_kw is a DC limit, not the AC
        # scheduler limit (e.g. 13 kW DC versus a reported 10,500 W fdPwr).
        current = await self._request('/op/v2/device/scheduler/get', {'deviceSN': sn})
        power_limit_w = _scheduler_power_limit(current.get('result'), self.system.ac_kw)
        group = self._group(command, power_limit_w)
        _validate_group(group, (current.get('result') or {}).get('properties') or {})
        await self._request('/op/v2/device/scheduler/enable', {'deviceSN': sn, 'groups': [group]}, write=True)
        await self._request('/op/v1/device/scheduler/set/flag', {'deviceSN': sn, 'enable': 1}, write=True)
        checks = []
        verified = False
        remaining = (command.end - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            try:
                # The cloud can acknowledge a write before the new group appears on read-back.
                # Wait briefly for confirmation without resending either write.
                async with asyncio.timeout(min(remaining, SCHEDULER_READ_TIMEOUT)):
                    for attempt in range(SCHEDULER_READ_ATTEMPTS):
                        if attempt:
                            await asyncio.sleep(SCHEDULER_READ_PAUSE)
                        try:
                            async with asyncio.timeout(SCHEDULER_REQUEST_TIMEOUT):
                                back = await self._request('/op/v2/device/scheduler/get', {'deviceSN': sn})
                        except (TimeoutError, httpx.TransportError) as error:
                            checks.append({'error': type(error).__name__})
                            continue
                        except httpx.HTTPStatusError as error:
                            status = error.response.status_code
                            if status not in (408, 429) and not 500 <= status < 600:
                                raise
                            checks.append({'http_status': status})
                            continue
                        result = back.get('result')
                        result = result if isinstance(result, dict) else {}
                        groups = result.get('groups')
                        groups = groups if isinstance(groups, list) else []
                        enabled = _same_number(result.get('enable'), 1)
                        matched = any(_same_group(group, g) for g in groups)
                        checks.append({'enabled': enabled, 'groups': len(groups), 'matched': matched})
                        if enabled and matched:
                            verified = True
                            break
            except TimeoutError:
                pass
        last_count = next((check['groups'] for check in reversed(checks) if 'groups' in check), 0)
        return CommandResult(accepted=True, verified=verified, detail={
            'group': group, 'scheduler_power_limit_w': power_limit_w,
            'read_back_groups': last_count, 'read_back_checks': checks,
        })

    async def release(self) -> CommandResult:
        sn = await self.serial()
        await self._request('/op/v1/device/scheduler/set/flag', {'deviceSN': sn, 'enable': 0}, write=True)
        attempts = 0
        disabled = False
        try:
            # The disable flag can also lag behind its acknowledged write.
            async with asyncio.timeout(20):
                for attempt in range(5):
                    if attempt:
                        await asyncio.sleep(SCHEDULER_READ_PAUSE)
                    attempts += 1
                    try:
                        async with asyncio.timeout(4):
                            flag = await self._request('/op/v1/device/scheduler/get/flag', {'deviceSN': sn})
                    except (TimeoutError, httpx.TransportError):
                        continue
                    if _same_number((flag.get('result') or {}).get('enable'), 0):
                        disabled = True
                        break
        except TimeoutError:
            pass
        return CommandResult(accepted=True, verified=disabled,
                             detail={'scheduler_enabled': not disabled, 'read_back_attempts': attempts})

    def _schedule_groups(self, commands, metadata):
        if not commands:
            raise PluginError('A daily schedule must contain at least one time group')
        limit = _scheduler_power_limit(metadata, self.system.ac_kw)
        maximum = metadata.get('maxGroupCount')
        if not isinstance(maximum, (int, float)) or len(commands) > maximum:
            raise PluginError('FoxESS cannot hold this daily schedule; use short commands instead')
        zone = ZoneInfo(self.context.timezone)
        day = commands[0].start.astimezone(zone).date()
        previous = None
        groups = []
        for command in commands:
            if command.end <= command.start or command.start.astimezone(zone).date() != day:
                raise PluginError('FoxESS daily schedule must stay within one local day')
            local_end = command.end.astimezone(zone)
            if local_end.date() != day and not (local_end.date() == day + timedelta(days=1) and
                                                local_end.hour == 0 and local_end.minute == 0):
                raise PluginError('FoxESS daily schedule must stay within one local day')
            if previous and command.start < previous:
                raise PluginError('FoxESS daily schedule groups overlap')
            group = self._group(command, limit)
            group.pop('enable')  # v3 schedules have no per-group enable field.
            _validate_group(group, metadata.get('properties') or {})
            groups.append(group)
            previous = command.end
        return groups, limit

    async def apply_schedule(self, commands: list[BatteryCommand]) -> CommandResult:
        sn = await self.serial()
        back = await self._request('/op/v3/device/scheduler/get', {'deviceSN': sn})
        metadata = back.get('result') or {}
        groups, limit = self._schedule_groups(commands, metadata)
        if _same_schedule(groups, metadata, datetime.now(ZoneInfo(self.context.timezone))):
            return CommandResult(True, True, {'groups': groups, 'scheduler_power_limit_w': limit, 'reused': True})
        await self._request('/op/v3/device/scheduler/enable', {'deviceSN': sn, 'groups': groups}, write=True)
        await self._request('/op/v1/device/scheduler/set/flag', {'deviceSN': sn, 'enable': 1}, write=True)
        checks = []
        verified = False
        try:
            async with asyncio.timeout(45):
                for attempt in range(5):
                    if attempt:
                        await asyncio.sleep(2)
                    try:
                        async with asyncio.timeout(12):
                            back = await self._request('/op/v3/device/scheduler/get', {'deviceSN': sn})
                        result = back.get('result') or {}
                        verified = _same_schedule(groups, result, datetime.now(ZoneInfo(self.context.timezone)))
                        checks.append({'enabled': _same_number(result.get('enable'), 1), 'matched': verified})
                        if verified:
                            break
                    except (TimeoutError, httpx.TransportError):
                        checks.append({'error': 'ReadTimeout'})
        except TimeoutError:
            pass
        return CommandResult(True, verified, {'groups': groups, 'scheduler_power_limit_w': limit, 'read_back_checks': checks})

    async def verify_schedule(self, commands: list[BatteryCommand]) -> CommandResult:
        back = await self._request('/op/v3/device/scheduler/get', {'deviceSN': await self.serial()})
        result = back.get('result') or {}
        groups, limit = self._schedule_groups(commands, result)
        return CommandResult(True, _same_schedule(groups, result, datetime.now(ZoneInfo(self.context.timezone))), {'scheduler_power_limit_w': limit})


def _scheduler_power_limit(result: dict | None, ac_kw: float) -> int:
    """Use advertised watts, or the configured AC rating on older firmware."""
    properties = result.get('properties') if isinstance(result, dict) else None
    properties = properties if isinstance(properties, dict) else {}
    for name, prop in properties.items():
        if name.lower() != 'fdpwr':
            continue
        try:
            maximum = _number(prop['range']['max'])
            unit = prop.get('unit', 'W')
            if maximum is not None and 1 <= maximum <= 100_000 and unit == 'W':
                return math.floor(maximum)
        except (TypeError, ValueError, KeyError, OverflowError):
            pass
        raise PluginError('FoxESS returned an invalid scheduler power limit')
    return math.floor(ac_kw * 1000)


def _same_number(value, expected: int | float) -> bool:
    try:
        return float(value) == expected
    except (TypeError, ValueError, OverflowError):
        return False


def _same_group(sent: dict, got: dict) -> bool:
    if not isinstance(got, dict) or got.get('workMode') != sent['workMode']:
        return False
    if 'enable' in got and not _same_number(got['enable'], 1):
        return False
    keys = ('startHour', 'startMinute', 'endHour', 'endMinute') + (('enable',) if 'enable' in sent else ())
    if any(not _same_number(got.get(k), sent[k]) for k in keys):
        return False
    extra = got.get('extraParam')
    if not isinstance(extra, dict):
        return False
    required = {'minSocOnGrid'}
    if sent['workMode'] == 'ForceDischarge':
        required.update(('fdSoc', 'fdPwr'))
    elif sent['workMode'] == 'ForceCharge':
        required.update(('maxSoc', 'fdPwr'))
    return all(_same_number(extra.get(k), sent['extraParam'][k]) for k in required | (extra.keys() & sent['extraParam'].keys()))


def _same_schedule(sent, result, local_now=None):
    if not isinstance(result, dict) or not _same_number(result.get('enable'), 1):
        return False
    groups = result.get('groups')
    if not isinstance(groups, list):
        return False
    if local_now is not None:
        minute = local_now.hour * 60 + local_now.minute
        def future(items):
            out = []
            for item in items:
                if not isinstance(item, dict):
                    return []
                try:
                    start = int(item['startHour']) * 60 + int(item['startMinute'])
                    end = int(item['endHour']) * 60 + int(item['endMinute'])
                except (KeyError, ValueError, TypeError):
                    return []
                if end <= minute:
                    continue
                start = max(start, minute)
                out.append({**item, 'startHour': start // 60, 'startMinute': start % 60})
            return out
        sent, groups = future(sent), future(groups)
    if not sent or len(groups) != len(sent):
        return False
    remaining = list(groups)
    for group in sent:
        match = next((i for i, got in enumerate(remaining) if _same_group(group, got)), None)
        if match is None:
            return False
        remaining.pop(match)
    return True


def _validate_group(group, properties):
    if group['endHour'] * 60 + group['endMinute'] <= group['startHour'] * 60 + group['startMinute']:
        raise PluginError('FoxESS time groups must have a positive minute duration')
    normalized = {k.lower(): v for k, v in properties.items()}
    modes = (normalized.get('workmode') or {}).get('enumList')
    if modes and group['workMode'] not in modes:
        raise PluginError('FoxESS does not support the requested work mode')
    for field, value in group['extraParam'].items():
        bounds = (normalized.get(field.lower()) or {}).get('range') or {}
        low, high = _number(bounds.get('min')), _number(bounds.get('max'))
        if (low is not None and value < low) or (high is not None and value > high):
            raise PluginError(f'FoxESS {field} is outside the inverter-supported range')


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
            load_kw=_number(values.get('loadsPower')), ac_output_kw=_number(values.get('generationPower')), extra=energy_counters(values)))
    return readings
