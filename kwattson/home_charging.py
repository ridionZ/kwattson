"""Vendor-neutral home charging evidence; retain no coordinates or journeys."""
import hashlib
from datetime import datetime, timedelta
from uuid import uuid4

from .config import stamp
from .db import decode
from .plugins.base import PluginError
from .plugins import registry

KEY = 'vehicle_home'
PROBES = 'home_charge_probes'


def active_plan(db, vehicle_id, now):
    rows = db.rows("SELECT id,data FROM overrides WHERE kind='ev_charge_now' AND cancelled_at IS NULL AND starts<=? AND expires>? ORDER BY id DESC",
                   (stamp(now), stamp(now - timedelta(minutes=30))))
    return next(({'id': row['id'], **decode(row['data'])} for row in rows
                 if decode(row['data']).get('vehicle_id') == vehicle_id
                 and decode(row['data']).get('state') not in ('finished', 'confirmed', 'unconfirmed')), None)


def observe_vehicle(db, vehicle_id, plugin, reading):
    records = db.meta(KEY, {})
    old = records.get(vehicle_id, {})
    now = reading.ts
    if old.get('plugin') != plugin:
        old = {}
    if old.get('seen_at', '') > stamp(now):
        return
    plan = active_plan(db, vehicle_id, now)
    session = hashlib.sha256(str(reading.charge_session).encode()).hexdigest()[:20] if reading.charge_session else old.get('session')
    own = db.meta('ev_charge_pending', {}).get(vehicle_id, {})
    own_start = own.get('action') == 'start' and now - datetime.fromisoformat(own['requested_at']) < timedelta(minutes=5)
    changed = ((old.get('plugged') is False and reading.plugged_in is True)
               or (session and old.get('session') and session != old['session'] and not own_start
                   and (old.get('plugged') is not True or reading.plugged_in is not True)))
    record = dict(old)
    if changed or not record:
        record = dict(connection=uuid4().hex, needs_verification=bool(old.get('charged_plan') and plan and old['charged_plan'] == plan['id']))
        if old.get('disconnected_at'):
            record['disconnected_at'] = old['disconnected_at']
    if reading.plugged_in is False:
        record.update(disconnected_at=stamp(now), needs_verification=False)
        record.pop('positive', None)
    positive_at = reading.home_seen_at or now
    if reading.at_home is True and stamp(positive_at) >= old.get('disconnected_at', '') and not record.get('rejected'):
        record['positive'] = dict(source=reading.home_source or 'vehicle', at=stamp(positive_at))
        record['needs_verification'] = False
    # A negative location can be a stale/poor GPS fix. It never excludes home.
    if reading.charging is True and old.get('charging') is not True:
        record['charging_since'] = stamp(now)
    if reading.charging is True and plan:
        record['charged_plan'] = plan['id']
    record.update(plugin=plugin, plugged=reading.plugged_in if reading.plugged_in is not None else old.get('plugged'),
                  charging=reading.charging, seen_at=stamp(now), session=session,
                  wall_kw=reading.charge_kw, battery_kw=reading.battery_charge_kw)
    records[vehicle_id] = record
    db.set_meta(KEY, records)


def observe_site(db, site):
    records = db.meta(KEY, {})
    at = site['ts']
    if isinstance(at, str):
        at = datetime.fromisoformat(at)
    changed = False
    for vehicle_id, record in records.items():
        if record.get('charging') is not True or not record.get('seen_at') or record.get('last_meter_at', '') >= stamp(at):
            continue
        if abs(datetime.fromisoformat(record['seen_at']) - at) > timedelta(minutes=7):
            continue
        since = datetime.fromisoformat(record.get('charging_since', record['seen_at']))
        if at < since + timedelta(minutes=2):
            continue  # Let both cloud readings settle after a start.
        power = max(record.get('wall_kw') or 0, record.get('battery_kw') or 0)
        if power < .5:
            continue
        tolerance = max(.3, power * .2)
        mismatch = site['load_kw'] + tolerance < power
        record['last_meter_at'] = stamp(at)
        if mismatch:
            first = record.setdefault('mismatch_since', stamp(at))
            record['mismatch_samples'] = record.get('mismatch_samples', 0) + 1
            if record['mismatch_samples'] >= 2 and at - datetime.fromisoformat(first) >= timedelta(minutes=2):
                if not record.get('rejected'):
                    db.audit('home_charging_ruled_out', dict(vehicle=vehicle_id, car_kw=power, home_kw=site['load_kw'], first_at=first, last_at=stamp(at)))
                record['rejected'] = 'Car charging power exceeds the whole-home load on sustained, aligned readings.'
        else:
            record.pop('mismatch_since', None)
            record.pop('mismatch_samples', None)
        changed = True
    if changed:
        db.set_meta(KEY, records)


