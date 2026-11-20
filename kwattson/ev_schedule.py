"""Conservative, tariff-aware planning for one EV departure."""
from datetime import datetime, timedelta

from .config import stamp
from .db import decode, encode
from .ev_charging import can_schedule, charge_rate, recent_reading, session_estimate
from .optimizer import allowance_used, dispatch_policy, price_points
from . import plans

STEP = timedelta(minutes=5)
LEAD = timedelta(minutes=30)
SAFETY_FACTOR = 0.8


def forecast_rows(decision, settings, now, rows, reviewed_at=None):
    return rows if (decision and decision['summary'].get('dispatch_policy') == dispatch_policy(settings)
                    and reviewed_at and timedelta(0) <= now - datetime.fromisoformat(reviewed_at) <= timedelta(minutes=35)) else []


def unavailable_periods(db, vehicle_id, now, deadline):
    end, start = stamp(deadline), stamp(now)
    rows = db.rows('SELECT start, end FROM holidays WHERE ev_away AND start<? AND end>?', (end, start))
    for row in db.rows("SELECT starts, expires, data FROM overrides WHERE kind='ev_away' AND cancelled_at IS NULL "
                      'AND starts<? AND expires>?', (end, start)):
        if decode(row['data']).get('vehicle_id') in (None, vehicle_id):
            rows.append(dict(start=row['starts'], end=row['expires']))
    return [(datetime.fromisoformat(r['start']), datetime.fromisoformat(r['end'])) for r in rows]


def priced_slots(settings, now, deadline):
    boundary = now.replace(second=0, microsecond=0, minute=now.minute // 5 * 5)
    changes = sorted(at for p in settings.tariffs if p.status == 'active' and (at := p.start()) and now < at < deadline)
    slots = []
    while boundary < deadline:
        start = max(boundary, now)
        end = min(boundary + STEP, deadline)
        for cut in [at for at in changes if start < at < end] + [end]:
            if cut > start:
                slots.append(dict(ts=stamp(start), end=cut))
            start = cut
        boundary += STEP
    return list(zip(slots, price_points(settings, slots)))


def grid_only_cost(db, settings, now, finish, kw):
    used = allowance_used(db, settings, now)
    floor = now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0)
    paid = cost = 0.0
    for slot, price in priced_slots(settings, floor, finish):
        start, end = datetime.fromisoformat(slot['ts']), slot['end']
        tier = price['imports'][0]
        group = tier['group']
        if group and start < now:
            past = (min(now, end) - start).total_seconds() / 3600
            used[group] = used.get(group, 0) + settings.site.grid_import_kw * past
        if end <= now:
            continue
        hours = (end - max(start, now)).total_seconds() / 3600
        rate = tier['rate']
        if group:
            used[group] = used.get(group, 0) + settings.site.grid_import_kw * hours
            if used[group] > tier['limit'] + 1e-6:
                rate = price['imports'][-1]['rate']
        energy = kw * hours
        cost += energy * rate
        paid += energy if rate > 0 else 0.0
    return dict(paid_grid_kwh=round(paid, 2), paid_grid_aud=round(cost, 2))


def at_rate(settings, at):
    return price_points(settings, [{'ts': stamp(at)}])[0]['imports']


def merge(items, keys):
    out = []
    for item in items:
        if out and out[-1]['end'] == item['start'] and all(out[-1].get(key) == item.get(key) for key in keys):
            out[-1]['end'] = item['end']
        else:
            out.append(item.copy())
    return out


