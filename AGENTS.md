# Guide for AI agents and contributors

kWattson is a self-hosted home energy controller. It forecasts household load and solar,
plans battery use around the owner's tariff with a mixed-integer program, explains every
decision, and (optionally, after commissioning) executes the plan on the inverter.

**Priorities, in order:** (1) save the owner money, (2) keep data and decisions so the
algorithms can be improved later, (3) keep the owner informed (dashboard, notifications).
Safety beats savings: when unsure, do nothing to the hardware.

## Map

| Path | Responsibility |
|---|---|
| `kwattson/config.py` | Settings model (site, systems[], vehicles[], forecast, alerts, tariffs[]); runtime env |
| `kwattson/tariffs.py` | Tariffs as data, rate lookup, daily allowance billing |
| `kwattson/tariff_import.py` | AER CDR (Energy Made Easy) plan → `TariffPlan` |
| `kwattson/plugins/` | Hardware contract (`base.py`), registry, built-in plugins |
| `kwattson/devices.py` | Plugin lifecycle, polling, per-plugin context (secrets, state) |
| `kwattson/ingest.py` | Combine system readings into site readings; integrate to half-hour intervals |
| `kwattson/weather.py` | Open-Meteo forecast/archive → `weather_hours` |
| `kwattson/forecast.py` | Load + solar + EV forecast; model fitting; forecast evaluation |
| `kwattson/optimizer.py` | MILP planner, self-use baseline, action labels and reasons |
| `kwattson/control.py` | Guards, command dispatch, verification, fail-safe release |
| `kwattson/advice.py` | Marginal-cost advice: EV window, AC signal, clipping, suggested schedule |
| `kwattson/outcomes.py` | Measured daily cost with counterfactuals |
| `kwattson/alerts.py`, `notifications/` | Event detection, rules, channels, delivery outbox |
| `kwattson/engine.py` | The worker loop that ties it together |
| `kwattson/web/` | FastAPI app (security middleware, API); `static/` holds the built dashboard |
| `frontend/` | Dashboard source (React + MUI + Vite); `npm run build` writes `kwattson/web/static/` |
| `kwattson/exports.py` | Agent-friendly JSONL export |
| `kwattson/backtest.py` | Replay past days per tariff with perfect foresight |
| `kwattson/cli.py` | Owner recovery, imports, backfill, plan, backtest, export, backup |

Docs: `docs/ALGORITHMS.md` (models and their assumptions), `docs/DATA_DICTIONARY.md` (every
table/column), `docs/PLUGINS.md`, `docs/SECURITY.md`, `docs/COMMISSIONING.md`, `tariffs/README.md`.

## Ground rules

1. **Never write to real hardware** while developing or testing. Use the `simulator` plugin and
   `DEMO_MODE=true`. Read-only calls to a real account only with the owner's permission.
2. **Never put credentials in code, tests, fixtures, logs, exports or error messages.** Secrets live
   encrypted in the `secrets` table (`master.key` in the data dir).
3. **Vendor code stays in plugins.** Core modules must not import a specific plugin.
4. **Don't fabricate data.** Missing readings are gaps (`coverage_s` < 1800), not zeros. Unknown
   battery level is treated as "at reserve", never as "full".
5. **Keep decisions explainable and stored.** Any planner change must still write `decisions` +
   `decision_intervals` with a plain-English `reason`, and bump `optimizer.VERSION`.
   Forecast changes bump `forecast.LOAD_VERSION` / `SOLAR_VERSION`.
6. **Label modelled numbers as modelled.** Measured outcomes come from `daily_outcomes`.
7. Schema changes: bump `db.SCHEMA_VERSION` and add a migration; update `docs/DATA_DICTIONARY.md`
   and `exports.TABLES`.

## Improving the algorithms (the intended agent workflow)

1. Get data: `kwattson export --days 180 --out data.jsonl` (or the dashboard's export).
   Line 1 is a manifest. Exclude rows inside `exclusions`.
2. Measure the current model: `evaluations` (forecast error by run), `daily_outcomes`
   (`saving_vs_self_use_aud`, counterfactuals), `decision_intervals` vs `intervals`.
3. Change one thing (e.g. load model features, solar ratio learning, risk handling, terminal value).
4. Validate offline: `kwattson backtest --days 60` and the pytest suite (`pytest -q`).
5. Record what changed and why in the commit and in `docs/ALGORITHMS.md`.

Ideas with likely value (not done yet): learned temperature response per hour of day; using
Open-Meteo ensemble spread for solar uncertainty; stochastic/robust optimisation over p10/p90;
learning the actual round-trip efficiency and charge taper from readings; Amber/wholesale dynamic
price plugin (implement price per interval in `optimizer.price_points`); EV smart-charger control.

## AI skills
Two kWattson skills are included in `.claude/skills/` and load automatically in Claude Code:
- `add-hardware-plugin`: how to add support for a new inverter, battery or car safely.
- `check-dashboard`: run the demo and check the dashboard in a real browser (phone and desktop,
  light and dark, no security-policy violations).

Recommended third-party skills, installed with `tools/install-skills.sh` (pinned and checksum-verified):
- [frontend-design](https://github.com/anthropics/skills/tree/main/skills/frontend-design) (Anthropic): visual design direction.
- [frontend-ui-engineering](https://github.com/addyosmani/agent-skills) (Addy Osmani): component quality and accessibility.
- [webapp-testing](https://github.com/anthropics/skills/tree/main/skills/webapp-testing) (Anthropic): Playwright browser testing.
- [modern-web-guidance](https://www.npmjs.com/package/modern-web-guidance) (Google Chrome): current web platform best practice.
  Use its CLI offline: `node .claude/skills/modern-web-guidance/modern-web.mjs search "<query>"`.

## Running

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,mg]'
.venv/bin/pytest -q
DEMO_MODE=true DEMO_PASSWORD='demo-password-123456' APP_URL=http://localhost:8080 DEV_HTTP=true \
  HOST=127.0.0.1 DATA_DIR="$(mktemp -d)" .venv/bin/kwattson serve        # then open http://localhost:8080 (user "demo")
```
