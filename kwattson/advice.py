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

from datetime import datetime, timedelta

from .config import Settings, stamp, utcnow
from .db import Database, decode


def plan_intervals(db: Database, decision_id: int) -> list[dict]:
    rows = db.rows('SELECT di.*, fp.temperature, fp.radiation FROM decision_intervals di JOIN decisions d ON d.id=di.decision_id '
                   'LEFT JOIN forecast_points fp ON fp.forecast_id=d.forecast_id AND fp.ts=di.ts WHERE di.decision_id=? ORDER BY di.ts',
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


def ev_advice(settings: Settings, rows: list[dict], vehicle, soc: float | None, now: datetime) -> dict:
    """Cheapest continuous charging window in the next 24 h for the energy this car needs."""
    need = max(0.0, (vehicle.target_pct - soc) / 100 * vehicle.capacity_kwh / vehicle.charger_efficiency) if soc is not None else \
        vehicle.km_per_week * vehicle.kwh_per_100km / 100 / vehicle.charger_efficiency / max(1, len(vehicle.schedule.days) or 2)
    upcoming = [r for r in rows if datetime.fromisoformat(r['ts']) < now + timedelta(hours=24)]
    if need < 0.5 or not upcoming:
        return dict(vehicle=vehicle.id, name=vehicle.name, need_kwh=round(need, 1), recommendation=None)
    slots_needed = max(1, round(need / (vehicle.charger_kw * 0.5)))
    best = None
    for start in range(len(upcoming)):
        window = upcoming[start:start + slots_needed]
        if len(window) < slots_needed:
            break
        energy = need
        cost = 0.0
        for r in window:
            e = min(energy, vehicle.charger_kw * r['hours'])
            cost += marginal_cost(r, e)
            energy -= e
        if best is None or cost < best[0] - 0.01:
            best = (cost, window)
    cost, window = best
    now_cost = sum(marginal_cost(r, min(need, vehicle.charger_kw * r['hours'])) for r in upcoming[:slots_needed])
    start = datetime.fromisoformat(window[0]['ts'])
    end = datetime.fromisoformat(window[-1]['ts']) + timedelta(hours=window[-1]['hours'])
    local = lambda t: t.astimezone(settings.zone).strftime('%a %-I:%M %p')
    return dict(vehicle=vehicle.id, name=vehicle.name, need_kwh=round(need, 1), soc=soc,
                recommendation=dict(start=stamp(start), end=stamp(end), cost_aud=round(cost, 2),
                                    cents_per_kwh=round(cost / need * 100, 1), now_cost_aud=round(now_cost, 2),
                                    text=f'Charge {local(start)} – {local(end)}: about ${cost:.2f} for {need:.0f} kWh '
                                         f'({cost / need * 100:.0f}c/kWh) versus ${now_cost:.2f} starting now.'))


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
    if decision_id is None:
        return dict(vehicles=[], climate=None, clipping=None, schedule=[])
    rows = plan_intervals(db, decision_id)
    away = db.one('SELECT 1 FROM holidays WHERE start<=? AND end>?', (stamp(now), stamp(now))) is not None
    vehicles = []
    for v in settings.active_vehicles():
        latest = db.latest_vehicle(v.id)
        fresh = latest and now - datetime.fromisoformat(latest['ts']) < timedelta(hours=6)
        soc = latest['soc'] if fresh and latest['soc'] is not None else v.manual_soc
        vehicles.append(ev_advice(settings, rows, v, soc, now))
    return dict(vehicles=vehicles, climate=climate_signal(settings, rows, now, away), clipping=clipping_today(settings, rows, now),
                schedule=suggested_schedule(settings, [r for r in rows if datetime.fromisoformat(r['ts']) < now + timedelta(hours=24)]))
