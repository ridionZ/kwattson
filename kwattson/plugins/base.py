"""The contract every hardware plugin implements.

Core code only talks to devices through these classes and dataclasses, so a new
vendor never requires changes outside its own plugin module (or pip package).
See docs/PLUGINS.md for a walkthrough and plugins/simulator.py for a complete,
dependency-free example.

Units: kW for power (always positive; direction is in the field name), percent
for state of charge, timezone-aware datetimes for timestamps.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict


class PluginConfig(BaseModel):
    """Base class for a plugin's non-secret configuration (stored in settings)."""
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


@dataclass(frozen=True)
class SecretField:
    """A credential the owner enters in the UI; stored encrypted, never exported."""
    name: str
    label: str
    help: str = ''
    required: bool = True
    multiline: bool = False


@dataclass
class SystemReading:
    """One measurement from an inverter/battery system.

    Fill what the device reports; leave the rest None. ``import_kw``,
    ``export_kw`` and ``load_kw`` are whole-site values and only needed from the
    system marked as the site meter.
    """
    ts: datetime
    time_verified: bool                 # True if ts came from the device, not our clock
    soc: float | None = None            # battery state of charge, %
    pv_kw: float | None = None          # DC PV power produced
    charge_kw: float | None = None      # battery charging power
    discharge_kw: float | None = None   # battery discharging power
    ac_output_kw: float | None = None   # inverter AC output (if reported)
    import_kw: float | None = None      # site grid import
    export_kw: float | None = None      # site grid export
    load_kw: float | None = None        # site household consumption
    battery_temp: float | None = None
    running_state: str | None = None    # vendor state text, for display/audit
    fault: bool | None = None           # True if the device reports a fault
    extra: dict[str, Any] = field(default_factory=dict)  # small, non-sensitive vendor detail

    def as_dict(self):
        data = asdict(self)
        data['ts'] = self.ts.isoformat()
        return data


@dataclass
class VehicleReading:
    ts: datetime
    time_verified: bool
    soc: float | None = None
    charging: bool | None = None
    plugged_in: bool | None = None
    at_home: bool | None = None
    charge_kw: float | None = None
    range_km: float | None = None


BatteryMode = Literal['self_use', 'force_charge', 'force_discharge', 'hold', 'export_priority']


@dataclass
class BatteryCommand:
    """A vendor-neutral instruction for one system for a short window.

    - self_use: normal operation (PV → home → battery → grid).
    - force_charge: charge to ``target_soc`` at up to ``power_kw``, from grid if needed.
    - force_discharge: discharge at ``power_kw`` (exporting surplus) down to ``target_soc``.
    - hold: do not discharge the battery; PV may still charge it.
    - export_priority: PV serves home then grid first; the battery only takes the
      excess (e.g. PV above the inverter's AC limit), keeping headroom for clipping.
    """
    mode: BatteryMode
    start: datetime
    end: datetime
    power_kw: float = 0.0
    target_soc: float | None = None
    min_soc: float | None = None
    reason: str = ''


@dataclass
class CommandResult:
    accepted: bool
    verified: bool                      # device read-back matches the request
    detail: dict[str, Any] = field(default_factory=dict)


class PluginContext(ABC):
    """Services the core provides to a plugin instance."""
    target_id: str

    @abstractmethod
    def secret(self, name: str) -> str | None: ...

    @abstractmethod
    def remember(self, key: str, value: Any) -> None:
        """Persist small non-secret plugin state (e.g. a discovered serial)."""

    @abstractmethod
    def recall(self, key: str, default=None) -> Any: ...

    @abstractmethod
    def record_raw(self, payload: Any) -> None:
        """Keep the raw vendor response for audit (retention-limited; never put secrets in it)."""

    @abstractmethod
    def check_url(self, url: str, expected_hosts: set[str] | None = None) -> str:
        """Raise unless the URL is HTTPS and allowed (SSRF protection)."""

    @abstractmethod
    def pushed(self) -> dict | None:
        """Latest payload pushed to /api/ingest for this target, if any."""

    timezone: str


class SystemPlugin(ABC):
    """Reads (and optionally controls) one inverter/battery system."""
    kind: ClassVar[str] = 'system'
    name: ClassVar[str]                      # registry key, e.g. 'foxess'
    title: ClassVar[str]                     # human name, e.g. 'FoxESS Cloud'
    description: ClassVar[str] = ''
    Config: ClassVar[type[PluginConfig]] = PluginConfig
    secrets: ClassVar[tuple[SecretField, ...]] = ()
    can_control: ClassVar[bool] = False
    supports_battery_schedule: bool = False
    supported_modes: ClassVar[tuple[str, ...]] = ()
    control_notes: ClassVar[str] = ''

    def __init__(self, context: PluginContext, config: PluginConfig, system):
        self.context = context
        self.config = config
        self.system = system  # config.EnergySystem (physical limits)

    @abstractmethod
    async def read(self) -> SystemReading:
        """Return the latest measurement. Must not change device settings."""

    async def test(self) -> dict:
        """Read-only connection check shown in the UI."""
        reading = await self.read()
        return {'reading': reading.as_dict()}

    async def history(self, start: datetime, end: datetime) -> list[SystemReading]:
        """Optional: past measurements for backfilling (used by `kwattson backfill`)."""
        raise NotImplementedError(f'{self.title} does not provide history')

    async def apply(self, command: BatteryCommand) -> CommandResult:
        raise NotImplementedError(f'{self.title} does not support battery control')

    async def apply_schedule(self, commands: list[BatteryCommand]) -> CommandResult:
        """Replace and verify today's complete schedule, without a rolling refresh."""
        raise NotImplementedError(f'{self.title} does not support battery schedules')

    async def verify_schedule(self, commands: list[BatteryCommand]) -> CommandResult:
        """Read-only confirmation that the enabled schedule still matches."""
        raise NotImplementedError(f'{self.title} does not support battery schedules')

    async def release(self) -> CommandResult:
        """Return the device to its own normal mode and remove any schedule we wrote."""
        raise NotImplementedError(f'{self.title} does not support battery control')

    async def close(self) -> None:
        return None


class VehiclePlugin(ABC):
    """Reads an EV’s state and optionally supports ending a charging session."""
    supports_charge_stop: ClassVar[bool] = False
    supports_charge_schedule: ClassVar[bool] = False
    kind: ClassVar[str] = 'vehicle'
    name: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str] = ''
    Config: ClassVar[type[PluginConfig]] = PluginConfig
    secrets: ClassVar[tuple[SecretField, ...]] = ()

    def __init__(self, context: PluginContext, config: PluginConfig, vehicle):
        self.context = context
        self.config = config
        self.vehicle = vehicle

    @abstractmethod
    async def read(self) -> VehicleReading | None: ...

    async def test(self) -> dict:
        reading = await self.read()
        return {'reading': asdict(reading) if reading else None}

    async def start_charging(self) -> None:
        raise PluginError('This connection cannot start charging')

    async def stop_charging(self) -> None:
        raise PluginError('This connection cannot stop charging')

    async def close(self) -> None:
        return None


class PluginError(Exception):
    """Raise with a short, credential-free message suitable for the owner."""
