"""Owner-editable settings and process runtime configuration.

The site is described as data so the core never needs vendor-specific code:

- ``systems``: one entry per inverter (hybrid, battery-only/AC-coupled or
  PV-only). Each names the *plugin* that reads/controls it and its physical
  limits. Mixed vendors are fine.
- ``vehicles``: one entry per EV, with an optional data plugin.
- ``tariffs``: price plans as data (see tariffs.py / tariffs/*.json).

Settings are stored as one validated JSON document. Every number is finite and
range-checked; plugin-specific config is validated by the plugin itself
(see plugins.registry.validate_settings).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .tariffs import TariffPlan, default_tariffs

UTC = timezone.utc
ID = r'^[a-z0-9][a-z0-9_-]{0,39}$'


def utcnow() -> datetime:
    return datetime.now(UTC)


def stamp(value: datetime | None = None) -> str:
    """Canonical UTC ISO-8601 text used for every stored timestamp."""
    return (value or utcnow()).astimezone(UTC).isoformat(timespec='seconds')


def parse(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Timestamp needs a timezone offset')
    return value


def _env_timezone() -> str:
    """Default site time zone: the container's TZ if it is a valid zone, else UTC."""
    name = os.getenv('TZ', '')
    try:
        ZoneInfo(name)
        return name
    except Exception:
        return 'UTC'


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, validate_assignment=True)


class BatterySpec(Strict):
    capacity_kwh: float = Field(gt=0.5, le=500)
    charge_kw: float = Field(gt=0, le=100, description='DC charge limit (PV + grid)')
    discharge_kw: float = Field(gt=0, le=100)
    round_trip_efficiency: float = Field(0.90, ge=0.6, le=0.99)
    reserve_pct: float = Field(15, ge=0, le=90, description='Never plan below this state of charge')
    max_soc_pct: float = Field(100, ge=30, le=100)
    wear_cents_per_kwh: float = Field(3.0, ge=0, le=50, description='Economic allowance per kWh discharged')

    @model_validator(mode='after')
    def order(self):
        if self.reserve_pct >= self.max_soc_pct:
            raise ValueError('Battery reserve must be below its maximum state of charge')
        return self


class EnergySystem(Strict):
    """An inverter and whatever is connected to it.

    coupling='dc': PV and battery share a DC bus (hybrid inverter). PV can charge
    the battery even when the AC output is at ``ac_kw``, which is why DC PV above
    ``ac_kw`` is only lost once the battery is full.
    coupling='ac': battery inverter with no PV (or PV-only inverter if battery is None).
    """
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=60)
    plugin: str = Field(pattern=r'^[a-z0-9_.-]{1,60}$')
    plugin_config: dict = Field(default_factory=dict)
    enabled: bool = True
    coupling: str = Field('dc', pattern='^(dc|ac)$')
    ac_kw: float = Field(gt=0, le=100, description='Inverter AC output limit')
    pv_kwp: float = Field(0, ge=0, le=200, description='Nameplate DC array size on this inverter')
    pv_dc_max_kw: float | None = Field(None, gt=0, le=200, description='Highest DC PV power the inverter accepts')
    battery: BatterySpec | None = None
    controllable: bool = Field(False, description='Owner allows the planner to send commands to this system')
    site_meter: bool = Field(False, description='This system reports whole-site grid import/export and load')

    @property
    def pv_limit_kw(self) -> float:
        return self.pv_dc_max_kw or self.pv_kwp * 1.1


class VehicleSchedule(Strict):
    days: list[int] = Field(default_factory=list, description='Usual charging days, 0=Monday')
    start_hour: int = Field(18, ge=0, le=23)

    @field_validator('days')
    @classmethod
    def weekdays(cls, value):
        if any(d not in range(7) for d in value):
            raise ValueError('Weekdays must be 0 (Monday) to 6 (Sunday)')
        return sorted(set(value))


class Vehicle(Strict):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=60)
    plugin: str = Field('manual', pattern=r'^[a-z0-9_.-]{1,60}$')
    plugin_config: dict = Field(default_factory=dict)
    enabled: bool = True
    capacity_kwh: float = Field(gt=5, le=250)
    capacity_confirmed: bool = False
    target_pct: float = Field(80, ge=20, le=100)
    manual_soc: float | None = Field(None, ge=0, le=100)
    charger_kw: float = Field(2.2, ge=0.5, le=22)
    charger_efficiency: float = Field(0.88, ge=0.6, le=0.99)
    km_per_week: float = Field(300, ge=0, le=5000)
    kwh_per_100km: float = Field(14, ge=5, le=60)
    schedule: VehicleSchedule = Field(default_factory=VehicleSchedule)
    reminder_soc_pct: float = Field(50, ge=0, le=100)


class SiteSettings(Strict):
    name: str = Field('Home', max_length=60)
    timezone: str = Field(default_factory=lambda: _env_timezone())
    latitude: float | None = Field(None, ge=-90, le=90, description='Needed for the weather and solar forecast')
    longitude: float | None = Field(None, ge=-180, le=180)
    export_kw: float = Field(10.0, ge=0, le=100, description='Approved export limit at the meter')
    export_limit_confirmed: bool = False
    grid_import_kw: float = Field(13.0, gt=0, le=200, description='Safe import limit for the connection')
    ac_kw: float = Field(2.5, gt=0, le=30, description='Typical air-conditioner electrical draw')
    poll_seconds: int = Field(120, ge=60, le=3600)
    weather_enabled: bool = True