def home_battery(db, settings, now, deadline, rows):
    """Only count spare energy above the reserve throughout the departure forecast."""
    if not rows or datetime.fromisoformat(rows[-1]['ts']) + timedelta(hours=rows[-1]['hours']) < deadline:
        return []
    rows = [r for r in rows if datetime.fromisoformat(r['ts']) < deadline
            and datetime.fromisoformat(r['ts']) + timedelta(hours=r['hours']) > now]
    assets = []
    for system in settings.active_systems():
        reading = db.latest_system(system.id) if system.battery else None
        if not reading or reading['soc'] is None or not timedelta(0) <= now - datetime.fromisoformat(reading['ts']) <= timedelta(minutes=15):
            continue
        flows = [r['systems'].get(system.id, {}) for r in rows]
        if not flows or any(p.get('soc_kwh') is None for p in flows):
            continue
        spec = system.battery
        reserve = spec.capacity_kwh * spec.reserve_pct / 100
        # Charging the car may replace an optional battery export. Keep the
        # house's planned energy and reserve, rather than reserving exports too.
        refunded = 0.0
        levels = [reading['soc'] / 100 * spec.capacity_kwh]
        for row, flow in zip(rows, flows):
            if system.controllable:
                start = max(now, datetime.fromisoformat(row['ts']))
                end = min(deadline, datetime.fromisoformat(row['ts']) + timedelta(hours=row['hours']))
                fraction = (end - start).total_seconds() / 3600 / row['hours']
                discharged = sum(p.get('dis', 0) for p in row['systems'].values())
                battery_export = min(row['export_kwh'], discharged)
                share = flow.get('dis', 0) / discharged if discharged > 0 else 0
                refunded += battery_export * share * fraction / spec.round_trip_efficiency ** .5
            levels.append(flow['soc_kwh'] + refunded)
        available = min(levels) - reserve
        if available > 0:
            assets.append((system, available * spec.round_trip_efficiency ** .5 * .8))
    return assets