def status(db, vehicle, now):
    record = db.meta(KEY, {}).get(vehicle.id, {})
    common = dict(tested_this_session=bool(record.get('probe_done')))
    probe = db.meta(PROBES, {}).get(vehicle.id)
    if probe:
        return dict(confirmed=False, source='verification', reason='Checking home charging with one stop/restart test.', probing=True, **common)
    if record.get('rejected'):
        return dict(confirmed=False, source='power_mismatch', reason=record['rejected'], probing=False, **common)
    if record.get('plugged') is False:
        return dict(confirmed=False, source='disconnected', reason='The car is not plugged in.', probing=False, **common)
    if record.get('seen_at') and now - datetime.fromisoformat(record['seen_at']) > timedelta(minutes=30):
        return dict(confirmed=False, source='stale', reason='Plug state has not been refreshed for 30 minutes.', probing=False, **common)
    positive = record.get('positive')
    if positive and now - datetime.fromisoformat(positive['at']) <= timedelta(hours=24):
        return dict(confirmed=True, source=positive['source'], reason='Home presence confirmed for this connected session.', probing=False, **common)
    if active_plan(db, vehicle.id, now):
        can_probe = bool(vehicle.plugin_config.get('allow_home_probe') and vehicle.plugin_config.get('allow_charge_schedule')
                         and getattr(registry.get(vehicle.plugin, 'vehicle'), 'supports_charge_schedule', False))
        if can_probe and record.get('needs_verification') and not record.get('probe_done') and record.get('charging') is True:
            return dict(confirmed=False, source='verification_pending', reason='A reconnected charging session needs one home check.',
                        probing=False, needs_verification=True, **common)
        return dict(confirmed=True, source='plan_assumption', reason='Active charging plan assumes home unless readings contradict it.',
                    probing=False, needs_verification=can_probe and record.get('needs_verification', False), **common)
    return dict(confirmed=False, source='unknown', reason='Home charging has not been established.', probing=False, **common)


async def begin_probe(db, settings, devices, vehicle, now, *, explicit=False):
    from .ev_charging import can_schedule, can_stop, request_control, recent_reading
    if not can_schedule(vehicle) or not can_stop(vehicle):
        raise PluginError('Home verification requires permission and support for both charging start and stop')
    record = db.meta(KEY, {}).get(vehicle.id, {})
    if record.get('probe_done'):
        raise PluginError('Home charging has already been tested during this connected session')
    if not explicit and not status(db, vehicle, now).get('needs_verification'):
        return False
    live = recent_reading(db, vehicle, now, timedelta(minutes=3))
    site = db.latest_site()
    power = max((live['charge_kw'] if live else None) or vehicle.charger_kw, record.get('battery_kw') or 0)
    if not live or live['charging'] != 1 or live['plugged_in'] != 1 or not site:
        raise PluginError('Verification needs the car charging, connected, and current home-meter readings')
    if now - datetime.fromisoformat(site['ts']) > timedelta(minutes=7):
        raise PluginError('Wait for a current home-meter reading before verification')
    if site['load_kw'] + max(.3, power * .2) < power:
        raise PluginError('The home is using less power than the car; leave charging alone and wait for aligned readings')
    probes = db.meta(PROBES, {})
    probes[vehicle.id] = dict(id=uuid4().hex, stage='stopping', started_at=stamp(now), power_kw=power,
                              baseline=dict(ts=site['ts'], load_kw=site['load_kw']), restore_by=stamp(now + timedelta(minutes=3)),
                              connection=record.get('connection'))
    db.set_meta(PROBES, probes)
    records = db.meta(KEY, {})
    record['probe_done'] = True
    records[vehicle.id] = record
    db.set_meta(KEY, records)
    try:
        await request_control(db, settings, devices, vehicle, now, stop=True, source='home_probe')
    except Exception:
        pass  # The persisted probe must restore the original charging state even after an uncertain receipt.
    db.audit('home_charge_probe_started', {'vehicle': vehicle.id, **probes[vehicle.id]})
    return True


def cancel_probe(db, vehicle_id, reason):
    probes = db.meta(PROBES, {})
    old = probes.pop(vehicle_id, None)
    if old:
        db.set_meta(PROBES, probes)
        db.audit('home_charge_probe_cancelled', {'vehicle': vehicle_id, 'reason': reason, **old})
    return old


