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
- Control defaults to one Cloud Open API v3 schedule for the rest of the local day. It is
  confirmed once, checked with reads every five minutes, and replaced when a materially
  different plan is adopted. Tariff buffers, charge cutoffs, and overnight reserves remain
  in the schedule. Daily read-back allows up to 45 seconds for cloud delays; release allows
  up to 20 seconds. An unchanged enabled schedule is reused without writes. Disable
  **Daily schedule** in the connection settings for the older ≤20-minute command path.
- **Residual risk:** FoxESS time groups repeat **daily**. If kWattson crashes or loses internet
  while a schedule is active, the entire stored daily schedule can recur until replaced, even
  if prices or the weather change. Keep the controller online. To clear it
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

## Temporary mode buttons

Overview and Solar & batteries offer self-use, pause discharge, grid charging, battery export and
solar-export-priority modes when the plugin supports them. Choose 5–30 minutes; configured
inverter, battery and site export limits cap power. Grid charging shows the current import price
before confirmation. Export retains the highest forecast home/car reserve throughout the override,
even if a lower cutoff is entered. An increased protected reserve can end manual export early.

The same commissioning, telemetry, fault, tariff and verified-plan guards apply. These requests do
not enable automatic mode or change commissioning settings. The confirmed short mode is overlaid
on the adopted daily program, preserving later steps, and checked periodically. The latest automatic
plan resumes at expiry. In monitor mode, expiry releases the inverter to its own default. Stop
automatic control also cancels temporary modes. FoxESS recurring-schedule risks described above
also apply to the overlaid program; keep the controller online.

The automatic forecast remains a reference during a temporary mode. Actual readings and command
results remain stored. Overview identifies the temporary mode and does not present automatic-plan
savings as the savings from that manual action.
An active car charging plan assumes home. Sustained incompatible car/house power readings
pause automatic commands; missing or distant GPS does not. Enable the optional reconnect test
only if a brief stop/restart is acceptable, including when charging away. It runs at most once
per continuous connection and needs permission for both commands. A manual verification button
provides the same explicit acknowledgement. Check charging if restoration cannot be confirmed.

MG uses its car-native timer by default for an opted-in ready-by schedule. One verified window
is stored at a time; its cutoff works without internet, but loading subsequent windows needs
the controller online. The timer repeats daily until cleared. Unplugging/confirmed-away state
suspends the native timer while keeping the plan for return; clearing requires connectivity.
Check that the car clock matches the site's timezone. Hardware SOC targets are ten-point steps
from 40%; exact intermediate targets need the online worker. Observe one full timer start/stop
before relying on it for a paid-rate boundary. Set 'Use the car charging timer' off only for a
model/firmware without timer support; that explicitly restores software start/stop control.
