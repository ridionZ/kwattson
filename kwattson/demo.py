"""Demo mode (DEMO_MODE=true): a simulated site with three weeks of synthetic history.

Lets people try the dashboard without hardware, and gives tests realistic data.
Never mixes with real devices: demo settings use only simulator plugins and
automatic control of real plugins is blocked while DEMO_MODE is on.
"""
from __future__ import annotations

import math
import os
import random
from datetime import datetime, timedelta, timezone

from .config import EnergySystem, BatterySpec, Settings, Vehicle, VehicleSchedule, stamp, utcnow
from .db import Database
from .ingest import rebuild_intervals
from .tariffs import Allowance, Period, TariffPlan
from .plugins.simulator import sim_load, sim_pv


def demo_tariff() -> TariffPlan:
    """A generic time-of-use plan that exercises every planner feature."""
    return TariffPlan(
        id='demo-tou', name='Demo time-of-use', retailer='Example Energy', supply_daily=1.10, status='active',
        periods=[Period(name='overnight', label='Overnight', start='22:00', end='07:00', import_rate=0.22, export_rate=0.03),
                 Period(name='morning', label='Shoulder', start='07:00', end='10:00', import_rate=0.30, export_rate=0.03),
                 Period(name='solar_soak', label='Solar soak', start='10:00', end='15:00', import_rate=0.0, export_rate=0.0),
                 Period(name='afternoon', label='Shoulder', start='15:00', end='16:00', import_rate=0.30, export_rate=0.03),
                 Period(name='peak', label='Peak', start='16:00', end='21:00', import_rate=0.48, export_rate=0.15),
                 Period(name='late', label='Shoulder', start='21:00', end='22:00', import_rate=0.30, export_rate=0.03)],
        allowances=[Allowance(kind='import', periods=['solar_soak'], kwh_per_day=30, rate_after=0.30),
                    Allowance(kind='export', periods=['peak'], kwh_per_day=20, rate_after=0.03)],
        notes='Demo tariff, not a real retail plan.')


def demo_settings() -> Settings:
    base = Settings()
    site = base.site.model_copy(update=dict(name='Demo house', timezone='Australia/Brisbane', latitude=None, longitude=None, weather_enabled=False))
    return base.model_copy(update=dict(
        site=site,
        tariffs=[demo_tariff()],
        systems=[EnergySystem(id='main', name='Simulated hybrid', plugin='simulator', ac_kw=5, pv_kwp=8, pv_dc_max_kw=9,
                              battery=BatterySpec(capacity_kwh=16, charge_kw=4, discharge_kw=4), controllable=True, site_meter=True)],
        vehicles=[Vehicle(id='car', name='Demo EV', plugin='simulator_vehicle', capacity_kwh=60, schedule=VehicleSchedule(days=[1, 4]))]))


def seed(db: Database, security, days: int = 21):
    if db.meta('demo_seeded'):
        return
    settings = demo_settings()
    db.save_settings(Settings.model_validate(settings.model_dump()), 'demo')
    password = os.getenv('DEMO_PASSWORD')
    if password and not security.owner_exists():
        security.create_owner('demo', password)
    rng = random.Random(42)
    zone = settings.zone
    end = utcnow().replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    system = settings.systems[0]
    battery = system.battery
    soc = 50.0
    rows, weather = [], []
    cursor = start
    clouds = {}
    while cursor < end:
        local = cursor.astimezone(zone)
        cloud = clouds.setdefault(local.date(), rng.choice([0.0, 0.05, 0.1, 0.3, 0.6, 0.85]))
        pv = min(system.pv_limit_kw, sim_pv(system.pv_kwp, local, cloud) * rng.uniform(0.93, 1.03))
        load = sim_load(0.55, local) * rng.uniform(0.85, 1.25) + (1.6 if local.weekday() >= 5 and 10 <= local.hour < 16 else 0)
        to_load = min(pv, load)
        surplus = pv - to_load
        charge = min(surplus, battery.charge_kw, max(0.0, (100 - soc) / 100 * battery.capacity_kwh * 12))
        export = min(surplus - charge, system.ac_kw - to_load)
        deficit = load - to_load
        discharge = min(deficit, battery.discharge_kw, max(0.0, (soc - battery.reserve_pct) / 100 * battery.capacity_kwh * 12))
        imp = deficit - discharge
        soc = min(100.0, max(0.0, soc + (charge * 0.95 - discharge / 0.95) / 12 / battery.capacity_kwh * 100))
        rows.append((stamp(cursor), soc, soc / 100 * battery.capacity_kwh, battery.capacity_kwh, pv - (surplus - charge - export), load, imp, export, charge, discharge,
                     0.0, 0, int(to_load + export >= system.ac_kw - 0.1), 0, 1, '{}', stamp()))
        cursor += timedelta(minutes=5)
    hour = start.replace(minute=0)
    while hour < end + timedelta(days=4):
        local = hour.astimezone(zone)
        cloud = clouds.get(local.date(), 0.2)
        temp = 22 + 7 * math.sin(math.pi * (local.hour - 9) / 12) + rng.uniform(-1, 1)
        radiation = max(0.0, 1000 * math.sin(math.pi * (local.hour - 0.5 - 5.5) / 13)) * (1 - 0.75 * cloud) if 6 <= local.hour <= 19 else 0.0
        weather.append((stamp(hour), temp, temp + 1, radiation, cloud * 100, stamp(end), round((hour - end).total_seconds() / 3600, 2)))
        hour += timedelta(hours=1)
    with db.connect() as c:
        c.executemany('INSERT OR REPLACE INTO site_readings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
        c.executemany('INSERT OR REPLACE INTO system_readings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      [(r[0], 'main', 'simulator', r[1], r[4], r[8], r[9], None, r[6], r[7], r[5], None, 'simulated', 0, 1, '{}', r[16]) for r in rows])
        c.executemany('INSERT OR REPLACE INTO weather_hours VALUES(?,?,?,?,?,?,?)', weather)
        rebuild_intervals(c, start, end)
    db.set_meta('demo_seeded', stamp())
