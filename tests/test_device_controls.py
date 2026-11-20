"""Owner controls: proof of parking, timed stopping and protected battery overrides."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from conftest import sunny_points
from kwattson import battery_manual, optimizer, vehicle_climate
from kwattson.config import Runtime, Vehicle, stamp, utcnow
from kwattson.control import Controller, commissioning_hash
from kwattson.ingest import combine, store_site, store_system
from kwattson.plugins.base import BatteryCommand, CommandResult, PluginError, SystemReading, VehicleClimate


@pytest.fixture
def car():
    return Vehicle(id='car', name='Example', plugin='mg_ismart', capacity_kwh=50,
                   plugin_config={'allow_climate_control': True})


@pytest.mark.parametrize('parked,verified,age', [(None, True, 0), (False, True, 0), (True, False, 0), (True, True, 180)])
def test_climate_never_starts_without_current_parked_proof(db, settings, car, parked, verified, age):
    now = utcnow()
    device = SimpleNamespace(read_climate=AsyncMock(return_value=VehicleClimate(now - timedelta(seconds=age), verified, parked)),
                             set_climate=AsyncMock())
    devices = SimpleNamespace(vehicle=lambda *_: device)
    with pytest.raises(PluginError, match='parked'):
        asyncio.run(vehicle_climate.request(db, settings, devices, car, now, enabled=True))
    device.set_climate.assert_not_called()
    assert db.one('SELECT * FROM commands') is None


def test_climate_start_is_pending_until_new_status_and_timer_survives_reload(db, settings, car):
    now = utcnow()
    device = SimpleNamespace(read_climate=AsyncMock(return_value=VehicleClimate(now, True, True, 'off')),
                             set_climate=AsyncMock())
    devices = SimpleNamespace(vehicle=lambda *_: device)
    notify = SimpleNamespace(emit=Mock())
    settings.vehicles = [car]

    async def run():
        command = await vehicle_climate.request(db, settings, devices, car, now, enabled=True, temperature_c=22, minutes=5)
        assert db.one('SELECT status FROM commands WHERE id=?', (command,))['status'] == 'pending'
        assert not vehicle_climate.verify(db, car.id, now)
        device.read_climate.return_value = VehicleClimate(now + timedelta(seconds=31), True, True, 'on')
        await vehicle_climate.refresh(db, settings, devices, car, now + timedelta(seconds=31))
        assert db.one('SELECT status FROM commands WHERE id=?', (command,))['status'] == 'verified'
        assert db.meta(vehicle_climate.KEY)[car.id]['until'] == stamp(now + timedelta(minutes=5))
        # No in-memory timer is needed: a later monitor call recovers it from the database.
        device.read_climate.return_value = VehicleClimate(now + timedelta(minutes=5), True, True, 'on')
        await vehicle_climate.monitor(db, settings, devices, notify, now + timedelta(minutes=5))
        device.set_climate.assert_awaited_with(False, 22)
        stop = db.one('SELECT * FROM commands ORDER BY id DESC LIMIT 1')
        assert stop['action'] == 'ev_climate_stop' and stop['status'] == 'pending'
        device.read_climate.return_value = VehicleClimate(now + timedelta(minutes=5, seconds=31), True, True, 'off')
        await vehicle_climate.refresh(db, settings, devices, car, now + timedelta(minutes=5, seconds=31))
        assert db.one('SELECT status FROM commands WHERE id=?', (stop['id'],))['status'] == 'verified'
        assert 'until' not in db.meta(vehicle_climate.KEY)[car.id]
    asyncio.run(run())


def test_uncertain_climate_receipt_retains_stop_timer_and_bounds_retries(db, settings, car):
    now = utcnow()
    device = SimpleNamespace(read_climate=AsyncMock(return_value=VehicleClimate(now, True, True, 'off')),
                             set_climate=AsyncMock(side_effect=TimeoutError))
    devices = SimpleNamespace(vehicle=lambda *_: device)
    notify = SimpleNamespace(emit=Mock())
    settings.vehicles = [car]

    async def run():
        with pytest.raises(TimeoutError):
            await vehicle_climate.request(db, settings, devices, car, now, enabled=True, minutes=5)
        assert db.meta(vehicle_climate.KEY)[car.id]['until']
        for seconds in (300, 320, 400, 500, 520, 800, 850):
            at = now + timedelta(seconds=seconds)
            device.read_climate.return_value = VehicleClimate(at, True, True, 'on')
            await vehicle_climate.monitor(db, settings, devices, notify, at)
        assert device.set_climate.await_count == 4  # one uncertain start; at most three stops
        assert 'until' not in db.meta(vehicle_climate.KEY)[car.id]
        assert notify.emit.called
    asyncio.run(run())


def ready_controller(db, settings, tmp_path):
    settings.mode = 'automatic'
    settings.tariffs = [t for t in settings.tariffs if t.status == 'active']
    settings.systems[0].controllable = True
    db.save_settings(settings)
    now = utcnow().replace(second=0, microsecond=0)
    reading = SystemReading(now, True, soc=90, import_kw=.5, export_kw=0, load_kw=.5, pv_kw=0, charge_kw=0, discharge_kw=0)
    store_system(db, 'main', 'simulator', reading)
    store_site(db, combine(settings, {'main': reading}))
    optimizer.plan(db, settings, 0, sunny_points(now, hours=24, peak_kw=0), {}, now=now)
    db.set_meta('control_commissioned', {'hash': commissioning_hash(settings)})
    device = SimpleNamespace(supports_battery_schedule=True, apply_schedule=AsyncMock(return_value=CommandResult(True, True, {})),
                             verify_schedule=AsyncMock(return_value=CommandResult(True, True, {})),
                             release=AsyncMock(return_value=CommandResult(True, True, {})))
    notify = SimpleNamespace(emit=Mock())
    controller = Controller(db, SimpleNamespace(system=lambda *_: device), notify, Runtime(data_dir=tmp_path, worker=False))
    return now, controller, device


def test_temporary_battery_mode_is_sent_once_verified_and_expires_to_plan(db, settings, tmp_path):
    now, controller, device = ready_controller(db, settings, tmp_path)
    system = settings.systems[0]
    record = battery_manual.request(db, settings, system, now, mode='hold', minutes=15)

    async def run():
        result = await controller.cycle(settings, now)
        assert result['executed'], result
        assert device.apply_schedule.await_count == 1
        assert any(c.mode == 'hold' and c.end == now + timedelta(minutes=15) for c in device.apply_schedule.call_args.args[0])
        await controller.cycle(settings, now + timedelta(seconds=30))
        assert device.apply_schedule.await_count == 1
        await controller.cycle(settings, now + timedelta(minutes=6))
        device.verify_schedule.assert_awaited_once()
        store_system(db, 'main', 'simulator', SystemReading(now + timedelta(minutes=15), True, soc=90, import_kw=0, export_kw=0))
        await controller.cycle(settings, now + timedelta(minutes=15))
        assert not db.meta(battery_manual.KEY)
        assert device.apply_schedule.await_count == 2
        assert db.one('SELECT status FROM commands WHERE id=?', (db.rows("SELECT id FROM commands WHERE action='manual_hold'")[0]['id'],))['status'] == 'verified'
        assert record['end'] == stamp(now + timedelta(minutes=15))
    asyncio.run(run())


def test_manual_battery_still_obeys_stale_guards_and_owner_stop(db, settings, tmp_path):
    now, controller, device = ready_controller(db, settings, tmp_path)
    battery_manual.request(db, settings, settings.systems[0], now, mode='force_charge', minutes=15)
    db.execute('DELETE FROM system_readings')
    result = asyncio.run(controller.cycle(settings, now))
    assert not result['executed'] and any('stale' in r for r in result['reasons'])
    device.apply_schedule.assert_not_called()
    asyncio.run(controller.stop(settings, 'Owner stop'))
    assert not db.meta(battery_manual.KEY)


def test_export_protects_overnight_car_reserve_and_power_limits(db, settings, tmp_path):
    now, _, _ = ready_controller(db, settings, tmp_path)
    system = settings.systems[0]
    floor = battery_manual.export_floor(db, settings, system, now, now + timedelta(minutes=15))
    result = battery_manual.request(db, settings, system, now, mode='force_discharge', minutes=15, target_pct=15)
    assert result['target_soc'] >= floor
    with pytest.raises(PluginError, match='power'):
        battery_manual.request(db, settings, system, now, mode='force_charge', minutes=15, power_kw=100)
    store_system(db, 'main', 'simulator', SystemReading(now, True, soc=15, pv_kw=0))
    with pytest.raises(PluginError, match='protected'):
        battery_manual.request(db, settings, system, now, mode='force_discharge', minutes=15)


def test_manual_overlay_preserves_future_plan_and_has_no_overlapping_commands():
    now = utcnow().replace(second=0, microsecond=0)
    base = [BatteryCommand('self_use', now, now + timedelta(hours=1)),
            BatteryCommand('force_discharge', now + timedelta(hours=1), now + timedelta(hours=2), 3, 40)]
    manual = BatteryCommand('force_charge', now, now + timedelta(minutes=15), 2, 90)
    program = battery_manual.overlay(base, manual)
    assert [c.mode for c in program] == ['force_charge', 'self_use', 'force_discharge']
    assert all(a.end <= b.start for a, b in zip(program, program[1:]))
    assert program[-1] == base[-1]