def preview(db, settings, vehicle, now, deadline, data, decision, rows):
    rate, basis = charge_rate(db, vehicle, now)
    planned_kw = min(vehicle.charger_kw, rate) * SAFETY_FACTOR
    live = recent_reading(db, vehicle, now, timedelta(hours=1))
    soc = live['soc'] if live and live['soc'] is not None else data.get('starting_soc', vehicle.manual_soc)
    target = data['target_pct']
    capacity = vehicle.capacity_kwh / vehicle.charger_efficiency
    need = None if soc is None else max(0, (target - soc) / 100 * capacity)
    allowed = data.get('grid_rates')
    away = unavailable_periods(db, vehicle.id, now, deadline)
    if live and live['at_home'] == 0:
        away.append((now, deadline))
    rows = forecast_rows(decision, settings, now, rows, plans.reviewed_at(db, decision))
    batteries = home_battery(db, settings, now, deadline, rows)
    battery_budget = sum(kwh for _, kwh in batteries)
    pv = any(s.pv_kwp > 0 for s in settings.active_systems())
    allowance_spent = allowance_used(db, settings, now)
    # The allowance check gives the house priority up to its connection limit. Count the
    # unmetered part of the current half hour at that limit too.
    floor = now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0)
    past = priced_slots(settings, floor, now)
    for slot, price in past:
        tier = price['imports'][0]
        if tier['group']:
            hours = (slot['end'] - datetime.fromisoformat(slot['ts'])).total_seconds() / 3600
            allowance_spent[tier['group']] = allowance_spent.get(tier['group'], 0) + settings.site.grid_import_kw * hours
    candidates, tariffs = [], []
    ix = 0
    buffer = timedelta(minutes=max(5, settings.forecast.tariff_buffer_minutes))
    for slot, price in priced_slots(settings, now, deadline):
        start, end = datetime.fromisoformat(slot['ts']), slot['end']
        hours = (end - start).total_seconds() / 3600
        tier = price['imports'][0]
        group = tier['group']
        possible_rate = tier['rate']
        if group:
            used = allowance_spent.get(group, 0) + settings.site.grid_import_kw * hours
            allowance_spent[group] = used
            margin = settings.site.grid_import_kw * buffer.total_seconds() / 3600 if data.get('auto_schedule') else 0
            if used + margin > tier['limit'] + 1e-6:
                possible_rate = price['imports'][-1]['rate']
        tariffs.append(dict(start=stamp(start), end=stamp(end), rate=tier['rate'],
                            overflow_rate=price['imports'][-1]['rate'] if group else None))
        if any(a < end and b > start for a, b in away):
            continue
        while ix < len(rows):
            row = rows[ix]
            at = datetime.fromisoformat(row['ts'])
            if at + timedelta(hours=row['hours']) > start:
                break
            ix += 1
        row = rows[ix] if ix < len(rows) and datetime.fromisoformat(rows[ix]['ts']) <= start else None
        solar_kw = 0.0
        battery_kw = 0.0
        if row and row['import_kwh'] < .01:
            if pv:
                solar_kw = max(0, (row['export_kwh'] - row['discharge_kwh']) / row['hours']) * .85
            if row['action'] in ('self_use', 'force_discharge', 'export_priority'):
                for system, _ in batteries:
                    flow = row['systems'].get(system.id, {})
                    if flow.get('grid_bat', 0) + flow.get('pv_bat', 0) < .001:
                        dis = flow.get('dis', 0) / row['hours']
                        ac = system.ac_kw - flow.get('pv_ac', 0) / row['hours']
                        total_dis = sum(p.get('dis', 0) for p in row['systems'].values()) / row['hours']
                        exported = min(dis, row['export_kwh'] / row['hours'] * dis / total_dis) if system.controllable and total_dis > 0 else 0
                        battery_kw += max(0, min(system.battery.discharge_kw, ac) - dis + exported) * .8
        grid_allowed = allowed is None or possible_rate in allowed
        # Keep automatic windows inside permitted tariff boundaries, including quota
        # boundaries. Reserve one interval on either side of an ineligible period.
        if data.get('auto_schedule') and allowed is not None and grid_allowed:
            around = (start - buffer, end + buffer - timedelta(seconds=1))
            neighbours = [at_rate(settings, point)[0]['rate'] for point in around]
            if any(rate not in allowed for rate in neighbours):
                continue
        candidates.append(dict(start=start, end=end, hours=hours, solar_kw=solar_kw, battery_kw=battery_kw,
                               grid_allowed=grid_allowed, rate=possible_rate, sell=price['sell']))

    storage_value = decision['summary'].get('terminal_value_per_kwh', .15) if rows else .15

    def allocate(until):
        remaining = need
        battery_left = battery_budget
        chosen = []
        slots = [r for r in candidates if r['end'] <= until]
        while remaining and remaining > 1e-7 and slots:
            def score(row):
                energy = planned_kw * row['hours']
                solar = min(energy, row['solar_kw'] * row['hours'])
                battery = min(energy - solar, row['battery_kw'] * row['hours'], battery_left)
                grid = energy - solar - battery
                if (not row['grid_allowed'] and (grid > 1e-6 or
                        row['solar_kw'] + row['battery_kw'] < vehicle.charger_kw)):
                    return float('inf'), row['start']
                # Price stored energy against future grid purchases and wear.
                cost = solar * row['sell'] + battery * (storage_value + .03) + grid * row['rate']
                return cost / energy, row['start']

            row = min(slots, key=score)
            if score(row)[0] == float('inf'):
                break
            slots.remove(row)
            energy = min(remaining, planned_kw * row['hours'])
            hours = energy / planned_kw
            solar = min(energy, row['solar_kw'] * hours)
            battery = min(energy - solar, row['battery_kw'] * hours, battery_left)
            grid = max(0, energy - solar - battery)
            battery_left -= battery
            remaining -= energy
            chosen.append(dict(start=stamp(row['start']), end=stamp(row['start'] + timedelta(hours=hours)),
                               surplus_only=not row['grid_allowed'],
                               solar_kwh=solar, battery_kwh=battery, grid_kwh=grid,
                               paid_grid_kwh=grid if row['rate'] > 0 else 0, grid_cost=grid * row['rate']))
        return sorted(chosen, key=lambda r: r['start']), remaining

    chosen, missing = allocate(deadline - LEAD)
    if missing:
        chosen, missing = allocate(deadline)
    windows = merge([dict(start=r['start'], end=r['end'], **({'surplus_only': True} if r['surplus_only'] else {}))
                     for r in chosen], ('surplus_only',))
    energy = need - missing if need is not None else 0
    end_soc = min(100, soc + energy / capacity * 100) if soc is not None else None
    source = {key: sum(r[key] for r in chosen) for key in ('solar_kwh', 'battery_kwh', 'grid_kwh', 'paid_grid_kwh', 'grid_cost')}
    finish = windows[-1]['end'] if windows else stamp(now)
    return dict(finish=finish, deadline=stamp(deadline), energy_kwh=energy, end_soc=end_soc, soc=soc,
                target_reachable=missing <= 1e-6 if missing is not None else None, kw=round(planned_kw, 2),
                rate_basis=basis, paid_grid_kwh=round(source['paid_grid_kwh'], 2), paid_grid_aud=round(source['grid_cost'], 2),
                solar_kwh=round(source['solar_kwh'], 2), battery_kwh=round(source['battery_kwh'], 2),
                grid_kwh=round(source['grid_kwh'], 2), windows=windows, tariffs=merge(tariffs, ('rate', 'overflow_rate')),
                rate_options=sorted({t.rate_after for p in settings.tariffs if p.status == 'active'
                                              for t in p.allowances if t.kind == 'import'}
                                    | {p.import_rate for plan in settings.tariffs if plan.status == 'active' for p in plan.periods}))


