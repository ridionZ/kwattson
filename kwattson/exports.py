"""Agent-friendly JSON Lines export.

Line 1 is a manifest describing every record type, units and caveats. Each
following line is ``{"type": <table>, ...row}``. The export is meant to be
handed to an analyst or AI agent tuning the forecaster/optimiser, so it holds
everything needed to replay decisions — but never credentials, password/session/
token hashes, notification destinations, raw vendor payloads (may contain device
serials) or plugin connection settings.
"""
from __future__ import annotations

from datetime import timedelta

from . import __version__
from .config import stamp, utcnow
from .db import SCHEMA_VERSION, Database, decode, encode

TABLES = {
    'settings_snapshot': 'Current settings with plugin connection details removed',
    'settings_history': 'When settings changed and which sections (full snapshot in data)',
    'exclusions': 'Periods the owner marked as unrepresentative; never train on them',
    'holidays': 'Away periods (house mostly empty)',
    'annotations': 'Owner notes (guests, new appliance, outage...)',
    'intervals': 'Half-hour energy (kWh) per source; coverage_s < 1800 means partial data',
    'site_readings': 'Combined instantaneous readings (kW, % SoC) at each poll',
    'system_readings': 'Per-inverter/battery readings',
    'vehicle_readings': 'EV state of charge and charging flags',
    'weather_hours': 'Hourly Open-Meteo values; ts is the END of the averaging hour',
    'forecasts': 'Forecast runs with fitted model summary',
    'forecast_points': 'Per-interval forecast values (kWh) for each run',
    'evaluations': 'Forecast accuracy against observed intervals (first 24 h)',
    'model_params': 'Latest fitted load/solar model parameters',
    'decisions': 'Planner runs: summary and inputs (settings omitted; see settings_history)',
    'decision_intervals': 'Planned flows per interval, with self-use baseline and reason',
    'commands': 'Device commands attempted and their verification result',
    'overrides': 'Owner overrides (hold, reserve, EV...)',
    'daily_outcomes': 'Measured daily cost with self-use / no-battery / no-solar counterfactuals',
    'daily_history': 'Imported daily totals from vendor reports (no intervals)',
    'notifications': 'Alerts raised (in-app record)',
}


def manifest(db: Database, days: int) -> dict:
    settings = db.settings()
    return dict(type='manifest', app='kwattson', app_version=__version__, schema_version=SCHEMA_VERSION, generated_at=stamp(),
                days=days, timezone=settings.site.timezone, tables=TABLES,
                units=dict(energy='kWh', power='kW', soc='%', money='AUD', rates='AUD/kWh', time='UTC ISO-8601'),
                docs='docs/DATA_DICTIONARY.md and docs/ALGORITHMS.md in the source repository',
                caveats=['Savings in decisions are modelled against a simulated self-use baseline, not measured.',
                         'daily_outcomes are telemetry estimates, not retailer bills.',
                         'EV energy may be estimated from vehicle charging state (site_readings.ev_estimated).',
                         'Exclusions mark periods that must not be used for training.'])


def _sanitised_settings(db: Database) -> dict:
    data = db.settings().model_dump()
    for item in data['systems'] + data['vehicles']:
        item['plugin_config'] = '[removed]'
    for key in ('latitude', 'longitude'):
        if data['site'][key] is not None:
            data['site'][key] = round(data['site'][key], 1)
    return data


def lines(db: Database, days: int):
    since = stamp(utcnow() - timedelta(days=days))
    yield encode(manifest(db, days)) + '\n'
    yield encode({'type': 'settings_snapshot', **_sanitised_settings(db)}) + '\n'
    simple = [
        ('settings_history', 'SELECT id, at, actor, changed FROM settings_history WHERE at>=? ORDER BY id', ('changed',)),
        ('exclusions', 'SELECT * FROM exclusions WHERE end>=? ORDER BY start', ()),
        ('holidays', 'SELECT id, name, start, end, load_factor, ev_away FROM holidays WHERE end>=? ORDER BY start', ()),
        ('annotations', 'SELECT * FROM annotations WHERE start>=? ORDER BY start', ()),
        ('intervals', 'SELECT * FROM intervals WHERE ts>=? ORDER BY ts, source', ()),
        ('site_readings', 'SELECT ts, soc, stored_kwh, capacity_kwh, solar_kw, load_kw, import_kw, export_kw, charge_kw, discharge_kw, '
                          'ev_kw, ev_estimated, output_limited, fault, time_verified FROM site_readings WHERE ts>=? ORDER BY ts', ()),
        ('system_readings', 'SELECT ts, system_id, plugin, soc, pv_kw, charge_kw, discharge_kw, ac_output_kw, import_kw, export_kw, '
                            'load_kw, battery_temp, running_state, fault, time_verified FROM system_readings WHERE ts>=? ORDER BY ts', ()),
        ('vehicle_readings', 'SELECT ts, vehicle_id, plugin, soc, charging, plugged_in, at_home, charge_kw, range_km, time_verified '
                             'FROM vehicle_readings WHERE ts>=? ORDER BY ts', ()),
        ('weather_hours', 'SELECT * FROM weather_hours WHERE ts>=? ORDER BY ts', ()),
        ('forecasts', 'SELECT * FROM forecasts WHERE created_at>=? ORDER BY id', ('model',)),
        ('forecast_points', 'SELECT p.* FROM forecast_points p JOIN forecasts f ON f.id=p.forecast_id WHERE f.created_at>=? ORDER BY p.forecast_id, p.ts',
         ('solar_by_system',)),
        ('evaluations', 'SELECT * FROM evaluations WHERE evaluated_at>=? ORDER BY forecast_id', ('metrics',)),
        ('decision_intervals', 'SELECT i.* FROM decision_intervals i JOIN decisions d ON d.id=i.decision_id WHERE d.created_at>=? '
                               'ORDER BY i.decision_id, i.ts', ('systems',)),
        ('commands', 'SELECT id, created_at, decision_id, action, payload, status, result, verified_at FROM commands WHERE created_at>=? ORDER BY id',
         ('payload', 'result')),
        ('overrides', 'SELECT * FROM overrides WHERE created_at>=? ORDER BY id', ('data',)),
        ('daily_outcomes', 'SELECT day, computed_at, coverage, data FROM daily_outcomes WHERE computed_at>=? OR day>=substr(?,1,10) ORDER BY day', ('data',)),
        ('notifications', 'SELECT id, created_at, event, severity, title, body FROM notifications WHERE created_at>=? ORDER BY id', ()),
    ]
    for table, query, json_columns in simple:
        args = (since, since) if query.count('?') == 2 else (since,)
        with db.connect() as c:
            for row in c.execute(query, args):
                record = dict(row)
                for column in json_columns:
                    record[column] = decode(record[column])
                yield encode({'type': table, **record}) + '\n'
    with db.connect() as c:
        for row in c.execute('SELECT * FROM model_params'):
            yield encode({'type': 'model_params', **dict(row), 'data': decode(row['data'])}) + '\n'
        for row in c.execute('SELECT * FROM daily_history ORDER BY day'):
            yield encode({'type': 'daily_history', **dict(row), 'data': decode(row['data'])}) + '\n'
        for row in c.execute('SELECT id, created_at, version, forecast_id, mode, tariff_id, input_hash, inputs, summary FROM decisions '
                             'WHERE created_at>=? ORDER BY id', (since,)):
            inputs = decode(row['inputs'])
            inputs.pop('settings', None)
            yield encode({'type': 'decisions', **dict(row), 'inputs': inputs, 'summary': decode(row['summary'])}) + '\n'
