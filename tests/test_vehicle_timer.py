import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from kwattson import ev_charging, vehicle_timer
from kwattson.config import stamp
from kwattson.db import decode, encode
from kwattson.plugins.base import CommandResult
from test_ev_schedule import CAR, NOW, MIDNIGHT, DEPART, reading, save_schedule


def setup(db, settings):
    settings.vehicles = [CAR]
    identity, data = save_schedule(db, settings)
    data.update(target_pct=100, charge_windows=[dict(start=stamp(NOW), end=stamp(MIDNIGHT + timedelta(hours=3, minutes=50))),
        dict(start=stamp(MIDNIGHT + timedelta(hours=8, minutes=30)), end=stamp(DEPART))])
    db.execute('UPDATE overrides SET data=? WHERE id=?', (encode(data), identity))
    state = dict(enabled=False, stop_at_time=False, start_minute=0, end_minute=0, target_pct=80)

    async def program(start, end, target, zone):
        a, b = start.astimezone(zone), end.astimezone(zone)
        state.update(enabled=True, stop_at_time=True, start_minute=a.hour * 60 + a.minute,
                     end_minute=b.hour * 60 + b.minute, target_pct=target)
        return CommandResult(True, True, dict(state))

    async def clear(previous):
        state.update(enabled=False, stop_at_time=False, target_pct=previous or 100)
        return CommandResult(True, True, dict(state))

    plugin = SimpleNamespace(supports_native_charge_timer=True, read_charge_timer=AsyncMock(side_effect=lambda: dict(state)),
        set_charge_timer=AsyncMock(side_effect=program), clear_charge_timer=AsyncMock(side_effect=clear),
        start_charging=AsyncMock(), stop_charging=AsyncMock())
    return identity, data, state, plugin, SimpleNamespace(vehicle=lambda *_: plugin), SimpleNamespace(emit=Mock())


def test_native_window_written_once_checked_without_writes_and_handed_over(db, settings):
    identity, data, state, plugin, devices, notify = setup(db, settings)

    async def run():
        reading(db, NOW, 65, True)
        for minutes in (0, 1, 2, 5, 6):
            await ev_charging.monitor(db, settings, devices, notify, NOW + timedelta(minutes=minutes))
        plugin.set_charge_timer.assert_awaited_once()
        assert plugin.read_charge_timer.await_count == 2
        plugin.start_charging.assert_not_called()
        plugin.stop_charging.assert_not_called()
        assert state['end_minute'] == 230  # 03:50 local, not a rolling 20-minute cutoff.
        at = MIDNIGHT + timedelta(hours=3, minutes=50)
        reading(db, at, 85, False)
        await ev_charging.monitor(db, settings, devices, notify, at)
        assert plugin.set_charge_timer.await_count == 2
        assert state['start_minute'] == 510 and state['end_minute'] == 600
        reading(db, DEPART, 99, False)
        await ev_charging.monitor(db, settings, devices, notify, DEPART)
        plugin.clear_charge_timer.assert_awaited_once_with(80)
        # Disabling a timer can permit immediate charging: always send the final stop.
        plugin.stop_charging.assert_awaited_once()
        plugin.start_charging.assert_not_called()
    asyncio.run(run())


def test_unplug_clears_only_the_native_timer_keeps_goal_and_restores_on_return(db, settings):
    identity, data, state, plugin, devices, notify = setup(db, settings)

    async def run():
        reading(db, NOW, 65, True)
        await ev_charging.monitor(db, settings, devices, notify, NOW)
        reading(db, NOW + timedelta(minutes=5), 65, False, plugged=False)
        await ev_charging.monitor(db, settings, devices, notify, NOW + timedelta(minutes=5))
        assert state['enabled'] is False
        saved = db.one('SELECT * FROM overrides WHERE id=?', (identity,))
        assert not saved['cancelled_at'] and decode(saved['data'])['auto_schedule']
        assert not decode(saved['data']).get('state')
        plugin.stop_charging.assert_not_called()  # Never stop an away charge.
        reading(db, NOW + timedelta(minutes=20), 65, False, plugged=True)
        await ev_charging.monitor(db, settings, devices, notify, NOW + timedelta(minutes=20))
        assert plugin.set_charge_timer.await_count == 2
        assert plugin.set_charge_timer.call_args.args[0].isoformat(timespec='seconds') == data['charge_windows'][0]['start']
        assert state['enabled'] is True
    asyncio.run(run())


def test_uncertain_native_write_is_read_back_without_repeating_it(db, settings):
    identity, data, state, plugin, devices, notify = setup(db, settings)
    real = plugin.set_charge_timer.side_effect

    async def uncertain(*args):
        await real(*args)
        raise TimeoutError()
    plugin.set_charge_timer.side_effect = uncertain

    async def run():
        reading(db, NOW, 65, False)
        await ev_charging.monitor(db, settings, devices, notify, NOW)
        await ev_charging.monitor(db, settings, devices, notify, NOW + timedelta(seconds=30))
        plugin.set_charge_timer.assert_awaited_once()
        assert db.meta(vehicle_timer.KEY)[CAR.id]['state'] == 'verified'
        plugin.start_charging.assert_not_called()
    asyncio.run(run())


def test_owner_timer_change_is_not_overwritten(db, settings):
    identity, data, state, plugin, devices, notify = setup(db, settings)

    async def run():
        reading(db, NOW, 65, True)
        await ev_charging.monitor(db, settings, devices, notify, NOW)
        state['end_minute'] = 120
        await ev_charging.monitor(db, settings, devices, notify, NOW + timedelta(minutes=5))
        plugin.set_charge_timer.assert_awaited_once()
        assert db.meta(vehicle_timer.KEY)[CAR.id]['state'] == 'conflict'
        plugin.clear_charge_timer.assert_not_called()
        plugin.stop_charging.assert_not_called()
    asyncio.run(run())
