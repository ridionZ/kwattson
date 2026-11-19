"""MG / SAIC iSMART via the community saic-ismart-client-ng library.

Unofficial API: it may break without notice and frequent polling can trigger
account lockouts, so it polls at most every 30 minutes (10 while charging) and
backs off for 15 minutes after any error. Only energy-relevant fields are kept;
no GPS, journeys or VIN are stored. Remote charge stop is opt-in.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field

from .base import PluginConfig, PluginError, SecretField, VehiclePlugin, VehicleReading


class MGConfig(PluginConfig):
    allow_charge_stop: bool = Field(False, title='Allow kWattson to stop charging',
                                    description='Allow an opted-in charging session to send one stop request at its time or battery limit.')
    allow_charge_schedule: bool = Field(False, title='Allow kWattson to schedule charging',
                                        description='Allow start and stop requests for an opted-in home charging schedule.')
    region: Literal['au', 'eu'] = 'au'
    phone_country_code: str = Field('61', pattern=r'^\d{0,4}$', title='Country calling code (phone sign-in only)',
                                    description='Used only for phone sign-in. Enter the calling code for your phone number.')


class MGiSmart(VehiclePlugin):
    name = 'mg_ismart'
    supports_charge_stop = True
    supports_charge_schedule = True
    title = 'MG iSMART (unofficial)'
    description = 'Reads battery level and charging state from MG iSMART, with optional remote charge stop.'
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
        self.last: VehicleReading | None = None

    async def read(self) -> VehicleReading | None:
        async with self.lock:
            return await self._read()

    async def _read(self) -> VehicleReading | None:
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
                factory = SaicApiConfiguration.mg_au if self.config.region == 'au' else SaicApiConfiguration.mg_eu
                self.api = SaicApi(factory(username, password, username_is_email=email,
                                           phone_country_code=None if email else self.config.phone_country_code))
                await asyncio.wait_for(self.api.login(), timeout=15)
                if not vin:
                    stage = 'vehicle discovery'
                    vehicles = await asyncio.wait_for(self.api.vehicle_list(), timeout=15)
                    if not vehicles.vinList:
                        raise PluginError('MG vehicle discovery returned no cars. Check the region and linked vehicles in iSMART.')
                    if len(vehicles.vinList) != 1:
                        raise PluginError('Several vehicles on this account: enter the VIN')
                    info = vehicles.vinList[0]
                    vin = getattr(getattr(info, 'vinInfo', info), 'vin', None)
                if not vin:
                    raise PluginError('iSMART did not return a vehicle')
                self.vin = vin
            stage = 'battery read'
            charge = await asyncio.wait_for(self.api.get_vehicle_charging_management_data(self.vin), timeout=20)
            management = charge.chrgMgmtData
            raw_soc = getattr(management, 'bmsPackSOCDsp', None)
            if raw_soc is None or not 0 <= raw_soc <= 1000:
                raise PluginError('iSMART battery level missing or invalid')
            charging = management.is_bms_charging if management.bmsChrgSts is not None else None
            status = management.bms_charging_status
            plugged_in = None
            if status and status.name == 'UNPLUGGED':
                plugged_in = False
            elif charging or status and status.name in (
                    'CHARGE_DONE', 'CONNECTED_NOT_CHARGING', 'CHARGING_STOPPED', 'SCHEDULED_CHARGING'):
                plugged_in = True
            # The management reply has no device timestamp. Preserve receipt time as unverified.
            ts = datetime.now(timezone.utc)
            reading = VehicleReading(ts=ts, time_verified=False, soc=raw_soc / 10, charging=charging, plugged_in=plugged_in)
            self.context.record_raw({'soc': reading.soc, 'charging': charging, 'ts': ts.isoformat()})
            self.last = reading
            self.next_poll = time.time() + (600 if charging else 1800)
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

    async def start_charging(self) -> None:
        await self._request_charge(stop=False)

    async def stop_charging(self) -> None:
        await self._request_charge(stop=True)

    async def _request_charge(self, *, stop: bool) -> None:
        async with self.lock:
            permitted = self.config.allow_charge_schedule or (stop and self.config.allow_charge_stop)
            if not permitted or not self.api or not self.vin:
                raise PluginError('MG charging control requires opt-in and a live iSMART connection')
            try:
                await asyncio.wait_for(self.api.control_charging(self.vin, stop_charging=stop), timeout=20)
                self.next_poll = min(self.next_poll, time.time() + 600)
            except Exception:
                raise PluginError('MG did not confirm receipt of the charging request') from None

    async def close(self) -> None:
        if self.api is not None:
            try:
                await self.api.close()
            finally:
                self.api = None
