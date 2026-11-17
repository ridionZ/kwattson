# Tariff files

Each `*.json` file here (other than `tariff.schema.json`) is one retail electricity
plan in kWattson's native format: a `TariffPlan` as defined in
[`kwattson/tariffs.py`](../kwattson/tariffs.py). The JSON Schema in
[`tariff.schema.json`](tariff.schema.json) is generated from that model, so
editors can autocomplete and check field names and ranges:

```json
{ "$schema": "./tariff.schema.json", "id": "my-plan", "...": "..." }
```

(`$schema` is for editors only; strip it before loading, because the model
rejects unknown fields.)

The schema cannot express every rule. `TariffPlan.model_validate` also checks
that every 5 minutes of every weekday is covered by a period, that period
names are unique, and that allowances refer to existing periods and describe
the *better* tier. Always validate a file before using it:

```bash
.venv/bin/python -c "from kwattson.tariff_import import load_file; print(load_file('tariffs/example-tou.json').name)"
```

## Examples

| File | What it shows | Status |
|---|---|---|
| `flat-example.json` | Single rate: one period `00:00`-`24:00` every day. | `active`, `active_from: null` (base plan) |
| `example-tou.json` | Illustrative TOU: a window crossing midnight, weekday-only peak with a weekend alternative, import and export allowances. Not a real offer. | `pending` |
| `covau-solarmax-qld.json` | CovaU SolarMax (Energex), imported from AER CDR data: free 11:00-14:00 for the first 50 kWh/day, 15c export 18:00-21:00 for the first 30 kWh/day. | `pending` |

## Format

All rates are **AUD per kWh**. `import_rate` and `supply_daily` (AUD/day)
**include GST**, as on the bill. `export_rate` (feed-in) is as published.

| Field | Meaning |
|---|---|
| `id` | Stable identifier, `^[a-z0-9_-]+$`, at most 40 characters. |
| `name`, `retailer`, `plan_code` | Display information; `plan_code` is the Energy Made Easy plan id without `@EME`. |
| `status` | `active`, `pending` or `retired`. Only `active` plans are used for billing and control. |
| `active_from` | UTC ISO timestamp with offset (e.g. `2026-12-01T14:00:00Z`) from which an active plan applies. `null` means "base plan, applies from the beginning". |
| `supply_daily` | Daily supply charge, AUD incl GST. |
| `periods[]` | Time windows in **local wall-clock time**: `name` (`^[a-z0-9_]+$`, unique), `label`, `start`, `end` (`HH:MM`, 5-minute boundaries, end exclusive, `24:00` = midnight), `days` (0 = Monday ... 6 = Sunday, default all), `import_rate`, `export_rate`. If `end` is before `start` the window crosses midnight. |
| `allowances[]` | Daily kWh tiers: the first `kwh_per_day` imported (`kind: "import"`) or exported (`kind: "export"`) across the listed `periods` uses the period's rate; beyond that `rate_after` applies. The period rate must be the better tier (import: not dearer than `rate_after`; export: not lower). |
| `notes` | Free text: source, assumptions, what is not modelled. |

When an import window and an export window do not line up, split the period.
SolarMax's 15:00-21:00 peak is two periods, `peak` (15-18, 5c export) and
`peak_fit_peak` (18-21, 15c export), so the export allowance can name just the
18-21 window.

## Activating a plan

New and imported plans are `pending`. A pending plan is shown in previews
but never used for billing or battery control, because a signed plan usually
starts on a date only the retailer can confirm. When the retailer confirms the
switch, the owner sets `status` to `active` and `active_from` to the confirmed
start time (with a timezone offset). The newest active plan whose
`active_from` is at or before a moment applies to that moment, so earlier
plans keep pricing history correctly; mark a plan `retired` to stop using it
altogether.

## Importing from Energy Made Easy (AER CDR)

`kwattson/tariff_import.py` converts a Consumer Data Right plan-detail
response into this format:

```python
from kwattson.tariff_import import fetch_cdr, from_cdr, load_file

plan = load_file('detail-COV1117614MRE3_EME.json')          # CDR response or native file
detail = await fetch_cdr('COV1117614MRE3@EME', 'covau')    # network: Energy Made Easy
plan = from_cdr(detail)                                    # pending TariffPlan
```

Interpretation rules (read the plan's `notes` after importing):

* **GST**: CDR prices exclude GST, so usage rates and the daily supply charge
  are multiplied by 1.1. Feed-in rates are not adjusted.
* **Times**: CDR end times are inclusive (`20:59` means until 21:00);
  `23:59` and `00:00` as an end mean midnight; windows that end before they
  start are split across midnight. `HHMM` is accepted as well as `HH:MM`.
  Some retailers publish times in AEST without daylight saving; this is noted.
* **Tiers**: a usage block whose first rate has a daily `volume` (period `P1D`
  or "per day" in its text) and is cheaper than the next rate becomes an
  import allowance. Feed-in tiers (usually separate entries over the same
  window, one with a `volume`) become export allowances. Other tiers are not
  modelled: the first usage rate is used, and the notes say so.
* **Gaps**: times without a usage rate get the plan's single off-peak (or
  shoulder) rate, otherwise the import fails. Times without a retailer feed-in
  rate earn 0c.
* **Not modelled** (mentioned in notes): demand charges, controlled-load
  circuits, government/premium feed-in schemes, discounts, incentives and fees,
  and caps that appear only in free text.

Always compare an imported plan with the retailer's Energy Fact Sheet before
activating it.
