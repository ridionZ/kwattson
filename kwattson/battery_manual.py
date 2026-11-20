"""Short owner-selected battery modes, checked and dispatched by Controller."""
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

from . import plans
from .config import stamp
from .plugins import registry
from .plugins.base import BatteryCommand, PluginError

KEY = 'battery_manual'


def power_limits(db, settings, system, now):
    """Conservative current meter headroom; the device still enforces its own limits."""
    site = db.latest_site()
    if not site or not timedelta(0) <= now - datetime.fromisoformat(site['ts']) <= timedelta(minutes=10) or site['fault']:
        return None, None
    others = []
    for other in settings.active_systems():
        if other.id == system.id:
            continue
        reading = db.latest_system(other.id)
        if not reading or not timedelta(0) <= now - datetime.fromisoformat(reading['ts']) <= timedelta(minutes=10):
            return None, None
        others.append(reading)
    charge = min(system.ac_kw, system.battery.charge_kw,
                 max(0, settings.site.grid_import_kw - site['load_kw'] - sum(r['charge_kw'] or 0 for r in others)))
    live = db.latest_system(system.id)
    if not live or live['pv_kw'] is None:
        return charge, None
    other_output = sum(max(0, r['ac_output_kw'] if r['ac_output_kw'] is not None else (r['pv_kw'] or 0) + (r['discharge_kw'] or 0)) for r in others)
    export = max(0, min(system.battery.discharge_kw, system.ac_kw - live['pv_kw'],
                        settings.site.export_kw + site['load_kw'] - other_output - live['pv_kw']))
    return charge, export


def export_floor(db, settings, system, now, until):
    decision = plans.latest(db)
    floors = []
    if decision:
        for row in plans.intervals(db, decision['id']):
            at = datetime.fromisoformat(row['ts'])
            if at < until and at + timedelta(hours=row['hours']) > now:
                value = row['systems'].get(system.id, {}).get('export_floor_pct')
                if value is not None:
                    floors.append(value)
    if not floors:
        raise PluginError('A fresh overnight reserve forecast is required before manual battery export')
    return max(system.battery.reserve_pct, *floors)


def request(db, settings, system, now, *, mode, minutes, power_kw=None, target_pct=None):
    if not 5 <= minutes <= 30:
        raise PluginError('Choose a temporary mode duration between 5 and 30 minutes')
    if not system.enabled or not system.controllable or not system.battery:
        raise PluginError('Choose an enabled, commissioned battery system')
    plugin = registry.get(system.plugin, 'system')
    if mode not in plugin.supported_modes:
        raise PluginError('This inverter does not support the selected mode')
    local = now.astimezone(settings.zone)
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = min(now + timedelta(minutes=minutes), midnight - timedelta(minutes=1)).replace(second=0, microsecond=0)
    if end <= now:
        raise PluginError('Wait until the new local day before selecting a temporary mode')
    spec = system.battery
    maximum = min(system.ac_kw, spec.charge_kw if mode == 'force_charge' else spec.discharge_kw)
    if mode in ('force_charge', 'force_discharge'):
        charge, export = power_limits(db, settings, system, now)
        maximum = charge if mode == 'force_charge' else export
        if maximum is None:
            raise PluginError('Fresh site power readings are required before selecting a power mode')
    if maximum <= 0 and mode in ('force_charge', 'force_discharge'):
        raise PluginError('No power is available within the configured site and inverter limits')
    power = maximum if power_kw is None else power_kw
    if mode in ('force_charge', 'force_discharge') and not 0 < power <= maximum:
        raise PluginError(f'Choose power above zero and no greater than {maximum:g} kW')
    if mode == 'force_discharge':
        floor = export_floor(db, settings, system, now, end)
        target = max(floor, target_pct if target_pct is not None else floor)
        live = db.latest_system(system.id)
        if not live or live['soc'] is None or live['soc'] <= target:
            raise PluginError('The battery is at its protected level; export would spend energy reserved for the home or car')
    elif mode == 'force_charge':
        floor = None
        target = target_pct if target_pct is not None else spec.max_soc_pct
    else:
        live = db.latest_system(system.id)
        floor = live['soc'] if mode == 'hold' and live else None
        target = None
        power = 0
    if target is not None and not spec.reserve_pct <= target <= spec.max_soc_pct:
        raise PluginError('Choose a target within the configured battery reserve and maximum')
    record = dict(id=uuid4().hex, mode=mode, start=stamp(now.replace(second=0, microsecond=0)),
                  end=stamp(end), power_kw=power, target_soc=target, min_soc=floor,
                  reason=f'Owner selected {mode.replace("_", " ")} until {end.astimezone(settings.zone):%H:%M}')
    records = db.meta(KEY, {})
    records[system.id] = record
    db.set_meta(KEY, records)
    return record


def cancel(db, system_id):
    records = db.meta(KEY, {})
    old = records.pop(system_id, None)
    db.set_meta(KEY, records)
    # A manual day program must be replaced even if the adopted plan ID is unchanged.
    stored = db.meta('control_schedules', {})
    stored.pop(system_id, None)
    db.set_meta('control_schedules', stored)
    return old


def command(record):
    return BatteryCommand(record['mode'], datetime.fromisoformat(record['start']),
                          datetime.fromisoformat(record['end']), record['power_kw'],
                          record['target_soc'], record['min_soc'], record['reason'])


def overlay(commands, manual):
    """Keep the adopted day program on either side of the owner's short mode."""
    out = []
    for item in commands:
        if item.end <= manual.start or item.start >= manual.end:
            out.append(item)
        else:
            if item.start < manual.start:
                out.append(replace(item, end=manual.start))
            if item.end > manual.end:
                out.append(replace(item, start=manual.end))
    return sorted([*out, manual], key=lambda item: item.start)
