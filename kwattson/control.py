"""Guarded execution of the plan on controllable systems.

Nothing is ever sent to a device unless *every* guard passes. When guards fail
while a command is active, the device is released back to its own normal mode
(fail-safe). Automatic mode remains selected while checks fail. Command failures
trigger a cooldown and a verified release before the controller can try again.

Each command is short (<= 30 min) and refreshed while the plan still wants it,
so a crashed controller leaves at most one short window behind. FoxESS-style
schedulers that repeat daily are covered by ``release()`` on stop/failure; see
docs/COMMISSIONING.md for the residual risk.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from uuid import uuid4

from .config import Settings, stamp, utcnow
from .db import Database, decode, encode
from .optimizer import VERSION, dispatch_policy
from .plugins.base import BatteryCommand
from .tariffs import pending_plans

COMMAND_MINUTES = 20
RELEASE_RETRY = timedelta(minutes=2)


def commissioning_hash(settings: Settings) -> str:
    """Physical and plugin settings that, if changed, require re-commissioning."""
    relevant = [s.model_dump() for s in settings.systems if s.controllable]
    return hashlib.sha256(encode(relevant).encode()).hexdigest()


class Controller:
    def __init__(self, db: Database, devices, notifications, runtime):
        self.db, self.devices, self.notifications, self.runtime = db, devices, notifications, runtime
        self.applying: set[str] = set()

    def latest_decision(self):
        row = self.db.one('SELECT * FROM decisions ORDER BY id DESC LIMIT 1')
        if row:
            row['summary'] = decode(row['summary'])
        return row

    def guards(self, settings: Settings, now: datetime | None = None, *, commissioning=False, check_plan=True) -> list[str]:
        """Reasons automatic control may not act right now (empty list = clear)."""
        now = now or utcnow()
        reasons = []
        if settings.mode != 'automatic':
            reasons.append('Automatic control is off (monitor mode)')
        controllable = [s for s in settings.active_systems() if s.controllable and s.battery]
        if not controllable:
            reasons.append('No system is marked controllable')
        if not commissioning and not self.commissioned(settings):
            reasons.append('Control has not been commissioned for the current system settings')
        if pending_plans(settings.tariffs):
            reasons.append('A tariff is waiting for its confirmed start date')
        if self.runtime.demo and any(s.plugin != 'simulator' for s in controllable):
            reasons.append('Demo mode cannot control real devices')
        limit = timedelta(seconds=max(2 * settings.site.poll_seconds, 600))
        for s in controllable:
            latest = self.db.latest_system(s.id)
            if not latest or now - datetime.fromisoformat(latest['ts']) > limit:
                reasons.append(f'{s.name}: readings missing or stale')
            elif not latest['time_verified']:
                reasons.append(f'{s.name}: reading timestamps are not device-verified')
            elif latest['fault']:
                reasons.append(f'{s.name}: device reports a fault')
            elif latest['soc'] is None:
                reasons.append(f'{s.name}: battery level unknown')
        if check_plan:
            decision = self.latest_decision()
            if not decision or now - datetime.fromisoformat(decision['created_at']) > timedelta(minutes=35):
                reasons.append('Plan missing or older than 35 minutes')
            elif decision['version'] != VERSION or decision['summary'].get('dispatch_policy') != dispatch_policy(settings):
                reasons.append('Waiting for a plan with the current tariff and battery buffers')
            elif decision['summary']['solver'] != 'HiGHS MILP':
                reasons.append('Latest plan is a fallback, not a verified optimisation')
            elif not decision['summary'].get('telemetry_fresh'):
                reasons.append('Latest plan was made without fresh battery readings')
        if (settings.site.weather_enabled and any(s.pv_kwp > 0 for s in settings.active_systems())
                and (settings.site.latitude is None or settings.site.longitude is None)):
            reasons.append('Set your home location (Settings › Home & tariff) so weather forecasts can be fetched')
        return reasons

    def commissioned(self, settings: Settings) -> bool:
        record = self.db.meta('control_commissioned') or {}
        return record.get('hash') == commissioning_hash(settings)

    def recovery_reasons(self, now: datetime) -> list[str]:
        if self.db.meta('control_release_pending', {}).keys() - self.applying:
            return ['Waiting for the inverter to confirm release to self-use']
        recovery = self.db.meta('control_recovery') or {}
        if recovery and now < datetime.fromisoformat(recovery['retry_at']):
            return [recovery['reason'] + ' Automatic retry is scheduled.']
        return []

    async def retry_releases(self, settings: Settings, now: datetime):
        pending = self.db.meta('control_release_pending', {})
        for system in settings.systems:
            retry = pending.get(system.id)
            if retry and now >= datetime.fromisoformat(retry['retry_at']):
                await self.release(settings, system, retry['reason'])

    async def pause(self, settings: Settings, system, why: str, now: datetime):
        recovery = self.db.meta('control_recovery') or {}
        failures = min(5, recovery.get('failures', 0) + 1)
        retry = now + timedelta(minutes=min(30, 2 ** failures))
        self.db.set_meta('control_recovery', {'failures': failures, 'retry_at': stamp(retry), 'reason': why})
        active = self.db.meta('control_active', {})
        problems = []
        for target in settings.systems:
            if target.id == system.id or target.id in active:
                result = await self.release(settings, target, why)
                if result['status'] != 'released':
                    problems.append(target.name + ': ' + result['status'])
        message = f'{why} Automatic control is paused and will retry.'
        if problems:
            message += ' Release still needs confirmation: ' + ', '.join(problems) + '.'
        self.db.audit('control_paused', {'why': why, 'retry_at': stamp(retry), 'release_problems': problems})
        self.notifications.emit('command_failed', {'reason': message})

    def current_commands(self, decision_id: int, now: datetime) -> tuple[dict, dict] | None:
        row = self.db.one('SELECT * FROM decision_intervals WHERE decision_id=? AND ts<=? ORDER BY ts DESC LIMIT 1', (decision_id, stamp(now)))
        if not row or now >= datetime.fromisoformat(row['ts']) + timedelta(hours=row['hours']):
            return None
        return decode(row['systems']), row

    async def cycle(self, settings: Settings, now: datetime | None = None) -> dict:
        now = now or utcnow()
        await self.retry_releases(settings, now)
        reasons = self.guards(settings, now) + self.recovery_reasons(now)
        active = self.db.meta('control_active', {})
        if reasons:
            pending = self.db.meta('control_release_pending', {})
            for system in settings.systems:
                if system.id in active and system.id not in pending:
                    await self.release(settings, system, 'Control paused: ' + reasons[0])
            return {'executed': False, 'reasons': reasons}
        await self.check_response(settings, now)
        reasons = self.recovery_reasons(now)
        if reasons:
            return {'executed': False, 'reasons': reasons}
        decision = self.latest_decision()
        found = self.current_commands(decision['id'], now)
        if not found:
            if active:
                await self.release_all(settings, 'Current plan interval ended')
            return {'executed': False, 'reasons': ['No plan step for now']}
        systems, row = found
        step_start = datetime.fromisoformat(row['ts'])
        step_end = step_start + timedelta(hours=row['hours'])
        results = {}
        for system in [s for s in settings.active_systems() if s.controllable and s.battery]:
            command = (systems.get(system.id) or {}).get('command') or {'mode': 'self_use', 'power_kw': 0, 'target_soc': None}
            results[system.id] = await self.send(settings, system, command, decision['id'], now, row['reason'], step_start, step_end)
            if self.recovery_reasons(now):
                break
        reasons = self.recovery_reasons(now)
        if not reasons and any(result['status'] == 'verified' for result in results.values()):
            self.db.set_meta('control_recovery', None)
        return {'executed': not reasons, 'results': results, 'reasons': reasons}

    async def send(self, settings: Settings, system, command: dict, decision_id: int, now: datetime, reason: str, step_start: datetime, step_end: datetime) -> dict:
        active = self.db.meta('control_active', {})
        current = active.get(system.id)
        mode = command['mode']
        if mode == 'self_use':
            if current:
                return await self.release(settings, system, 'Plan returned to self-use')
            return {'status': 'self_use (no command needed)'}
        local = now.astimezone(settings.zone)
        midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        limit = min(step_end, midnight).replace(second=0, microsecond=0)
        end = min(now + timedelta(minutes=COMMAND_MINUTES), limit).replace(second=0, microsecond=0)
        first_minute = step_start.replace(second=0, microsecond=0)
        if first_minute < step_start:
            first_minute += timedelta(minutes=1)
        start = max(now.replace(second=0, microsecond=0), first_minute)
        same = (current and current['mode'] == mode and abs(current.get('power_kw', 0) - command['power_kw']) < 0.5
                and current.get('target_soc') == command.get('target_soc'))
        if same:
            current_end = datetime.fromisoformat(current['end'])
            if current_end <= limit and (current_end >= end or current_end - now > timedelta(minutes=8)):
                return {'status': 'unchanged'}
        if end <= start:
            if current and datetime.fromisoformat(current['end']) > limit:
                return await self.release(settings, system, 'Plan interval is ending')
            return {'status': 'skipped at end of plan interval'}
        live = self.db.latest_system(system.id)
        min_soc = live['soc'] if mode == 'hold' and live else None
        cmd = BatteryCommand(mode=mode, start=start, end=end, power_kw=command['power_kw'],
                             target_soc=command.get('target_soc'), min_soc=min_soc, reason=reason[:200])
        key = f'{system.id}:{decision_id}:{mode}:{stamp(start)}'
        with self.db.connect() as c:
            if c.execute('SELECT 1 FROM commands WHERE idempotency_key=?', (key,)).fetchone():
                return {'status': 'duplicate suppressed'}
            command_id = c.execute('INSERT INTO commands(created_at, decision_id, action, payload, status, result, idempotency_key) '
                                   'VALUES(?,?,?,?,?,?,?)', (stamp(now), decision_id, mode, encode(dict(system=system.id, **cmd.__dict__)),
                                                             'sending', '{}', key)).lastrowid
        self.expect_release(system.id, 'Checking an interrupted command', now)
        self.applying.add(system.id)
        try:
            result = await self.devices.system(settings, system.id).apply(cmd)
        except Exception as error:
            self.applying.discard(system.id)
            self.db.execute("UPDATE commands SET status='failed', result=? WHERE id=?", (encode({'error': type(error).__name__}), command_id))
            await self.pause(settings, system, f'{system.name}: command failed ({type(error).__name__}).', now)
            return {'status': 'failed'}
        finally:
            self.applying.discard(system.id)
        status = 'verified' if result.verified else 'unverified'
        self.db.execute('UPDATE commands SET status=?, result=?, verified_at=? WHERE id=?',
                        (status, encode(result.detail), stamp() if result.verified else None, command_id))
        if not result.verified:
            await self.pause(settings, system, f'{system.name}: device did not confirm the {mode} command.', now)
            return {'status': 'unverified'}
        active[system.id] = dict(mode=mode, power_kw=command['power_kw'], target_soc=command.get('target_soc'),
                                 start=stamp(cmd.start), end=stamp(cmd.end), command_id=command_id, mismatches=0)
        self.db.set_meta('control_active', active)
        self.release_confirmed(system.id)
        return {'status': 'verified', 'command_id': command_id}

    async def check_response(self, settings: Settings, now: datetime):
        """Check each distinct telemetry reading. Two misses pause dispatch."""
        active = self.db.meta('control_active', {})
        changed = False
        for system_id, cmd in list(active.items()):
            if not (datetime.fromisoformat(cmd['start']) + timedelta(minutes=6) <= now < datetime.fromisoformat(cmd['end'])):
                continue
            if cmd.get('checked_at') and now - datetime.fromisoformat(cmd['checked_at']) < timedelta(minutes=5):
                continue
            live = self.db.latest_system(system_id)
            if not live or live['soc'] is None or live['ts'] <= max(cmd['start'], cmd.get('checked_reading', '')):
                continue
            target = cmd.get('target_soc') or 0
            expected = cmd['power_kw'] * 0.3
            missed = (cmd['mode'] == 'force_charge' and live['soc'] < target - 1 and (live['charge_kw'] or 0) < expected) or \
                     (cmd['mode'] == 'force_discharge' and live['soc'] > target + 1 and (live['discharge_kw'] or 0) < expected)
            cmd['checked_at'] = stamp(now)
            cmd['checked_reading'] = live['ts']
            cmd['mismatches'] = cmd.get('mismatches', 0) + 1 if missed else 0
            changed = True
            if cmd['mismatches'] >= 2:
                system = next(s for s in settings.systems if s.id == system_id)
                await self.pause(settings, system, f'{system.name} is not following the {cmd["mode"]} command.', now)
                return
        if changed:
            self.db.set_meta('control_active', active)

    def expect_release(self, system_id: str, reason: str, now: datetime):
        pending = self.db.meta('control_release_pending', {})
        pending[system_id] = {'retry_at': stamp(now + RELEASE_RETRY), 'reason': reason}
        self.db.set_meta('control_release_pending', pending)

    def release_confirmed(self, system_id: str):
        pending = self.db.meta('control_release_pending', {})
        pending.pop(system_id, None)
        self.db.set_meta('control_release_pending', pending)

    async def release(self, settings: Settings, system, why: str) -> dict:
        active = self.db.meta('control_active', {})
        self.expect_release(system.id, why, utcnow())
        try:
            result = await self.devices.system(settings, system.id).release()
            status = 'released' if result.verified else 'release unconfirmed'
        except Exception as error:
            status = f'release failed ({type(error).__name__})'
        self.db.execute('INSERT INTO commands(created_at, decision_id, action, payload, status, result, idempotency_key) VALUES(?,?,?,?,?,?,?)',
                        (stamp(), None, 'release', encode({'system': system.id, 'why': why}), status, '{}',
                         f'release:{system.id}:{uuid4().hex}'))
        if status == 'released':
            active.pop(system.id, None)
            self.db.set_meta('control_active', active)
            self.release_confirmed(system.id)
        return {'status': status}

    async def release_all(self, settings: Settings, why: str) -> list[str]:
        problems = []
        for s in settings.systems:
            if s.controllable and s.battery:
                result = await self.release(settings, s, why)
                if result['status'] != 'released':
                    problems.append(f"{s.name}: {result['status']}")
        return problems

    async def stop(self, settings: Settings, why: str, actor='owner'):
        if settings.mode == 'automatic':
            settings.mode = 'monitor'
            self.db.save_settings(settings, actor)
        self.db.set_meta('control_recovery', None)
        problems = await self.release_all(settings, why)
        self.db.audit('control_stopped', {'why': why, 'release_problems': problems}, actor)
        if problems:
            self.notifications.emit('control_stopped', {'reason': why + '. Release still needs confirmation: ' + ', '.join(problems)})
        return problems
