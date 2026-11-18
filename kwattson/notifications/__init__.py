"""Event notifications: rules, quiet hours, cooldowns and a persistent delivery outbox.

``emit(event, data, key)`` records an in-app notification (always) and queues a
delivery for each channel the owner enabled for that event. The worker calls
``process()`` to deliver with retries. Credentials never enter logs or results.
"""
from __future__ import annotations

import string
from datetime import datetime, timedelta

import httpx

from ..config import stamp, utcnow
from ..db import Database, decode, encode
from .channels import CHANNELS, describe as describe_channels  # noqa: F401

# event: (label, severity, default title, default body, default enabled, cooldown minutes)
EVENTS = {
    'system_fault': ('Inverter or battery fault', 'critical', 'Solar system fault', '{reason}', True, 60),
    'telemetry_stale': ('Readings stopped', 'warning', 'No recent readings from {system}', '{reason}', True, 120),
    'integration_restored': ('Readings restored', 'info', 'Readings restored from {system}', '{reason}', True, 0),
    'plan_failed': ('Planner problem', 'warning', 'Planner is using its fallback', '{reason}', True, 240),
    'command_failed': ('Battery command failed', 'critical', 'Battery control needs attention', '{reason}', True, 30),
    'control_stopped': ('Automatic control stopped', 'warning', 'Automatic control stopped', '{reason}', True, 0),
    'low_battery': ('Home battery low', 'warning', 'Home battery at {soc}%', '{reason}', True, 360),
    'high_price': ('Paying a high price now', 'warning', 'Importing at {price}c/kWh', 'Importing {power} kW right now. {reason}', True, 120),
    'ev_charge_opportunity': ('Good time to charge the car', 'info', 'Good time to charge {vehicle}', '{reason}', True, 360),
    'ev_charge_reminder': ('Car charging reminder', 'info', 'Charge {vehicle}', '{reason}', True, 600),
    'climate_opportunity': ('Cheap heating / cooling', 'info', 'Good time to {climate_mode}', '{reason}', True, 360),
    'clipping_expected': ('Solar would be wasted', 'info', 'About {kwh} kWh of solar may be clipped today', '{reason}', True, 720),
    'cheap_power_window': ('Free / cheap power window', 'info', '{label} starts soon', '{reason}', False, 720),
    'daily_summary': ('Daily summary', 'info', 'Energy today: {net}', '{reason}', True, 0),
    'good_day': ('Great energy day', 'info', 'Great energy day: {net}', '{reason}', True, 0),
    'bad_day': ('Expensive energy day', 'warning', 'Expensive day: {net}', '{reason}', True, 0),
    'weekly_report': ('Weekly report', 'info', 'Your week: {net}', '{reason}', True, 0),
    'tariff_pending': ('New tariff waiting for confirmation', 'info', '{plan} is not confirmed yet', '{reason}', True, 4320),
    'forecast_drift': ('Forecast accuracy dropped', 'warning', 'Usage is drifting from the forecast', '{reason}', True, 1440),
}
SEVERITIES = ('info', 'warning', 'critical')

# Alerts that share delivery settings. Each group has one "send to", "at most every" and
# "respect quiet hours"; individual alerts inside it are just switched on or off.
GROUPS = {
    'system': ('System health', 'Faults, lost connections and planner or control problems',
               ['system_fault', 'telemetry_stale', 'integration_restored', 'command_failed', 'control_stopped', 'plan_failed', 'forecast_drift']),
    'prices': ('Prices & tariff', 'Expensive power right now, cheap or free windows, tariff changes',
               ['high_price', 'cheap_power_window', 'tariff_pending']),
    'reports': ('Daily & weekly reports', 'How your day or week went',
                ['daily_summary', 'good_day', 'bad_day', 'weekly_report']),
    'battery': ('Battery & solar', 'Low battery and solar that would be wasted',
                ['low_battery', 'clipping_expected']),
    'car': ('Car charging', 'Cheapest time to charge and charging reminders',
            ['ev_charge_opportunity', 'ev_charge_reminder']),
    'comfort': ('Comfort', 'Good times to cool or heat the house',
                ['climate_opportunity']),
}
assert sorted(e for g in GROUPS.values() for e in g[2]) == sorted(EVENTS), 'every event must be in exactly one group'


def default_rule(event):
    label, severity, title, body, enabled, cooldown = EVENTS[event]
    return dict(enabled=enabled, channels=[], cooldown_minutes=cooldown, respect_quiet=severity != 'critical', title=title, body=body)


def check_template(text: str):
    for _, field, spec, conversion in string.Formatter().parse(text):
        if field is not None and (not field.isidentifier() or spec or conversion):
            raise ValueError('Templates may only use simple placeholders like {reason}')


class _Values(dict):
    def __missing__(self, key):
        return ''


def render(template: str, data: dict) -> str:
    try:
        return string.Formatter().vformat(template, (), _Values({k: str(v)[:500] for k, v in data.items()}))
    except (ValueError, IndexError, KeyError):
        return data.get('reason', '')


