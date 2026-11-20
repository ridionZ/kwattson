"""Manual parked climate requests, independent confirmation and persistent stop timers."""
from dataclasses import asdict
from datetime import datetime, timedelta
from uuid import uuid4

from .config import stamp, utcnow
from .db import encode
from .plugins import registry
from .plugins.base import PluginError

KEY = 'vehicle_climate'


def supported(vehicle):
    return bool(getattr(registry.get(vehicle.plugin, 'vehicle'), 'supports_climate_control', False))


def permitted(vehicle):
    return vehicle.plugin_config.get('allow_climate_control') is True and supported(vehicle)


async def refresh(db, settings, devices, vehicle, now):
    if not supported(vehicle):
        raise PluginError('This vehicle connection does not support parked climate')
    state = await devices.vehicle(settings, vehicle.id).read_climate()
    now = max(now, utcnow())
    if not state.ts.tzinfo or state.ts > now + timedelta(minutes=5):
        raise PluginError('Invalid climate reading timestamp')
    records = db.meta(KEY, {})
    record = records.get(vehicle.id, {})
    record['reading'] = {**asdict(state), 'ts': stamp(state.ts)}
    record['next_poll'] = stamp(now + timedelta(seconds=30 if record.get('request') else 120))
    records[vehicle.id] = record
    db.set_meta(KEY, records)
    verify(db, vehicle.id, now)
    return record['reading']


def verify(db, vehicle_id, now):
    records = db.meta(KEY, {})
    record = records.get(vehicle_id, {})
    request = record.get('request')
    reading = record.get('reading')
    if not request or not reading:
        return False
    expected = 'on' if request['action'] == 'start' else 'off'
    if reading['ts'] > request['requested_at'] and reading['mode'] == expected:
        db.execute("UPDATE commands SET status='verified',verified_at=?,result=? WHERE id=?",
                   (stamp(now), encode({'mode': expected, 'reading_at': reading['ts']}), request['command_id']))
        record.pop('request')
        if expected == 'off':
            record.pop('until', None)
            record.pop('stop_attempts', None)
        records[vehicle_id] = record
        db.set_meta(KEY, records)
        return True
    return False


async def request(db, settings, devices, vehicle, now, *, enabled, temperature_c=22, minutes=10, source='manual'):
    if not permitted(vehicle):
        raise PluginError('Enable parked climate control in the vehicle connection first')
    if enabled:
        state = await refresh(db, settings, devices, vehicle, now)
        checked_at = max(now, utcnow())
        if state['parked'] is not True or not state['time_verified'] or not timedelta(0) <= checked_at - datetime.fromisoformat(state['ts']) <= timedelta(minutes=2):
            raise PluginError('Starting climate requires a fresh device-verified parked reading')
    records = db.meta(KEY, {})
    record = records.get(vehicle.id, {})
    old = record.get('request')
    action = 'start' if enabled else 'stop'
    if old and old['action'] == action and now - datetime.fromisoformat(old['requested_at']) < timedelta(seconds=90):
        return old['command_id']
    if old:
        db.execute("UPDATE commands SET status='superseded' WHERE id=? AND status='pending'", (old['command_id'],))
    command_id = db.execute('INSERT INTO commands(created_at,action,payload,status,result,idempotency_key) VALUES(?,?,?,?,?,?)',
                  (stamp(now), 'ev_climate_' + action, encode(dict(vehicle=vehicle.id, temperature_c=temperature_c if enabled else None,
                    minutes=minutes if enabled else None, source=source)), 'pending', '{}', 'climate:' + uuid4().hex))
    record['request'] = dict(action=action, command_id=command_id, requested_at=stamp(now))
    record['next_poll'] = stamp(now + timedelta(seconds=30))
    if enabled:
        # Persist before sending: a timeout may follow a successful start.
        record['until'] = stamp(now + timedelta(minutes=minutes))
        record['started_at'] = stamp(now)
        record.pop('stop_attempts', None)
        record.pop('stop_last_attempt_at', None)
    records[vehicle.id] = record
    db.set_meta(KEY, records)
    try:
        await devices.vehicle(settings, vehicle.id).set_climate(enabled, temperature_c)
    except Exception as error:
        db.execute('UPDATE commands SET result=? WHERE id=?', (encode({'receipt_error': type(error).__name__}), command_id))
        raise
    return command_id