def remaining_estimate(db, vehicle, now, session):
    """Update the saved schedule's projection without changing its control windows."""
    data = session['data']
    live = recent_reading(db, vehicle, now, timedelta(hours=1))
    soc = live['soc'] if live and live['soc'] is not None else data.get('starting_soc', vehicle.manual_soc)
    rate, basis = charge_rate(db, vehicle, now)
    kw = min(data.get('schedule_kw', vehicle.charger_kw), vehicle.charger_kw, rate * SAFETY_FACTOR)
    capacity = vehicle.capacity_kwh / vehicle.charger_efficiency
    target = data['target_pct']
    need = max(0, target - soc) / 100 * capacity if soc is not None else None
    remaining = need
    windows = []
    for window in data.get('charge_windows', []):
        start = max(now, datetime.fromisoformat(window['start']))
        end = min(datetime.fromisoformat(window['end']), datetime.fromisoformat(session['expires']))
        if start >= end or remaining is None or remaining <= 0:
            continue
        energy = min(kw * (end - start).total_seconds() / 3600, remaining)
        windows.append({'start': stamp(start), 'end': stamp(start + timedelta(hours=energy / kw))})
        remaining -= energy
    energy = need - remaining if need is not None else 0
    end_soc = min(100, soc + energy / capacity * 100) if soc is not None else None
    saved = data.get('charge_estimate') or {}
    estimate = {**saved, 'soc': soc, 'soc_at': live['ts'] if live else None, 'energy_kwh': energy, 'end_soc': end_soc,
            'target_reachable': remaining <= 1e-6 if remaining is not None else None,
            'finish': windows[-1]['end'] if windows else stamp(now), 'windows': windows,
            'kw': round(kw, 2), 'rate_basis': basis, 'charging': bool(live and live['charging']),
            'automatic': bool(data.get('auto_schedule')), 'grid_rates': data.get('grid_rates'),
            'estimate_updated_at': stamp(now)}
    # Original cost totals describe the original windows, not their remaining
    # portions. Avoid presenting those as a newly calculated cost.
    estimate.update(paid_grid_kwh=None, paid_grid_aud=None)
    outside = bool(live and live['charging'] and not any(
        w['start'] <= stamp(now) < w['end'] for w in data.get('charge_windows', [])))
    estimate['charging_outside_window'] = outside
    if not data.get('auto_schedule') and live and live['charging'] and live['at_home'] != 0:
        actual = session_estimate(vehicle, rate, now, datetime.fromisoformat(session['expires']), 100, soc)
        estimate.update(continuous_end_soc=actual['end_soc'], continuous_target_reachable=(
            actual['end_soc'] >= target if actual['end_soc'] is not None else None))
    return estimate


def grid_allowed_now(db, settings, now, allowed):
    if allowed is None:
        return True
    tiers = at_rate(settings, now)
    tier = tiers[0]
    rate = tier['rate']
    if tier['group']:
        used = allowance_used(db, settings, now).get(tier['group'], 0)
        buffer = settings.site.grid_import_kw * max(5, settings.forecast.tariff_buffer_minutes) / 60
        if used + buffer >= tier['limit']:
            rate = tiers[-1]['rate']
    return rate in allowed