class Notifications:
    def __init__(self, db: Database, security):
        self.db, self.security = db, security
        with db.connect() as c:
            for event in EVENTS:
                c.execute('INSERT OR IGNORE INTO notification_rules VALUES(?,?)', (event, encode(default_rule(event))))

    def rule(self, event):
        row = self.db.one('SELECT data FROM notification_rules WHERE event=?', (event,))
        return {**default_rule(event), **(decode(row['data']) if row else {})}

    def emit(self, event: str, data: dict | None = None, key: str | None = None, now: datetime | None = None) -> int | None:
        """Record and queue a notification. ``key`` deduplicates (e.g. one per day)."""
        if event not in EVENTS:
            raise ValueError(f'Unknown event {event}')
        now = now or utcnow()
        data = data or {}
        rule = self.rule(event)
        if not rule['enabled']:
            return None
        if rule['cooldown_minutes']:
            last = self.db.one('SELECT created_at FROM notifications WHERE event=? ORDER BY id DESC LIMIT 1', (event,))
            if last and datetime.fromisoformat(last['created_at']) > now - timedelta(minutes=rule['cooldown_minutes']):
                return None
        title = render(rule['title'], data)[:200] or EVENTS[event][2]
        body = render(rule['body'], data)[:4000]
        severity = EVENTS[event][1]
        settings = self.db.settings()
        due = now
        if rule['respect_quiet']:
            local = now.astimezone(settings.zone)
            a, b = settings.alerts.quiet_start_hour, settings.alerts.quiet_end_hour
            quiet = (local.hour >= a or local.hour < b) if a > b else (a <= local.hour < b)
            if quiet:
                wake = local.replace(hour=b, minute=0, second=0, microsecond=0)
                due = wake + timedelta(days=1) if wake <= local else wake
        with self.db.connect() as c:
            cursor = c.execute('INSERT OR IGNORE INTO notifications(created_at, event, severity, title, body, data, dedupe) VALUES(?,?,?,?,?,?,?)',
                               (stamp(now), event, severity, title, body, encode(data), key or f'{event}:{stamp(now)}'))
            if not cursor.rowcount:
                return None
            notification_id = cursor.lastrowid
            for channel_id in rule['channels']:
                if c.execute('SELECT 1 FROM channels WHERE id=? AND enabled=1', (channel_id,)).fetchone():
                    c.execute('INSERT OR IGNORE INTO deliveries(notification_id, channel_id, status, next_at, result) VALUES(?,?,?,?,?)',
                              (notification_id, channel_id, 'pending', stamp(due), '{}'))
        return notification_id

    def groups(self) -> list[dict]:
        out = []
        for group_id, (label, description, events) in GROUPS.items():
            rules = [self.rule(e) for e in events]
            shared = rules[0]
            out.append(dict(id=group_id, label=label, description=description,
                            events=[dict(event=e, label=EVENTS[e][0], severity=EVENTS[e][1], enabled=r['enabled'])
                                    for e, r in zip(events, rules)],
                            channels=shared['channels'], cooldown_minutes=shared['cooldown_minutes'],
                            respect_quiet=shared['respect_quiet']))
        return out

    def save_group(self, group_id: str, enabled: dict[str, bool], channels: list[int], cooldown_minutes: int, respect_quiet: bool):
        if group_id not in GROUPS:
            raise ValueError('Unknown alert group')
        events = GROUPS[group_id][2]
        unknown = set(enabled) - set(events)
        if unknown:
            raise ValueError(f'Not in this group: {", ".join(sorted(unknown))}')
        valid = {r['id'] for r in self.db.rows('SELECT id FROM channels')}
        if set(channels) - valid:
            raise ValueError('Unknown channel')
        with self.db.connect() as c:
            for event in events:
                rule = self.rule(event)
                rule.update(enabled=enabled.get(event, rule['enabled']), channels=channels, cooldown_minutes=cooldown_minutes,
                            # critical alerts always ignore quiet hours
                            respect_quiet=respect_quiet and EVENTS[event][1] != 'critical')
                c.execute('INSERT OR REPLACE INTO notification_rules VALUES(?,?)', (event, encode(rule)))

    def channel(self, channel_id: int):
        row = self.db.one('SELECT * FROM channels WHERE id=?', (channel_id,))
        if not row:
            raise ValueError('Channel not found')
        config = decode(self.security.secret(f'channel:{channel_id}'), {})
        return CHANNELS[row['kind']](config)

    async def send_test(self, channel_id: int):
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            return await self.channel(channel_id).send('kWattson test', 'This channel works. Nothing on your system was changed.', 'info', client)

    async def process(self, now: datetime | None = None):
        now = now or utcnow()
        due = self.db.rows("SELECT d.*, n.title, n.body, n.severity FROM deliveries d JOIN notifications n ON n.id=d.notification_id "
                           "JOIN channels c ON c.id=d.channel_id WHERE d.status='pending' AND c.enabled=1 AND d.next_at<=? ORDER BY d.id LIMIT 20",
                           (stamp(now),))
        if not due:
            return
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            for row in due:
                attempts = row['attempts'] + 1
                try:
                    result = await self.channel(row['channel_id']).send(row['title'], row['body'], row['severity'], client)
                    self.db.execute("UPDATE deliveries SET status='sent', attempts=?, result=? WHERE id=?",
                                    (attempts, encode({'sent_at': stamp(), **result}), row['id']))
                except Exception as error:  # never log the message/URL: they may contain secrets
                    status = 'failed' if attempts >= 5 else 'pending'
                    self.db.execute('UPDATE deliveries SET status=?, attempts=?, next_at=?, result=? WHERE id=?',
                                    (status, attempts, stamp(now + timedelta(minutes=2 ** attempts)),
                                     encode({'error': type(error).__name__, 'message': _safe(error)}), row['id']))


def _safe(error: Exception) -> str:
    text = str(error)
    return text[:160] if 'http' not in text.lower() and 'token' not in text.lower() else 'details withheld'
