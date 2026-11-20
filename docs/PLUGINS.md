# Writing a hardware plugin

A plugin teaches kWattson to **read** (and optionally **control**) one kind of
inverter, battery or vehicle. Plugins never touch the forecaster, optimiser,
database or web code: they only implement the small contract in
[`kwattson/plugins/base.py`](../kwattson/plugins/base.py).

You often don't need a plugin at all:

| Situation | Use |
|---|---|
| Home Assistant already knows your device | built-in `homeassistant` / `homeassistant_vehicle` plugins (map entity ids) |
| You have a script, Node-RED flow or device that can make HTTP requests | built-in `push` / `push_vehicle` plugins: POST readings to `/api/ingest/...` |
| You want direct cloud/local API support, or battery **control** | write a plugin (this guide) |

## Concepts

- **System** (`config.EnergySystem`): one inverter plus whatever is attached to it, either
  `coupling="dc"` (hybrid: PV and battery share a DC bus) or `coupling="ac"` (battery inverter,
  or PV-only inverter). A site can have several systems from different vendors. Physical
  limits (`ac_kw`, `pv_kwp`, `pv_dc_max_kw`, `battery.*`) live in settings, not in the plugin,
  so the optimiser can reason about any vendor.
- **Site meter**: exactly one system (or the only system) reports whole-site grid import/export
  and household load. Other systems report just PV/battery.
- **Vehicle**: an EV with optional data plugin. Vehicle *control* is not part of the contract yet.

## Build and install the example

