# Algorithms, assumptions and uncertainty

Everything here is deliberately simple and explainable. Each component has a version string
stored with its output so results can be compared across changes.

## 1. Data pipeline
- Each enabled system is polled every `site.poll_seconds` (default 2 min on new installs). Readings are stored
  per system (`system_readings`) and combined into a site reading (`site_readings`): PV and battery
  powers summed; grid import/export and load from the site-meter system; load derived from the AC
  energy balance if the meter doesn't report it.
- Power is integrated into half-hour **intervals** with the trapezoid rule. Segments longer than
  15 minutes are not integrated (gap). Every new reading recomputes the affected slots from the
  stored readings, so late/out-of-order data can't double count.
- `output_limited` marks readings where a DC-coupled inverter was at ≥97% of its AC limit with PV
  present — PV may have been curtailed, so these hours are excluded from solar model training.
- EV charging power is taken from the vehicle plugin, or estimated (charger kW) when the car reports
  charging at home (`ev_estimated=1`). It is subtracted from load to learn *house* load.

## 2. Forecast (`forecast.py`, version `profile-temperature-v2+hourly-ratio-v2`)
Weather-enabled plans use forecasts fetched within 24 hours. Within that limit, records older than six hours get a conservative blend of higher load and lower solar. Missing or older hourly records use the fallback. Early accuracy uses the same error calculation as the final daily evaluation, counting only finished half-hours with at least 25 minutes of measurements. The comparison charts use those same complete slots and show delivered solar even when output was limited. Electricity use includes the EV. Battery percentage pairs the original plan’s end-of-slot projection with the recorded site battery level, including in monitoring mode.

**House load**, per half-hour slot:
`profile[day type, slot] + b_cool·(CDD − CDD_ref) + b_heat·(HDD − HDD_ref)`
- Profile: recency-weighted (half-life 28 days), 10–90% winsorised mean of observed house load over
  the last `history_days` (84). Uses the same weekday if ≥3 samples, else weekday/weekend (≥4), else
  all days, else a declared cold-start shape scaled to imported daily totals (or 20–26 kWh/day).
- CDD/HDD = degrees above `cooling_base_c` / below `heating_base_c` at that hour.
  `b_cool`, `b_heat` are least-squares fits on residuals, clipped to 0–1.5 kW/°C, fitted only with
  ≥14 days of data and ≥40 hot (or cold) half-hours. Otherwise 0.
- p10/p90: empirical residual quantiles per slot. They are ranges, not calibrated probabilities.
- Excluded periods and holidays are never used for training. Holidays replace the forecast with
  `holiday_base_load_kw` (or scale it by the holiday's `load_factor`).

**Solar**, per system and hour:
`pv = ratio[local hour] × shortwave radiation (W/m²)`, capped at the system's `pv_dc_max_kw`.
- `ratio` = median of observed PV/radiation for that hour over 45 days, excluding hours with limited
  output or a full battery. It absorbs orientation, tilt, shading and losses without asking for them.
  <5 samples → all-hours median (≥20 samples) → default `pv_kwp × 0.85 / 1000`.
- p10 = 10th percentile ratio, further reduced by cloud cover; p90 = 90th percentile.
- Without usable weather (>24 h old or missing) a cautious clear-sky shape at 45% of nameplate is used.
- **Important:** PV is *not* capped at the inverter's AC rating. A hybrid inverter can put DC PV
  straight into the battery while its AC output is at the limit. For example, a hybrid receiving
  8 kW from its panels could send 5 kW to the AC side and 3 kW to a DC battery.

**EV**: per vehicle, if charging now → energy to reach `target_pct` from now at `charger_kw`;
otherwise sessions on `schedule.days` at `start_hour` sized from live SoC (first session) or
weekly km × kWh/100km ÷ sessions. Away overrides/holidays remove sessions.

**Risk setting**: `balanced` plans on the centre; `conservative` moves load halfway to p90 and solar
halfway to p10; `optimistic` the opposite.

**Evaluation**: after 25 h, each forecast's first 24 h is scored against observed intervals
(`evaluations`: MAE, bias, band coverage for house and solar).

## 3. Planner (`optimizer.py`, version `milp-dc-coupled-v1`)
Mixed-integer linear program solved by HiGHS (SciPy) over the forecast horizon (48 h, first step
partial, then half-hours). See the module docstring for the full constraint list. Key ideas:
- Per system: PV splits into AC output, battery (DC) and curtailment; AC output (PV + discharge)
  ≤ `ac_kw`; battery charge (PV + grid) ≤ `charge_kw`; binaries forbid simultaneous charge and
  discharge and simultaneous import and export.
- Battery efficiency: one-way √(round-trip) on charge and discharge.
- State of charge stays within [reserve, max]; if it starts below reserve, the floor is the starting
  level (no energy is invented).
- Tariff: per-interval import/export rate from the active plan. Daily allowances become two price
  tiers; the better tier is capped at what remains of today's allowance (measured use, with
  unmeasured time counted at the connection limit) or the full allowance on future days.
  Plan intervals split at tariff boundaries and at the configured buffer (5 minutes by default)
  inside a cheaper import or higher export window. The buffered edges restrict battery grid
  charging or battery export respectively. Billing rates, household use and passive solar
  export still follow the actual tariff. A free or negative import price reserves that window
  for charging and self-use. Battery export and the active solar feed-in-priority mode are
  kept outside those windows, while surplus solar can still export naturally.
