"""Vendor-neutral daily battery schedules built from the adopted plan."""
from datetime import datetime, timedelta

from .config import stamp
from .plugins.base import BatteryCommand


def build(settings, system, rows, now, live_soc=None):
    end = (now.astimezone(settings.zone) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # Recurring schedulers express midnight as 23:59; leave that final minute to self-use.
    limit = end - timedelta(minutes=1)
    commands = []
    last_price = None
    for row in rows:
        at = datetime.fromisoformat(row['ts'])
        start = max(at, now.replace(second=0, microsecond=0))
        stop = min(at + timedelta(hours=row['hours']), limit)
        if stop <= start:
            continue
        data = row['systems'].get(system.id) or {}
        action = data.get('command') or {'mode': 'self_use', 'power_kw': 0}
        target = action.get('target_soc')
        minimum = target if action['mode'] == 'hold' else data.get('export_floor_pct') if action['mode'] == 'force_discharge' else None
        if action['mode'] == 'hold' and minimum is not None and start <= now < stop and live_soc is not None:
            minimum = max(minimum, live_soc)
        command = BatteryCommand(action['mode'], start, stop, action.get('power_kw', 0), target,
                                 minimum, row.get('reason', '')[:200])
        price = (row['buy_rate'], row['sell_rate'])
        if commands and commands[-1].mode == command.mode and commands[-1].end == start and price == last_price:
            previous = commands[-1]
            # Use the final cutoff for a continuous charge/discharge window. The
            # planner's overnight reserve remains a hard floor for the whole block.
            target = command.target_soc
            if command.mode == 'force_discharge':
                floor = max(previous.min_soc or 0, command.min_soc or 0, system.battery.reserve_pct)
                target = max(target or floor, floor)
            commands[-1] = BatteryCommand(command.mode, previous.start, stop,
                                          max(previous.power_kw, command.power_kw), target,
                                          max(previous.min_soc or 0, command.min_soc or 0) or None, previous.reason)
        else:
            commands.append(command)
        last_price = price
    return commands, end


def encode_commands(commands):
    return [{**c.__dict__, 'start': stamp(c.start), 'end': stamp(c.end)} for c in commands]


def decode_commands(commands):
    return [BatteryCommand(**{**c, 'start': datetime.fromisoformat(c['start']), 'end': datetime.fromisoformat(c['end'])}) for c in commands]


def current(commands, now):
    return next((c for c in commands if c.start <= now < c.end), None)
