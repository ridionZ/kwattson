"""Plugin discovery.

Built-in plugins are listed below. Third-party packages register additional ones
with a Python entry point, without touching this repository::

    # pyproject.toml of "kwattson-acme"
    [project.entry-points."kwattson.plugins"]
    acme = "kwattson_acme:AcmeInverter"

Disable third-party discovery with KWATTSON_EXTERNAL_PLUGINS=false.
"""
from __future__ import annotations

import importlib
import logging
import os
from importlib.metadata import entry_points

from pydantic import ValidationError

from .base import SystemPlugin, VehiclePlugin

log = logging.getLogger(__name__)

BUILTIN = {
    'simulator': 'kwattson.plugins.simulator:SimulatedHybrid',
    'foxess': 'kwattson.plugins.foxess:FoxESSCloud',
    'homeassistant': 'kwattson.plugins.homeassistant:HomeAssistantSystem',
    'push': 'kwattson.plugins.push:PushedSystem',
    'manual': 'kwattson.plugins.vehicles:ManualVehicle',
    'mg_ismart': 'kwattson.plugins.mg_ismart:MGiSmart',
    'homeassistant_vehicle': 'kwattson.plugins.homeassistant:HomeAssistantVehicle',
    'push_vehicle': 'kwattson.plugins.push:PushedVehicle',
    'simulator_vehicle': 'kwattson.plugins.simulator:SimulatedVehicle',
}

_cache: dict[str, type] | None = None


def _load(target: str):
    module, _, attribute = target.partition(':')
    return getattr(importlib.import_module(module), attribute)


def plugins() -> dict[str, type]:
    global _cache
    if _cache is not None:
        return _cache
    found: dict[str, type] = {}
    for name, target in BUILTIN.items():
        try:
            found[name] = _load(target)
        except ImportError as error:  # optional dependency missing
            log.warning('Plugin %s unavailable: %s', name, error)
    if os.getenv('KWATTSON_EXTERNAL_PLUGINS', 'true').lower() == 'true':
        for entry in entry_points(group='kwattson.plugins'):
            if entry.name in found:
                log.warning('Ignoring external plugin %s: name already used', entry.name)
                continue
            try:
                cls = entry.load()
                if not issubclass(cls, (SystemPlugin, VehiclePlugin)):
                    raise TypeError('not a SystemPlugin or VehiclePlugin')
                found[entry.name] = cls
            except Exception as error:  # a broken add-on must not stop the app
                log.warning('External plugin %s failed to load: %s', entry.name, error)
    _cache = found
    return found


def get(name: str, kind: str):
    cls = plugins().get(name)
    if cls is None:
        raise ValueError(f'Unknown plugin "{name}"')
    if cls.kind != kind:
        raise ValueError(f'Plugin "{name}" is a {cls.kind} plugin, not a {kind} plugin')
    return cls


def describe() -> list[dict]:
    """Catalogue for the settings UI: names, config JSON schema, secret fields."""
    out = []
    for name, cls in sorted(plugins().items()):
        out.append(dict(name=name, kind=cls.kind, title=cls.title, description=cls.description,
                        config_schema=cls.Config.model_json_schema(),
                        secrets=[dict(name=s.name, label=s.label, help=s.help, required=s.required, multiline=s.multiline)
                                 for s in cls.secrets],
                        can_control=getattr(cls, 'can_control', False),
                        supported_modes=list(getattr(cls, 'supported_modes', ())),
                        control_notes=getattr(cls, 'control_notes', '')))
    return out


def validate_settings(settings) -> None:
    """Validate each system/vehicle's plugin_config against its plugin's schema."""
    for system in settings.systems:
        cls = get(system.plugin, 'system')
        _validate(cls, system.plugin_config, f'System "{system.name}"')
        if system.controllable and not cls.can_control:
            raise ValueError(f'System "{system.name}": plugin {cls.title} cannot control batteries')
    for vehicle in settings.vehicles:
        cls = get(vehicle.plugin, 'vehicle')
        _validate(cls, vehicle.plugin_config, f'Vehicle "{vehicle.name}"')


def _validate(cls, config, label):
    try:
        cls.Config.model_validate(config)
    except ValidationError as error:
        first = error.errors()[0]
        where = '.'.join(str(p) for p in first['loc'])
        raise ValueError(f'{label}: {where or "config"}: {first["msg"]}') from None
