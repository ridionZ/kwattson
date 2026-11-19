"""One-session EV charging estimates and optional stopping."""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median

from .config import Settings, Vehicle, stamp
from .db import Database, decode, encode
from .plugins import registry

LIVE_WINDOW = timedelta(minutes=25)
END_GRACE = timedelta(minutes=30)


def can_stop(vehicle: Vehicle) -> bool:
    if vehicle.plugin_config.get('allow_charge_stop') is not True and not can_schedule(vehicle):
        return False
    try:
        return registry.get(vehicle.plugin, 'vehicle').supports_charge_stop
    except ValueError:
        return False


def can_schedule(vehicle: Vehicle) -> bool:
    if vehicle.plugin_config.get('allow_charge_schedule') is not True:
        return False
    try:
        plugin = registry.get(vehicle.plugin, 'vehicle')
        return plugin.supports_charge_schedule and plugin.supports_charge_stop
    except ValueError:
        return False


def recent_reading(db: Database, vehicle: Vehicle, now: datetime, max_age: timedelta = LIVE_WINDOW) -> dict | None:
    row = db.latest_vehicle(vehicle.id)
    if row and row['plugin'] == vehicle.plugin and timedelta(0) <= now - datetime.fromisoformat(row['ts']) <= max_age:
        return row
    return None


def charge_rate(db: Database, vehicle: Vehicle, now: datetime) -> tuple[float, str]:
    """Estimate AC energy at the wall from home-like charge sessions."""
    latest = recent_reading(db, vehicle, now)
    if latest and latest['charging'] and latest['at_home'] != 0 and latest['charge_kw']:
        if 0.2 <= latest['charge_kw'] <= vehicle.charger_kw * 1.35:
            return latest['charge_kw'], 'measured'

    samples = db.rows(
        'SELECT ts, soc, charging, at_home FROM vehicle_readings WHERE vehicle_id=? AND plugin=? AND ts>=? AND ts<=? ORDER BY ts',
        (vehicle.id, vehicle.plugin, stamp(now - timedelta(days=30)), stamp(now)),
    )
    rates = []
    first = previous = None
    for r in samples:
        if not r['charging'] or r['at_home'] == 0 or r['soc'] is None:
            first = previous = None
            continue
        at = datetime.fromisoformat(r['ts'])
        if previous is None or at - previous > LIVE_WINDOW:
            first = (at, r['soc'])
        previous = at
        hours = (at - first[0]).total_seconds() / 3600
        if hours >= 1 / 3:
            kw = (r['soc'] - first[1]) / 100 * vehicle.capacity_kwh / vehicle.charger_efficiency / hours
            if 0.2 <= kw <= vehicle.charger_kw * 1.35:
                rates.append(kw)
            first = (at, r['soc'])
    if rates:
        return float(median(rates)), 'learned'
    return vehicle.charger_kw, 'configured'


def session_estimate(vehicle: Vehicle, rate: float, now: datetime, until: datetime,
                     target: float | None = None, soc: float | None = None) -> dict:
    capacity = vehicle.capacity_kwh / vehicle.charger_efficiency
    want = (max(0, (target - soc) / 100 * capacity) if target is not None and soc is not None else None)
    possible = max(0, (until - now).total_seconds()) / 3600 * rate
    room = max(0, (100 - soc) / 100 * capacity) if soc is not None else capacity
    energy = min(possible, room, want if want is not None else possible)
    finish = now + timedelta(hours=energy / rate)
    reached = want is not None and energy >= want - 1e-6
    end_soc = min(100, soc + energy / capacity * 100) if soc is not None else None
    return dict(energy_kwh=energy, finish=finish, target_reachable=reached if want is not None else None, end_soc=end_soc)


def session_soc(db: Database, vehicle: Vehicle, now: datetime, data: dict, starts: str, rate: float) -> float | None:
    live = recent_reading(db, vehicle, now, timedelta(hours=1))
    if live and live['soc'] is not None:
        return live['soc']
    initial = data.get('starting_soc', vehicle.manual_soc)
    if initial is None:
        return None
    elapsed = max(0, (now - datetime.fromisoformat(starts)).total_seconds()) / 3600
    return min(100, initial + elapsed * rate * vehicle.charger_efficiency / vehicle.capacity_kwh * 100)


def planned_window(vehicle: Vehicle, rate: float, row: dict) -> tuple[datetime, datetime]:
    """Expected charging interval, capped at the energy for a full charge from empty."""
    start = datetime.fromisoformat(row['starts'])
    full_hours = vehicle.capacity_kwh / vehicle.charger_efficiency / rate
    duration = datetime.fromisoformat(row['expires']) - start
    return start, start + min(duration, timedelta(hours=full_hours))


