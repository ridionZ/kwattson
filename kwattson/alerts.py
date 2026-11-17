"""Turns the current state into notification events. Called by the worker each poll."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from .config import Settings, stamp, utcnow
from .db import Database, decode
from .tariffs import pending_plans, rate_at
from . import advice as advice_mod
from . import outcomes


def money(value: float) -> str:
    return f'-${-value:.2f}' if value < 0 else f'${value:.2f}'


def check(db: Database, settings: Settings, notify, devices_status: dict, transitions: list, decision_id: int | None,
          now: datetime | None = None):
    now = now or utcnow()
    local = now.astimezone(settings.zone)
    day = local.date().isoformat()
    a = settings.alerts
    emit = notify.emit

    for kind, system, message in transitions:
        if kind == 'failed':
            emit('telemetry_stale', {'system': system.name, 'reason': f'Could not read {system.name}: {message}. '
                                     'Automatic control is paused until readings return.'})
        else:
            emit('integration_restored', {'system': system.name, 'reason': f'{system.name} is reporting again.'})

    site = db.latest_site()
    if site and now - datetime.fromisoformat(site['ts']) < timedelta(minutes=20):
        if site['fault']:
            emit('system_fault', {'reason': 'A device reports a fault state. Check the inverter display or app.'})
        if site['soc'] is not None and site['soc'] < a.low_battery_pct:
            emit('low_battery', {'soc': round(site['soc']), 'reason': 'Consider holding off on large loads until the battery recovers.'})
        rate = rate_at(settings.tariffs, now, settings.zone)
        if site['import_kw'] > 0.3 and rate['buy'] * 100 >= a.high_price_cents:
            emit('high_price', {'price': round(rate['buy'] * 100, 1), 'power': round(site['import_kw'], 1),
                                'reason': f'{rate["label"]} rate. Postpone flexible loads (dishwasher, dryer, pool pump, car) if you can.'})

    for plan in pending_plans(settings.tariffs):
        emit('tariff_pending', {'plan': plan.name, 'reason': f'Planning still uses your current tariff. When {plan.retailer or "the retailer"} '
                                'confirms the start date, activate it in Settings → Tariffs.'}, key=f'pending:{plan.id}:{day}')

    if decision_id:
        info = advice_mod.build(db, settings, decision_id, now)
        for v in info['vehicles']:
            rec = v.get('recommendation')
            if rec and v['need_kwh'] >= 5:
                start = datetime.fromisoformat(rec['start'])
                if now - timedelta(minutes=5) <= start <= now + timedelta(minutes=30) and rec['cents_per_kwh'] <= 12:
                    emit('ev_charge_opportunity', {'vehicle': v['name'], 'reason': rec['text']}, key=f'ev-opportunity:{v["vehicle"]}:{day}:{start.hour}')
        for vehicle in settings.active_vehicles():
            if local.weekday() in vehicle.schedule.days and local.hour == (vehicle.schedule.start_hour - 1) % 24:
                latest = db.latest_vehicle(vehicle.id)
                soc = latest['soc'] if latest else vehicle.manual_soc
                if soc is None or soc < vehicle.target_pct - 5:
                    rec = next((v['recommendation'] for v in info['vehicles'] if v['vehicle'] == vehicle.id), None)
                    tail = f' Cheapest window: {rec["text"]}' if rec else ''
                    emit('ev_charge_reminder', {'vehicle': vehicle.name, 'reason': f'You usually charge tonight.{tail}'},
                         key=f'ev-reminder:{vehicle.id}:{day}')
        climate = info['climate']
        if climate and climate['available']:
            emit('climate_opportunity', {'climate_mode': climate['mode'], 'reason': climate['reason']})
        clip = info['clipping']
        if clip and 6 <= local.hour <= 10 and clip['planned_kwh'] >= a.clipping_alert_kwh:
            emit('clipping_expected', {'kwh': round(clip['planned_kwh'], 1), 'reason':
                 f'Peak solar of about {clip["peak_solar_kw"]:.1f} kW will exceed what the inverter and battery can take. '
                 'Run the dishwasher, pool pump or car charger around midday to use it.'}, key=f'clip:{day}')
        upcoming = db.one('SELECT * FROM decision_intervals WHERE decision_id=? AND ts>? AND ts<=? AND buy_rate<=0.0001 ORDER BY ts LIMIT 1',
                          (decision_id, stamp(now), stamp(now + timedelta(minutes=45))))
        if upcoming:
            emit('cheap_power_window', {'label': 'Free power', 'reason': 'Free grid power starts soon. Good for laundry, '
                                        'dishwasher, hot water or the car.'}, key=f'free:{day}')

    if local.hour == a.daily_summary_hour and local.minute < 30:
        today = outcomes.today_so_far(db, settings, now)
        if today and today['coverage'] > 0.6:
            text = (f"Net {money(today['net_aud'])} so far (supply included). Solar {today['solar_kwh']:.1f} kWh, used {today['load_kwh']:.1f} kWh, "
                    f"imported {today['import_kwh']:.1f}, exported {today['export_kwh']:.1f}. "
                    f"Self-use alone would have been {money(today['self_use_net_aud'])}; with no solar {money(today['no_solar_net_aud'])}.")
            emit('daily_summary', {'net': money(today['net_aud']), 'reason': text}, key=f'daily:{day}')
            if today['net_aud'] <= a.good_day_aud:
                emit('good_day', {'net': money(today['net_aud']), 'reason': text}, key=f'good:{day}')
            elif today['net_aud'] >= a.bad_day_aud:
                emit('bad_day', {'net': money(today['net_aud']), 'reason': text}, key=f'bad:{day}')
        if local.weekday() == 6:
            week = [decode(r['data']) for r in db.rows('SELECT data FROM daily_outcomes WHERE day>=? AND day<?',
                                                        ((local.date() - timedelta(days=7)).isoformat(), day))]
            if len(week) >= 5:
                net = sum(d['net_aud'] for d in week)
                saved = sum(d['no_solar_net_aud'] - d['net_aud'] for d in week)
                vs = sum(d['saving_vs_self_use_aud'] for d in week)
                emit('weekly_report', {'net': money(net), 'reason':
                     f'{len(week)} days: net {money(net)}, {money(saved)} better than buying all power from the grid, '
                     f'{money(vs)} better than plain self-use (estimated). Exported {sum(d["export_kwh"] for d in week):.0f} kWh.'},
                     key=f'weekly:{day}')

    recent = [decode(r['metrics']) for r in db.rows('SELECT metrics FROM evaluations ORDER BY forecast_id DESC LIMIT 12')]
    recent = [m for m in recent if m.get('house_actual_kwh')]
    if len(recent) >= 6:
        error = np.mean([abs(m['house_actual_kwh'] - m['house_forecast_kwh']) / m['house_actual_kwh'] for m in recent]) * 100
        if error > a.forecast_drift_pct:
            emit('forecast_drift', {'reason': f'Recent 24-hour usage forecasts were off by {error:.0f}% on average. '
                                    'Has something changed (guests, new appliance, holiday not entered)?'}, key=f'drift:{day}')
