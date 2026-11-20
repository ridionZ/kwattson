# Data dictionary

SQLite database `kwattson.sqlite3` in `DATA_DIR` (WAL mode). Schema version in `meta.schema_version`.
Conventions: timestamps are UTC ISO-8601 text; half-hour rows are keyed by interval **start**;
energy kWh, power kW, state of charge %, money AUD, rates AUD/kWh. JSON columns are noted.

## Measurements
**system_readings** — one row per poll per system. `ts`, `system_id`, `plugin`, `soc`, `pv_kw` (DC PV),
`charge_kw`, `discharge_kw`, `ac_output_kw`, `import_kw`/`export_kw`/`load_kw` (site values, only from the
site-meter system), `battery_temp`, `running_state` (vendor text), `fault` (0/1/NULL), `time_verified`
(timestamp came from the device), `extra` (JSON, small vendor detail), `received_at`.

**site_readings** — combined site view per poll. `soc` (capacity-weighted), `stored_kwh`, `capacity_kwh`,
`solar_kw`, `load_kw`, `import_kw`, `export_kw`, `charge_kw`, `discharge_kw`, `ev_kw`, `ev_estimated`
(EV power inferred from car state), `output_limited` (a DC inverter at its AC limit with PV: PV may be
curtailed), `fault`, `time_verified`, `systems` (JSON per-system readings).

**vehicle_readings** — `vehicle_id`, `soc`, `charging`, `plugged_in`, `at_home`, `charge_kw`, `range_km`, `time_verified`.

**intervals** — half-hour energy. `source`: `telemetry` (integrated from site_readings), or imports such
as `meter`. `solar_kwh`, `load_kwh` (includes EV), `ev_kwh`, `import_kwh`, `export_kwh`, `charge_kwh`,
`discharge_kwh`, `soc_end`, `limited_s` (seconds output-limited), `coverage_s` (seconds actually
measured; <1800 = partial), `quality` (`observed` ≥ 1500 s, else `partial`). A `_with_counters`
quality suffix indicates that at least one metric used a device energy counter. Use
`ingest.select_intervals` to pick one row per slot.

**daily_history** — imported daily totals (e.g. FoxESS monthly reports): JSON `data` with solar/import/
export/house/charge/discharge kWh. No intraday detail.

**raw_readings** — raw vendor payloads for audit, deleted after `raw_retention_days`. Not exported.

**pushed_readings** — latest payload pushed to `/api/ingest/...` per target.

## Context the owner provides
**exclusions** (`start`, `end`, `reason`): never train on these periods (empty by default; add
fault weeks or other unrepresentative periods). **holidays** (`name`, `start`, `end`, `load_factor` NULL = use base load,
`ev_away`). **annotations** (`kind`: note/guests/appliance/outage/maintenance/exclude, `text`).
**overrides** (`kind`: hold, no_battery_export, reserve, ev_charge_now, ev_charge_planned, ev_away; `data` JSON e.g.
`{"pct": 60}`; `starts`, `expires`, `cancelled_at`). EV charging overrides can store a
`vehicle_id`, `target_pct`, `starting_soc`, `stop_at_limit`, and the observed stop state and
request timestamp. Ready-by plans also record the `charge_mode`, permitted `grid_rates`, estimated
final charge and cost, planned window boundaries, and the tariff policy and command state used by
automatic scheduling. Forecast-only future charges store the car ID and use the override
start and expiry as their planned time range.
**settings** (current JSON) and **settings_history** (`changed` sections, full `data` snapshot each change).

## Weather
**weather_hours** — `ts` is the **end** of the averaging hour (Open-Meteo convention). `temperature`,
`apparent_temperature` (°C), `radiation` (shortwave W/m²), `cloud_cover` (%), `fetched_at`,
`lead_hours` (ts − fetched_at; negative = near-observed analysis).

## Forecasts and models
**forecasts** — `id`, `created_at`, `version`, `model` (JSON: load model summary, solar sample counts,
vehicles, holidays, overrides, confidence, weather freshness).
**forecast_points** — per forecast and interval: `lead_minutes`, `hours`, `house_kwh` (+`house_p10`,
`house_p90`), `ev_kwh`, `solar_kwh` (+p10/p90), `temperature`, `radiation`, `load_basis`, `solar_basis`
(text), `samples`, `solar_by_system` (JSON). `model.checkpoint` identifies the first forecast
saved in each local hour. Checkpoints and adopted forecasts retain detail after 30 days;
other historical detail is thinned to the existing three-hour sample.
**evaluations** — `metrics` JSON: samples, house/solar MAE and bias (kWh per half-hour), totals,
`band_coverage` (share of actuals inside p10–p90).
**model_params** — latest fitted parameters (`load`, `solar`), JSON.

