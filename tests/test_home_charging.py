import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kwattson import ev_charging, home_charging
from kwattson.config import Vehicle, stamp, utcnow
from kwattson.db import encode
from kwattson.ingest import combine, store_site, store_vehicle
from kwattson.plugins.base import PluginError, SystemReading, VehicleReading


def setup(db, settings, *, plugged=True, charging=True, at_home=None):
    now = utcnow().replace(microsecond=0)
    car = Vehicle(id='car', name='Car', plugin='mg_ismart', capacity_kwh=50, charger_kw=1.9,
                  plugin_config={'allow_charge_schedule': True, 'allow_home_probe': True})
    settings.vehicles = [car]
    db.save_settings(settings)
    db.execute('INSERT INTO overrides(created_at,starts,expires,kind,data) VALUES(?,?,?,?,?)',
               (stamp(now - timedelta(hours=1)), stamp(now - timedelta(hours=1)), stamp(now + timedelta(hours=12)),
                'ev_charge_now', encode({'vehicle_id': car.id, 'auto_schedule': True, 'charge_mode': 'ready_by'})))
    read(db, car, now - timedelta(minutes=10), plugged=plugged, charging=charging, at_home=at_home)
    return now, car


def read(db, car, at, *, plugged=True, charging=True, at_home=None, power=1.9):
    store_vehicle(db, car.id, car.plugin, VehicleReading(at, False, soc=60, charging=charging,
                  plugged_in=plugged, at_home=at_home, charge_kw=power if charging else 0))


def meter(db, settings, at, load):
    system = settings.systems[0]
    reading = SystemReading(at, True, soc=80, load_kw=load, import_kw=load, export_kw=0,
                            pv_kw=0, charge_kw=0, discharge_kw=0)
    store_site(db, combine(settings, {system.id: reading}))


def test_active_plan_assumes_home_and_negative_gps_does_not_exclude_it(db, settings):
    now, car = setup(db, settings, at_home=False)
    assert home_charging.status(db, car, now)['source'] == 'plan_assumption'
    assert home_charging.status(db, car, now)['confirmed']


def test_only_sustained_aligned_impossible_power_rules_out_home(db, settings):
    now, car = setup(db, settings)
    read(db, car, now)
    meter(db, settings, now - timedelta(minutes=5), 1)
    assert home_charging.status(db, car, now)['confirmed']
    meter(db, settings, now - timedelta(minutes=2), 1)
    assert not home_charging.status(db, car, now)['confirmed']
    plugin = SimpleNamespace(stop_charging=AsyncMock())
    devices = SimpleNamespace(vehicle=lambda *_: plugin)
    with pytest.raises(PluginError, match='exceeds'):
        asyncio.run(ev_charging.request_control(db, settings, devices, car, now, stop=True, source='session:1'))
    plugin.stop_charging.assert_not_called()


def test_cooking_or_large_house_load_never_proves_home_without_a_plan(db, settings):
    now, car = setup(db, settings)
    db.execute('DELETE FROM overrides')
    read(db, car, now)
    meter(db, settings, now - timedelta(minutes=5), 6)
    meter(db, settings, now - timedelta(minutes=2), 6)
    assert not home_charging.status(db, car, now)['confirmed']


def test_meter_samples_arriving_after_car_reading_can_rule_out_home(db, settings):
    now, car = setup(db, settings)
    read(db, car, now)
    meter(db, settings, now + timedelta(minutes=1), 1)
    assert home_charging.status(db, car, now + timedelta(minutes=1))['confirmed']
    meter(db, settings, now + timedelta(minutes=3), 1)
    assert home_charging.status(db, car, now + timedelta(minutes=3))['source'] == 'power_mismatch'


def test_start_settling_period_avoids_rejecting_lagged_home_readings(db, settings):
    now, car = setup(db, settings, charging=False)
    read(db, car, now)
    meter(db, settings, now - timedelta(minutes=1), .5)
    meter(db, settings, now, .5)
    assert home_charging.status(db, car, now)['confirmed']


