"""Owner-facing advice derived from the latest plan: when extra load is cheap.

The *marginal cost* of using one more kWh in an interval is estimated from the
planned energy flows, cheapest source first:

1. PV the plan expects to curtail (free),
2. planned exports (you lose the export rate),
3. grid import at that interval's price (free while an allowance remains).

That drives the EV charging window, the "good time to run the AC" signal (also
published to Home Assistant) and clipping alerts.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from .config import Settings, Vehicle, stamp, utcnow
from .db import Database, decode
from .ev_charging import charge_rate, planned_window, session_estimate, session_soc
from .ev_schedule import grid_only_cost, home_battery
from .optimizer import allowance_used, dispatch_policy, price_points
from .vehicle_usage import daily_drain, midnight, project


def plan_intervals(db: Database, decision_id: int) -> list[dict]:
    rows = db.rows('SELECT di.*, fp.temperature, fp.radiation, fp.ev_kwh * di.hours / fp.hours AS planned_ev_kwh FROM decision_intervals di JOIN decisions d ON d.id=di.decision_id '
                   'LEFT JOIN forecast_points fp ON fp.forecast_id=d.forecast_id AND fp.ts=('
                   'SELECT ts FROM forecast_points WHERE forecast_id=d.forecast_id AND ts<=di.ts ORDER BY ts DESC LIMIT 1) WHERE di.decision_id=? ORDER BY di.ts',
                   (decision_id,))
    for r in rows:
        r['systems'] = decode(r['systems'])
    return rows


def marginal_cost(row: dict, extra_kwh: float) -> float:
    """AUD for consuming ``extra_kwh`` more energy in this planned interval."""
    remaining = extra_kwh
    cost = 0.0
    take = min(remaining, row['curtailed_kwh'])
    remaining -= take
    take = min(remaining, row['export_kwh'])
    cost += take * row['sell_rate']
    remaining -= take
    cost += remaining * row['buy_rate']
    return cost


def charging_preview(db: Database, settings: Settings, vehicle: Vehicle, now: datetime, until: datetime, data: dict,
                     starts: str, decision: dict | None) -> dict:
    """Estimate charge delivered, and the paid part of grid import during it."""
    rate, basis = charge_rate(db, vehicle, now)
    soc = session_soc(db, vehicle, now, data, starts, rate)
    estimate = session_estimate(vehicle, rate, now, until, data.get('target_pct'), soc)
    finish = estimate['finish']
    result = {**estimate, 'finish': stamp(finish), 'soc': soc, 'kw': round(rate, 2), 'rate_basis': basis,
              'paid_grid_kwh': None, 'paid_grid_aud': None}
    if data.get('charge_mode') == 'percentage' and soc is None:
        return {**result, 'energy_kwh': 0}
    if estimate['energy_kwh'] == 0:
        return {**result, 'paid_grid_kwh': 0, 'paid_grid_aud': 0}
    grid_only = not any(s.pv_kwp > 0 or s.battery for s in settings.active_systems())
    from .plans import reviewed_at
    checked = reviewed_at(db, decision)
    if (not decision or not checked or not timedelta(0) <= now - datetime.fromisoformat(checked) <= timedelta(minutes=35)
            or decision['summary'].get('dispatch_policy') != dispatch_policy(settings)):
        return {**result, **grid_only_cost(db, settings, now, finish, rate)} if grid_only else result
    rows = plan_intervals(db, decision['id'])
    prices = price_points(settings, rows)
    remaining = {(entry['day'], entry['plan'], entry['index']): entry['kwh']
                 for entry in decision['summary'].get('allowance_remaining', [])}
    used = allowance_used(db, settings, now) if remaining else {}
    batteries = home_battery(db, settings, now, finish, rows)
    stored = sum(kwh for _, kwh in batteries)
    paid = cost = covered = solar_total = battery_total = 0.0
    pv = any(s.pv_kwp > 0 for s in settings.active_systems())
    for r, price in zip(rows, prices):
        at = datetime.fromisoformat(r['ts'])
        end = at + timedelta(hours=r['hours'])
        seconds = (min(finish, end) - max(at, now)).total_seconds()
        if seconds <= 0:
            continue
        planned_scale = seconds / (3600 * r['hours'])
        energy = rate * seconds / 3600
        included = min(energy, (r.get('planned_ev_kwh') or 0) * planned_scale)
        other_load = max(0, r['import_kwh'] * planned_scale - included)
        planned_grid = min(included, r['import_kwh'] * planned_scale)
        surplus = (r['export_kwh'] + r['curtailed_kwh']) * planned_scale
        extra_grid = max(0, energy - included - surplus)
        if pv:
            solar_total += min(max(0, energy - included), max(0, surplus - r.get('discharge_kwh', 0) * planned_scale))
        if stored and r.get('action') == 'self_use' and r['import_kwh'] < .01:
            spare = 0.0
            for system, _ in batteries:
                flow = r['systems'].get(system.id, {})
                if flow.get('pv_bat', 0) + flow.get('grid_bat', 0) < .001:
                    headroom = min(system.battery.discharge_kw * r['hours'], system.ac_kw * r['hours'] - flow.get('pv_ac', 0))
                    spare += max(0, headroom - flow.get('dis', 0)) * planned_scale * .8
            battery = min(extra_grid, spare, stored)
            extra_grid -= battery
            stored -= battery
            battery_total += battery
        ev_grid = planned_grid + extra_grid
        tiers = price['imports']
        for tier in tiers:
            if tier['group'] is not None:
                key = tier['group']
                measured_remaining = max(0, tier['limit'] - used.get(key, 0))
                budget = min(remaining.get(key, tier['limit']), measured_remaining)
                other_free = min(other_load, budget)
                other_load -= other_free
                free_for_car = min(ev_grid, budget - other_free)
                remaining[key] = budget - other_free - free_for_car
                assigned = free_for_car
            else:
                assigned = ev_grid
            ev_grid -= assigned
            if tier['rate'] > 0:
                paid += assigned
                cost += assigned * tier['rate']
        covered += seconds
    if abs(covered - (finish - now).total_seconds()) <= 1:
        result.update(paid_grid_kwh=round(paid, 2), paid_grid_aud=round(cost, 2),
                      solar_kwh=round(solar_total, 2), battery_kwh=round(battery_total, 2))
    elif grid_only:
        result.update(grid_only_cost(db, settings, now, finish, rate))
    return result


def charge_window(rows, now, deadline, need, charger_kw, unavailable):
    """Choose the cheapest continuous window, with a partial top-up if time is short."""
    slots = []
    for row in rows:
        at = datetime.fromisoformat(row['ts'])
        start, end = max(at, now), min(at + timedelta(hours=row['hours']), deadline)
        if end <= start or any(a < end and b > start for a, b in unavailable):
            continue
        slots.append((start, end, row))
    best = None
    for i, (start, _, _) in enumerate(slots):
        remaining, cost, end = need, 0.0, start
        for at, stop, row in slots[i:]:
            if at != end:
                break
            hours = min((stop - at).total_seconds() / 3600, remaining / charger_kw)
            scale = hours / row['hours']
            supplies = {**row, 'curtailed_kwh': row['curtailed_kwh'] * scale,
                        'export_kwh': row['export_kwh'] * scale}
            energy = hours * charger_kw
            cost += marginal_cost(supplies, energy)
            remaining -= energy
            end = stop if hours == (stop - at).total_seconds() / 3600 else at + timedelta(hours=hours)
            if remaining < 1e-6:
                break
        delivered = need - remaining
        score = (-round(delivered, 6), round(cost, 2), start)
        if best is None or score < best[0]:
            best = (score, start, end, delivered, cost)
    return best[1:] if best else None


def ev_advice(db: Database, settings: Settings, rows: list[dict], vehicle: Vehicle, now: datetime,
              unavailable: list[tuple[datetime, datetime]], scheduled: list[dict] | None = None) -> dict:
    latest = db.latest_vehicle(vehicle.id)
    fresh = (latest and latest['plugin'] == vehicle.plugin
             and timedelta(0) <= now - datetime.fromisoformat(latest['ts']) < timedelta(hours=6))
    live_soc = latest['soc'] if fresh else None
    soc = live_soc if live_soc is not None else vehicle.manual_soc
    # Presence expires faster than battery percentage.
    present = fresh and now - datetime.fromisoformat(latest['ts']) < timedelta(minutes=30)
    at_home = latest['at_home'] if present else None
    history = daily_drain(db, vehicle, now, settings.zone) if soc is not None else {}
    charge_by_day = defaultdict(float)
    upcoming = []
    if scheduled:
        rate, _ = charge_rate(db, vehicle, now)
        for row in scheduled:
            start, end = planned_window(vehicle, rate, row)
            cursor = max(now, start)
            while cursor < end:
                day = cursor.astimezone(settings.zone).date()
                boundary = min(end, midnight(day + timedelta(days=1), settings.zone))
                if not any(a < boundary and cursor < b for a, b in unavailable):
                    wall_kwh = (boundary - cursor).total_seconds() * rate / 3600
                    charge_by_day[day] += wall_kwh * vehicle.charger_efficiency / vehicle.capacity_kwh * 100
                    upcoming.append((cursor, boundary, rate))
                cursor = boundary
    projection = project(soc, now, settings.zone, vehicle, history, charge_by_day)
    result = dict(vehicle=vehicle.id, name=vehicle.name, soc=soc, at_home=at_home, reminder_soc_pct=vehicle.reminder_soc_pct,
                  projection=projection, need_kwh=0, recommendation=None, suggested_day=None)
    if (soc is None or at_home == 0 or (fresh and latest['charging'])
            or any(start <= now < end for start, end in unavailable)):
        return result

    floor = vehicle.reminder_soc_pct
    low = soc <= floor
    crossing = next((p for p in projection if any(value is not None and value <= floor
                     for value in (p['soc_pct'], p['soc_upper_pct']))), None)
    if not low and crossing is None:
        return result
    result['suggested_day'] = now.astimezone(settings.zone).date().isoformat() if low else crossing['day']
    need = (vehicle.target_pct - soc) / 100 * vehicle.capacity_kwh / vehicle.charger_efficiency
    if need < 0.5:
        return result

    if low:
        deadline = now + timedelta(hours=24)
        reason = f'Battery at {soc:.0f}% (reminder level {floor:.0f}%).'
    else:
        day = datetime.fromisoformat(crossing['day']).date()
        deadline = midnight(day, settings.zone)
        if deadline <= now:
            end_today = midnight(day + timedelta(days=1), settings.zone)
            deadline = now + (end_today - now) * ((soc - floor) / crossing['drain_pct'])
        basis = 'Estimated' if any(p['source'] == 'estimate' for p in projection if p['day'] <= crossing['day']) else 'Likely'
        if crossing['soc_pct'] is None:
            reason = f'Could reach {crossing["soc_upper_pct"]:.0f}% or lower by {day:%a} evening.'
        else:
            reason = f'{basis} to reach {crossing["soc_pct"]:.0f}% by {day:%a} evening.'
    planned_kwh = sum(max(0, (min(end, deadline) - start).total_seconds()) * rate / 3600
                      for start, end, rate in upcoming)
    if planned_kwh >= need - 0.5:
        result['suggested_day'] = None
        return result
    if not rows:
        return result
    last = datetime.fromisoformat(rows[-1]['ts']) + timedelta(hours=rows[-1]['hours'])
    if deadline > last and not low:
        return result
    unavailable = unavailable + [(start, end) for start, end, _ in upcoming]
    window = charge_window(rows, now, deadline, need - planned_kwh, vehicle.charger_kw, unavailable)
    if window is None:
        return result
    start, end, energy, cost = window
    if energy < 0.5:
        return result
    local_start = start.astimezone(settings.zone)
    local_end = end.astimezone(settings.zone)
    tomorrow = local_start.date() + timedelta(days=1)
    tomorrow_rows = [r for r in rows if datetime.fromisoformat(r['ts']).astimezone(settings.zone).date() == tomorrow]
    if (crossing and crossing['day'] == tomorrow.isoformat()
            and sum(r['hours'] for r in tomorrow_rows if r.get('radiation') is not None and
                    8 <= datetime.fromisoformat(r['ts']).astimezone(settings.zone).hour < 16) >= 7.5
            and sum(r['curtailed_kwh'] + r['export_kwh'] for r in tomorrow_rows) < energy / 2):
        reason = reason.removesuffix('.') + ', with little spare solar forecast that day.'
    action = 'Charge' if energy >= need - planned_kwh - 1e-6 else 'Top up'
    if at_home != 1:
        action = f'If home, {action.lower()}'
    when = f'{local_start:%a %-I:%M %p}–{local_end:%-I:%M %p}'
    if local_end.date() != local_start.date():
        when = f'{local_start:%a %-I:%M %p}–{local_end:%a %-I:%M %p}'
    return {**result, 'need_kwh': round(need, 1),
            'recommendation': dict(start=stamp(start), end=stamp(end), cost_aud=round(cost, 2),
                                   energy_kwh=round(energy, 1), cents_per_kwh=round(cost / energy * 100, 1), reason=reason,
                                   text=f'{action} {when}. {reason}')}


def climate_signal(settings: Settings, rows: list[dict], now: datetime, house_away: bool) -> dict:
    """Should Home Assistant pre-cool / pre-heat now? Advisory signal only; HA decides."""
    a = settings.alerts
    signal = dict(available=False, mode=None, reason='', valid_until=stamp(now + timedelta(minutes=15)),
                  duration_minutes=0, estimated_cost_aud=None, power_kw=settings.site.ac_kw, cents_per_kwh=None)
    if house_away:
        return {**signal, 'reason': 'House is marked as away'}
    ahead = [r for r in rows if now - timedelta(minutes=30) <= datetime.fromisoformat(r['ts']) < now + timedelta(hours=8)]
    temps = [r['temperature'] for r in ahead if r.get('temperature') is not None]
    if not ahead or not temps:
        return {**signal, 'reason': 'No current plan with weather'}
    hot, cold = max(temps) >= a.ac_hot_c, min(temps) <= a.ac_cold_c
    if not hot and not cold:
        return {**signal, 'reason': f'Forecast {min(temps):.0f}–{max(temps):.0f}°C is inside your comfort range'}
    mode = 'cool' if hot else 'heat'
    duration, cost = 0, 0.0
    for r in ahead:
        if datetime.fromisoformat(r['ts']) + timedelta(hours=r['hours']) <= now:
            continue
        energy = settings.site.ac_kw * r['hours']
        c = marginal_cost(r, energy)
        if c / energy > a.ac_max_cents / 100 or duration >= a.ac_duration_minutes:
            break
        duration += round(r['hours'] * 60)
        cost += c
    if duration < 20:
        current = ahead[0]
        rate = marginal_cost(current, settings.site.ac_kw * current['hours']) / (settings.site.ac_kw * current['hours'])
        return {**signal, 'mode': mode, 'reason': f'{"Hot" if hot else "Cold"} weather ahead, but power now costs about {rate * 100:.0f}c/kWh'}
    duration = min(duration, a.ac_duration_minutes)
    energy = settings.site.ac_kw * duration / 60
    peak = max(temps) if hot else min(temps)
    return {**signal, 'available': True, 'mode': mode, 'duration_minutes': duration,
            'valid_until': stamp(now + timedelta(minutes=duration)), 'estimated_cost_aud': round(cost, 2),
            'cents_per_kwh': round(cost / energy * 100, 1) if energy else 0,
            'reason': f'{"Up to" if hot else "Down to"} {peak:.0f}°C forecast and spare solar/cheap power now: '
                      f'{mode}ing for {duration} min costs about ${cost:.2f}.'}


def clipping_today(settings: Settings, rows: list[dict], now: datetime) -> dict:
    today = now.astimezone(settings.zone).date()
    todays = [r for r in rows if datetime.fromisoformat(r['ts']).astimezone(settings.zone).date() == today]
    return dict(planned_kwh=round(sum(r['curtailed_kwh'] for r in todays), 2),
                self_use_kwh=round(sum(r['baseline_curtailed_kwh'] for r in todays), 2),
                peak_solar_kw=round(max((r['solar_kwh'] / r['hours'] for r in todays if r['hours'] > 0.2), default=0), 2))


def suggested_schedule(settings: Settings, rows: list[dict]) -> list[dict]:
    """Merge consecutive intervals with the same action: a schedule the owner can copy into the inverter app."""
    out = []
    for r in rows:
        start = datetime.fromisoformat(r['ts'])
        end = start + timedelta(hours=r['hours'])
        if out and out[-1]['action'] == r['action'] and out[-1]['end'] == stamp(start):
            out[-1]['end'] = stamp(end)
            out[-1]['soc_end'] = r['soc_pct']
        else:
            out.append(dict(action=r['action'], fox_mode=r['fox_mode'], start=stamp(start), end=stamp(end), soc_end=r['soc_pct'], reason=r['reason']))
    return out


def build(db: Database, settings: Settings, decision_id: int | None, now: datetime | None = None) -> dict:
    now = now or utcnow()
    rows = plan_intervals(db, decision_id) if decision_id is not None else []
    away = db.one('SELECT 1 FROM holidays WHERE start<=? AND end>?', (stamp(now), stamp(now))) is not None
    holidays = db.rows('SELECT start, end FROM holidays WHERE ev_away AND end>?', (stamp(now),))
    overrides = db.rows("SELECT starts AS start, expires AS end, data FROM overrides WHERE kind='ev_away' "
                        'AND cancelled_at IS NULL AND expires>?', (stamp(now),))
    scheduled = defaultdict(list)
    for row in db.rows("SELECT id, starts, expires, data FROM overrides WHERE kind='ev_charge_planned' AND cancelled_at IS NULL AND expires>? AND starts<? ORDER BY starts",
                       (stamp(now), stamp(now + timedelta(days=7)))):
        scheduled[decode(row['data']).get('vehicle_id')].append(row)
    vehicles = []
    for v in settings.active_vehicles():
        away_periods = holidays + [o for o in overrides if decode(o['data']).get('vehicle_id') in (None, v.id)]
        unavailable = [(datetime.fromisoformat(p['start']), datetime.fromisoformat(p['end'])) for p in away_periods]
        vehicles.append(ev_advice(db, settings, rows, v, now, unavailable, scheduled.get(v.id)))
    if decision_id is None:
        return dict(vehicles=vehicles, climate=None, clipping=None, schedule=[])
    return dict(vehicles=vehicles, climate=climate_signal(settings, rows, now, away), clipping=clipping_today(settings, rows, now),
                schedule=suggested_schedule(settings, [r for r in rows if datetime.fromisoformat(r['ts']) < now + timedelta(hours=24)]))
