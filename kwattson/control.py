"""Guarded execution of the plan on controllable systems.

Nothing is ever sent to a device unless *every* guard passes. When guards fail
while a command is active, the device is released back to its own normal mode
(fail-safe), and if the problem persists for 30 minutes, or the device faults,
automatic mode is switched off and the owner is notified.

Each command is short (<= 30 min) and refreshed while the plan still wants it,
so a crashed controller leaves at most one short window behind. FoxESS-style
schedulers that repeat daily are covered by ``release()`` on stop/failure; see
docs/COMMISSIONING.md for the residual risk.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from .config import Settings, stamp, utcnow
from .db import Database, decode, encode
from .optimizer import VERSION, dispatch_policy
from .plugins.base import BatteryCommand
from .tariffs import pending_plans
from .weather import freshness_hours

COMMAND_MINUTES = 20
MAX_BLOCKED = timedelta(minutes=30)


def commissioning_hash(settings: Settings) -> str:
    """Physical and plugin settings that, if changed, require re-commissioning."""
    relevant = [s.model_dump() for s in settings.systems if s.controllable]
    return hashlib.sha256(encode(relevant).encode()).hexdigest()


class Controller:
    def __init__(self, db: Database, devices, notifications, runtime):
        self.db, self.devices, self.notifications, self.runtime = db, devices, notifications, runtime
        self.blocked_since = None

    def latest_decision(self):
        row = self.db.one('SELECT * FROM decisions ORDER BY id DESC LIMIT 1')
        if row:
            row['summary'] = decode(row['summary'])
        return row

    def guards(self, settings: Settings, now: datetime | None = None) -> list[str]:
        """Reasons automatic control may not act right now (empty list = clear)."""
        now = now or utcnow()
        reasons = []
        if settings.mode != 'automatic':
            reasons.append('Automatic control is off (monitor mode)')
        controllable = [s for s in settings.active_systems() if s.controllable and s.battery]
        if not controllable:
            reasons.append('No system is marked controllable')
        commissioned = self.db.meta('control_commissioned')
        if not commissioned or commissioned.get('hash') != commissioning_hash(settings):
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
        decision = self.latest_decision()
        if not decision or now - datetime.fromisoformat(decision['created_at']) > timedelta(minutes=35):
            reasons.append('Plan missing or older than 35 minutes')
        elif decision['version'] != VERSION or decision['summary'].get('dispatch_policy') != dispatch_policy(settings):
            reasons.append('Waiting for a plan with the current tariff and battery buffers')
        elif decision['summary']['solver'] != 'HiGHS MILP':
            reasons.append('Latest plan is a fallback, not a verified optimisation')
        elif not decision['summary'].get('telemetry_fresh'):
            reasons.append('Latest plan was made without fresh battery readings')
        if any(s.pv_kwp > 0 for s in settings.active_systems()) and settings.site.latitude is None:
            reasons.append('Set your home location (Settings › Home & tariff) so weather forecasts can be fetched')
        elif any(s.pv_kwp > 0 for s in settings.active_systems()) and settings.site.weather_enabled:
            age = freshness_hours(self.db, now)
            if age is None or age > 6:
                reasons.append('Weather forecast is missing or stale')
        return reasons

    def current_commands(self, decision_id: int, now: datetime) -> tuple[dict, dict] | None:
        row = self.db.one('SELECT * FROM decision_intervals WHERE decision_id=? AND ts<=? ORDER BY ts DESC LIMIT 1', (decision_id, stamp(now)))
        if not row or now >= datetime.fromisoformat(row['ts']) + timedelta(hours=row['hours']):
            return None
        return decode(row['systems']), row

    async def cycle(self, settings: Settings, now: datetime | None = None) -> dict:
        now = now or utcnow()
        reasons = self.guards(settings, now)
        active = self.db.meta('control_active', {})
        if reasons:
            if active:
                await self.release_all(settings, 'Guard failed: ' + reasons[0])
            if settings.mode == 'automatic':
                self.blocked_since = self.blocked_since or now
                fault = any('fault' in r for r in reasons)
                if fault or now - self.blocked_since >= MAX_BLOCKED:
                    await self.stop(settings, f'Automatic control stopped: {reasons[0]}', actor='system')
            return {'executed': False, 'reasons': reasons}
        self.blocked_since = None
        await self.check_response(settings, now)
        if settings.mode != 'automatic':
            return {'executed': False, 'reasons': ['Automatic control stopped during command verification']}
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
        return {'executed': True, 'results': results}

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
        try:
            result = await self.devices.system(settings, system.id).apply(cmd)
        except Exception as error:
            self.db.execute("UPDATE commands SET status='failed', result=? WHERE id=?", (encode({'error': type(error).__name__}), command_id))
            await self.stop(settings, f'{system.name}: command failed ({type(error).__name__}); device released', actor='system',
                            event='command_failed')
            return {'status': 'failed'}
        status = 'verified' if result.verified else 'unverified'
        self.db.execute('UPDATE commands SET status=?, result=?, verified_at=? WHERE id=?',
                        (status, encode(result.detail), stamp() if result.verified else None, command_id))
        if not result.verified:
            await self.stop(settings, f'{system.name}: device did not confirm the {mode} command; released', actor='system', event='command_failed')
            return {'status': 'unverified'}
        active[system.id] = dict(mode=mode, power_kw=command['power_kw'], target_soc=command.get('target_soc'),
                                 start=stamp(cmd.start), end=stamp(cmd.end), command_id=command_id, mismatches=0)
        self.db.set_meta('control_active', active)
        return {'status': 'verified', 'command_id': command_id}

    async def check_response(self, settings: Settings, now: datetime):
        """Did the battery actually do what we asked? Two misses in a row stop control."""
        active = self.db.meta('control_active', {})
        changed = False
        for system_id, cmd in list(active.items()):
            if not (datetime.fromisoformat(cmd['start']) + timedelta(minutes=6) <= now < datetime.fromisoformat(cmd['end'])):
                continue
            if cmd.get('checked_at') and now - datetime.fromisoformat(cmd['checked_at']) < timedelta(minutes=5):
                continue
            live = self.db.latest_system(system_id)
            if not live or live['soc'] is None:
                continue
            target = cmd.get('target_soc') or 0
            expected = cmd['power_kw'] * 0.3
            missed = (cmd['mode'] == 'force_charge' and live['soc'] < target - 1 and (live['charge_kw'] or 0) < expected) or \
                     (cmd['mode'] == 'force_discharge' and live['soc'] > target + 1 and (live['discharge_kw'] or 0) < expected)
            cmd['checked_at'] = stamp(now)
            cmd['mismatches'] = cmd.get('mismatches', 0) + 1 if missed else 0
            changed = True
            if cmd['mismatches'] >= 2:
                system = next(s for s in settings.systems if s.id == system_id)
                await self.stop(settings, f'{system.name} is not following {cmd["mode"]} commands; control stopped', actor='system',
                                event='command_failed')
                return
        if changed:
            self.db.set_meta('control_active', active)

    async def release(self, settings: Settings, system, why: str) -> dict:
        active = self.db.meta('control_active', {})
        try:
            result = await self.devices.system(settings, system.id).release()
            status = 'released' if result.verified else 'release unconfirmed'
        except Exception as error:
            status = f'release failed ({type(error).__name__})'
        self.db.execute('INSERT INTO commands(created_at, decision_id, action, payload, status, result, idempotency_key) VALUES(?,?,?,?,?,?,?)',
                        (stamp(), None, 'release', encode({'system': system.id, 'why': why}), status, '{}',
                         f'release:{system.id}:{stamp()}:{len(active)}'))
        active.pop(system.id, None)
        self.db.set_meta('control_active', active)
        return {'status': status}

    async def release_all(self, settings: Settings, why: str) -> list[str]:
        problems = []
        for s in settings.systems:
            if s.controllable and s.battery:
                result = await self.release(settings, s, why)
                if result['status'] != 'released':
                    problems.append(f"{s.name}: {result['status']}")
        return problems

    async def stop(self, settings: Settings, why: str, actor='owner', event='control_stopped'):
        problems = await self.release_all(settings, why)
        if settings.mode == 'automatic':
            settings.mode = 'monitor'
            self.db.save_settings(settings, actor)
        self.blocked_since = None
        self.db.audit('control_stopped', {'why': why, 'release_problems': problems}, actor)
        detail = why + ('. Check the inverter app: ' + '; '.join(problems) if problems else '')
        self.notifications.emit(event, {'reason': detail})
        return problems

