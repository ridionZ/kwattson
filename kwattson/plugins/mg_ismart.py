"""MG / SAIC iSMART via the community saic-ismart-client-ng library.

Unofficial API: it may break without notice and frequent polling can trigger
account lockouts, so it polls every five minutes while idle, two while connected,
and briefly refreshes after a control request. It
backs off for 15 minutes after any error. Only energy-relevant fields are kept;
no GPS, journeys or VIN are stored. Remote charge stop is opt-in.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field

from .base import PluginConfig, PluginError, SecretField, VehicleClimate, VehiclePlugin, VehicleReading

# The vendor library logs raw response bodies on errors. Keep those out of app
# logs; this plugin reports only the stage, exception type and service code.
logging.getLogger('saic_ismart_client_ng').addHandler(logging.NullHandler())
logging.getLogger('saic_ismart_client_ng').propagate = False

IDLE_POLL_SECONDS = 300
CONNECTED_POLL_SECONDS = 120
COMMAND_POLL_SECONDS = 30
REQUEST_TIMEOUT = 45


class MGConfig(PluginConfig):
    allow_home_probe: bool = Field(False, title='Allow one home charging test after reconnecting',
                                  description='When an active plan reconnects without home evidence, permit one brief stop/restart to compare house power. It can interrupt an away charging session; requires start/stop permission. Never repeats during continuous connection.')
    home_radius_m: int = Field(200, ge=50, le=500, title='Home location radius (m)',
                               description='Fresh GPS inside this radius confirms home. Missing or distant GPS never rules home out; vehicle coordinates are not retained.')
    allow_climate_control: bool = Field(False, title='Allow parked climate control',
                                       description='Allow manual climate start/stop while parked. Starting needs a fresh parked reading. Timed stopping requires this controller online.')
    single_phase_ac: bool = Field(False, title='Read single-phase AC charger power',
                                  description='Enable for a single-phase home or portable AC charger. Leave off for three-phase or DC charging; its power remains estimated.')
    allow_charge_stop: bool = Field(False, title='Allow kWattson to stop charging',
                                    description='Allow an opted-in charging session to stop at its time or battery limit, with confirmation and bounded retries.')
    allow_charge_schedule: bool = Field(False, title='Allow kWattson to schedule charging',
                                        description='Allow start and stop requests for an opted-in home charging schedule.')
    region: Literal['au', 'eu'] = 'au'
    phone_country_code: str = Field('61', pattern=r'^\d{0,4}$', title='Country calling code (phone sign-in only)',
                                    description='Used only for phone sign-in. Enter the calling code for your phone number.')


class MGiSmart(VehiclePlugin):
    name = 'mg_ismart'
    supports_charge_stop = True
    supports_charge_schedule = True
    supports_climate_control = True
    title = 'MG iSMART (unofficial)'
    description = 'Reads battery level and charging state from MG iSMART, with optional remote start and stop.'
    Config = MGConfig
    secrets = (SecretField('username', 'iSMART email or phone'),
               SecretField('password', 'iSMART password'),
               SecretField('vin', 'VIN (only if the account has several cars)', required=False))

    def __init__(self, *args):
        super().__init__(*args)
        self.lock = asyncio.Lock()
        self.last_failure = ''
        self.api = None
        self.signature = None
        self.vin = None
        self.retry_after = 0.0
        self.next_poll = 0.0
        self.fast_poll_until = 0.0
        self.last: VehicleReading | None = None
        self.last_climate: VehicleClimate | None = None
        self.home_checked_after = 0.0
        self.home_seen_at = None

    async def read(self) -> VehicleReading | None:
        async with self.lock:
            return await self._read()

    @property
    def poll_seconds(self):
        return max(15, min(300, max(self.next_poll, self.retry_after) - time.time()))

    async def refresh(self) -> VehicleReading | None:
        async with self.lock:
            if self.last and time.time() - self.last.ts.timestamp() < COMMAND_POLL_SECONDS:
                return self.last
            self.next_poll = 0
            return await self._read()

    async def _credentials(self):
        username, password = self.context.secret('username'), self.context.secret('password')
        if not username or not password:
            raise PluginError('MG iSMART credentials are not configured')
        vin = self.context.secret('vin')
        identity = (username, password, self.config.region, self.config.phone_country_code, vin or '')
        signature = hashlib.sha256('\0'.join(identity).encode()).hexdigest()
        if self.signature != signature:
            await self.close()
            self.signature = signature
            self.next_poll = self.retry_after = 0
            self.last = None
            self.last_climate = None
            self.home_seen_at = None
            self.home_checked_after = 0
        return username, password, vin

    async def _read(self) -> VehicleReading | None:
        username, password, vin = await self._credentials()
        if time.time() < self.retry_after:
            raise PluginError(self.last_failure + ' Automatic retry after the 15-minute cooldown.')
        if time.time() < self.next_poll:
            return self.last
        try:
            from saic_ismart_client_ng import SaicApi
            from saic_ismart_client_ng.model import SaicApiConfiguration
        except ImportError:
            raise PluginError('saic-ismart-client-ng is not installed') from None
        email = '@' in username
        if not email and not self.config.phone_country_code:
            raise PluginError('Phone sign-in needs a country calling code. Email sign-in only needs the email and password.')
        stage = 'sign-in'
        try:
            if self.api is None:
                await self._connect(username, password, vin)
            stage = 'battery read'
            charge = await self._vehicle_call('get_vehicle_charging_management_data')
            management = charge.chrgMgmtData
            raw_soc = getattr(management, 'bmsPackSOCDsp', None)
            if raw_soc is None or not 0 <= raw_soc <= 1000:
                raise PluginError('iSMART battery level missing or invalid')
            status = management.bms_charging_status
            charging = management.is_bms_charging if status is not None else None
            plugged_in = None
            if status and status.name == 'UNPLUGGED':
                plugged_in = False
            elif charging or status and status.name in (
                    'CHARGE_DONE', 'CONNECTED_NOT_CHARGING', 'CHARGING_STOPPED', 'SCHEDULED_CHARGING'):
                plugged_in = True
            # Use the connector flags when supplied, including a plugged car
            # waiting for a remote start. A stopped car is still connected.
            plugs = [getattr(management, field, None) for field in ('ccuOnbdChrgrPlugOn', 'ccuOffBdChrgrPlugOn')]
            # MGS5 reports 3 while both charging and remotely stopped; 1 is
            # reported by other MG models. Neither means the cable was removed.
            if any(plug in (1, 3) for plug in plugs):
                plugged_in = True
            charge_kw = None
            current = getattr(management, 'onBdChrgrAltrCrntInptCrnt', None)
            voltage = getattr(management, 'onBdChrgrAltrCrntInptVol', None)
            if (self.config.single_phase_ac and plugs[0] in (1, 3) and plugs[1] in (0, None)
                    and current is not None and voltage is not None and 0 <= current < 255 and 0 <= voltage < 255):
                # OBC AC input: 0.2 A and 2 V units (upstream SAIC gateway).
                if charging is False and current == 0:
                    charge_kw = 0.0
                elif charging is True and 90 <= voltage <= 150 and current > 0:
                    charge_kw = round(current / 5 * voltage * 2 / 1000, 3)
            # The management reply has no device timestamp. Preserve receipt time as unverified.
            ts = datetime.now(timezone.utc)
            at_home, home_seen_at = await self._home_presence()
            history = getattr(charge, 'rvsChargeStatus', None)
            session_start = getattr(history, 'startTime', None)
            battery_kw = None
            if charging and getattr(management, 'bmsPackCrntV', None) == 0:
                dc = getattr(management, 'decoded_power', None)
                if isinstance(dc, (int, float)) and -1000 <= dc < 0:
                    battery_kw = round(-dc, 3)
            reading = VehicleReading(ts=ts, time_verified=False, soc=raw_soc / 10, charging=charging,
                                     plugged_in=plugged_in, charge_kw=charge_kw, at_home=at_home,
                                     home_source='gps' if at_home else None, home_seen_at=home_seen_at,
                                     charge_session=str(session_start) if isinstance(session_start, (int, float)) and session_start > 0 else None,
                                     battery_charge_kw=battery_kw)
            self.context.record_raw({'soc': reading.soc, 'charging': charging, 'plugged_in': plugged_in,
                                     'charge_kw': charge_kw, 'ts': ts.isoformat()})
            self.last = reading
            interval = COMMAND_POLL_SECONDS if time.time() < self.fast_poll_until else CONNECTED_POLL_SECONDS if plugged_in or charging else IDLE_POLL_SECONDS
            self.next_poll = time.time() + interval
            return reading
        except Exception as error:
            self.retry_after = time.time() + 900
            await self.close()
            if isinstance(error, PluginError):
                self.last_failure = str(error)
            else:
                code = getattr(error, 'return_code', None)
                reason = 'timed out' if isinstance(error, TimeoutError) else 'failed'
                detail = f' (service code {int(code)})' if isinstance(code, int) else ''
                self.last_failure = f'MG {stage} {reason}{detail}. Check the region and iSMART app before retrying.'
            raise PluginError(self.last_failure) from None

    async def _home_presence(self):
        home = getattr(self.context, 'home_coordinates', lambda: None)()
        if not home:
            return None, None
        if time.time() >= self.home_checked_after:
            self.home_checked_after = time.time() + 300
            self.home_seen_at = None
            try:
                response = await self._vehicle_call('get_vehicle_status_cached')
                gps = response.gpsPosition
                point = gps.wayPoint.position if gps and gps.wayPoint else None
                if gps and gps.gpsStatus in (2, 3) and point and gps.timeStamp:
                    observed = datetime.fromtimestamp(gps.timeStamp, timezone.utc)
                    if 0 <= time.time() - observed.timestamp() <= 600 and point.latitude is not None and point.longitude is not None:
                        lat, lon = point.latitude / 1_000_000, point.longitude / 1_000_000
                        if -90 <= lat <= 90 and -180 <= lon <= 180:
                            a, b = math.radians(home[0]), math.radians(lat)
                            dlat, dlon = b - a, math.radians(lon - home[1])
                            distance = 6_371_000 * 2 * math.asin(min(1, math.sqrt(math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2)))
                            if distance <= self.config.home_radius_m:
                                self.home_seen_at = observed
                # Coordinates, GPS fixes and journeys are discarded immediately.
            except Exception:
                pass  # Optional location failure must not break energy monitoring.
        return (True, self.home_seen_at) if self.home_seen_at and time.time() - self.home_seen_at.timestamp() <= 600 else (None, None)

    async def _connect(self, username, password, vin):
        from saic_ismart_client_ng import SaicApi
        from saic_ismart_client_ng.model import SaicApiConfiguration
        email = '@' in username
        if not email and not self.config.phone_country_code:
            raise PluginError('Phone sign-in needs a country calling code')
        factory = SaicApiConfiguration.mg_au if self.config.region == 'au' else SaicApiConfiguration.mg_eu
        self.api = SaicApi(factory(username, password, username_is_email=email,
                                   phone_country_code=None if email else self.config.phone_country_code, read_timeout=15))
        try:
            await asyncio.wait_for(self.api.login(), timeout=REQUEST_TIMEOUT)
            if not vin:
                try:
                    vehicles = await asyncio.wait_for(self.api.vehicle_list(), timeout=REQUEST_TIMEOUT)
                except Exception as error:
                    code = getattr(error, 'return_code', None)
                    detail = f' (service code {int(code)})' if isinstance(code, int) else ''
                    raise PluginError('MG vehicle discovery failed' + detail) from None
                if not vehicles.vinList:
                    raise PluginError('MG vehicle discovery returned no cars. Check the region and linked vehicles in iSMART.')
                if len(vehicles.vinList) != 1:
                    raise PluginError('Several vehicles on this account: enter the VIN')
                info = vehicles.vinList[0]
                vin = getattr(getattr(info, 'vinInfo', info), 'vin', None)
            if not vin:
                raise PluginError('iSMART did not return a vehicle')
            self.vin = vin
        except Exception:
            await self.close()
            raise

    async def start_charging(self) -> None:
        await self._request_charge(stop=False)

    async def stop_charging(self) -> None:
        await self._request_charge(stop=True)

    async def _ready(self):
        username, password, vin = await self._credentials()
        if self.api is None:
            await self._connect(username, password, vin)

    async def read_climate(self) -> VehicleClimate:
        async with self.lock:
            await self._ready()
            return await self._read_climate()

    async def _read_climate(self, *, force=False):
        if not force and self.last_climate and time.time() - self.last_climate.ts.timestamp() < 30:
            return self.last_climate
        try:
            response = await self._vehicle_call('get_vehicle_status')
            basic = response.basicVehicleStatus
            if basic is None:
                raise PluginError('MG did not provide parked climate status')
            parked = True if basic.handBrake == 1 or basic.engineStatus == 0 else False if basic.handBrake == 0 and basic.engineStatus == 1 else None
            mode = {0: 'off', 1: 'fan', 2: 'on', 5: 'defrost'}.get(basic.remoteClimateStatus)
            received = datetime.now(timezone.utc)
            reported = getattr(response, 'statusTime', None)
            ts = datetime.fromtimestamp(reported / 1000 if reported > 1e12 else reported, timezone.utc) if isinstance(reported, (int, float)) and reported > 0 else received
            temperature = lambda value: float(value) if isinstance(value, (int, float)) and -50 <= value <= 80 else None
            state = VehicleClimate(ts=ts, time_verified=ts != received, parked=parked, mode=mode,
                                   interior_c=temperature(basic.interiorTemperature), exterior_c=temperature(basic.exteriorTemperature))
            self.last_climate = state
            # No GPS, lock, journey or VIN data is retained from the status reply.
            self.context.record_raw({'climate': {'ts': ts.isoformat(), 'parked': parked, 'mode': mode,
                                                'interior_c': state.interior_c, 'exterior_c': state.exterior_c}})
            return state
        except PluginError:
            raise
        except Exception as error:
            code = getattr(error, 'return_code', None)
            detail = f' (service code {code})' if isinstance(code, int) else ''
            raise PluginError('MG parked climate status is unavailable' + detail) from None

    async def set_climate(self, enabled: bool, temperature_c: int = 22) -> None:
        async with self.lock:
            if not self.config.allow_climate_control:
                raise PluginError('Enable parked climate control in the vehicle connection first')
            await self._ready()
            state = await self._read_climate(force=True)
            age = time.time() - state.ts.timestamp()
            if state.parked is not True or not state.time_verified or not 0 <= age <= 120:
                raise PluginError('Climate control needs a fresh device-verified reading showing the car parked')
            if enabled:
                if not 17 <= temperature_c <= 28:
                    raise PluginError('Choose a parked climate temperature between 17 and 28 C')
            try:
                if enabled:
                    # Both common MG temperature tables map 22 C to index 8.
                    await self._vehicle_call('start_ac', temperature_idx=temperature_c - 14)
                else:
                    await self._vehicle_call('stop_ac')
                self.last_climate = None
                self.next_poll = min(self.next_poll, time.time() + 30)
            except Exception as error:
                code = getattr(error, 'return_code', None)
                detail = f' (service code {code})' if isinstance(code, int) else ''
                raise PluginError('MG climate request receipt is uncertain' + detail) from None

    async def _vehicle_call(self, method, **kwargs):
        try:
            return await asyncio.wait_for(getattr(self.api, method)(self.vin, **kwargs), timeout=REQUEST_TIMEOUT)
        except Exception as error:
            if not getattr(error, 'is_logged_out', False):
                raise
            # The phone app can replace our login. An explicit logout means the
            # rejected operation was not accepted, so one reconnect is safe.
            await self.close()
            username, password, vin = await self._credentials()
            await self._connect(username, password, vin)
            return await asyncio.wait_for(getattr(self.api, method)(self.vin, **kwargs), timeout=REQUEST_TIMEOUT)

    async def _request_charge(self, *, stop: bool) -> None:
        async with self.lock:
            permitted = self.config.allow_charge_schedule or (stop and self.config.allow_charge_stop)
            if not permitted:
                raise PluginError('MG charging control requires opt-in and a live iSMART connection')
            try:
                if self.context is not None:
                    username, password, vin = await self._credentials()
                if not self.api or not self.vin:
                    await self._connect(username, password, vin)
                await self._vehicle_call('control_charging', stop_charging=stop)
                self.retry_after = 0
                self.fast_poll_until = time.time() + 300
                self.next_poll = time.time() + COMMAND_POLL_SECONDS
            except Exception:
                self.fast_poll_until = time.time() + 300
                self.next_poll = time.time() + COMMAND_POLL_SECONDS
                await self.close()
                raise PluginError('MG did not confirm receipt of the charging request') from None

    async def close(self) -> None:
        if self.api is not None:
            try:
                await self.api.close()
            finally:
                self.api = None