- Battery exports retain the normal reserve plus expected net house demand from the end of the
  export step to 06:00 local time, converted to stored energy using discharge efficiency.
  An extra margin uses half the gap between high and typical demand forecasts, capped at four
  battery percentage points by default. Stable hours add less. The export floor applies only
  to selling battery energy. Self-use can spend the retained energy down to the normal reserve.
  Without a contiguous forecast through 06:00, the plan allows house discharge and solar export
  as usual and holds off on battery export.
- Objective: import cost − export credit + `wear_cents_per_kwh` × discharge − terminal value ×
  stored energy at the end. Terminal value = `terminal_value_factor` × 20th-percentile import price
  over the final 24 h × discharge efficiency (low enough not to cause grid charging for it, high
  enough not to dump the battery at the horizon end).
- Tie-breakers: tiny cost on curtailment and grid charging; tiny reward for stored energy decaying
  with lead time (≤0.5c/kWh/day), so among equal plans it charges sooner (more robust to forecast
  error) but never at the expense of real price differences.
- The solution is checked against all bounds/constraints before use; otherwise the **self-use
  simulation** is stored as a fallback (and automatic control is blocked).

**Actions** (per system, per interval) derived from the solution:
`force_charge` (grid charging), `force_discharge` (battery exporting), `export_priority`
(exporting PV while the battery has room — keeping headroom for PV above the AC limit, FoxESS
"Feed-in First"), `hold` (importing while the battery has energy), else `self_use`.
Each interval stores a plain-English reason.

**Modelled saving** = self-use baseline cost − plan cost + terminal value × (plan − baseline
end-of-horizon energy). It is a model-vs-model comparison on the same forecast, not a measurement.

## 4. Advice (`advice.py`)
Marginal cost of extra load in a planned interval: first from planned curtailment (free), then
planned export (lose the feed-in rate), then import (that interval's price). Used for the cheapest
EV charging window, the AC opportunity signal (hot/cold forecast + marginal cost ≤ `ac_max_cents`)
and clipping alerts.

EV advice learns typical battery drain from up to sixteen weeks of vehicle readings. It requires
three days for each local weekday, each with at least 18 hours covered by readings at most 12
hours apart. Charging, holidays and excluded days do not contribute to learned drain. Repeating
two-, three- and four-week rosters are considered when each phase has three samples. The model
uses a roster for a weekday when holding out individual observations shows an average improvement
of at least one battery percentage point and 25% over the weekday median.

Optional Monday-to-Sunday driving estimates fill in for weekdays that lack sufficient history,
using the vehicle's configured efficiency and capacity. Zero kilometres is a valid estimate.
The seven-day projection assumes no charging and prorates today's typical drain over the remaining
hours. Unknown days leave the exact projection unavailable. If known driving alone would reach
the reminder level, advice can use that conditional estimate. Current battery levels at or below
the charge reminder level can still trigger advice while the model learns.

When projected drain reaches the reminder level within the energy plan's horizon, advice prices
continuous charging windows before the expected low day, using the plan's spare solar and grid
prices. If time is short it looks for a partial top-up. A fresh home-presence reading is required
for notifications. Dashboard advice is conditional when presence is unknown, and marked-away
periods are unavailable for charging. This is advice only and does not change scheduled EV load
or send any charger commands.

## 5. Outcomes (`outcomes.py`)
The Overview cost card applies the tariff to measured grid flows for today, yesterday and the
current and previous Monday-start weeks. It includes daily supply on dates with readings and reports
coverage over elapsed time. Today’s projection adds the plan’s remaining energy cost to the
measured total and counts the supply charge once. Plan cost uses the optimiser's saved energy cash flows, so tiered
allowances are already included. It displays imports, export credits and the net energy result.
The value of different ending battery levels and modelled wear are shown separately.

The default Forecast Accuracy charts pair each completed local half-hour with the latest forecast
already available when that slot began. Individual saved runs can still be selected. Hourly and
three-hourly grouped bars sum elapsed half-hours and leave gaps where a series is incomplete.

Per completed day: tariff applied to measured import/export, plus counterfactuals replaying measured
solar and load through (a) simulated self-use from the measured starting SoC, (b) no battery,
(c) no solar. Savings vs self-use are adjusted for end-of-day stored energy. Known limits: measured
PV is already curtailed when output was limited; EV energy may be estimated.

## 6. Backtest (`backtest.py`)
For past days with ≥90% coverage, uses measured load/solar as a *perfect* forecast and compares
optimiser vs self-use under each tariff. PV in output-limited intervals is reconstructed from the
solar model where higher. Upper bound on achievable savings; real results are lower.

## Known gaps / ideas
- Temperature response is one global linear term; per-hour responses would fit AC use better.
- No ensemble weather or stochastic optimisation yet; risk is handled by the risk setting.
- Battery charge taper near 100% and real efficiency are not learned.
- Demand charges and controlled-load circuits are not modelled.
- Daylight saving: tariffs are evaluated in the site's local time zone.
