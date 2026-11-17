"""Offline replay of past days: what would the optimiser have done, per tariff?

For each past local day with good data coverage, the measured load and solar
are used as a *perfect* forecast and the optimiser is compared with simulated
self-use under each tariff. This shows the upper bound of what planning can
add, and lets you compare tariffs on your own data.

Caveat: measured PV is already curtailed whenever the inverter was at its AC
limit with a full battery. For those intervals the solar model's estimate
(radiation × learned ratio) is used when higher, flagged as ``reconstructed``.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import optimizer
from .config import Settings, stamp, utcnow
from .db import Database, decode
from .ingest import select_intervals
from .outcomes import _split_solar, day_bounds


def _points(db: Database, settings: Settings, day) -> tuple[list[dict], float | None, int]:
    start, end = day_bounds(settings, day)
    rows = select_intervals(db, stamp(start), stamp(end))
    if sum(r['coverage_s'] for r in rows) < 0.9 * 86400:
        return [], None, 0
    solar_model = db.one("SELECT data FROM model_params WHERE name='solar'")
    ratios = decode(solar_model['data'])['systems'] if solar_model else {}
    weather = {r['ts']: r for r in db.rows('SELECT * FROM weather_hours WHERE ts>=? AND ts<=?', (stamp(start), stamp(end + timedelta(hours=1))))}
    points, reconstructed = [], 0
    for i, r in enumerate(rows):
        at = datetime.fromisoformat(r['ts'])
        hours = r['coverage_s'] / 3600
        solar = r['solar_kwh']
        if r['limited_s'] > 0:
            w = weather.get(stamp((at + timedelta(minutes=45)).replace(minute=0)))
            potential = 0.0
            for s in settings.active_systems():
                model = ratios.get(s.id, {}).get('hours', {}).get(str(at.astimezone(settings.zone).hour))
                if model and w and w['radiation']:
                    potential += min(s.pv_limit_kw, model['ratio'] * w['radiation']) * hours
            if potential > solar:
                solar, reconstructed = potential, reconstructed + 1
        points.append(dict(ts=r['ts'], hours=hours, lead_minutes=i * 30, load_kwh=r['load_kwh'], house_kwh=r['load_kwh'] - r['ev_kwh'],
                           house_p10=0, house_p90=0, ev_kwh=r['ev_kwh'], solar_kwh=solar, solar_p10=solar, solar_p90=solar,
                           solar_by_system=_split_solar(settings, solar), temperature=None))
    first_soc = next((r['soc_end'] for r in rows if r['soc_end'] is not None), None)
    return points, first_soc, reconstructed


def run(db: Database, settings: Settings, days: int, tariff_ids: list[str] | None = None) -> dict:
    today = utcnow().astimezone(settings.zone).date()
    plans = [t for t in settings.tariffs if t.status != 'retired' and (not tariff_ids or t.id in tariff_ids)]
    results = {t.id: dict(name=t.name, days=0, self_use_aud=0.0, optimised_aud=0.0, supply_aud=0.0, clipped_self_use_kwh=0.0,
                          clipped_optimised_kwh=0.0) for t in plans}
    used_days, reconstructed = [], 0
    for back in range(days, 0, -1):
        day = today - timedelta(days=back)
        points, first_soc, rebuilt = _points(db, settings, day)
        if not points:
            continue
        used_days.append(day.isoformat())
        reconstructed += rebuilt
        for tariff in plans:
            # Evaluate as if this tariff were the only active plan.
            trial = settings.model_copy(update={'tariffs': [tariff.model_copy(update={'status': 'active', 'active_from': None})],
                                                'forecast': settings.forecast.model_copy(update={'terminal_value_factor': 0.8})})
            initial = {s.id: s.battery.capacity_kwh * (first_soc or 50) / 100 for s in trial.active_systems() if s.battery}
            start = datetime.fromisoformat(points[0]['ts'])
            result = optimizer.plan(db, trial, 0, points, {'confidence': 'backtest'}, now=start, initial=initial, overrides=[], store=False)
            summary = result['summary']
            r = results[tariff.id]
            r['days'] += 1
            r['self_use_aud'] += summary['baseline_cash_aud']
            r['optimised_aud'] += summary['baseline_cash_aud'] - summary['modelled_saving_aud']
            r['supply_aud'] += tariff.supply_daily
            r['clipped_self_use_kwh'] += summary['baseline_curtailed_kwh']
            r['clipped_optimised_kwh'] += summary['curtailed_kwh']
    for r in results.values():
        for key in list(r):
            if isinstance(r[key], float):
                r[key] = round(r[key], 2)
        if r['days']:
            r['self_use_total_aud'] = round(r['self_use_aud'] + r['supply_aud'], 2)
            r['optimised_total_aud'] = round(r['optimised_aud'] + r['supply_aud'], 2)
            r['per_day_saving_aud'] = round((r['self_use_aud'] - r['optimised_aud']) / r['days'], 2)
    return dict(days=used_days, reconstructed_intervals=reconstructed, tariffs=results,
                caveats=['Perfect foresight: real forecasts are worse, so real savings will be lower.',
                         'Energy-only (supply listed separately); stored energy at day end valued at the 20th-percentile import price.'])
