"""External plugin example. All readings are synthetic."""
from datetime import datetime, timezone

from pydantic import Field

from kwattson.plugins.base import PluginConfig, SystemPlugin, SystemReading


class MeterConfig(PluginConfig):
    load_kw: float = Field(1.5, ge=0, le=20, description="Simulated household demand in kW")


class ExampleMeter(SystemPlugin):
    name = "example_meter"
    title = "Example meter (synthetic)"
    description = "Returns a fixed household load for testing plugin installation."
    Config = MeterConfig

    async def read(self):
        return SystemReading(ts=datetime.now(timezone.utc), time_verified=False,
                             load_kw=self.config.load_kw, import_kw=self.config.load_kw)