def home_energy_available(db, settings, vehicle, now):
    """Require current metering and spare discharge capacity before a surplus-only start."""
    site = db.latest_site()
    if (not site or not timedelta(0) <= now - datetime.fromisoformat(site['ts']) <= timedelta(minutes=3)
            or site['fault'] or site['import_kw'] is None or site['import_kw'] > .1):
        return False
    spare = site['export_kw'] or 0
    unsafe_discharge = 0
    for system in settings.active_systems():
        battery = system.battery
        live = db.latest_system(system.id) if battery else None
        if (not live or live['soc'] is None or live['fault']
                or not timedelta(0) <= now - datetime.fromisoformat(live['ts']) <= timedelta(minutes=3)):
            continue
        # Leave time to see reserve approaching and complete a cloud stop.
        margin = max(.02 * battery.capacity_kwh, vehicle.charger_kw * (settings.site.poll_seconds + 120) / 3600)
        available = (live['soc'] - battery.reserve_pct) / 100 * battery.capacity_kwh
        if available > margin:
            spare += max(0, min(battery.discharge_kw, system.ac_kw) - (live['discharge_kw'] or 0))
        else:
            unsafe_discharge += live['discharge_kw'] or 0
    # The current car load is already included in the meter, so it need not be
    # supplied twice when checking a session that is underway.
    car = recent_reading(db, vehicle, now)
    supplied = (car['charge_kw'] or vehicle.charger_kw) if car and car['charging'] == 1 else 0
    return spare + max(0, supplied - unsafe_discharge) >= vehicle.charger_kw


