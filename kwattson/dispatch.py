"""Tariff boundaries and overnight energy to retain before selling battery power."""
from datetime import datetime, timedelta
from typing import NamedTuple

from .config import Settings, UTC, stamp
from .tariffs import plan_at


def split_points(settings: Settings, points: list[dict]) -> list[dict]:
    """Keep the real tariff at the edges and restrict battery actions separately."""
    start = datetime.fromisoformat(points[0]['ts']).astimezone(UTC)
    end = datetime.fromisoformat(points[-1]['ts']) + timedelta(hours=points[-1]['hours'])
    buffer = timedelta(minutes=settings.forecast.tariff_buffer_minutes)
    scan_start, scan_end = start - buffer, end + buffer
    boundary = scan_start.replace(minute=scan_start.minute // 5 * 5, second=0, microsecond=0)
    candidates = set()
    while boundary <= scan_end:
        candidates.add(boundary)
        boundary += timedelta(minutes=5)
    candidates.update(at for t in settings.tariffs if t.status == 'active'
                      and (at := t.start()) is not None and scan_start <= at <= scan_end)

    def tariff(at):
        plan = plan_at(settings.tariffs, at)
        return plan.id, plan.period_at(at.astimezone(settings.zone))

    edges = []
    charge_blocks, export_blocks = [], []
    for at in sorted(candidates):
        local = at.astimezone(settings.zone)
        if (local.hour, local.minute, local.second, local.microsecond) == (6, 0, 0, 0):
            edges.append(at)
        before, after = tariff(at - timedelta(microseconds=1)), tariff(at)
        if before == after:
            continue
        edges.append(at)
        for old, new, blocks in ((before[1].import_rate, after[1].import_rate, charge_blocks),
                                 (-before[1].export_rate, -after[1].export_rate, export_blocks)):
            if not buffer or old == new:
                continue
            block = (at, at + buffer) if new < old else (at - buffer, at)
            blocks.append(block)
            edges.extend(block)
    edges = sorted(set(edges))
    result = []
    energy_keys = ('house_kwh', 'house_p10', 'house_p90', 'ev_kwh', 'load_kwh', 'solar_kwh', 'solar_p10', 'solar_p90')
    for p in points:
        at = datetime.fromisoformat(p['ts'])
        stop = at + timedelta(hours=p['hours'])
        bounds = [at, *(edge for edge in edges if at < edge < stop), stop]
        for a, b in zip(bounds, bounds[1:]):
            h = (b - a).total_seconds() / 3600
            fraction = h / p['hours']
            point = {**p, **{key: p[key] * fraction for key in energy_keys},
                     'planned_ev_kwh': p.get('planned_ev_kwh', 0) * fraction,
                     'ts': stamp(a), 'hours': h, 'lead_minutes': int((a - start).total_seconds() // 60),
                     'solar_by_system': {k: v * fraction for k, v in p['solar_by_system'].items()},
                     'charge_allowed': not any(lo <= a < hi for lo, hi in charge_blocks),
                     'export_allowed': not any(lo <= a < hi for lo, hi in export_blocks)}
            result.append(point)
    return result


class OvernightNeed(NamedTuple):
    energy_kwh: float
    uncertainty_kwh: float
    planned_ev_kwh: float


def overnight_need(points: list[dict], settings: Settings) -> list[OvernightNeed | None]:
    """Remaining net home and planned EV demand and uncertainty through the next local 06:00."""
    need = [None] * len(points)
    ac_limits = {s.id: s.ac_kw for s in settings.active_systems()}
    future = None
    next_start = None
    for i in range(len(points) - 1, -1, -1):
        p = points[i]
        start = datetime.fromisoformat(p['ts'])
        end = start + timedelta(hours=p['hours'])
        local = end.astimezone(settings.zone)
        if (local.hour, local.minute, local.second, local.microsecond) == (6, 0, 0, 0):
            future = OvernightNeed(0.0, 0.0, 0.0)
        elif end != next_start:
            future = None
        need[i] = future
        if future is not None:
            solar = sum(min(kwh, ac_limits[sid] * p['hours']) for sid, kwh in p['solar_by_system'].items() if sid in ac_limits)
            ev = p.get('planned_ev_kwh', 0)
            central = max(0.0, p['house_kwh'] + ev - solar)
            upper = max(0.0, p['house_p90'] + ev - solar)
            future = OvernightNeed(future.energy_kwh + central, future.uncertainty_kwh + 0.5 * max(0.0, upper - central),
                                   future.planned_ev_kwh + ev)
        next_start = start
    return need
