"""Keep every candidate, but adopt only material changes to the day's plan."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from .config import Settings, stamp
from .db import decode, encode


def latest(db):
    active = db.meta('active_plan', {})
    row = db.one('SELECT * FROM decisions WHERE id=?', (active['decision_id'],)) if active else None
    if row is None:
        row = db.one('SELECT * FROM decisions ORDER BY id DESC LIMIT 1')
    if row:
        row['summary'] = decode(row['summary'])
    return row


def invalidate_review(db):
    active = db.meta('active_plan', {})
    if active:
        db.set_meta('active_plan', {**active, 'valid': False})


def reviewed_at(db, decision):
    """Freshness of an adopted plan, including successful background reviews."""
    review = db.meta('active_plan', {})
    if review and decision and review.get('decision_id') == decision.get('id'):
        return review.get('reviewed_at') if review.get('valid') else None
    return decision.get('created_at') if decision else None


def policy(db, settings, now):
    overrides = db.rows('SELECT id, kind, data, starts, expires FROM overrides WHERE cancelled_at IS NULL '
                        'AND starts<=? AND expires>? ORDER BY id', (stamp(now), stamp(now)))
    for override in overrides:
        if override['kind'] == 'ev_charge_now':
            data = decode(override['data'])
            # Poll/command evidence is not an owner change to the energy plan.
            # Hash only fields that change the requested load or its control policy.
            override['data'] = {key: data[key] for key in (
                'vehicle_id', 'charge_mode', 'target_pct', 'starting_soc', 'charge_windows',
                'schedule_kw', 'auto_schedule', 'stop_at_limit', 'dispatch_policy',
                'cancel_requested', 'state') if key in data}
    relevant = dict(systems=[s.model_dump() for s in settings.systems],
                    vehicles=[v.model_dump() for v in settings.vehicles],
                    tariffs=[t.model_dump() for t in settings.tariffs], forecast=settings.forecast.model_dump(),
                    timezone=settings.site.timezone, overrides=overrides)
    return hashlib.sha256(encode(relevant).encode()).hexdigest()


def intervals(db, decision_id):
    rows = db.rows('SELECT * FROM decision_intervals WHERE decision_id=? ORDER BY ts', (decision_id,))
    for row in rows:
        row['systems'] = decode(row['systems'])
    return rows


def _segments(rows, start, end):
    out = []
    for row in rows:
        at = datetime.fromisoformat(row['ts'])
        stop = at + timedelta(hours=row['hours'])
        if at < end and stop > start:
            out.append((max(at, start), min(stop, end), row))
    return out


def compare(settings: Settings, old_rows, new_rows, now):
    end = (now.astimezone(settings.zone) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    old_stop = max((datetime.fromisoformat(r['ts']) + timedelta(hours=r['hours']) for r in old_rows), default=now)
    new_stop = max((datetime.fromisoformat(r['ts']) + timedelta(hours=r['hours']) for r in new_rows), default=now)
    if old_stop < now + timedelta(minutes=35) and new_stop > old_stop:
        return dict(significant=True, reason='Adopted plan is nearing its horizon', energy_kwh=None, cost_change_aud=None)
    end = min(end, old_stop, new_stop)
    if end <= now:
        return dict(significant=True, reason='No remaining plan coverage', energy_kwh=None, cost_change_aud=None)
    old, new = _segments(old_rows, now, end), _segments(new_rows, now, end)
    cuts = sorted({now, end, *(t for series in (old, new) for a, b, _ in series for t in (a, b))})
    energy = cost = 0.0
    target_energy = {}
    mode_minutes = 0.0
    for a, b in zip(cuts, cuts[1:]):
        previous = next((r for x, y, r in old if x <= a < y), None)
        proposed = next((r for x, y, r in new if x <= a < y), None)
        if proposed is None or previous is None:
            return dict(significant=True, reason='Plan coverage changed', energy_kwh=None, cost_change_aud=None)
        hours = (b - a).total_seconds() / 3600
        energy += hours * sum(abs(proposed[k] / proposed['hours'] - previous[k] / previous['hours'])
                              for k in ('grid_to_battery_kwh', 'discharge_kwh'))
        cost += hours * (proposed['cost'] / proposed['hours'] - previous['cost'] / previous['hours'])
        mode_changed = False
        for system in settings.active_systems():
            if not system.battery:
                continue
            before = (previous['systems'].get(system.id) or {}).get('command') or {}
            after = (proposed['systems'].get(system.id) or {}).get('command') or {}
            if before.get('mode') != after.get('mode'):
                mode_changed = True
            if before.get('mode') == after.get('mode') and after.get('mode') in ('force_charge', 'force_discharge', 'hold'):
                delta = abs((after.get('target_soc') or 0) - (before.get('target_soc') or 0))
                target_energy[system.id] = max(target_energy.get(system.id, 0), delta * system.battery.capacity_kwh / 100)
        if mode_changed:
            mode_minutes += hours * 60

    def boundaries(series, sid):
        blocks = []
        for a, b, row in series:
            mode = ((row['systems'].get(sid) or {}).get('command') or {}).get('mode', 'self_use')
            if blocks and blocks[-1][0] == mode:
                blocks[-1] = (mode, blocks[-1][1], b)
            else:
                blocks.append((mode, a, b))
        return blocks

    shift = 0.0
    for system in settings.active_systems():
        before, after = boundaries(old, system.id), boundaries(new, system.id)
        if [r[0] for r in before] == [r[0] for r in after]:
            shift = max(shift, max((abs((x[i] - y[i]).total_seconds()) / 60
                                    for x, y in zip(before, after) for i in (1, 2)), default=0))
    shift = max(shift, mode_minutes)
    energy = max(energy, sum(target_energy.values()))
    limits = settings.forecast
    reason = ('Schedule timing changed' if shift > limits.plan_change_minutes else
              'Battery or grid energy changed' if energy >= limits.plan_change_kwh else
              'Modelled cost changed' if abs(cost) >= limits.plan_change_aud else None)
    return dict(significant=reason is not None, reason=reason or 'Changes below adoption thresholds',
                energy_kwh=round(energy, 3), cost_change_aud=round(cost, 3), timing_minutes=round(shift, 1))


def consider(db, settings: Settings, previous, candidate, now):
    """Atomically publish a candidate or record that the current plan was reviewed."""
    signature = policy(db, settings, now)
    active = db.meta('active_plan', {})
    valid = candidate['summary'].get('solver') == 'HiGHS MILP' and bool(candidate['summary'].get('telemetry_fresh'))
    day = now.astimezone(settings.zone).date().isoformat()
    if not previous or not active or active.get('day') != day or active.get('policy') != signature or previous.get('version') != candidate.get('version'):
        comparison = dict(significant=True, reason='New day, settings, override or first plan')
    else:
        comparison = compare(settings, intervals(db, previous['id']), intervals(db, candidate['id']), now)
    adopt = (valid and comparison['significant']) or previous is None
    summary = {**candidate['summary'], 'adopted': adopt, 'adoption': comparison}
    chosen = candidate if adopt else previous
    record = dict(decision_id=chosen['id'], reviewed_at=stamp(now), candidate_id=candidate['id'],
                  valid=valid, candidate_policy=signature, candidate_day=day,
                  policy=signature if adopt else active.get('policy'), day=day if adopt else active.get('day'))
    with db.connect() as c:
        c.execute('UPDATE decisions SET summary=? WHERE id=?', (encode(summary), candidate['id']))
        c.execute("INSERT OR REPLACE INTO meta VALUES('active_plan', ?)", (encode(record),))
    chosen['summary'] = summary if adopt else chosen['summary']
    return chosen


def checkpoint(db, forecast_id, settings, now):
    """Retain the first forecast in each local hour, including the 6 am baseline."""
    local = now.astimezone(settings.zone).replace(minute=0, second=0, microsecond=0)
    key = local.isoformat()
    existing = db.one("SELECT id FROM forecasts WHERE json_extract(model, '$.checkpoint')=? LIMIT 1", (key,))
    if not existing:
        row = db.one('SELECT model FROM forecasts WHERE id=?', (forecast_id,))
        model = {**decode(row['model']), 'checkpoint': key}
        db.execute('UPDATE forecasts SET model=? WHERE id=?', (encode(model), forecast_id))
