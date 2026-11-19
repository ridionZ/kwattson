"""The background worker: one loop owns polling, planning, control, alerts and housekeeping.

Run exactly one worker per database (enforced with a file lock in web/app.py).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from . import advice, alerts, ev_charging, forecast, optimizer, outcomes, weather
from .config import Runtime, Settings, stamp, utcnow
from .control import Controller
from .db import Database, decode
from .devices import Devices
from .notifications import Notifications
from .security import Security

log = logging.getLogger('kwattson')
PLAN_SECONDS = 900
TICK = 20


class Engine:
    def __init__(self, db: Database, security: Security, runtime: Runtime):
        self.db, self.security, self.runtime = db, security, runtime
        self.devices = Devices(db, security)
        self.notifications = Notifications(db, security)
        self.controller = Controller(db, self.devices, self.notifications, runtime)
        self.lock = asyncio.Lock()
        self.control_lock = asyncio.Lock()
        self.ev_lock = asyncio.Lock()
        self.next = dict(poll=0.0, vehicles=0.0, weather=0.0, plan=0.0, control=0.0, hourly=0.0, daily=0.0)
        self.transitions = []
        self.last_error = None

    def request_plan(self):
        self.next['plan'] = 0.0

    def latest_decision(self):
        return self.controller.latest_decision()

    async def replan(self, settings: Settings | None = None) -> dict:
        async with self.lock:
            settings = settings or self.db.settings()
            forecast_id, points, model = await asyncio.to_thread(forecast.build, self.db, settings)
            decision = await asyncio.to_thread(optimizer.plan, self.db, settings, forecast_id, points, model)
            if decision['summary']['solver'] != 'HiGHS MILP':
                self.notifications.emit('plan_failed', {'reason': 'The optimiser found no verified solution; showing a self-use plan. '
                                                                  f"Solver said: {decision['summary']['solver_message']}"})
            return decision

    async def poll(self):
        settings = self.db.settings()
        result = await self.devices.poll_systems(settings)
        self.transitions.extend(result['transitions'])
        return result

    async def run_control(self):
        async with self.control_lock:
            settings = self.db.settings()
            if settings.mode == 'automatic' or self.db.meta('control_active') or self.db.meta('control_release_pending'):
                await self.controller.cycle(settings)
            self.next['control'] = time.time() + 30
        return settings

    async def cycle(self):
        if not self.security.owner_exists() and not self.runtime.demo:
            return
        settings = self.db.settings()
        now = time.time()
        recovery = self.controller.recovery_status()
        if recovery and recovery['phase'] == 'release' and recovery['retry_at'] <= stamp():
            settings = await self.run_control()
        if now >= self.next['poll']:
            self.next['poll'] = now + settings.site.poll_seconds
            first = self.db.latest_site() is None
            await self.poll()
            if first:
                self.request_plan()
        if now >= self.next['vehicles']:
            self.next['vehicles'] = now + 300
            await self.devices.poll_vehicles(settings)
        async with self.ev_lock:
            if await ev_charging.monitor(self.db, self.db.settings(), self.devices, self.notifications, utcnow()):
                self.request_plan()
        if not self.runtime.demo and settings.site.weather_enabled and now >= self.next['weather']:
            self.next['weather'] = now + 3600
            try:
                await weather.refresh(self.db, settings)
            except Exception as error:
                self.next['weather'] = now + 600
                self.db.audit('weather_failed', {'error': type(error).__name__})
        if now >= self.next['plan']:
            self.next['plan'] = now + PLAN_SECONDS
            try:
                await self.replan()
            except Exception as error:
                log.exception('Planning failed')
                self.notifications.emit('plan_failed', {'reason': f'Planning failed ({type(error).__name__}); no automatic commands are sent.'})
        recovery = self.controller.recovery_status()
        if time.time() >= self.next['control'] or (recovery and recovery['retry_at'] <= stamp()):
            settings = await self.run_control()
        decision = self.latest_decision()
        alerts.check(self.db, settings, self.notifications, self.devices.status, self.transitions, decision['id'] if decision else None)
        self.transitions.clear()
        await self.notifications.process()
        if now >= self.next['hourly']:
            self.next['hourly'] = now + 3600
            await asyncio.to_thread(self.hourly, settings)
        if now >= self.next['daily']:
            self.next['daily'] = now + 86400
            await asyncio.to_thread(self.daily, settings)
        self.db.set_meta('worker_heartbeat', stamp())

    def hourly(self, settings):
        forecast.evaluate(self.db)
        outcomes.update(self.db, settings)
        now = utcnow()
        with self.db.connect() as c:
            c.execute('DELETE FROM raw_readings WHERE at<?', (stamp(now - timedelta(days=settings.raw_retention_days)),))
            c.execute('DELETE FROM sessions WHERE expires<?', (stamp(now),))

    def daily(self, settings):
        """Thin old plan detail: keep every forecast/decision for 30 days, then one per 3 hours."""
        cutoff = stamp(utcnow() - timedelta(days=30))
        with self.db.connect() as c:
            old = [r['id'] for r in c.execute("SELECT id, created_at FROM forecasts WHERE created_at<? AND "
                                               "(CAST(strftime('%H', created_at) AS INTEGER) % 3 != 0 OR CAST(strftime('%M', created_at) AS INTEGER) >= 15)",
                                               (cutoff,))]
            for i in range(0, len(old), 500):
                chunk = old[i:i + 500]
                marks = ','.join('?' * len(chunk))
                c.execute(f'DELETE FROM forecast_points WHERE forecast_id IN ({marks})', chunk)
                ids = [r[0] for r in c.execute(f'SELECT id FROM decisions WHERE forecast_id IN ({marks})', chunk)]
                if ids:
                    c.execute(f'DELETE FROM decision_intervals WHERE decision_id IN ({",".join("?" * len(ids))})', ids)
        backups = self.runtime.data_dir / 'backups'
        backups.mkdir(mode=0o700, exist_ok=True)
        self.db.backup(backups / f'kwattson-{utcnow():%Y%m%d}.sqlite3')
        for old_file in sorted(backups.glob('kwattson-*.sqlite3'))[:-14]:
            old_file.unlink()

    def advice(self):
        decision = self.latest_decision()
        return advice.build(self.db, self.db.settings(), decision['id'] if decision else None)

    async def run(self):
        while True:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.exception('Worker cycle failed')
                self.last_error = type(error).__name__
                self.db.audit('worker_error', {'error': type(error).__name__})
            retry = self.controller.recovery_status()
            delay = TICK
            if retry:
                seconds = datetime.fromisoformat(retry['retry_at']).timestamp() - time.time()
                if seconds > 0:
                    delay = min(delay, seconds)
            await asyncio.sleep(delay)

    async def close(self):
        await self.devices.close()