def test_plan_created_before_arrival_does_not_need_a_probe_on_first_connection(db, settings):
    now, car = setup(db, settings, plugged=False, charging=False)
    read(db, car, now)
    assert home_charging.status(db, car, now)['confirmed']
    assert not home_charging.status(db, car, now).get('needs_verification')


def test_returning_after_shops_can_verify_once_and_continuous_plug_does_not_repeat(db, settings):
    now, car = setup(db, settings)
    read(db, car, now - timedelta(minutes=5), plugged=False, charging=False)
    read(db, car, now)
    assert home_charging.status(db, car, now)['needs_verification']
    record = db.meta(home_charging.KEY)[car.id]
    record.update(probe_done=True, needs_verification=False)
    db.set_meta(home_charging.KEY, {car.id: record})
    read(db, car, now + timedelta(minutes=1), charging=False)
    read(db, car, now + timedelta(minutes=2))
    assert home_charging.status(db, car, now + timedelta(minutes=2))['tested_this_session']
    assert not home_charging.status(db, car, now + timedelta(minutes=2)).get('needs_verification')


def test_paired_probe_confirms_only_matching_stop_and_restart_and_cannot_repeat(db, settings):
    now, car = setup(db, settings)
    read(db, car, now)
    meter(db, settings, now - timedelta(seconds=1), 2.5)
    plugin = SimpleNamespace(stop_charging=AsyncMock(), start_charging=AsyncMock())
    devices = SimpleNamespace(vehicle=lambda *_: plugin)
    notify = SimpleNamespace(emit=Mock())

    async def run():
        await home_charging.begin_probe(db, settings, devices, car, now, explicit=True)
        plugin.stop_charging.assert_awaited_once()
        read(db, car, now + timedelta(seconds=30), charging=False)
        meter(db, settings, now + timedelta(seconds=60), .6)
        await home_charging.monitor(db, settings, devices, notify, now + timedelta(seconds=65))
        plugin.start_charging.assert_awaited_once()
        read(db, car, now + timedelta(seconds=95))
        meter(db, settings, now + timedelta(seconds=125), 2.5)
        await home_charging.monitor(db, settings, devices, notify, now + timedelta(seconds=130))
        assert not db.meta(home_charging.PROBES)
        assert home_charging.status(db, car, now + timedelta(seconds=130))['source'] == 'paired_power_test'
        with pytest.raises(PluginError, match='already'):
            await home_charging.begin_probe(db, settings, devices, car, now + timedelta(seconds=140), explicit=True)
    asyncio.run(run())


def test_probe_restarts_within_three_minutes_even_if_home_meter_does_not_update(db, settings):
    now, car = setup(db, settings)
    read(db, car, now)
    meter(db, settings, now - timedelta(seconds=1), 2.5)
    plugin = SimpleNamespace(stop_charging=AsyncMock(), start_charging=AsyncMock())
    devices = SimpleNamespace(vehicle=lambda *_: plugin)
    notify = SimpleNamespace(emit=Mock())

    async def run():
        await home_charging.begin_probe(db, settings, devices, car, now, explicit=True)
        read(db, car, now + timedelta(seconds=30), charging=False)
        await home_charging.monitor(db, settings, devices, notify, now + timedelta(minutes=3))
        plugin.start_charging.assert_awaited_once()
        read(db, car, now + timedelta(minutes=4))
        await home_charging.monitor(db, settings, devices, notify, now + timedelta(minutes=6))
        await home_charging.monitor(db, settings, devices, notify, now + timedelta(minutes=7))
        assert not db.meta(home_charging.PROBES)
        plugin.stop_charging.assert_awaited_once()
        plugin.start_charging.assert_awaited_once()
    asyncio.run(run())


def test_probe_never_stops_a_car_it_cannot_restart(db, settings):
    now, car = setup(db, settings)
    car.plugin_config = {'allow_charge_stop': True}
    devices = SimpleNamespace(vehicle=Mock())
    with pytest.raises(PluginError, match='both'):
        asyncio.run(home_charging.begin_probe(db, settings, devices, car, now, explicit=True))
    devices.vehicle.assert_not_called()
