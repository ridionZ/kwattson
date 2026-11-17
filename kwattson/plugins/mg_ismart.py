"""MG / SAIC iSMART (read-only) via the community saic-ismart-client-ng library.

Unofficial API: it may break without notice and frequent polling can trigger
account lockouts, so it polls at most every 30 minutes (10 while charging) and
backs off for 15 minutes after any error. Only energy-relevant fields are kept;
no GPS, journeys or VIN are stored.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone

from pydantic import Field

from .base import PluginConfig, PluginError, SecretField, VehiclePlugin, VehicleReading


class MGConfig(PluginConfig):
    region: str = Field('au', pattern='^(au|eu)$')
    phone_country_code: str = Field('61', pattern=r'^\d{1,4}$')


class MGiSmart(VehiclePlugin):
    name = 'mg_ismart'
    title = 'MG iSMART (unofficial)'
    description = 'Reads battery level and charging state from the MG iSMART cloud. Read-only.'
    Config = MGConfig
    secrets = (SecretField('username', 'iSMART email or phone'),
               SecretField('password', 'iSMART password'),
               SecretField('vin', 'VIN (only if the account has several cars)', required=False))

    def __init__(self, *args):
        super().__init__(*args)
        self.api = None
        self.signature = None
        self.vin = None
        self.retry_after = 0.0
        self.next_poll = 0.0
        self.last: VehicleReading | None = None

    async def read(self) -> VehicleReading | None:
        if time.time() < self.next_poll:
            return self.last
        if time.time() < self.retry_after:
            raise PluginError('iSMART is cooling down after an error')
        username, password = self.context.secret('username'), self.context.secret('password')
        if not username or not password:
            raise PluginError('MG iSMART credentials are not configured')
        try:
            from saic_ismart_client_ng import SaicApi
            from saic_ismart_client_ng.model import SaicApiConfiguration
        except ImportError:
            raise PluginError('saic-ismart-client-ng is not installed') from None
        signature = hashlib.sha256(f'{username}\0{password}\0{self.config.region}'.encode()).hexdigest()
        try:
            if self.api is None or self.signature != signature:
                await self.close()
                factory = SaicApiConfiguration.mg_au if self.config.region == 'au' else SaicApiConfiguration.mg_eu
                self.api = SaicApi(factory(username, password, username_is_email='@' in username,
                                           phone_country_code=self.config.phone_country_code))
                await self.api.login()
                self.signature = signature
                vin = self.context.secret('vin')
                if not vin:
                    vehicles = await self.api.vehicle_list()
                    if len(vehicles.vinList) != 1:
                        raise PluginError('Several vehicles on this account: enter the VIN')
                    info = vehicles.vinList[0]
                    vin = getattr(getattr(info, 'vinInfo', info), 'vin', None)
                if not vin:
                    raise PluginError('iSMART did not return a vehicle')
                self.vin = vin
            status = await self.api.get_vehicle_status_cached(self.vin)
            charge = await self.api.get_vehicle_charging_management_data(self.vin)
            management = charge.chrgMgmtData
            raw_soc = getattr(management, 'bmsPackSOCDsp', None)
            if raw_soc is None:
                raw_soc = getattr(charge.rvsChargeStatus, 'bmsPackSOCDsp', None)
            if raw_soc is None or not 0 <= raw_soc <= 1000:
                raise PluginError('iSMART battery level missing or invalid')
            charging = bool(getattr(management, 'is_bms_charging', False))
            when = status.statusTime
            ts = datetime.fromtimestamp(when, timezone.utc) if isinstance(when, (int, float)) else datetime.now(timezone.utc)
            reading = VehicleReading(ts=ts, time_verified=isinstance(when, (int, float)), soc=raw_soc / 10, charging=charging)
            self.context.record_raw({'soc': reading.soc, 'charging': charging, 'ts': ts.isoformat()})
            self.last = reading
            self.next_poll = time.time() + (600 if charging else 1800)
            return reading
        except Exception:
            self.retry_after = time.time() + 900
            await self.close()
            raise

    async def close(self) -> None:
        if self.api is not None:
            try:
                await self.api.close()
            finally:
                self.api = None
