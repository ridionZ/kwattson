---
name: add-hardware-plugin
description: Add support for a new inverter, battery, solar system or EV to kWattson as a plugin. Use whenever someone asks to support a new brand/model of hardware, connect a device kWattson can't read yet, or add battery control for a vendor.
---

# Add a hardware plugin to kWattson

kWattson's core never contains vendor code. New hardware is a plugin implementing the contract in
`kwattson/plugins/base.py`. Read `docs/PLUGINS.md` first — it is the authoritative guide — then:

1. **Check whether a plugin is needed at all.** If Home Assistant already integrates the device, the
   built-in `homeassistant` / `homeassistant_vehicle` plugins (entity mapping) may be enough. If the
   owner can run a script/Node-RED flow, the `push` plugins accept readings at `/api/ingest/...`.
2. **Decide where it lives.** Copy the installable synthetic package in `examples/meter-plugin/` and follow the wheel and discovery checks in `docs/PLUGINS.md`. Prefer a separate pip package registered under the
   `kwattson.plugins` entry point. Only widely used, well-tested plugins become built-ins
   (`kwattson/plugins/<name>.py` + an entry in `plugins/registry.py:BUILTIN`).
3. **Implement reading first** (`read()` → `SystemReading` / `VehicleReading`): kW, positive values,
   direction in the field name; timezone-aware `ts`; `time_verified=True` only for device timestamps;
   `None` for unknowns (never zeros); short credential-free `PluginError` messages; respect vendor
   rate limits; `self.context.check_url()` for any owner-supplied URL; secrets via `SecretField` and
   `self.context.secret()`; `record_raw()` must never receive credentials.
4. **Add `history()`** if the vendor offers it (enables `kwattson backfill`).
5. **Control is optional and dangerous.** Only with `can_control = True`, `apply()` that verifies by
   read-back, `release()` that restores the device's own mode, short command windows, and documented
   `control_notes`. Never call `apply()`/`release()` against real hardware during development; test
   with recorded responses and the `simulator` plugin (`plugins/simulator.py` is the reference).
6. **Tests:** parsing tests from recorded, anonymised fixtures (no serials, keys, addresses, VINs).
   Run `.venv/bin/pytest -q`.
7. **Docs:** add a row to the hardware table in `README.md` and note limitations in the plugin docstring.

Physical limits (AC kW, PV kWp, DC PV max, battery specs, coupling) are settings on the
`EnergySystem`, not plugin config — don't duplicate them in the plugin.