async def run_schedule(db, settings, vehicle, session, devices, notify, now, away):
    """Run only the saved windows. Verify requests on a later vehicle reading."""
    data = session['data']
    live = recent_reading(db, vehicle, now)
    deadline = datetime.fromisoformat(session['expires'])
    target = data['target_pct']
    actual = live['soc'] if live else None
    reached = actual is not None and actual >= target
    auto = data.get('auto_schedule')
    command = data.get('charge_command', {})
    now_stamp = stamp(now)
    windows = data['charge_windows']
    window = next((w for w in windows if w['start'] <= now_stamp < w['end']), None)
    progressed = live and live['ts'] >= session['starts']
    moved = away or (live and (live['at_home'] == 0 or
                              (data.get('observed_plug') and live['plugged_in'] == 0)))
    invalid_tariff = auto and data['dispatch_policy'] != dispatch_policy(settings)
    cancelled = data.get('cancel_requested')
    changed = False

    def save():
        db.execute('UPDATE overrides SET data=? WHERE id=?', (encode(data), session['id']))

    def alert(reason, label):
        notify.emit('ev_charge_session', {'vehicle': vehicle.name, 'reason': reason},
                    key=f'ev-session:{session["id"]}:{label}', now=now)

    def finish(reason, state='finished'):
        data['state'] = state
        if cancelled:
            db.execute('UPDATE overrides SET cancelled_at=? WHERE id=?', (now_stamp, session['id']))
        save()
        alert(reason, 'done')
        return True

    if moved:
        if cancelled:
            return finish('Charging schedule cancelled.')
        return finish('Charging schedule ended because the car was away or disconnected. Check the car before leaving.')

    if auto and progressed and live['plugged_in'] == 1 and not data.get('observed_plug'):
        data['observed_plug'] = True
        save()
    if auto and not can_schedule(vehicle):
        return finish('Automatic scheduling is no longer enabled. Check the car or charger.', 'unconfirmed')

    # End a running session even if the cutoff was brought forward by a changed tariff,
    # a user cancellation or an observed percentage target.
    should_charge = window and not (reached or cancelled or invalid_tariff) and now < deadline
    if auto and should_charge and not grid_allowed_now(db, settings, now, data.get('grid_rates')):
        available = window.get('surplus_only') and home_energy_available(db, settings, vehicle, now)
        cooldown = data.get('surplus_paused_until', '') > now_stamp
        if not available or cooldown:
            should_charge = False
            if not available and not cooldown:
                data['surplus_paused_until'] = stamp(now + timedelta(minutes=10))
                save()
                changed = True
                if not data.get('surplus_warned'):
                    data['surplus_warned'] = True
                    save()
                    alert('Home energy is insufficient or unverified. Charging is paused to avoid paid grid power.', 'surplus')
    charging = live['charging'] if live else None
    later = live and live['ts'] > command.get('requested_at', now_stamp)
    command_age = now - datetime.fromisoformat(command['requested_at']) if command else timedelta(0)
    if auto:
        if command.get('action') == 'stop':
            if later and charging == 0:
                data.pop('charge_command')
                command = {}
                save()
                changed = True
            elif not (later and charging == 1 and command['tries'] < 3 and command_age >= timedelta(seconds=90)):
                if command_age >= timedelta(minutes=5):
                    return finish('The car has not confirmed it stopped. Check the car or charger now.', 'unconfirmed')
                return changed
        outstanding_start = command.get('action') == 'start' and not (later and charging == 0)
        if not should_charge and (charging == 1 or outstanding_start or command.get('action') == 'stop'):
            if live and live['plugged_in'] == 0:
                return finish('Charging state is unavailable at the cutoff. Check the car or charger now.', 'unconfirmed')
            action, key = 'stop', now_stamp
        elif should_charge and progressed and charging == 0 and live['plugged_in'] == 1 and (
                command.get('key') != window['start'] or
                (later and command['tries'] < 2 and command_age >= timedelta(minutes=15) and
                 datetime.fromisoformat(window['end']) - now >= timedelta(minutes=15))):
            action, key = 'start', window['start']
        else:
            action = None
        if action:
            attempts = command.get('tries', 0) + 1 if command.get('action') == action else 1
            data['charge_command'] = dict(action=action, key=key, requested_at=now_stamp, tries=attempts)
            save()
            db.audit('ev_charge_command', dict(override_id=session['id'], vehicle_id=vehicle.id, action=action, attempt=attempts))
            try:
                from .ev_charging import request_control
                await request_control(db, settings, devices, vehicle, now, stop=action == 'stop', source=f'session:{session["id"]}')
            except Exception:
                alert(f'Could not confirm the {action} request. Check the car or charger.', action)
            return True
        if (should_charge and now - datetime.fromisoformat(window['start']) >= timedelta(minutes=20)
                and charging != 1 and not data.get('start_warned')):
            data['start_warned'] = True
            save()
            alert('The car has not confirmed charging started. Check that it is plugged in at home.', 'start')
            changed = True

    if cancelled:
        return finish('Charging schedule cancelled.')
    if invalid_tariff:
        return finish('The tariff changed. Review the charging schedule and check the car or charger.')
    if reached:
        if auto and charging == 0:
            return finish(f'The car reported it reached {target:g}% and is no longer charging.', 'confirmed')
        if auto:
            return finish(f'The car reached {target:g}%, but its stopped state is unknown. Check the car or charger.', 'unconfirmed')
        return finish(f'The car reported it reached {target:g}%. Automatic control is off; kWattson did not stop charging.')
    if now >= deadline:
        return finish(f'The {target:g}% target was not confirmed by the deadline. Check the car before leaving.', 'unconfirmed')

    if not data.get('progress_warned'):
        capacity = vehicle.capacity_kwh / vehicle.charger_efficiency
        since = datetime.fromisoformat(live['ts']) if live and live['charging'] else now
        hours = sum(max(0, (datetime.fromisoformat(w['end']) - max(since, datetime.fromisoformat(w['start']))).total_seconds())
                    for w in windows) / 3600
        if progressed and actual is not None:
            expected = actual + hours * data['schedule_kw'] / capacity * 100
        elif now_stamp < (windows[0]['start'] if windows else session['expires']):
            expected = data['scheduled_end_soc']
        else:
            expected = None
        if expected is not None and expected < target - 1:
            data['progress_warned'] = True
            save()
            alert(f'The car may only reach {expected:.0f}% by your deadline. Review the charging plan.', 'shortfall')
            changed = True
    return changed
