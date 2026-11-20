"""Persist one car-native window; never refresh its endpoints on a polling tick."""
from datetime import datetime, timedelta
from uuid import uuid4

from .config import stamp
from .db import encode
from .plugins.base import PluginError

KEY = 'vehicle_timers'


def capable(vehicle):
    from .plugins import registry
    return supported(registry.get(vehicle.plugin, 'vehicle'), vehicle)


def supported(device, vehicle):
    return (getattr(device, 'supports_native_charge_timer', False) is True
            and vehicle.plugin_config.get('use_native_charge_timer', True))


def desired(window, target, zone):
    start, end = (datetime.fromisoformat(window[key]) for key in ('start', 'end'))
    if start.second or start.microsecond:
        start = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = end.replace(second=0, microsecond=0)
    if not timedelta(0) < end - start < timedelta(days=1):
        raise PluginError('A car-native window must be shorter than 24 hours and at least one minute')
    a, b = start.astimezone(zone), end.astimezone(zone)
    if a.utcoffset() != b.utcoffset():
        raise PluginError('A native charging window cannot cross a daylight-saving clock change')
    return dict(start=stamp(start), end=stamp(end), start_minute=a.hour * 60 + a.minute,
                end_minute=b.hour * 60 + b.minute, target_pct=target, timezone=str(zone))


def matches(state, wanted, native_target=None):
    return (state.get('enabled') is True and state.get('stop_at_time') is True
            and all(state.get(key) == wanted[key] for key in ('start_minute', 'end_minute'))
            and state.get('target_pct') is not None and state['target_pct'] >= wanted['target_pct']
            and (native_target is None or state['target_pct'] == native_target))


def save(db, vehicle_id, record):
    records = db.meta(KEY, {})
    if record is None:
        records.pop(vehicle_id, None)
    else:
        records[vehicle_id] = record
    db.set_meta(KEY, records)


def warn(notify, vehicle, record, reason, now):
    notify.emit('ev_charge_session', {'vehicle': vehicle.name, 'reason': reason}, key=f'ev-timer:{record["id"]}', now=now)


async def ensure(db, vehicle, device, session_id, window, target, zone, notify, now):
    try:
        wanted = desired(window, target, zone)
    except PluginError:
        warn(notify, vehicle, {'id': f'{vehicle.id}:{session_id}'}, 'The charging window cannot be represented by the car timer. Review its duration and clock changes.', now)
        return False
    record = db.meta(KEY, {}).get(vehicle.id)
    same = record and record.get('desired') == wanted and record.get('session_id') == session_id
    if record and record.get('state') in ('clearing', 'conflict'):
        return False
    if same and record.get('state') == 'verified' and now - datetime.fromisoformat(record['checked_at']) < timedelta(minutes=5):
        return True
    try:
        state = await device.read_charge_timer()
        if same and matches(state, wanted, record.get('native_target_pct')):
            record.update(state='verified', checked_at=stamp(now), native_target_pct=state['target_pct'])
            save(db, vehicle.id, record)
            db.execute("UPDATE commands SET status='verified',verified_at=?,result=? WHERE id=?",
                       (stamp(now), encode(state), record['command_id']))
            return True
        if same and record.get('state') == 'verified':
            record['state'] = 'conflict'
            save(db, vehicle.id, record)
            warn(notify, vehicle, record, 'The car timer changed outside kWattson. Automatic charging is paused; review the car timer.', now)
            return False
        if same and now - datetime.fromisoformat(record['attempted_at']) < timedelta(minutes=3):
            return False  # An uncertain write gets read-back, not another write.
        if same and record.get('attempts', 0) >= 3:
            warn(notify, vehicle, record, 'The car has not confirmed its native charging timer. Check iSMART or the car.', now)
            return False
        if record and not same and state.get('enabled') is not False and not matches(state, record['desired'], record.get('native_target_pct')):
            record['state'] = 'conflict'
            save(db, vehicle.id, record)
            warn(notify, vehicle, record, 'The car timer was changed manually. The next window was not installed.', now)
            return False
        previous_target = record.get('previous_target_pct') if record else state.get('target_pct')
        record = dict(id=record['id'] if same else uuid4().hex, desired=wanted, session_id=session_id,
                      previous_target_pct=previous_target, state='writing', attempted_at=stamp(now),
                      attempts=record.get('attempts', 0) + 1 if same else 1)
        record['command_id'] = db.execute('INSERT INTO commands(created_at,action,payload,status,result,idempotency_key) VALUES(?,?,?,?,?,?)',
            (stamp(now), 'ev_native_timer', encode(dict(vehicle=vehicle.id, session_id=session_id, **wanted)), 'sending', '{}', 'ev-timer:' + uuid4().hex))
        save(db, vehicle.id, record)  # Ownership survives an uncertain receipt/restart.
        result = await device.set_charge_timer(datetime.fromisoformat(wanted['start']), datetime.fromisoformat(wanted['end']), target, zone)
        verified = result.verified and matches(result.detail, wanted)
        record.update(state='verified' if verified else 'writing', checked_at=stamp(now), native_target_pct=result.detail.get('target_pct'))
        save(db, vehicle.id, record)
        db.execute('UPDATE commands SET status=?,result=?,verified_at=? WHERE id=?',
                   ('verified' if verified else 'unverified', encode(result.detail), stamp(now) if verified else None, record['command_id']))
        db.audit('ev_native_timer_programmed', {'vehicle_id': vehicle.id, 'session_id': session_id, 'verified': bool(verified), **wanted})
        if not verified:
            warn(notify, vehicle, record, 'The car has not confirmed its native charging timer. Check iSMART or the car.', now)
        return bool(verified)
    except Exception as error:
        if record:
            db.execute("UPDATE commands SET status='unverified',result=? WHERE id=?", (encode({'error': type(error).__name__}), record['command_id']))
            warn(notify, vehicle, record, 'Native car timer receipt or read-back is uncertain. Check the car; the saved plan has not been replaced.', now)
        else:
            warn(notify, vehicle, {'id': f'{vehicle.id}:{session_id}'}, 'Could not read the car timer before programming it. Check the vehicle connection.', now)
        return False


