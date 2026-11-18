"""A dependency-free simulated hybrid inverter and EV.

Useful for demos, tests and as a reference when writing a new plugin: it shows
every method a system plugin can implement. It models the DC/AC split of a
hybrid inverter, so clipping above ``ac_kw`` is visible when the battery is full.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import Field

from .base import (BatteryCommand, CommandResult, PluginConfig, SystemPlugin, SystemReading, VehiclePlugin,
                   VehicleReading)


class SimConfig(PluginConfig):
    cloudiness: float = Field(0.1, ge=0, le=1, description='0 = clear sky, 1 = overcast')
    base_load_kw: float = Field(0.6, ge=0, le=20)
    start_soc: float = Field(50, ge=0, le=100)


def sim_pv(kwp: float, local: datetime, cloud: float) -> float:
    hour = local.hour + local.minute / 60
    if not 5.5 <= hour <= 18.5:
        return 0.0
    shape = max(0.0, math.sin(math.pi * (hour - 5.5) / 13)) ** 1.5
    return kwp * 1.02 * shape * (1 - 0.75 * cloud)


def sim_load(base: float, local: datetime) -> float:
    hour = local.hour + local.minute / 60
    morning = 1.2 * math.exp(-((hour - 7.5) ** 2) / 1.5)
    evening = 2.5 * math.exp(-((hour - 18.5) ** 2) / 3)
    return base + morning + evening


class SimulatedHybrid(SystemPlugin):
    name = 'simulator'
    title = 'Simulator (demo)'
    description = 'A simulated hybrid inverter with battery. Safe for demos and testing; controls nothing real.'
    Config = SimConfig
    can_control = True
    supported_modes = ('self_use', 'force_charge', 'force_discharge', 'hold', 'export_priority')
    control_notes = 'Simulated only.'

    async def read(self) -> SystemReading:
        now = datetime.now(timezone.utc)
        local = now.astimezone(ZoneInfo(self.context.timezone))
        battery = self.system.battery
        state = self.context.recall('state') or {'soc': self.config.start_soc, 'at': now.isoformat()}
        elapsed_h = min(1.0, max(0.0, (now - datetime.fromisoformat(state['at'])).total_seconds() / 3600))
        command = self.context.recall('command')
        if command and not (datetime.fromisoformat(command.get('start', state['at'])) <= now < datetime.fromisoformat(command['end'])):
            command = None
        mode = command['mode'] if command else 'self_use'

        pv = min(sim_pv(self.system.pv_kwp, local, self.config.cloudiness), self.system.pv_limit_kw)
        load = sim_load(self.config.base_load_kw, local)
        soc = state['soc']
        capacity = battery.capacity_kwh if battery else 0
        room_kw = max(0.0, (battery.max_soc_pct - soc) / 100 * capacity) / max(elapsed_h, 1 / 12) if battery else 0
        avail_kw = max(0.0, (soc - battery.reserve_pct) / 100 * capacity) / max(elapsed_h, 1 / 12) if battery else 0
        charge = discharge = grid_charge = 0.0
        pv_ac = min(pv, load, self.system.ac_kw)
        surplus = pv - pv_ac
        if battery:
            eff = math.sqrt(battery.round_trip_efficiency)
            if mode == 'force_charge' and command['target_soc'] is not None:
                room_kw = min(room_kw, max(0.0, command['target_soc'] - soc) / 100 * capacity /
                              (eff * max(elapsed_h, 1 / 12)))
            if mode == 'export_priority':
                to_grid = min(surplus, self.system.ac_kw - pv_ac)
                charge = min(surplus - to_grid, battery.charge_kw, room_kw)
            else:
                charge = min(surplus, battery.charge_kw, room_kw)
            if mode == 'force_charge':
                grid_charge = min(battery.charge_kw - charge, room_kw - charge, command['power_kw'] or battery.charge_kw)
                grid_charge = max(0.0, grid_charge)
            deficit = load - pv_ac
            if mode == 'force_discharge':
                target = max(battery.reserve_pct, command['target_soc'] or 0)
                avail_kw = max(0.0, soc - target) / 100 * capacity * eff / max(elapsed_h, 1 / 12)
                discharge = min(battery.discharge_kw, avail_kw, command['power_kw'] or battery.discharge_kw, self.system.ac_kw - pv_ac)
            elif mode != 'hold':
                discharge = min(deficit, battery.discharge_kw, avail_kw, self.system.ac_kw - pv_ac)
        ac_out = min(self.system.ac_kw, pv - charge + discharge)
        net = load + grid_charge - ac_out
        imp, exp = max(0.0, net), max(0.0, -net)
        if battery:
            eff = math.sqrt(battery.round_trip_efficiency)
            soc += ((charge + grid_charge) * eff - discharge / eff) * elapsed_h / capacity * 100
            soc = max(0.0, min(100.0, soc))
        self.context.remember('state', {'soc': soc, 'at': now.isoformat()})
        return SystemReading(ts=now, time_verified=True, soc=round(soc, 2) if battery else None, pv_kw=round(min(pv, charge + max(0.0, ac_out - discharge)), 3),
                             charge_kw=round(charge + grid_charge, 3), discharge_kw=round(discharge, 3), ac_output_kw=round(ac_out, 3),
                             import_kw=round(imp, 3), export_kw=round(exp, 3), load_kw=round(load, 3),
                             running_state='simulated:' + mode, fault=False)

    async def history(self, start, end):
        return []

    async def apply(self, command: BatteryCommand) -> CommandResult:
        self.context.remember('command', {'mode': command.mode, 'start': command.start.isoformat(), 'end': command.end.isoformat(),
                                          'power_kw': command.power_kw, 'target_soc': command.target_soc})
        return CommandResult(accepted=True, verified=True, detail={'simulated': True, 'mode': command.mode})

    async def release(self) -> CommandResult:
        self.context.remember('command', None)
        return CommandResult(accepted=True, verified=True, detail={'simulated': True})


class SimulatedVehicle(VehiclePlugin):
    name = 'simulator_vehicle'
    title = 'Simulated EV (demo)'
    description = 'Reports a fixed state of charge for demos.'

    class Config(PluginConfig):
        soc: float = Field(55, ge=0, le=100)

    async def read(self) -> VehicleReading:
        return VehicleReading(ts=datetime.now(timezone.utc) - timedelta(minutes=2), time_verified=True,
                              soc=self.config.soc, charging=False, plugged_in=False, at_home=True)