## Decisions and control
**decisions** — one per planner run: `version`, `forecast_id`, `mode`, `tariff_id`, `input_hash`
(SHA-256 of inputs), `inputs` (JSON: settings, initial battery energy, overrides, allowance use),
`summary` (JSON: solver status, plan vs baseline cash, modelled saving, curtailment, action, reason).
Every candidate remains stored. `summary.adopted` identifies plans chosen for use;
`summary.adoption` records the reason, changed battery/grid energy, modelled cost change,
and changed schedule minutes. `meta.active_plan` records the adopted decision ID, last
candidate review time and ID, review validity, local day, and a hash of settings/overrides.
The current plan can be older than its last successful background review.
**decision_intervals** — per run and interval: `action`, `fox_mode`, `load_kwh`, `solar_kwh`,
`pv_to_ac_kwh`, `pv_to_battery_kwh`, `grid_to_battery_kwh`, `discharge_kwh`, `import_kwh`, `export_kwh`,
`curtailed_kwh`, `soc_pct`, `buy_rate`, `sell_rate`, `cost`, `baseline_soc_pct`, `baseline_cost`,
`baseline_curtailed_kwh`, `reason`, `systems` (JSON per-system flows and the `command` sent).
**commands** — device commands: `action`, `payload` (JSON), `status` (sending/verified/unverified/
failed/released/...), `result` (JSON, credential-free), `verified_at`, `idempotency_key`.
`daily_schedule` actions contain the vendor-neutral command list and expiry. Results contain
the confirmed vendor time groups and scheduler limit. `meta.control_schedules` retains the
day's confirmed program and last read-only verification time; release removes it.
`manual_<mode>` actions record temporary battery requests and any overlaid daily program.
`meta.battery_manual` holds per-system start/end, mode, power, protected targets, command ID and
verification state. It is cleared on expiry, cancellation or owner stop. `ev_climate_start` and
`ev_climate_stop` record parked climate requests separately from confirmation. `meta.vehicle_climate`
stores only parked state, remote climate mode, temperatures, timestamp, pending request and stop timer.
Vehicle climate metadata contains no GPS, journey, VIN or credentials. These use the existing JSON
metadata and command tables; there is no schema migration.
`meta.vehicle_home` stores connection identity, plug/charge evidence timestamps, a hashed
vendor session marker, positive home evidence, power-mismatch samples and one-test state.
`meta.home_charge_probes` holds the brief verification stage, meter baseline/low/high samples,
restart deadline and confirmation. No car coordinates or journeys are retained. Charge estimates
include `source_windows`, `projection` (timestamp, expected SOC, conservative SOC),
`expected_end_soc`, `expected_finish` and `expected_target_reachable`; these are modelled.
`meta.vehicle_timers` holds the owned native EV window, session ID, desired local minutes,
target, original SOC target, command ID, read-back/clear state and bounded retry counters.
`ev_native_timer` and `ev_native_timer_clear` commands record configuration separately from
physical charging confirmation. `overrides.data.surplus_guard` retains distinct meter sample
timestamps and the fixed restart deadline; saved `charge_windows` are never shifted by polling.
**daily_outcomes** — per local day JSON: measured net cost, import cost, export credit, supply,
counterfactual net costs (`self_use_net_aud`, `no_battery_net_aud`, `no_solar_net_aud`),
`saving_vs_self_use_aud`, energy totals, coverage, decision/command counts.

## Notifications
**notifications** (in-app feed), **notification_rules** (per event JSON), **channels** (name/kind;
configuration is encrypted in `secrets` as `channel:<id>`), **deliveries** (status, attempts, next try).

## Security (never exported)
**users** (`username`, `role` admin/member/viewer, `last_login`, optional encrypted TOTP seed),
**sessions** (hashed tokens), **api_tokens** (hashed), **rate_limits**, **secrets**
(Fernet-encrypted), **audit** (who did what; no secret values).
