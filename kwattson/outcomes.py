"""Daily outcomes from *measured* data, with simple counterfactuals.

For each completed local day with enough coverage:

- actual: tariff applied to measured import/export (supply included).
- self_use: the same measured solar and load replayed through a simulated
  self-use battery (starting from the measured morning state of charge), i.e.
  "what the inverter would have done on its own".
- no_battery: measured solar and load with no battery.
- no_solar: all load bought from the grid.

Savings vs self-use are adjusted for any difference in end-of-day stored energy,
valued at that day's 20th-percentile import price, so draining the battery
doesn't look like a saving. These are estimates from telemetry, not a bill.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from .config import Settings, stamp, utcnow
from .db import Database, encode
from .ingest import select_intervals
from .optimizer import price_points, self_use
from .tariffs import bill


def day_bounds(settings: Settings, day) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=settings.zone)
    return start, start + timedelta(days=1)


def compute_day(db: Database, settings: Settings, day) -> dict | None:
    start, end = day_bounds(settings, day)
    rows = select_intervals(db, stamp(start), stamp(end))
    seconds = (end - start).total_seconds()
    coverage = sum(r['coverage_s'] for r in rows) / seconds
    if coverage < 0.5:
        return None
    zone = settings.zone
    actual = bill(settings.tariffs, rows, zone).get(day.isoformat())
    if not actual:
        return None
    points = [dict(ts=r['ts'], hours=r['coverage_s'] / 3600, load_kwh=r['load_kwh'],
                   solar_by_system=_split_solar(settings, r['solar_kwh'])) for r in rows]
    prices = price_points(settings, points)
    first_soc = next((r['soc_end'] for r in rows if r['soc_end'] is not None), None)
    batteries = [s for s in settings.active_systems() if s.battery]
    initial = {s.id: s.battery.capacity_kwh * (first_soc or s.battery.reserve_pct) / 100 for s in batteries}
    simulated = self_use(settings, points, prices, initial)
    sim_rows = [dict(ts=r['ts'], import_kwh=s['import_kwh'], export_kwh=s['export_kwh']) for r, s in zip(rows, simulated)]
    self_use_bill = bill(settings.tariffs, sim_rows, zone)[day.isoformat()]
    no_battery = []
    for r in rows:
        net = r['load_kwh'] - r['solar_kwh']
        no_battery.append(dict(ts=r['ts'], import_kwh=max(0.0, net), export_kwh=max(0.0, -net)))
    no_battery_bill = bill(settings.tariffs, no_battery, zone)[day.isoformat()]
    no_solar_bill = bill(settings.tariffs, [dict(ts=r['ts'], import_kwh=r['load_kwh'], export_kwh=0.0) for r in rows], zone)[day.isoformat()]
    last_soc = next((r['soc_end'] for r in reversed(rows) if r['soc_end'] is not None), None)
    capacity = settings.battery_kwh()
    actual_end = (last_soc or 0) / 100 * capacity
    sim_end = sum(simulated[-1]['soc'].values()) if simulated else 0
    value = float(np.quantile([p['buy'] for p in prices], 0.2)) if prices else 0
    adjustment = (actual_end - sim_end) * value
    totals = {k: float(sum(r[k] for r in rows)) for k in ('solar_kwh', 'load_kwh', 'ev_kwh', 'import_kwh', 'export_kwh', 'charge_kwh', 'discharge_kwh')}
    decisions = db.one('SELECT COUNT(*) AS n FROM decisions WHERE created_at>=? AND created_at<?', (stamp(start), stamp(end)))['n']
    commands = db.one("SELECT COUNT(*) AS n FROM commands WHERE created_at>=? AND created_at<? AND action!='release'", (stamp(start), stamp(end)))['n']
    return dict(day=day.isoformat(), coverage=round(coverage, 3), plan=actual['plan'], **{k: round(v, 3) for k, v in totals.items()},
                limited_hours=round(sum(r['limited_s'] for r in rows) / 3600, 2),
                net_aud=round(actual['net'], 2), import_cost_aud=round(actual['import_cost'], 2), export_credit_aud=round(actual['export_credit'], 2),
                supply_aud=round(actual['supply'], 2), self_use_net_aud=round(self_use_bill['net'], 2),
                no_battery_net_aud=round(no_battery_bill['net'], 2), no_solar_net_aud=round(no_solar_bill['net'], 2),
                saving_vs_self_use_aud=round(self_use_bill['net'] - actual['net'] + adjustment, 2),
                stored_energy_adjustment_aud=round(adjustment, 2), soc_start=first_soc, soc_end=last_soc,
                simulated_self_use_clipped_kwh=round(sum(s['curtailed'] for s in simulated), 2),
                decisions=decisions, commands=commands,
                note='Telemetry-based estimate. Counterfactuals replay measured solar/load through simple models.')


def _split_solar(settings: Settings, kwh: float) -> dict:
    systems = [s for s in settings.active_systems() if s.pv_kwp > 0]
    total = sum(s.pv_kwp for s in systems) or 1
    return {s.id: kwh * s.pv_kwp / total for s in systems}


def update(db: Database, settings: Settings, now: datetime | None = None, days: int = 3) -> list[dict]:
    """(Re)compute recent completed days. Safe to call repeatedly."""
    now = now or utcnow()
    today = now.astimezone(settings.zone).date()
    out = []
    for back in range(1, days + 1):
        day = today - timedelta(days=back)
        result = compute_day(db, settings, day)
        if result:
            db.execute('INSERT OR REPLACE INTO daily_outcomes VALUES(?,?,?,?)', (day.isoformat(), stamp(now), result['coverage'], encode(result)))
            out.append(result)
    return out


def today_so_far(db: Database, settings: Settings, now: datetime | None = None) -> dict | None:
    now = now or utcnow()
    return compute_day(db, settings, now.astimezone(settings.zone).date())