The [example meter](../examples/meter-plugin/) is a complete, independently installable Python package.
Its reading comes from a number you enter in the wizard. From the kWattson source directory, run:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e . ./examples/meter-plugin
.venv/bin/python -c "from kwattson.plugins.registry import get; print(get('example_meter', 'system').title)"
```

The last command should print `Example meter (synthetic)`. Start a disposable **normal-mode** install
to exercise the same discovery and onboarding a device owner will see:

```sh
export DATA_DIR="$(mktemp -d)" DEMO_MODE=false DEV_HTTP=true
export APP_URL=http://localhost:8080 HOST=127.0.0.1 PORT=8080
.venv/bin/kwattson setup-token
.venv/bin/kwattson serve
```

Open the local address, create the admin account and select **Add a system → Example meter (synthetic)**.
For this fixture enter 5 kW inverter power, 0 kWp panels and turn off **Has a battery**. Give it a
name, run **Test connection** and save. The home screen will show the meter after the worker polls.
Demo mode offers only the built-in local data sources. Restart the server after installing a package.
If the example is missing, check that the server uses the same Python environment and that
`KWATTSON_EXTERNAL_PLUGINS` is enabled.

To build an installable wheel for another host, run
`.venv/bin/python -m pip wheel --no-deps ./examples/meter-plugin -w dist`.
Install the wheel with the Python interpreter that runs kWattson. To include a plugin in Docker, put the
wheel beside this derived Dockerfile in a separate build directory, then build and deploy that image:

```dockerfile
FROM kwattson:latest
USER root
COPY *.whl /tmp/plugin/
RUN python -m pip install --no-cache-dir /tmp/plugin/*.whl && rm -r /tmp/plugin
USER 99:100
```

Match the final user to your base image if you built it with custom IDs. Plugins run Python code
inside the server, so review their source before installation. To build your own plugin, copy the
example into a separate project, rename the distribution, module and entry point, and implement
the reading contract below. The wizard uses the configuration fields and labels your class declares.

## Minimal read-only system plugin

```python
# kwattson_acme/__init__.py  (its own pip package)
from datetime import datetime, timezone
import httpx
from pydantic import Field
from kwattson.plugins.base import (PluginConfig, PluginError, SecretField,
                                       SystemPlugin, SystemReading)

class AcmeConfig(PluginConfig):          # non-secret settings, shown as a form in the UI
    host: str = Field(description='Inverter address, e.g. https://192.168.1.50')

class AcmeInverter(SystemPlugin):
    name = 'acme'                         # unique registry key
    title = 'ACME Hybrid (local API)'
    description = 'Reads ACME hybrid inverters over the local HTTPS API.'
    Config = AcmeConfig
    secrets = (SecretField('password', 'Installer password'),)   # stored encrypted

    async def read(self) -> SystemReading:
        self.context.check_url(self.config.host)                    # SSRF guard: required for user-supplied URLs
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, verify=True) as client:
            r = await client.get(self.config.host + '/api/live',
                                 auth=('installer', self.context.secret('password') or ''))
            r.raise_for_status()
            d = r.json()
        self.context.record_raw({k: d[k] for k in ('time', 'soc', 'pv_w', 'battery_w', 'grid_w', 'load_w')})
        if not 0 <= d['soc'] <= 100:
            raise PluginError('ACME returned an invalid state of charge')
        return SystemReading(
            ts=datetime.fromtimestamp(d['time'], timezone.utc), time_verified=True,
            soc=d['soc'], pv_kw=d['pv_w'] / 1000,
            charge_kw=max(0, -d['battery_w']) / 1000, discharge_kw=max(0, d['battery_w']) / 1000,
            import_kw=max(0, d['grid_w']) / 1000, export_kw=max(0, -d['grid_w']) / 1000,
            load_kw=d['load_w'] / 1000, fault=d['state'] == 'FAULT', running_state=d['state'])
```

Register it in **your** package's `pyproject.toml` — no change to this repository:

```toml
[project.entry-points."kwattson.plugins"]
acme = "kwattson_acme:AcmeInverter"
```

`pip install kwattson-acme` into the same environment (or a derived Docker image) and
"ACME Hybrid" appears in *Settings → Systems*. Built-in plugins are listed in
`plugins/registry.py:BUILTIN` instead.

## Reading rules

- All power values are **kW and positive**; direction is in the field name.
- `ts` must be timezone-aware. Set `time_verified=True` only if the timestamp came from the device
  (automatic control requires verified timestamps).
- Leave unknown values as `None` — never guess zeros.
- Raise `PluginError("short owner-friendly message")` for expected failures. Messages are shown in
  the UI and logs, so **never include tokens, passwords or full URLs** in them.
- A connection test can return `hardware` beside `reading`: `ac_kw` (AC rating),
  `capacity_kwh` (reported battery design capacity), `reserve_pct` (reported on-grid reserve),
  `min_soc_pct` (hardware floor), `scheduler_limit_kw` (AC control limit), and scheduling
  capabilities. `connection.serial` identifies an auto-discovered inverter; `warnings` is a
  list of setup cautions. Return only reported values. Do not present an AC limit as a battery
  DC charge rating, or a hardware minimum as the owner's chosen reserve.
- `read()` must not change device settings. It may be called every 1–5 minutes; respect vendor rate
  limits (see `foxess.py` for a shared lock + minimum interval).
- Use `self.context.remember/recall` for small non-secret state (e.g. a discovered serial).

## Adding control

Set `can_control = True`, list `supported_modes`, and implement:

```python
async def apply(self, command: BatteryCommand) -> CommandResult: ...
async def release(self) -> CommandResult: ...
```

`BatteryCommand.mode` is one of:

| mode | meaning |
|---|---|
| `self_use` | device's normal mode (the core calls `release()` instead of sending this) |
| `force_charge` | charge towards `target_soc` at up to `power_kw`, from grid if needed |
| `force_discharge` | discharge at `power_kw` (exporting) down to `target_soc` |
| `hold` | don't discharge (keep at least `min_soc`); PV may still charge |
| `export_priority` | PV serves home then grid first; battery only takes what can't be exported (keeps headroom for clipped DC PV) |

Contract:
- A command covers `start`–`end` (≤ 30 min). The core refreshes it while the plan wants it.
  If your device can only do recurring schedules, make sure `release()` removes them.
- Return `CommandResult(accepted=True, verified=True)` **only** after reading the device's settings
  back and confirming they match. Unverified results immediately stop automatic control.
- `release()` must put the device back in its own normal mode and verify it.
- The core separately checks the *physical* response (does the battery actually charge/discharge?)
  and stops control after two misses.
- Document limitations in `control_notes`; they're shown to the owner before commissioning.

For daily scheduling, expose `supports_battery_schedule = True` and implement
`apply_schedule(commands: list[BatteryCommand])` plus the read-only
`verify_schedule(commands)`. The controller sends the adopted plan's remaining local day,
keeps its confirmed command list, checks every five minutes, and does not refresh short
windows. `apply_schedule` should reuse an already-matching enabled program without writes.
Reject oversized, overlapping, unsupported, or out-of-range programs rather than dropping
groups. A stop/fault must disable every future group. Explain recurrence when offline in
`control_notes`. Individual commands within a daily program can exceed 30 minutes.

## Optional: history

Implement `history(start, end) -> list[SystemReading]` so owners can backfill with
`kwattson backfill --days 60`. More history = better forecasts from day one.

## Vehicle plugins

Subclass `VehiclePlugin`, implement `read() -> VehicleReading | None` (return `None` when there is
nothing new). Cloud vehicle APIs often lock accounts if polled too often: throttle inside the plugin
(see `mg_ismart.py`). Store only energy-relevant data — no GPS tracks, VINs or trip history.
Plugins with a remote stop can expose `supports_charge_stop` and implement `stop_charging()`.
Report command errors as `PluginError` without credentials. A successful call means the request
was accepted. Subsequent `VehicleReading.charging` confirms whether the car stopped. Plugins that also support scheduling can expose
`supports_charge_schedule` and implement `start_charging()`. Return `plugged_in` for ready-by
automation. Settings and an individual session must both opt in to start requests.

## Testing your plugin

- Unit-test parsing against recorded (anonymised) responses.
- Run the disposable normal-mode install described above, using synthetic or anonymised responses.
- Read-only *Test connection* in the UI calls `test()`; override it to add useful diagnostics.
- Never test `apply()` on real hardware without the owner present and the inverter app open.
- `plugins/simulator.py` implements everything (including control) without hardware — copy it.

## Checklist for a pull request
- [ ] No vendor code outside your plugin module.
- [ ] Config model has descriptions; secrets declared as `SecretField`.
- [ ] Errors are short and credential-free.
- [ ] Tests with recorded fixtures (no real serials, keys or addresses).
- [ ] Control verified by read-back; `release()` implemented; limitations documented.
