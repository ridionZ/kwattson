"""Store readings and derive half-hour energy from counters or power samples.

Integration is *recomputed* from stored readings for every half-hour slot a new
reading can affect, so late or out-of-order readings never double count. Gaps
longer than MAX_GAP are left uncovered (``coverage_s`` < 1800) rather than
being filled with zeros.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite

from .config import Settings, stamp
from .db import Database, decode, encode
from .plugins.base import SystemReading, VehicleReading

MAX_GAP = timedelta(minutes=15)
SLOT = timedelta(minutes=30)
ENERGY = {'solar_kw': 'solar_kwh', 'load_kw': 'load_kwh', 'ev_kw': 'ev_kwh', 'import_kw': 'import_kwh',
          'export_kw': 'export_kwh', 'charge_kw': 'charge_kwh', 'discharge_kw': 'discharge_kwh'}


def slot_start(at: datetime) -> datetime:
    return at.replace(minute=0 if at.minute < 30 else 30, second=0, microsecond=0)


def store_system(db: Database, system_id: str, plugin: str, r: SystemReading):
    with db.connect() as c:
        c.execute('INSERT OR REPLACE INTO system_readings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            stamp(r.ts), system_id, plugin, r.soc, r.pv_kw, r.charge_kw, r.discharge_kw, r.ac_output_kw, r.import_kw,
            r.export_kw, r.load_kw, r.battery_temp, r.running_state, None if r.fault is None else int(r.fault),
            int(r.time_verified), encode(r.extra), stamp()))


def store_vehicle(db: Database, vehicle_id: str, plugin: str, r: VehicleReading):
    flag = lambda v: None if v is None else int(v)
    db.execute('INSERT OR REPLACE INTO vehicle_readings VALUES(?,?,?,?,?,?,?,?,?,?,?)', (
        stamp(r.ts), vehicle_id, plugin, r.soc, flag(r.charging), flag(r.plugged_in), flag(r.at_home),
        r.charge_kw, r.range_km, int(r.time_verified), stamp()))
    from . import home_charging
    home_charging.observe_vehicle(db, vehicle_id, plugin, r)


def combine(settings: Settings, readings: dict[str, SystemReading], ev_kw: float = 0.0, ev_estimated: bool = False) -> dict | None:
    """Merge per-system readings into one site reading.

    Grid import/export and household load come from the site-meter system (or
    the only system). Without a meter the site reading cannot be formed.
    """
    systems = {s.id: s for s in settings.active_systems()}
    readings = {k: v for k, v in readings.items() if k in systems}
    if not readings:
        return None
    meter_id = next((s.id for s in systems.values() if s.site_meter), None)
    if meter_id is None and len(systems) == 1:
        meter_id = next(iter(systems))
    meter = readings.get(meter_id)
    if meter is None or meter.import_kw is None or meter.export_kw is None:
        return None
    solar = sum(r.pv_kw or 0 for r in readings.values())
    charge = sum(r.charge_kw or 0 for r in readings.values())
    discharge = sum(r.discharge_kw or 0 for r in readings.values())
    load = meter.load_kw
    if load is None:
        # AC energy balance: everything the inverters output plus imports, less exports.
        load = max(0.0, solar - charge + discharge + meter.import_kw - meter.export_kw)
    stored = capacity = 0.0
    limited = False
    for system_id, reading in readings.items():
        system = systems[system_id]
        if system.battery and reading.soc is not None:
            stored += system.battery.capacity_kwh * reading.soc / 100
            capacity += system.battery.capacity_kwh
        output = reading.ac_output_kw if reading.ac_output_kw is not None else (reading.pv_kw or 0) - (reading.charge_kw or 0) + (reading.discharge_kw or 0)
        if system.coupling == 'dc' and (reading.pv_kw or 0) > 0.5 and output >= 0.97 * system.ac_kw:
            limited = True
    details = {k: v.as_dict() for k, v in readings.items()}
    counters = {}
    ac_limit = sum(s.ac_kw for s in systems.values())
    for metric, ids, maximum in (
        ('import_kwh', [meter_id], settings.site.grid_import_kw),
        ('export_kwh', [meter_id], max(settings.site.export_kw, ac_limit)),
        ('load_kwh', [meter_id], settings.site.grid_import_kw + ac_limit),
        ('solar_kwh', [s.id for s in systems.values() if s.pv_kwp > 0], sum(s.pv_limit_kw for s in systems.values() if s.pv_kwp > 0)),
    ):
        values = {}
        for sid in ids:
            r = readings.get(sid)
            if not r or not r.time_verified or r.ts != meter.ts:
                break
            value = r.extra.get('energy_counters_kwh', {}).get(metric)
            if not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
                break
            values[sid] = value
        if ids and len(values) == len(ids):
            counters[metric] = {'sources': values, 'max_kw': maximum}
    if counters:
        details['_energy'] = counters
    return dict(ts=meter.ts, soc=stored / capacity * 100 if capacity else None, stored_kwh=stored if capacity else None,
                capacity_kwh=capacity or None, solar_kw=solar, load_kw=load, import_kw=meter.import_kw, export_kw=meter.export_kw,
                charge_kw=charge, discharge_kw=discharge, ev_kw=min(ev_kw, load), ev_estimated=int(ev_estimated),
                output_limited=int(limited), fault=int(any(r.fault for r in readings.values())),
                time_verified=int(all(r.time_verified for r in readings.values())),
                systems=details)


def store_site_row(c, site: dict):
    c.execute('INSERT OR REPLACE INTO site_readings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
        stamp(site['ts']), site['soc'], site['stored_kwh'], site['capacity_kwh'], site['solar_kw'], site['load_kw'],
        site['import_kw'], site['export_kw'], site['charge_kw'], site['discharge_kw'], site['ev_kw'],
        site['ev_estimated'], site['output_limited'], site['fault'], site['time_verified'], encode(site['systems']), stamp()))


def store_site(db: Database, site: dict):
    at = site['ts']
    with db.connect() as c:
        store_site_row(c, site)
        rebuild_intervals(c, at - MAX_GAP, at + MAX_GAP)
    from . import home_charging
    home_charging.observe_site(db, site)


def counter_deltas(before: dict, after: dict, seconds: float) -> dict:
    deltas = {}
    for metric in before.keys() & after.keys():
        a, b = before[metric]['sources'], after[metric]['sources']
        if a.keys() != b.keys():
            continue
        changes = [b[source] - a[source] for source in a]
        total = sum(changes)
        cap = max(before[metric]['max_kw'], after[metric]['max_kw']) * seconds / 3600
        if all(change >= 0 for change in changes) and total <= cap + 0.2 * len(changes):
            deltas[metric] = total
    return deltas


def rebuild_intervals(c, start: datetime, end: datetime, source='telemetry'):
    """Recompute every half-hour slot overlapping [start, end] from site_readings."""
    first, last = slot_start(start), slot_start(end) + SLOT
    rows = c.execute('SELECT * FROM site_readings WHERE ts>=? AND ts<? ORDER BY ts',
                     (stamp(first - MAX_GAP), stamp(last + MAX_GAP))).fetchall()
    slots: dict[datetime, dict] = {}
    counters = [decode(row['systems']).get('_energy', {}) for row in rows]
    for i, (a, b) in enumerate(zip(rows, rows[1:])):
        t0, t1 = datetime.fromisoformat(a['ts']), datetime.fromisoformat(b['ts'])
        span = (t1 - t0).total_seconds()
        if not 0 < span <= MAX_GAP.total_seconds():
            continue
        measured = counter_deltas(counters[i], counters[i + 1], span)
        cursor = t0
        while cursor < t1:
            slot = slot_start(cursor)
            stop = min(t1, slot + SLOT)
            if first <= slot < last:
                seconds = (stop - cursor).total_seconds()
                # Linear interpolation of power at the sub-segment midpoint (trapezoid rule).
                mid = ((cursor - t0).total_seconds() + (stop - t0).total_seconds()) / 2 / span
                bucket = slots.setdefault(slot, {k: 0.0 for k in ENERGY.values()} | {'coverage_s': 0.0, 'limited_s': 0.0, 'soc_end': None, 'used_counters': False})
                if measured:
                    bucket['used_counters'] = True
                for power, energy in ENERGY.items():
                    if energy in measured:
                        # The counter gives the exact span total, but not when it changed within that span.
                        bucket[energy] += measured[energy] * seconds / span
                    else:
                        p0, p1 = a[power] or 0.0, b[power] or 0.0
                        bucket[energy] += (p0 + (p1 - p0) * mid) * seconds / 3600
                bucket['coverage_s'] += seconds
                if a['output_limited'] and b['output_limited']:
                    bucket['limited_s'] += seconds
                if b['soc'] is not None:
                    bucket['soc_end'] = b['soc']
            cursor = stop
    c.execute('DELETE FROM intervals WHERE source=? AND ts>=? AND ts<?', (source, stamp(first), stamp(last)))
    for slot, v in slots.items():
        quality = 'observed' if v['coverage_s'] >= 1500 else 'partial'
        if v['used_counters']:
            quality += '_with_counters'
        c.execute('INSERT INTO intervals(ts, source, solar_kwh, load_kwh, ev_kwh, import_kwh, export_kwh, charge_kwh, discharge_kwh, '
                  'soc_end, limited_s, coverage_s, quality) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (stamp(slot), source, v['solar_kwh'], v['load_kwh'], v['ev_kwh'], v['import_kwh'], v['export_kwh'],
                   v['charge_kwh'], v['discharge_kwh'], v['soc_end'], v['limited_s'], min(1800.0, v['coverage_s']),
                   quality))


def select_intervals(db: Database, start: str, end: str | None = None) -> list[dict]:
    """One row per slot, preferring metered/imported history over power integration when better covered."""
    rows = db.rows('SELECT * FROM intervals WHERE ts>=? AND ts<? ORDER BY ts', (start, end or stamp()))
    rank = {'meter': 3, 'telemetry': 2}
    chosen: dict[str, dict] = {}
    for r in rows:
        current = chosen.get(r['ts'])
        if current is None or (rank.get(r['source'], 1), r['coverage_s']) > (rank.get(current['source'], 1), current['coverage_s']):
            chosen[r['ts']] = r
    return list(chosen.values())
