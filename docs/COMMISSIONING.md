# Commissioning automatic battery control

kWattson starts in **monitor mode**: it reads, forecasts, plans and advises, but sends nothing
to your inverter. You can follow the *Suggested inverter schedule* on the Plan page by hand
(e.g. set FoxESS "Feed-in First" for the morning on very sunny days) with zero risk.

Automatic control is only possible when **all** guards pass:
- the system is marked *controllable* and its plugin supports control;
- control was commissioned for the current system settings (changing them requires re-commissioning);
- no tariff is waiting for a confirmed start date;
- fresh readings with device-verified timestamps, no fault and a known battery level;
- a fresh verified plan made using current readings;
- the site location is configured when weather is enabled for PV.

If a guard fails, kWattson releases its active commands and leaves Automatic selected.
Dispatch resumes when the guards pass. An unverified command or two distinct telemetry misses
trigger a retry after about one minute. Unconfirmed releases are retried first, about every
30 seconds. Overview and Automatic control show the next attempt and give admins a **Retry now**
button. Device faults keep dispatch paused until the fault clears, and manual retries still
require the normal control checks.
Missing weather uses a conservative fallback forecast.

## Before you enable it
1. Run for at least a week in monitor mode. Check *Plan* and *Decisions* make sense to you.
2. Confirm the tariff: activate the new plan with the retailer-confirmed start date/time.
3. Check physical settings: inverter AC limit, export limit (confirm your approved limit with your
   network/installer), battery capacity, charge/discharge limits and reserve.
4. Run *Test connection* (read-only) on the system.
5. Know how to stop it: the **Stop automatic control** button, and the inverter's own app.
6. Be present for the first commands and watch the inverter app. Turning on automatic control
   builds a fresh plan and applies its current step as soon as the control checks pass. After
   commissioning, authorized users can turn control on and off without repeating the checklist.
   Changing the commissioned system configuration requires commissioning again.

## FoxESS specifics
- Control uses the Cloud Open API *scheduler* (time groups). Each command writes one ≤20-minute group
  and enables the scheduler. Each group ends by its plan interval's boundary, including the
  configured buffer around favourable tariff windows. Force-discharge targets retain the
  planned overnight reserve. The controller reads the schedule back up to five times within
  15 seconds to allow for cloud delays. It continues only when the scheduler is enabled and the
  requested group and its required limits match. *Release* disables the scheduler.
- **Residual risk:** FoxESS time groups repeat **daily**. If kWattson crashes or loses internet
  while a group is active, that group can recur at the same time each day until replaced. The worst
  case is one ≤20-minute force charge/discharge window per day, bounded by your reserve. To clear it
  manually: FoxESS app → device → *Mode Scheduler* → turn off.
- Force-charge power may be capped by the inverter's own settings.
- Before writing a schedule, the plugin reads the inverter's `fdPwr` range and caps the
  requested power at that limit. The battery's configured charge limit describes DC power;
  it can exceed the scheduler's AC limit (for example, 13 kW DC versus 10.5 kW AC).
  Older firmware without this range uses the configured inverter AC rating. Invalid reported
  limits block the write. The applied group and scheduler limit are stored in the command result.
  FoxESS can acknowledge an out-of-range request without applying it, so matching read-back
  remains mandatory.
- Scheduler writes and release have been checked on a KH9.9. Treat commissioning on other
  models or firmware as a test and confirm physical charging in the inverter app.

## Stopping
- Dashboard: **Stop automatic control** (always visible in automatic mode).
- Console: `docker exec kwattson kwattson stop-control` (releases devices too).
- If the app or the internet is down: disable the scheduler in the vendor app.
