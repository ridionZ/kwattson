import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('KWATTSON_EXTERNAL_PLUGINS', 'false')

from kwattson.config import Settings, stamp  # noqa: E402
from kwattson.db import Database  # noqa: E402

ZONE = ZoneInfo('Australia/Brisbane')


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / 'test.sqlite3')


TARIFFS = Path(__file__).resolve().parents[1] / 'tariffs'
BATTERY_KWH = 40.0


def make_test_site() -> Settings:
    """A generic test house: a 10 kW hybrid inverter with 13 kWp of panels (DC can reach 14 kW),
    a 40 kWh battery, a flat-rate tariff and a pending public time-of-use plan (CovaU SolarMax)."""
    import json
    from kwattson.config import BatterySpec, EnergySystem, SiteSettings
    from kwattson.tariffs import TariffPlan, default_tariffs
    solarmax = TariffPlan.model_validate({**json.loads((TARIFFS / 'covau-solarmax-qld.json').read_text()), 'id': 'covau-solarmax'})
    return Settings(
        site=SiteSettings(timezone='Australia/Brisbane', latitude=-27.47, longitude=153.03),
        systems=[EnergySystem(id='main', name='Test hybrid', plugin='simulator', coupling='dc', ac_kw=10, pv_kwp=13.0,
                              pv_dc_max_kw=14.0, battery=BatterySpec(capacity_kwh=BATTERY_KWH, charge_kw=8, discharge_kw=8, reserve_pct=15),
                              site_meter=True)],
        tariffs=[*default_tariffs(), solarmax])


@pytest.fixture
def settings():
    return make_test_site()


def solarmax(settings: Settings) -> Settings:
    plan = next(t for t in settings.tariffs if t.id == 'covau-solarmax')
    plan.status = 'active'
    plan.active_from = '2026-01-01T00:00:00+10:00'
    return Settings.model_validate(settings.model_dump())


def sunny_points(start_local: datetime, hours=48, peak_kw=13.5, house_daily=24.0, system='main'):
    """Synthetic forecast points: clear-sky PV peaking at ``peak_kw`` DC and a typical load shape."""
    from kwattson.forecast import COLD_START_SHAPE
    start = start_local.astimezone(timezone.utc)
    points = []
    for i in range(hours * 2):
        at = start + timedelta(minutes=30 * i)
        local = at.astimezone(ZONE)
        hour = local.hour + local.minute / 60
        pv = max(0.0, math.sin(math.pi * (hour - 5.5) / 13)) ** 1.5 * peak_kw if 5.5 < hour < 18.5 else 0.0
        house = house_daily * COLD_START_SHAPE[local.hour] / sum(COLD_START_SHAPE)
        points.append(dict(ts=stamp(at), hours=0.5, lead_minutes=30 * i, house_kwh=house / 2, house_p10=0, house_p90=0, ev_kwh=0.0,
                           load_kwh=house / 2, solar_kwh=pv / 2, solar_p10=pv / 2, solar_p90=pv / 2, solar_by_system={system: pv / 2},
                           temperature=25.0, radiation=None, load_basis='test', solar_basis='test', samples=0))
    return points
