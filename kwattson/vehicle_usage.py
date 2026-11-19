"""Learn daily EV battery drain from observed state-of-charge changes."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from statistics import median

from .config import UTC, stamp


def midnight(day, zone):
    return datetime.combine(day, time.min, zone).astimezone(UTC)


def daily_drain(db, vehicle, now, zone):
    today = now.astimezone(zone).date()
    end = midnight(today, zone)
    start = midnight(today - timedelta(weeks=16), zone)
    readings = db.rows(
        'SELECT ts, soc, charging FROM vehicle_readings '
        'WHERE vehicle_id=? AND plugin=? AND ts>=? AND ts<=? AND soc BETWEEN 0 AND 100 ORDER BY ts',
        (vehicle.id, vehicle.plugin, stamp(start - timedelta(hours=12)), stamp(end + timedelta(hours=12))),
    )
    excluded = [(datetime.fromisoformat(r['start']), datetime.fromisoformat(r['end']))
                for r in db.rows('SELECT start, end FROM exclusions WHERE start<? AND end>? UNION ALL '
                                 'SELECT start, end FROM holidays WHERE start<? AND end>?',
                                 (stamp(end), stamp(start), stamp(end), stamp(start)))]
    use = defaultdict(float)
    coverage = defaultdict(float)
    for earlier, later in zip(readings, readings[1:]):
        at, stop = (datetime.fromisoformat(r['ts']) for r in (earlier, later))
        hours = (stop - at).total_seconds() / 3600
        if stop > now or not 0 < hours <= 12:
            continue
        drop = 0 if earlier['charging'] or later['charging'] else max(0, earlier['soc'] - later['soc'])
        cursor = max(start, at)
        while cursor < min(stop, end):
            day = cursor.astimezone(zone).date()
            boundary = min(stop, midnight(day + timedelta(days=1), zone))
            observed = (boundary - cursor).total_seconds() / 3600
            use[day] += drop * observed / hours
            coverage[day] += observed
            cursor = boundary
    daily = {}
    for day, hours in coverage.items():
        begin = midnight(day, zone)
        finish = midnight(day + timedelta(days=1), zone)
        if hours >= 18 and not any(a < finish and b > begin for a, b in excluded):
            daily[day] = use[day]
    return daily


def phase(day, period):
    return ((day.toordinal() - 1) // 7) % period


def fit_cycle(daily):
    """Choose a repeating roster only when it improves held-out prediction."""
    weekdays = defaultdict(list)
    for day, drain in daily.items():
        weekdays[day.weekday()].append((day, drain))
    weekly = {key: median(drain for _, drain in samples)
              for key, samples in weekdays.items() if len(samples) >= 3}
    best_period, best_values, best_improvement = 1, {}, 0.0
    for period in (2, 3, 4):
        values = {}
        improvement = 0.0
        for weekday, samples in weekdays.items():
            phases = [[(day, drain) for day, drain in samples if phase(day, period) == index]
                      for index in range(period)]
            if any(len(group) < 3 for group in phases):
                continue
            baseline_error = cycle_error = 0.0
            for group in phases:
                for day, actual in group:
                    baseline = median(drain for date, drain in samples if date != day)
                    estimate = median(drain for date, drain in group if date != day)
                    baseline_error += abs(actual - baseline)
                    cycle_error += abs(actual - estimate)
            gain = baseline_error - cycle_error
            if gain >= len(samples) and gain >= baseline_error * 0.25:
                for index, group in enumerate(phases):
                    values[weekday, index] = median(drain for _, drain in group)
                improvement += gain
        if improvement > best_improvement:
            best_period, best_values, best_improvement = period, values, improvement
    return weekly, best_period, best_values


def project(soc, now, zone, vehicle, daily, charge_by_day=None):
    """End-of-day SoC for seven local dates, starting with the remaining part of today."""
    today = now.astimezone(zone).date()
    weekly, period, roster = fit_cycle(daily)
    charge_by_day = charge_by_day or {}
    points = []
    gap = False
    for offset in range(7):
        day = today + timedelta(days=offset)
        start = midnight(day, zone)
        end = midnight(day + timedelta(days=1), zone)
        weekday = day.weekday()
        drain = roster.get((weekday, phase(day, period)), weekly.get(weekday))
        source = 'observed' if drain is not None else None
        km = vehicle.weekday_km[weekday]
        if drain is None and km is not None:
            drain = km * vehicle.kwh_per_100km / vehicle.capacity_kwh
            source = 'estimate'
        if drain is not None and offset == 0:
            drain *= (end - now) / (end - start)
        gap |= drain is None
        charge = charge_by_day.get(day, 0.0)
        if soc is not None:
            soc = min(100, max(0, soc - (drain or 0)) + charge)
        points.append(dict(day=day.isoformat(), soc_pct=None if gap else soc,
                           soc_upper_pct=soc if gap and (drain is not None or charge) else None,
                           drain_pct=drain, source=source, planned_charge_pct=round(charge, 1)))
    return points
