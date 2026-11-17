"""The no-integration vehicle: state of charge entered by hand (or not at all)."""
from __future__ import annotations

from .base import VehiclePlugin, VehicleReading


class ManualVehicle(VehiclePlugin):
    name = 'manual'
    title = 'Manual (no connection)'
    description = 'Uses the weekly driving estimate and any state of charge you type in. No credentials needed.'

    async def read(self) -> VehicleReading | None:
        return None  # the forecaster falls back to Vehicle.manual_soc and the weekly schedule
