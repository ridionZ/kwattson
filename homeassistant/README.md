# Home Assistant integration

kWattson **publishes** information; Home Assistant decides what to do with it. The
controller never switches your AC or other appliances directly.

| File | What it does |
|---|---|
| `packages/kwattson.yaml` | REST sensors polling `/api/ha/state` (battery, solar, grid, price, planned action, climate signal, online) and two helpers |
| `blueprints/kwattson_climate.yaml` | Opt-in automation: pre-cool / pre-heat when energy is cheap and the weather is hot/cold |

## Setup
1. kWattson → *Security* → *API tokens* → create a **read** token (shown once).
2. Add to `secrets.yaml`:
   ```yaml
   kwattson_state_url: https://solar.example.com/api/ha/state
   kwattson_token: "Bearer kw_your_token"
   ```
3. Copy `packages/kwattson.yaml` to `config/packages/` and enable packages in `configuration.yaml`:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
4. Copy the blueprint to `config/blueprints/automation/kwattson/` and create an automation from it.
5. Turn on **Let kWattson pre-cool / pre-heat** when you want it to act.

## Safety design of the AC blueprint
- Starts only if: opt-in switch is on, someone is home, the indoor temperature is beyond your threshold,
  and the AC is currently **off** (it never takes over an AC someone is using).
- Records that *it* started the AC (`input_boolean.kwattson_ac_running`) and only ever turns off what it started.
- Stops when the signal ends (5 min), kWattson goes offline (10 min), the opt-in is switched off,
  Home Assistant restarts, or the **hard maximum runtime** elapses — this last one runs entirely
  inside Home Assistant, so it works even if kWattson or the network is down.

## Other ideas
- Use `sensor.kwattson_import_price` to delay a dishwasher or pool pump during peak.
- Use `sensor.kwattson_planned_action` (`export_priority` = lots of spare solar soon) to run
  flexible loads at midday instead of exporting at a low feed-in rate.
- Feed data **into** kWattson from devices it has no plugin for: use the `homeassistant`
  system/vehicle plugins (entity mapping) or POST to `/api/ingest/...` with an *ingest* token.

> These YAML files have been written against current Home Assistant syntax but were not tested on a
> live Home Assistant instance by the author. Check *Developer tools → YAML → Check configuration*.