async def monitor(db, settings, devices, notify, now):
    from .ev_charging import recent_reading, request_control
    active = {vehicle.id for vehicle in settings.active_vehicles()}
    for vehicle_id in list(db.meta(PROBES, {})):
        if vehicle_id not in active:
            cancel_probe(db, vehicle_id, 'Vehicle was disabled or removed')
            notify.emit('ev_charge_session', {'vehicle': vehicle_id, 'reason': 'Home verification ended because the vehicle was disabled or removed. Check its charging state.'},
                        key=f'home-probe-disabled:{vehicle_id}', now=now)
    for vehicle in settings.active_vehicles():
        probe = db.meta(PROBES, {}).get(vehicle.id)
        if not probe:
            state = status(db, vehicle, now)
            if state.get('needs_verification') and not db.meta(KEY, {}).get(vehicle.id, {}).get('probe_done'):
                try:
                    await begin_probe(db, settings, devices, vehicle, now)
                except PluginError:
                    pass
            continue
        live = recent_reading(db, vehicle, now, timedelta(minutes=3))
        current = db.meta(KEY, {}).get(vehicle.id, {})
        if current.get('connection') != probe.get('connection') or live and live['plugged_in'] == 0:
            cancel_probe(db, vehicle.id, 'Car disconnected or its charging connection changed')
            continue
        site = db.latest_site()
        tolerance = max(.4, probe['power_kw'] * .25)
        if probe['stage'] == 'stopping':
            if live and live['charging'] == 0 and live['ts'] > probe['started_at']:
                probe.setdefault('stopped_at', live['ts'])
                if site and site['ts'] > probe['stopped_at'] and abs(probe['baseline']['load_kw'] - site['load_kw'] - probe['power_kw']) <= tolerance:
                    probe['low'] = dict(ts=site['ts'], load_kw=site['load_kw'])
            if probe.get('low') or stamp(now) >= probe['restore_by']:
                probe.update(stage='restarting', restart_at=stamp(now), attempts=1)
                probes = db.meta(PROBES, {}); probes[vehicle.id] = probe; db.set_meta(PROBES, probes)
                try:
                    await request_control(db, settings, devices, vehicle, now, stop=False, source='home_probe')
                except Exception:
                    pass
        elif probe['stage'] == 'restarting':
            if (not probe.get('low') and probe.get('stopped_at') and site and probe['stopped_at'] < site['ts'] < probe['restart_at']
                    and abs(probe['baseline']['load_kw'] - site['load_kw'] - probe['power_kw']) <= tolerance):
                probe['low'] = dict(ts=site['ts'], load_kw=site['load_kw'])
            if live and live['charging'] == 1 and live['ts'] > probe['restart_at']:
                probe.setdefault('resumed_at', live['ts'])
                if probe.get('low') and site and site['ts'] > probe['resumed_at']:
                    probe['high'] = dict(ts=site['ts'], load_kw=site['load_kw'])
                    if abs(site['load_kw'] - probe['low']['load_kw'] - probe['power_kw']) <= tolerance:
                        probe['verified'] = True
            elapsed = now - datetime.fromisoformat(probe['restart_at'])
            if probe.get('verified') or probe.get('resumed_at') and elapsed >= timedelta(minutes=3):
                records = db.meta(KEY, {})
                record = records.get(vehicle.id, {})
                record['needs_verification'] = False
                if probe.get('verified'):
                    record['positive'] = dict(source='paired_power_test', at=stamp(now))
                    record.pop('rejected', None)
                else:
                    record['last_probe'] = 'Inconclusive: charging was restored. The active-plan assumption remains unless power readings contradict it.'
                records[vehicle.id] = record; db.set_meta(KEY, records)
                db.audit('home_charge_probe_finished', {'vehicle': vehicle.id, **probe})
                probes = db.meta(PROBES, {}); probes.pop(vehicle.id); db.set_meta(PROBES, probes)
                if not probe.get('verified'):
                    notify.emit('ev_charge_session', {'vehicle': vehicle.name, 'reason': record['last_probe']}, key=f'home-probe:{probe["id"]}', now=now)
                continue
            if elapsed >= timedelta(minutes=5):
                notify.emit('ev_charge_session', {'vehicle': vehicle.name, 'reason': 'Home verification could not confirm charging restarted. Check the car or charger now.'},
                            key=f'home-probe-restore:{probe["id"]}', now=now)
                records = db.meta(KEY, {}); record = records.get(vehicle.id, {}); record['rejected'] = 'Charging restart after home verification is unconfirmed.'
                records[vehicle.id] = record; db.set_meta(KEY, records)
                db.audit('home_charge_probe_finished', {'vehicle': vehicle.id, **probe, 'restore_unconfirmed': True})
                probes = db.meta(PROBES, {}); probes.pop(vehicle.id); db.set_meta(PROBES, probes)
                continue
        probes = db.meta(PROBES, {}); probes[vehicle.id] = probe; db.set_meta(PROBES, probes)