async def clear(db, vehicle, device, notify, now, reason):
    record = db.meta(KEY, {}).get(vehicle.id)
    if not record:
        return True
    if record.get('clear_attempted_at') and now - datetime.fromisoformat(record['clear_attempted_at']) < timedelta(seconds=60):
        return False
    command_id = None
    try:
        state = await device.read_charge_timer()
        restoring = record.get('restore_target_pct')
        if state.get('enabled') is False and (restoring is None or state.get('target_pct') == restoring):
            save(db, vehicle.id, None)
            return True
        if state.get('enabled') is not False and not matches(state, record['desired'], record.get('native_target_pct')):
            record['state'] = 'conflict'
            save(db, vehicle.id, record)
            warn(notify, vehicle, record, 'The timer no longer matches the one kWattson wrote. Clear it in iSMART before resuming automatic charging.', now)
            return False
        record.update(state='clearing', clear_reason=reason)
        if record.get('clear_attempts', 0) >= 3:
            warn(notify, vehicle, record, 'Clearing the car timer is unconfirmed. Disable it in iSMART, especially before charging away.', now)
            return False
        previous = record.get('previous_target_pct')
        restore = restoring if restoring is not None else previous if previous is not None and previous != state.get('target_pct') else None
        record.update(clear_attempted_at=stamp(now), clear_attempts=record.get('clear_attempts', 0) + 1, restore_target_pct=restore)
        save(db, vehicle.id, record)
        command_id = db.execute('INSERT INTO commands(created_at,action,payload,status,result,idempotency_key) VALUES(?,?,?,?,?,?)',
            (stamp(now), 'ev_native_timer_clear', encode(dict(vehicle=vehicle.id, reason=reason)), 'sending', '{}', 'ev-timer-clear:' + uuid4().hex))
        result = await device.clear_charge_timer(restore)
        verified = result.verified and result.detail.get('enabled') is False
        db.execute('UPDATE commands SET status=?,result=?,verified_at=? WHERE id=?',
                   ('verified' if verified else 'unverified', encode(result.detail), stamp(now) if verified else None, command_id))
        if verified:
            save(db, vehicle.id, None)
            db.audit('ev_native_timer_cleared', dict(vehicle_id=vehicle.id, reason=reason))
            return True
    except Exception as error:
        if command_id:
            db.execute("UPDATE commands SET status='unverified',result=? WHERE id=?", (encode({'error': type(error).__name__}), command_id))
        record.update(state='clearing', clear_attempted_at=stamp(now))
        save(db, vehicle.id, record)
    warn(notify, vehicle, record, 'Clearing the car timer is unconfirmed. Disable it in iSMART, especially before charging away.', now)
    return False
