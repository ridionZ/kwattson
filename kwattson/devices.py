"""Runs plugin instances for the configured systems and vehicles.

Owns plugin lifecycle (instances are rebuilt when their settings change), gives
each plugin a ``PluginContext`` scoped to its own secrets/state, polls them and
stores normalised readings.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .config import Settings, stamp, utcnow
from .db import Database, decode, encode
from .ingest import combine, store_site, store_system, store_vehicle
from .plugins import registry
from .plugins.base import PluginContext, SystemPlugin, SystemReading, VehiclePlugin
from .security import Security, safe_destination

log = logging.getLogger(__name__)


DEMO_PLUGINS = {'simulator', 'simulator_vehicle', 'manual', 'push', 'push_vehicle'}


def check_demo_plugin(security, name):
    if security.runtime.demo and name not in DEMO_PLUGINS:
        raise ValueError('Use a simulated, manual or pushed device in the demo.')


class Context(PluginContext):
    def __init__(self, db: Database, security: Security, target_id: str, timezone: str):
        self.db, self.security, self.target_id, self.timezone = db, security, target_id, timezone

    def secret(self, name):
        return self.security.secret(f'{self.target_id}:{name}')

    def remember(self, key, value):
        self.db.set_meta(f'plugin:{self.target_id}:{key}', value)

    def recall(self, key, default=None):
        return self.db.meta(f'plugin:{self.target_id}:{key}', default)

    def record_raw(self, payload):
        self.db.raw(self.target_id, payload)

    def check_url(self, url, expected_hosts=None):
        return safe_destination(url, expected_hosts)

    def pushed(self):
        row = self.db.one('SELECT data FROM pushed_readings WHERE target=?', (self.target_id,))
        return decode(row['data']) if row else None


class DraftContext(Context):
    """Context for testing an unsaved system/vehicle: secrets and state live only in memory.

    Secrets left blank fall back to the ones already stored for that id (editing an existing
    system without retyping its API key). Nothing is written to the database.
    """

    def __init__(self, db, security, target_id, timezone, secrets: dict[str, str], use_stored: bool):
        super().__init__(db, security, target_id, timezone)
        self._secrets = {k: v for k, v in secrets.items() if v}
        self._use_stored = use_stored
        self._state: dict = {}

    def secret(self, name):
        return self._secrets.get(name) or (super().secret(name) if self._use_stored else None)

    def remember(self, key, value):
        self._state[key] = value

    def recall(self, key, default=None):
        return self._state.get(key, default)

    def record_raw(self, payload):
        return None


def draft_instance(db, security, kind: str, spec, timezone: str, secrets: dict[str, str]):
    check_demo_plugin(security, spec.plugin)
    cls = registry.get(spec.plugin, kind)
    items = getattr(db.settings(), kind + 's')
    use_stored = any(item.id == spec.id and item.plugin == spec.plugin for item in items)
    context = DraftContext(db, security, f'{kind}:{spec.id}', timezone, secrets, use_stored)
    return cls(context, cls.Config.model_validate(spec.plugin_config), spec)


class Devices:
    def __init__(self, db: Database, security: Security):
        self.db, self.security = db, security
        self._instances: dict[str, tuple[str, object]] = {}
        self.status: dict[str, dict] = {}   # last poll outcome per target, for the UI and alerts

    def _instance(self, target: str, spec, kind: str, timezone: str):
        check_demo_plugin(self.security, spec.plugin)
        signature = encode([spec.plugin, spec.plugin_config, spec.model_dump()])
        cached = self._instances.get(target)
        if cached and cached[0] == signature:
            return cached[1]
        cls = registry.get(spec.plugin, kind)
        instance = cls(Context(self.db, self.security, target, timezone), cls.Config.model_validate(spec.plugin_config), spec)
        self._instances[target] = (signature, instance)
        return instance

    def system(self, settings: Settings, system_id: str) -> SystemPlugin:
        spec = next(s for s in settings.systems if s.id == system_id)
        return self._instance(f'system:{spec.id}', spec, 'system', settings.site.timezone)

    def vehicle(self, settings: Settings, vehicle_id: str) -> VehiclePlugin:
        spec = next(v for v in settings.vehicles if v.id == vehicle_id)
        return self._instance(f'vehicle:{spec.id}', spec, 'vehicle', settings.site.timezone)

    def _mark(self, target, ok, error=None, extra=None):
        previous = self.status.get(target, {})
        self.status[target] = dict(ok=ok, at=stamp(), error=error, since=previous.get('since') if previous.get('ok') == ok else stamp(),
                                   failures=0 if ok else previous.get('failures', 0) + 1, **(extra or {}))
        return previous

    async def poll_systems(self, settings: Settings) -> dict:
        """Read every enabled system; store readings and the combined site reading."""
        readings: dict[str, SystemReading] = {}
        transitions = []
        for spec in settings.active_systems():
            target = f'system:{spec.id}'
            try:
                reading = await self.system(settings, spec.id).read()
                _validate(reading)
                store_system(self.db, spec.id, spec.plugin, reading)
                readings[spec.id] = reading
                previous = self._mark(target, True)
                if previous and previous.get('ok') is False:
                    transitions.append(('restored', spec, None))
            except Exception as error:
                message = _message(error)
                previous = self._mark(target, False, message)
                log.warning('Reading %s failed: %s', target, message)
                if previous.get('ok', True):
                    transitions.append(('failed', spec, message))
        ev_kw, estimated = self._ev_power(settings)
        site = combine(settings, readings, ev_kw, estimated)
        if site:
            store_site(self.db, site)
        return dict(site=site, readings=readings, transitions=transitions)

    def _ev_power(self, settings: Settings) -> tuple[float, bool]:
        """Estimate EV charging power from vehicle state (wall chargers are rarely metered separately)."""
        now = utcnow()
        total, estimated = 0.0, False
        for v in settings.active_vehicles():
            latest = self.db.latest_vehicle(v.id)
            if latest and latest['charging'] and latest['at_home'] != 0 and now - datetime.fromisoformat(latest['ts']) < timedelta(minutes=45):
                total += latest['charge_kw'] or v.charger_kw
                estimated = estimated or latest['charge_kw'] is None
        return total, estimated

    async def poll_vehicles(self, settings: Settings):
        for spec in settings.active_vehicles():
            target = f'vehicle:{spec.id}'
            try:
                reading = await self.vehicle(settings, spec.id).read()
                if reading is not None:
                    if reading.soc is not None and not 0 <= reading.soc <= 100:
                        raise ValueError('vehicle state of charge out of range')
                    store_vehicle(self.db, spec.id, spec.plugin, reading)
                self._mark(target, True)
            except Exception as error:
                self._mark(target, False, _message(error))

    async def close(self):
        for _, instance in self._instances.values():
            try:
                await instance.close()
            except Exception:
                pass
        self._instances.clear()


def _validate(r: SystemReading):
    for name in ('pv_kw', 'charge_kw', 'discharge_kw', 'ac_output_kw', 'import_kw', 'export_kw', 'load_kw'):
        value = getattr(r, name)
        if value is not None and not 0 <= value <= 1000:
            raise ValueError(f'{name} out of range')
    if r.soc is not None and not 0 <= r.soc <= 100:
        raise ValueError('state of charge out of range')
    if r.ts > utcnow() + timedelta(minutes=5):
        raise ValueError('reading timestamp is in the future')


def _message(error: Exception) -> str:
    """A short, credential-free error description."""
    from pydantic import ValidationError
    from .plugins.base import PluginError
    if isinstance(error, ValidationError):
        return 'Invalid connection settings. Check the values in the connection form.'
    if isinstance(error, (PluginError, ValueError, NotImplementedError)):
        return str(error)[:200]
    return f'{type(error).__name__} (details withheld)'
