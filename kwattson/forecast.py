"""Explainable 48-hour forecast of household load, EV charging and solar.

Every forecast is stored (forecasts + forecast_points) before the optimiser
uses it, together with the fitted model parameters, so its accuracy can be
scored later against what actually happened (see evaluate()).

Load model  (version LOAD_VERSION)
    house(slot) = profile[daytype, half-hour] + b_cool*(CDD - CDD_ref) + b_heat*(HDD - HDD_ref)
    - profile: recency-weighted, winsorised mean of observed house load (EV
      removed) for the same weekday, falling back to weekday/weekend, then any day.
    - CDD/HDD: degrees above cooling base / below heating base at that hour.
      Coefficients are least-squares fits on residuals, clipped to [0, 1.5]
      kW/°C, and only used with enough hot/cold examples.
    - p10/p90: empirical residual quantiles of matching slots.
    - Holidays replace the profile with a base load (or scale it).

Solar model (version SOLAR_VERSION), per system
    pv(hour) = ratio[local hour] * forecast shortwave radiation (W/m²), capped at
    the system's DC PV limit. ratio is the median observed PV/radiation over the
    last 45 days for that hour, excluding hours where output was limited
    (inverter at AC limit with battery full). It implicitly learns orientation,
    shading and losses. Without history a declared default ratio is used.

Never trains on excluded periods, holidays or forecast values.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

import numpy as np

from .config import Settings, stamp, utcnow
from .weather import freshness_hours
from .db import Database, decode, encode
from .ingest import select_intervals

LOAD_VERSION = 'profile-temperature-v2'
SOLAR_VERSION = 'hourly-ratio-v2'
VERSION = f'{LOAD_VERSION}+{SOLAR_VERSION}'
COLD_START_SHAPE = [.45, .35, .3, .3, .3, .35, .75, 1.15, .85, .65, .55, .6, .65, .65, .65, .7, .9, 1.25, 1.6, 1.5, 1.1, .85, .65, .5]


def _in(periods, at: str) -> bool:
    return any(p['start'] <= at < p['end'] for p in periods)


def _weather(db: Database, start: datetime, end: datetime) -> dict[str, dict]:
    rows = db.rows('SELECT * FROM weather_hours WHERE ts>=? AND ts<=?', (stamp(start - timedelta(hours=1)), stamp(end + timedelta(hours=1))))
    return {r['ts']: r for r in rows}


def _hour_key(at: datetime) -> str:
    """Open-Meteo values are averages over the hour *ending* at the timestamp."""
    ceiling = at.replace(minute=0, second=0, microsecond=0)
    if at > ceiling:
        ceiling += timedelta(hours=1)
    return stamp(ceiling)


def _degree(temp, settings: Settings):
    if temp is None:
        return None, None
    return max(0.0, temp - settings.forecast.cooling_base_c), max(0.0, settings.forecast.heating_base_c - temp)


# --------------------------------------------------------------------------- load
def fit_load(db: Database, settings: Settings, now: datetime) -> dict:
    zone = settings.zone
    start = now - timedelta(days=settings.forecast.history_days)
    rows = select_intervals(db, stamp(start), stamp(now))
    excluded = db.rows('SELECT start, end FROM exclusions') + db.rows('SELECT start, end FROM holidays')
    weather = _weather(db, start, now)
    samples = []
    for r in rows:
        if r['coverage_s'] < 1350 or _in(excluded, r['ts']):
            continue
        at = datetime.fromisoformat(r['ts'])
        hours = r['coverage_s'] / 3600
        house_kw = max(0.0, r['load_kwh'] - r['ev_kwh']) / hours
        local = at.astimezone(zone)
        w = weather.get(_hour_key(at + timedelta(minutes=15)))
        cdd, hdd = _degree(w['temperature'] if w else None, settings)
        age = (now - at).total_seconds() / 86400
        samples.append(dict(weekday=local.weekday(), slot=local.hour * 2 + local.minute // 30, kw=house_kw,
                            weight=0.5 ** (age / settings.forecast.half_life_days), cdd=cdd, hdd=hdd))

    def profile_for(items):
        values = np.array([s['kw'] for s in items])
        weights = np.array([s['weight'] for s in items])
        low, high = np.quantile(values, [0.1, 0.9]) if len(values) >= 5 else (values.min(), values.max())
        return float(np.average(np.clip(values, low, high), weights=weights))

    groups = {'weekday': defaultdict(list), 'daytype': defaultdict(list), 'any': defaultdict(list)}
    for s in samples:
        groups['weekday'][(s['weekday'], s['slot'])].append(s)
        groups['daytype'][(s['weekday'] >= 5, s['slot'])].append(s)
        groups['any'][s['slot']].append(s)
    profiles = {name: {k: profile_for(v) for k, v in g.items()} for name, g in groups.items()}
    counts = {name: {k: len(v) for k, v in g.items()} for name, g in groups.items()}

    # Temperature response on residuals from the same-daytype profile.
    residual, cool, heat = [], [], []
    for s in samples:
        if s['cdd'] is None:
            continue
        residual.append(s['kw'] - profiles['daytype'][(s['weekday'] >= 5, s['slot'])])
        cool.append(s['cdd'])
        heat.append(s['hdd'])
    b_cool = b_heat = 0.0
    hot = sum(1 for c in cool if c > 2)
    cold = sum(1 for h in heat if h > 2)
    if len(residual) >= 14 * 48:
        y = np.array(residual)
        columns = [np.array(cool) - np.mean(cool)] if hot >= 40 else []
        columns += [np.array(heat) - np.mean(heat)] if cold >= 40 else []
        if columns:
            x = np.column_stack(columns)
            coef, *_ = np.linalg.lstsq(x, y - y.mean(), rcond=None)
            coef = [float(np.clip(c, 0, 1.5)) for c in coef]
            if hot >= 40:
                b_cool = coef.pop(0)
            if cold >= 40:
                b_heat = coef.pop(0)
    ref_cool = float(np.mean(cool)) if cool else 0.0
    ref_heat = float(np.mean(heat)) if heat else 0.0

    # Residual spread per slot (for p10/p90 bands).
    spread = defaultdict(list)
    for s in samples:
        spread[s['slot']].append(s['kw'] - profiles['daytype'][(s['weekday'] >= 5, s['slot'])])
    bands = {slot: (float(np.quantile(v, 0.1)), float(np.quantile(v, 0.9))) for slot, v in spread.items() if len(v) >= 6}

    daily = [decode(r['data']).get('house_kwh') for r in db.rows("SELECT data FROM daily_history WHERE quality='observed'")]
    daily = [d for d in daily if d]
    params = dict(version=LOAD_VERSION, samples=len(samples), days=len(samples) / 48, b_cool=b_cool, b_heat=b_heat,
                  ref_cdd=ref_cool, ref_hdd=ref_heat, hot_samples=hot, cold_samples=cold,
                  cold_start_daily_kwh=float(median(daily)) if daily else None,
                  profiles={name: {str(k): v for k, v in p.items()} for name, p in profiles.items()},
                  counts={name: {str(k): v for k, v in c.items()} for name, c in counts.items()},
                  bands={str(k): v for k, v in bands.items()})
    db.execute('INSERT OR REPLACE INTO model_params VALUES(?,?,?,?)', ('load', LOAD_VERSION, stamp(now), encode(params)))
    return params


def predict_house(params, settings: Settings, local: datetime, temperature) -> tuple[float, float, float, str, int]:
    """Returns kW (centre, p10, p90), basis text and sample count."""
    slot = local.hour * 2 + local.minute // 30
    keys = [('weekday', str((local.weekday(), slot)), 3, 'same weekday'),
            ('daytype', str((local.weekday() >= 5, slot)), 4, 'weekend' if local.weekday() >= 5 else 'weekdays'),
            ('any', str(slot), 4, 'all days')]
    for name, key, minimum, label in keys:
        count = params['counts'][name].get(key, 0)
        if count >= minimum:
            centre = params['profiles'][name][key]
            basis, samples = f'{label} profile ({count} samples)', count
            break
    else:
        daily = params.get('cold_start_daily_kwh') or (26 if local.month in (11, 12, 1, 2) else 20)
        centre = daily * COLD_START_SHAPE[local.hour] / sum(COLD_START_SHAPE)
        low, high = centre * 0.6, centre * 1.6
        return centre, low, high, 'cold start: typical daily shape (no history yet)', 0
    cdd, hdd = _degree(temperature, settings)
    if cdd is not None and (params['b_cool'] or params['b_heat']):
        adjust = params['b_cool'] * (cdd - params['ref_cdd']) + params['b_heat'] * (hdd - params['ref_hdd'])
        centre = max(0.05, centre + adjust)
        if abs(adjust) > 0.05:
            basis += f'; weather {adjust:+.2f} kW'
    low_r, high_r = params['bands'].get(str(slot), (-0.4 * centre, 0.6 * centre))
    return centre, max(0.0, centre + low_r), centre + high_r, basis, samples


# -------------------------------------------------------------------------- solar
def default_ratio(system) -> float:
    # kW per W/m²: nameplate × ~0.85 performance at 1000 W/m² horizontal.
    return system.pv_kwp * 0.85 / 1000


def fit_solar(db: Database, settings: Settings, now: datetime) -> dict:
    zone = settings.zone
    start = now - timedelta(days=45)
    weather = _weather(db, start, now)
    excluded = db.rows('SELECT start, end FROM exclusions')
    limited_hours = {_hour_key(datetime.fromisoformat(r['ts'])) for r in
                     db.rows('SELECT ts FROM site_readings WHERE ts>=? AND output_limited=1', (stamp(start),))}
    out = {}
    for system in settings.active_systems():
        if system.pv_kwp <= 0:
            continue
        hourly = defaultdict(list)
        for r in db.rows('SELECT ts, pv_kw, soc FROM system_readings WHERE system_id=? AND ts>=? AND pv_kw IS NOT NULL',
                         (system.id, stamp(start))):
            at = datetime.fromisoformat(r['ts'])
            if _in(excluded, r['ts']):
                continue
            full = system.battery and r['soc'] is not None and r['soc'] >= system.battery.max_soc_pct - 1
            hourly[_hour_key(at)].append((r['pv_kw'], full))
        ratios = defaultdict(list)
        for key, values in hourly.items():
            w = weather.get(key)
            if not w or w['radiation'] is None or w['radiation'] < 50 or len(values) < 6 or key in limited_hours:
                continue
            if any(full for _, full in values):
                continue  # battery full: PV may have been curtailed
            mean_kw = sum(v for v, _ in values) / len(values)
            local_hour = (datetime.fromisoformat(key) - timedelta(minutes=30)).astimezone(zone).hour
            ratios[local_hour].append(mean_kw / w['radiation'])
        all_ratios = [x for v in ratios.values() for x in v]
        fallback = float(np.median(all_ratios)) if len(all_ratios) >= 20 else default_ratio(system)
        upper = system.pv_kwp * 1.3 / 1000
        hours = {}
        for hour in range(24):
            values = ratios.get(hour, [])
            if len(values) >= 5:
                q10, q50, q90 = (float(np.clip(q, 0, upper)) for q in np.quantile(values, [0.1, 0.5, 0.9]))
                hours[str(hour)] = dict(ratio=q50, p10=q10, p90=q90, samples=len(values), basis='learned')
            else:
                hours[str(hour)] = dict(ratio=fallback, p10=fallback * 0.7, p90=min(upper, fallback * 1.15),
                                        samples=len(values), basis='learned (all hours)' if len(all_ratios) >= 20 else 'default')
        out[system.id] = dict(hours=hours, samples=len(all_ratios), fallback=fallback)
    params = dict(version=SOLAR_VERSION, systems=out)
    db.execute('INSERT OR REPLACE INTO model_params VALUES(?,?,?,?)', ('solar', SOLAR_VERSION, stamp(now), encode(params)))
    return params


def clear_sky_shape(local: datetime) -> float:
    hour = local.hour + local.minute / 60
    return max(0.0, math.sin(math.pi * (hour - 6) / 12)) if 6 <= hour <= 18 else 0.0


# ---------------------------------------------------------------------------- EV
def ev_plan(db: Database, settings: Settings, now: datetime, slots: list[tuple[datetime, float]],
            holidays: list[dict], overrides: list[dict]) -> tuple[list[float], dict]:
    """Expected EV charging energy (kWh at the wall) per slot, summed over vehicles."""
    zone = settings.zone
    total = [0.0] * len(slots)
    info = {}
    for vehicle in settings.active_vehicles():
        latest = db.latest_vehicle(vehicle.id)
        fresh = latest and (now - datetime.fromisoformat(latest['ts'])) < timedelta(hours=6)
        soc = latest['soc'] if fresh and latest['soc'] is not None else vehicle.manual_soc
        charging = bool(fresh and latest['charging'])
        away_now = bool(fresh and latest['at_home'] == 0)
        mine = [o for o in overrides if o['data'].get('vehicle_id') in (None, vehicle.id)]
        force = any(o['kind'] == 'ev_charge_now' for o in mine)
        away_until = max((o['expires'] for o in mine if o['kind'] == 'ev_away'), default=None)
        need = lambda: max(0.0, (vehicle.target_pct - soc) / 100 * vehicle.capacity_kwh / vehicle.charger_efficiency) if soc is not None else None
        per_session = vehicle.km_per_week * vehicle.kwh_per_100km / 100 / vehicle.charger_efficiency / max(1, len(vehicle.schedule.days))
        remaining = (need() or per_session) if (charging or force) else 0.0
        first_session = True
        started = set()
        for i, (at, hours) in enumerate(slots):
            local = at.astimezone(zone)
            iso = stamp(at)
            away = (away_now and i < 4) or (away_until and iso < away_until) or any(
                h['ev_away'] and h['start'] <= iso < h['end'] for h in holidays)
            day = local.date()
            if (not remaining and local.weekday() in vehicle.schedule.days and local.hour == vehicle.schedule.start_hour
                    and day not in started and not away):
                started.add(day)
                gap = need() if first_session else None
                remaining = gap if gap is not None else per_session
                first_session = False
            if away or remaining <= 0:
                continue
            energy = min(vehicle.charger_kw * hours, remaining)
            total[i] += energy
            remaining -= energy
        info[vehicle.id] = dict(soc=soc, soc_source='live' if fresh and latest['soc'] is not None else 'manual' if soc is not None else 'unknown',
                                charging=charging, forced=force, per_session_kwh=round(per_session, 2))
    return total, info


# ----------------------------------------------------------------------- forecast
def build(db: Database, settings: Settings, now: datetime | None = None) -> tuple[int, list[dict], dict]:
    now = (now or utcnow()).replace(microsecond=0)
    zone = settings.zone
    horizon = now + timedelta(hours=settings.forecast.horizon_hours)
    load_params = fit_load(db, settings, now)
    solar_params = fit_solar(db, settings, now)
    weather = _weather(db, now, horizon)
    weather_age = freshness_hours(db, now)
    use_weather = settings.site.weather_enabled or db.meta('demo_mode', False)
    weather_ok = use_weather and weather_age is not None and 0 <= weather_age <= 6
    holidays = db.rows('SELECT * FROM holidays WHERE end>? AND start<?', (stamp(now), stamp(horizon)))
    overrides = [{**o, 'data': decode(o['data'])} for o in
                 db.rows('SELECT * FROM overrides WHERE cancelled_at IS NULL AND expires>? AND starts<=?', (stamp(now), stamp(now)))]

    slots = []
    cursor = now
    while cursor < horizon:
        boundary = cursor.replace(minute=0 if cursor.minute < 30 else 30, second=0) + timedelta(minutes=30)
        stop = min(boundary, horizon)
        slots.append((cursor, (stop - cursor).total_seconds() / 3600))
        cursor = stop
    ev, ev_info = ev_plan(db, settings, now, slots, holidays, overrides)

    risk = settings.forecast.risk
    points = []
    for i, (at, hours) in enumerate(slots):
        local = at.astimezone(zone)
        w = weather.get(_hour_key(at + timedelta(minutes=15))) if weather_ok else None
        temperature = w['temperature'] if w else None
        radiation = w['radiation'] if w else None
        centre, low, high, load_basis, samples = predict_house(load_params, settings, local, temperature)
        holiday = next((h for h in holidays if h['start'] <= stamp(at) < h['end']), None)
        if holiday:
            if holiday['load_factor'] is not None:
                centre, low, high = (x * holiday['load_factor'] for x in (centre, low, high))
            else:
                centre = settings.forecast.holiday_base_load_kw
                low, high = centre * 0.7, centre * 1.8
            load_basis = f'holiday "{holiday["name"]}"'
        by_system, solar_low, solar_high, solar_basis = {}, 0.0, 0.0, []
        for system in settings.active_systems():
            if system.pv_kwp <= 0:
                continue
            model = solar_params['systems'][system.id]['hours'][str(local.hour)]
            if radiation is not None:
                kw = min(system.pv_limit_kw, model['ratio'] * radiation)
                lo = min(system.pv_limit_kw, model['p10'] * radiation) * (1 - 0.3 * (w['cloud_cover'] or 0) / 100)
                hi = min(system.pv_limit_kw, model['p90'] * radiation)
                solar_basis.append(f'{system.name}: {model["basis"]} ratio × {radiation:.0f} W/m²')
            else:
                kw = system.pv_kwp * 0.45 * clear_sky_shape(local)
                lo, hi = kw * 0.3, system.pv_kwp * 0.9 * clear_sky_shape(local)
                solar_basis.append(f'{system.name}: no fresh weather; cautious clear-sky shape')
            by_system[system.id] = kw * hours
            solar_low += lo * hours
            solar_high += hi * hours
        solar = sum(by_system.values())
        if risk == 'conservative':
            planned_house = centre + 0.5 * (high - centre)
            scale = 1 - 0.5 * (1 - solar_low / solar) if solar > 0 else 1
        elif risk == 'optimistic':
            planned_house = centre - 0.5 * (centre - low)
            scale = 1 + 0.5 * (solar_high / solar - 1) if solar > 0 else 1
        else:
            planned_house, scale = centre, 1.0
        points.append(dict(
            ts=stamp(at), hours=hours, lead_minutes=int((at - now).total_seconds() // 60),
            house_kwh=centre * hours, house_p10=low * hours, house_p90=high * hours, ev_kwh=ev[i],
            load_kwh=planned_house * hours + ev[i],
            solar_kwh=solar, solar_p10=solar_low, solar_p90=solar_high,
            solar_by_system={k: v * scale for k, v in by_system.items()},
            temperature=temperature, radiation=radiation, load_basis=load_basis,
            solar_basis='; '.join(solar_basis) or 'no PV', samples=samples, holiday=holiday['name'] if holiday else None))

    model = dict(version=VERSION, created_at=stamp(now), risk=risk, weather_fresh=weather_ok,
                 weather_age_hours=None if weather_age is None else round(weather_age, 2),
                 load=dict((k, v) for k, v in load_params.items() if k not in ('profiles', 'counts', 'bands')),
                 solar={k: dict(samples=v['samples'], fallback=v['fallback']) for k, v in solar_params['systems'].items()},
                 vehicles=ev_info, holidays=[h['name'] for h in holidays], overrides=[o['kind'] for o in overrides],
                 confidence='learning' if load_params['days'] < 14 else 'established',
                 note='p10/p90 are empirical ranges from recent history, not calibrated probabilities.')
    with db.connect() as c:
        forecast_id = c.execute('INSERT INTO forecasts(created_at, version, model) VALUES(?,?,?)', (stamp(now), VERSION, encode(model))).lastrowid
        c.executemany('INSERT INTO forecast_points VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', [
            (forecast_id, p['ts'], p['lead_minutes'], p['hours'], p['house_kwh'], p['house_p10'], p['house_p90'], p['ev_kwh'],
             p['solar_kwh'], p['solar_p10'], p['solar_p90'], p['temperature'], p['radiation'], p['load_basis'],
             p['solar_basis'], p['samples'], encode(p['solar_by_system'])) for p in points])
    return forecast_id, points, model


def _elapsed_slots(db: Database, forecast_id: int, now: datetime):
    """Pair each completed, aligned half-hour with its best available observation."""
    points = db.rows('SELECT * FROM forecast_points WHERE forecast_id=? AND lead_minutes<1440 ORDER BY ts', (forecast_id,))
    if points:
        end = min(now, datetime.fromisoformat(points[-1]['ts']) + timedelta(hours=points[-1]['hours']))
        actual = {r['ts']: r for r in select_intervals(db, points[0]['ts'], stamp(end))}
    else:
        actual = {}
    for p in points:
        start = datetime.fromisoformat(p['ts'])
        end = start + timedelta(hours=p['hours'])
        if end <= now and p['hours'] == 0.5:
            observation = actual.get(p['ts'])
            yield p, end, observation if observation and observation['coverage_s'] >= 1500 else None


def comparison(db: Database, forecast_id: int, now: datetime) -> dict:
    decision = db.one('SELECT id, mode, summary FROM decisions WHERE forecast_id=? ORDER BY id LIMIT 1', (forecast_id,))
    planned = {}
    if decision and decode(decision['summary']).get('initial_soc_pct') is not None:
        planned = {stamp(datetime.fromisoformat(r['ts']) + timedelta(hours=r['hours'])): r['soc_pct'] for r in db.rows(
            'SELECT ts, hours, soc_pct FROM decision_intervals WHERE decision_id=?', (decision['id'],))}
    points = []
    for p, end, actual in _elapsed_slots(db, forecast_id, now):
        points.append(dict(end=stamp(end), forecast_solar=p['solar_kwh'], actual_solar=actual['solar_kwh'] if actual else None,
                           forecast_load=p['house_kwh'] + p['ev_kwh'], actual_load=actual['load_kwh'] if actual else None,
                           planned_soc=planned.get(stamp(end)), actual_soc=actual['soc_end'] if actual else None))
    return dict(forecast_id=forecast_id, mode=decision['mode'] if decision else None, points=points)


def score_run(db: Database, forecast_id: int, now: datetime) -> dict:
    house, solar = [], []
    for p, _, a in _elapsed_slots(db, forecast_id, now):
        if not a:
            continue
        house.append(((a['load_kwh'] - a['ev_kwh']), p['house_kwh'], p['house_p10'], p['house_p90']))
        if a['limited_s'] < 60:
            solar.append((a['solar_kwh'], p['solar_kwh']))
    metrics = dict(samples=len(house), solar_samples=len(solar))
    if house:
        err = np.array([a - f for a, f, _, _ in house])
        metrics.update(house_mae_kwh=float(np.abs(err).mean()), house_bias_kwh=float(err.mean()),
                       house_actual_kwh=float(sum(a for a, *_ in house)), house_forecast_kwh=float(sum(f for _, f, *_ in house)),
                       band_coverage=float(np.mean([lo <= a <= hi for a, _, lo, hi in house])))
    if solar:
        err = np.array([a - f for a, f in solar])
        metrics.update(solar_mae_kwh=float(np.abs(err).mean()), solar_bias_kwh=float(err.mean()),
                       solar_actual_kwh=float(sum(a for a, _ in solar)), solar_forecast_kwh=float(sum(f for _, f in solar)))
    return metrics


def evaluate(db: Database, now: datetime | None = None) -> int:
    """Score forecasts whose first 24 hours have passed against observed intervals."""
    now = now or utcnow()
    pending = db.rows('SELECT f.id, f.created_at FROM forecasts f LEFT JOIN evaluations e ON e.forecast_id=f.id '
                      'WHERE e.forecast_id IS NULL AND f.created_at<? ORDER BY f.id LIMIT 50', (stamp(now - timedelta(hours=25)),))
    done = 0
    for f in pending:
        metrics = score_run(db, f['id'], now)
        db.execute('INSERT OR REPLACE INTO evaluations VALUES(?,?,?)', (f['id'], stamp(now), encode(metrics)))
        done += 1
    return done


def solar_snapshot(db: Database, settings: Settings, now: datetime) -> dict:
    run = db.one('SELECT id, created_at FROM forecasts ORDER BY id DESC LIMIT 1')
    if not run or run['created_at'] < db.meta('location_changed_at', ''):
        return {}
    if datetime.fromisoformat(run['created_at']) < now - timedelta(hours=1):
        return {}
    points = db.rows('SELECT ts, hours, solar_kwh FROM forecast_points WHERE forecast_id=? ORDER BY ts', (run['id'],))
    midnight = (now.astimezone(settings.zone) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    def estimate(until):
        seconds = energy = 0.0
        for point in points:
            start = datetime.fromisoformat(point['ts'])
            duration = timedelta(hours=point['hours'])
            overlap = (min(start + duration, until) - max(start, now)).total_seconds()
            if overlap > 0:
                energy += point['solar_kwh'] * overlap / duration.total_seconds()
                seconds += overlap
        return round(energy, 2) if abs(seconds - (until - now).total_seconds()) < 1 else None
    return dict(created_at=run['created_at'], next_hour=estimate(now + timedelta(hours=1)), rest_today=estimate(midnight))