def sessions(db: Database, now: datetime) -> list[dict]:
    rows = db.rows("SELECT * FROM overrides WHERE kind='ev_charge_now' AND cancelled_at IS NULL "
                   "AND starts<=? AND (expires>? OR (expires>? AND json_extract(data,'$.state')='stopping')) "
                   "ORDER BY id DESC", (stamp(now), stamp(now - END_GRACE), stamp(now - 2 * END_GRACE)))
    return [{**r, 'data': decode(r['data'])} for r in rows]


async def monitor(db: Database, settings: Settings, devices, notify, now: datetime) -> bool:
    """Evaluate limits, and confirm a stop with a subsequent charging reading."""
    from .ev_schedule import run_schedule

    active = sessions(db, now)
    if not active:
        return False
    changed = False
    seen = set()
    away_all = db.one('SELECT 1 FROM holidays WHERE ev_away AND start<=? AND end>?', (stamp(now), stamp(now)))
    away_vehicles = {decode(r['data']).get('vehicle_id') for r in db.rows(
        "SELECT data FROM overrides WHERE kind='ev_away' AND cancelled_at IS NULL AND starts<=? AND expires>?",
        (stamp(now), stamp(now)))}
    vehicles = {v.id: v for v in settings.active_vehicles()}
    for session in active:
        data = session['data']
        sid = data.get('vehicle_id')
        if sid in seen or sid not in vehicles:
            continue
        seen.add(sid)
        v = vehicles[sid]
        state = data.get('state')
        if state in ('finished', 'confirmed', 'unconfirmed'):
            continue
        away = away_all or None in away_vehicles or sid in away_vehicles
        if data.get('charge_mode') == 'ready_by':
            changed |= await run_schedule(db, settings, v, session, devices, notify, now, away)
            continue
        live = recent_reading(db, v, now)
        if live and live['ts'] < session['starts']:
            live = None
        deadline = datetime.fromisoformat(session['expires'])
        target = data.get('target_pct')
        limit = 'time limit' if now >= deadline and data.get('charge_mode') != 'percentage' else None
        if target is not None and live and live['soc'] is not None and live['soc'] >= target:
            limit = f'{target:g}% target'

        message = None
        if state == 'stopping':
            requested = datetime.fromisoformat(data['stop_requested_at'])
            if live and live['ts'] > data['stop_requested_at'] and live['charging'] == 0:
                data['state'] = 'confirmed'
                message = 'Charging stopped and the car confirmed it.'
            elif now - requested >= LIVE_WINDOW:
                data['state'] = 'unconfirmed'
                message = 'The car hasn’t confirmed that charging stopped. Check the car or charger.'
            else:
                continue
        elif data.get('charge_mode') == 'percentage' and now >= deadline and not limit:
            data['state'] = 'unconfirmed'
            message = 'Charge monitoring expired before the target was confirmed. Check the car or charger.'
        elif away or (live and (
                live['at_home'] == 0 or live['plugged_in'] == 0
                or (data.get('observed_charge') and live['charging'] == 0 and not limit))):
            data['state'] = 'finished'
            message = 'Charging plan ended because the car left, disconnected or stopped charging.'
        elif not limit:
            if data.get('stop_at_limit') and live and live['charging'] == 1 and not data.get('observed_charge'):
                data['observed_charge'] = True
                db.execute('UPDATE overrides SET data=? WHERE id=?', (encode(data), session['id']))
            continue
        elif live and live['charging'] == 0:
            data['state'] = 'finished'
            message = f'{limit.capitalize()} reached. The car is already stopped.'
        elif data.get('stop_at_limit') and can_stop(v) and live and live['charging'] == 1 and live['at_home'] != 0 and live['plugged_in'] != 0:
            # Record the attempt before sending it. A worker restart must not repeat an uncertain write.
            data.update(state='stopping', stop_requested_at=stamp(now), stop_reason=limit)
            db.execute('UPDATE overrides SET data=? WHERE id=?', (encode(data), session['id']))
            try:
                await devices.vehicle(settings, sid).stop_charging()
            except Exception:
                data['state'] = 'unconfirmed'
                message = 'The charging stop could not be confirmed. Check the car or charger.'
            changed = True
        else:
            data['state'] = 'finished' if not data.get('stop_at_limit') else 'unconfirmed'
            message = f'{limit.capitalize()} reached. Check the car or charger to stop charging.'

        if message:
            db.execute('UPDATE overrides SET data=? WHERE id=?', (encode(data), session['id']))
            notify.emit('ev_charge_session', {'vehicle': v.name, 'reason': message}, key=f'ev-session:{session["id"]}', now=now)
            changed = True
    return changed