async def monitor(db, settings, devices, notify, now):
    for vehicle in settings.vehicles:
        record = db.meta(KEY, {}).get(vehicle.id, {})
        if not record.get('request') and not record.get('until'):
            continue
        if not vehicle.enabled or not permitted(vehicle):
            pending = record.get('request')
            if pending:
                db.execute("UPDATE commands SET status='unverified' WHERE id=? AND status='pending'", (pending['command_id'],))
            notify.emit('vehicle_climate', {'vehicle': vehicle.name,
                        'reason': 'Parked climate control was disabled. Any outstanding stop is unconfirmed; check the car or iSMART.'},
                        key=f'climate-disabled:{vehicle.id}:{record.get("until", record.get("request"))}', now=now)
            record.pop('request', None)
            record.pop('until', None)
            records = db.meta(KEY, {})
            records[vehicle.id] = record
            db.set_meta(KEY, records)
            continue
        if record.get('next_poll', '') <= stamp(now):
            try:
                await refresh(db, settings, devices, vehicle, now)
            except Exception:
                records = db.meta(KEY, {})
                records[vehicle.id]['next_poll'] = stamp(now + timedelta(seconds=90))
                db.set_meta(KEY, records)
            record = db.meta(KEY, {}).get(vehicle.id, {})
        pending = record.get('request')
        if pending and now - datetime.fromisoformat(pending['requested_at']) >= timedelta(minutes=5):
            db.execute("UPDATE commands SET status='unverified' WHERE id=?", (pending['command_id'],))
            notify.emit('vehicle_climate', {'vehicle': vehicle.name,
                        'reason': f'The car has not confirmed climate {pending["action"]}. Check iSMART or the car.'},
                        key=f'climate:{pending["command_id"]}', now=now)
            record.pop('request')
            records = db.meta(KEY, {})
            records[vehicle.id] = record
            db.set_meta(KEY, records)
        if (record.get('until') and not record.get('request') and record.get('reading', {}).get('mode') == 'off'
                and record['reading']['ts'] > record.get('started_at', '')):
            record.pop('until')
            records = db.meta(KEY, {})
            records[vehicle.id] = record
            db.set_meta(KEY, records)
        if record.get('until', 'z') > stamp(now):
            continue
        if record.get('stop_last_attempt_at') and now - datetime.fromisoformat(record['stop_last_attempt_at']) < timedelta(seconds=90):
            continue
        pending = record.get('request')
        if pending and pending['action'] == 'stop' and now - datetime.fromisoformat(pending['requested_at']) < timedelta(seconds=90):
            continue
        attempts = record.get('stop_attempts', 0)
        if attempts >= 3:
            if not pending:
                notify.emit('vehicle_climate', {'vehicle': vehicle.name,
                            'reason': 'Timed climate stopping could not be confirmed. Stop it in iSMART or at the car.'},
                            key=f'climate-timer:{vehicle.id}:{record["until"]}', now=now)
                record.pop('until')
                records = db.meta(KEY, {})
                records[vehicle.id] = record
                db.set_meta(KEY, records)
            continue
        record['stop_attempts'] = attempts + 1
        record['stop_last_attempt_at'] = stamp(now)
        records = db.meta(KEY, {})
        records[vehicle.id] = record
        db.set_meta(KEY, records)
        try:
            await request(db, settings, devices, vehicle, now, enabled=False, source='timer')
        except Exception:
            pass  # A later status read determines what happened; the timer survives restarts.