class ForecastSettings(Strict):
    risk: str = Field('balanced', pattern='^(conservative|balanced|optimistic)$')
    history_days: int = Field(84, ge=14, le=730)
    half_life_days: float = Field(28, ge=3, le=365)
    horizon_hours: int = Field(48, ge=12, le=96)
    holiday_base_load_kw: float = Field(0.35, ge=0, le=10, description='Fridge, standby etc. when away')
    cooling_base_c: float = Field(24, ge=15, le=35)
    heating_base_c: float = Field(17, ge=0, le=25)
    terminal_value_factor: float = Field(0.8, ge=0, le=1.2,
        description='Value of energy left in the battery at the horizon end, as a fraction of the average import price')


class AlertSettings(Strict):
    high_price_cents: float = Field(35, ge=0, le=500)
    good_day_aud: float = Field(0, ge=-100, le=100, description='A day at or below this net cost is a good day')
    bad_day_aud: float = Field(8, ge=-100, le=200)
    low_battery_pct: float = Field(20, ge=0, le=100)
    ac_hot_c: float = Field(29, ge=15, le=50)
    ac_cold_c: float = Field(14, ge=-20, le=25)
    ac_max_cents: float = Field(6, ge=0, le=100, description='Max marginal cost to suggest pre-cooling/heating')
    ac_duration_minutes: int = Field(90, ge=15, le=240)
    clipping_alert_kwh: float = Field(1.0, ge=0, le=100, description='Tell me when more than this would be clipped today')
    quiet_start_hour: int = Field(22, ge=0, le=23)
    quiet_end_hour: int = Field(6, ge=0, le=23)
    daily_summary_hour: int = Field(21, ge=0, le=23)
    forecast_drift_pct: float = Field(35, ge=5, le=500)


def default_systems() -> list[EnergySystem]:
    """New installs start empty; the dashboard's setup wizard adds systems."""
    return []


def default_vehicles() -> list[Vehicle]:
    return []


class Settings(Strict):
    mode: str = Field('monitor', pattern='^(monitor|automatic)$')
    site: SiteSettings = Field(default_factory=SiteSettings)
    systems: list[EnergySystem] = Field(default_factory=default_systems, max_length=20)
    vehicles: list[Vehicle] = Field(default_factory=default_vehicles, max_length=10)
    forecast: ForecastSettings = Field(default_factory=ForecastSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    tariffs: list[TariffPlan] = Field(default_factory=default_tariffs, min_length=1, max_length=30)
    raw_retention_days: int = Field(180, ge=7, le=3650)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.site.timezone)

    def active_systems(self) -> list[EnergySystem]:
        return [s for s in self.systems if s.enabled]

    def active_vehicles(self) -> list[Vehicle]:
        return [v for v in self.vehicles if v.enabled]

    def battery_kwh(self) -> float:
        return sum(s.battery.capacity_kwh for s in self.active_systems() if s.battery)

    @model_validator(mode='after')
    def check(self):
        try:
            ZoneInfo(self.site.timezone)
        except (KeyError, ValueError):
            raise ValueError('Enter a valid time zone such as Europe/London or UTC') from None
        for label, items in (('Tariff', self.tariffs), ('System', self.systems), ('Vehicle', self.vehicles)):
            ids = [i.id for i in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f'{label} ids must be unique')
        if not any(t.status == 'active' and t.active_from is None for t in self.tariffs):
            raise ValueError('One active tariff must have no start date (the base plan)')
        if sum(1 for s in self.systems if s.site_meter and s.enabled) > 1:
            raise ValueError('Only one system can be the site meter')
        return self


class Runtime:
    """Process configuration from environment variables (never stored in the DB)."""

    def __init__(self, data_dir=None, app_url=None, dev_http=None, worker=None):
        self.data_dir = Path(data_dir or os.getenv('DATA_DIR', './data')).resolve()
        self.dev_http = dev_http if dev_http is not None else os.getenv('DEV_HTTP', 'false').lower() == 'true'
        self.app_url = (app_url or os.getenv('APP_URL', 'https://solar.example.com')).rstrip('/')
        parts = urlsplit(self.app_url)
        if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('', '/'):
            raise ValueError('APP_URL must be an origin such as https://solar.example.com')
        loopback = parts.hostname in ('localhost', '127.0.0.1', '::1')
        if parts.scheme != 'https' and not (self.dev_http and loopback and parts.scheme == 'http'):
            raise ValueError('APP_URL must use HTTPS (plain HTTP is allowed only on loopback with DEV_HTTP=true)')
        self.host = parts.netloc
        self.cookie = 'kwattson_dev_session' if self.dev_http else '__Host-kwattson_session'
        self.worker = worker if worker is not None else os.getenv('WORKER_ENABLED', 'true').lower() == 'true'
        self.hsts_seconds = int(os.getenv('HSTS_SECONDS', '300'))
        self.demo = os.getenv('DEMO_MODE', 'false').lower() == 'true'
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
