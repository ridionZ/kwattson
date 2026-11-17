# Contributing

Thanks for helping! The most valuable contributions are usually:

1. **Hardware plugins:** see [docs/PLUGINS.md](docs/PLUGINS.md). Prefer publishing them as separate
   packages via the `kwattson.plugins` entry point; widely used ones can be proposed as built-ins.
2. **Tariff support:** importer fixes, example tariff files, dynamic-price sources.
3. **Better forecasts or planning:** back changes with numbers from `kwattson backtest` and the
   forecast `evaluations`. See [docs/ALGORITHMS.md](docs/ALGORITHMS.md) and [AGENTS.md](AGENTS.md).

## Development
```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,mg]'
.venv/bin/pytest -q
```
Use `DEMO_MODE=true` and the `simulator` plugin for local runs.

## Rules
- Tests must not use the network or real hardware.
- No credentials, device serials, addresses, VINs or personal energy data in commits (including fixtures).
- Vendor-specific code lives only in its plugin.
- Keep decisions explainable: planner changes need reasons and a bumped `VERSION`.
- Schema changes: bump `SCHEMA_VERSION`, add a migration, update `docs/DATA_DICTIONARY.md`.
- Dashboard: no `innerHTML`, inline scripts/styles or third-party resources (CSP and Trusted Types will block them).

## Pull requests
Describe what changed, why, how you tested it, and, for control code, which hardware you verified it on.
