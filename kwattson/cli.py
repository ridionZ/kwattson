"""Command line tools. Run inside the container: ``docker exec -it kwattson kwattson <command>``.

Commands
  serve                     run the web app + worker (what the container does)
  setup-token               print the one-time setup token for creating the owner
  list-users                show users and their roles
  reset-user NAME           set a new password for a user (--disable-mfa, --make-admin)
  set-secret TARGET NAME    store a credential, e.g. set-secret system:main api_key (prompts)
  import-foxess-history F.. import FoxESS history JSON files (from the API history endpoint)
  import-foxess-reports F.. import FoxESS monthly report JSON files as daily totals
  backfill --days N         fetch history from each system plugin that supports it, plus weather
  import-settings FILE      load site/systems/vehicles/tariffs from JSON (export-settings --out F saves them)
  import-tariff FILE        add a tariff (native JSON or AER CDR plan detail) as pending
  plan                      build a forecast and plan now and print the summary
  backtest --days N         replay past days with perfect foresight: optimiser vs self-use per tariff
  export --days N --out F   write the agent-friendly JSONL export
  backup --out F            consistent copy of the database (keep master.key with it!)
  stop-control              switch to monitor mode and release every controlled device
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .config import Runtime, Settings, stamp, utcnow
from .db import Database, encode


def _open(args):
    runtime = Runtime(worker=False)
    db = Database(runtime.data_dir / 'kwattson.sqlite3', demo=runtime.demo)
    from .security import Security
    return runtime, db, Security(db, runtime)


def cmd_serve(args):
    import os
    import uvicorn
    uvicorn.run('kwattson.web.app:create_app', factory=True, host=os.getenv('HOST', '0.0.0.0'), port=int(os.getenv('PORT', '8080')),
                workers=1, proxy_headers=True, forwarded_allow_ips=os.getenv('FORWARDED_ALLOW_IPS', '127.0.0.1'),
                server_header=False, log_level=os.getenv('LOG_LEVEL', 'info'))


def cmd_setup_token(args):
    runtime, db, security = _open(args)
    if security.owner_exists():
        print('An owner already exists. Use reset-user <username> to recover access.')
        return
    print(security.bootstrap.read_text().strip())


def cmd_reset_user(args):
    runtime, db, security = _open(args)
    if not security.owner_exists():
        print('No users yet; open the web app and use the setup token.')
        return
    first = getpass.getpass(f'New password for {args.username} (14+ characters): ')
    if first != getpass.getpass('Repeat: '):
        sys.exit('Passwords differ')
    security.reset_user(args.username, first, args.disable_mfa, args.make_admin)
    print(f'Password reset for {args.username}; their sessions were signed out.'
          + (' Two-step sign-in turned off.' if args.disable_mfa else '') + (' Now an admin.' if args.make_admin else ''))


def cmd_list_users(args):
    runtime, db, security = _open(args)
    for u in db.rows('SELECT username, role, totp_cipher IS NOT NULL AS mfa, last_login FROM users ORDER BY username'):
        print(f"{u['username']:30} {u['role']:7} 2-step:{'on ' if u['mfa'] else 'off'} last sign-in: {u['last_login'] or 'never'}")


def cmd_set_secret(args):
    runtime, db, security = _open(args)
    value = getpass.getpass(f'Value for {args.target}:{args.name} (hidden): ') if sys.stdin.isatty() else sys.stdin.read().strip()
    security.save_secret(f'{args.target}:{args.name}', value)
    db.audit('credentials_changed', {'target': args.target, 'fields': [args.name]}, 'console')
    print('Saved (encrypted).')


def cmd_import_foxess_history(args):
    from .ingest import rebuild_intervals, store_system, combine
    from .plugins.foxess import parse_history
    runtime, db, security = _open(args)
    settings = db.settings()
    system = next(s for s in settings.systems if s.id == args.system)
    total, first, last = 0, None, None
    for name in args.files:
        payload = json.loads(Path(name).read_text())
        readings = parse_history(payload, settings.site.timezone)
        for r in readings:
            store_system(db, system.id, 'foxess', r)
            site = combine(settings, {system.id: r})
            if site:
                with db.connect() as c:
                    c.execute('INSERT OR REPLACE INTO site_readings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                        stamp(site['ts']), site['soc'], site['stored_kwh'], site['capacity_kwh'], site['solar_kw'], site['load_kw'],
                        site['import_kw'], site['export_kw'], site['charge_kw'], site['discharge_kw'], 0.0, 0, site['output_limited'],
                        0, 1, '{}', stamp()))
            first = min(first, r.ts) if first else r.ts
            last = max(last, r.ts) if last else r.ts
        total += len(readings)
    if first:
        with db.connect() as c:
            rebuild_intervals(c, first, last)
    print(f'Imported {total} readings ({first} → {last}).')


def cmd_import_foxess_reports(args):
    runtime, db, security = _open(args)
    count = 0
    for name in args.files:
        payload = json.loads(Path(name).read_text())
        if payload.get('errno') != 0:
            continue
        stem = Path(name).stem.split('-')  # report-YYYY-MM
        year, month = int(stem[-2]), int(stem[-1])
        series = {s['variable']: s.get('values', []) for s in payload['result']}
        for index in range(31):
            values = {k: (v[index] if index < len(v) else None) for k, v in series.items()}
            if not any(values.values()):
                continue
            try:
                day = datetime(year, month, index + 1).date().isoformat()
            except ValueError:
                continue
            data = dict(solar_kwh=values.get('PVEnergyTotal') or values.get('generation'), import_kwh=values.get('gridConsumption'),
                        export_kwh=values.get('feedin'), house_kwh=values.get('loads'),
                        charge_kwh=values.get('chargeEnergyToTal'), discharge_kwh=values.get('dischargeEnergyToTal'),
                        note='FoxESS monthly report; EV charging included in house_kwh where present')
            db.execute('INSERT OR REPLACE INTO daily_history VALUES(?,?,?,?)', (day, 'foxess_report', encode(data), 'observed'))
            count += 1
    print(f'Imported {count} daily totals.')


def cmd_backfill(args):
    from .devices import Devices
    from .ingest import rebuild_intervals, store_system, combine
    from . import weather
    runtime, db, security = _open(args)
    settings = db.settings()
    devices = Devices(db, security)
    end = utcnow()
    start = end - timedelta(days=args.days)

    async def run():
        for system in settings.active_systems():
            try:
                readings = await devices.system(settings, system.id).history(start, end)
            except NotImplementedError:
                print(f'{system.name}: plugin has no history support')
                continue
            for r in readings:
                store_system(db, system.id, system.plugin, r)
                site = combine(settings, {system.id: r})
                if site:
                    with db.connect() as c:
                        c.execute('INSERT OR IGNORE INTO site_readings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                            stamp(site['ts']), site['soc'], site['stored_kwh'], site['capacity_kwh'], site['solar_kw'], site['load_kw'],
                            site['import_kw'], site['export_kw'], site['charge_kw'], site['discharge_kw'], 0.0, 0, site['output_limited'],
                            0, 1, '{}', stamp()))
            print(f'{system.name}: {len(readings)} readings')
        with db.connect() as c:
            rebuild_intervals(c, start, end)
        if settings.site.weather_enabled and not runtime.demo:
            n = await weather.backfill(db, settings, start.date().isoformat(), (end - timedelta(days=2)).date().isoformat())
            print(f'Weather: {n} hours')
        await devices.close()
    asyncio.run(run())


def cmd_import_tariff(args):
    from .tariffs import TariffPlan
    runtime, db, security = _open(args)
    data = json.loads(Path(args.file).read_text())
    if 'data' in data and 'electricityContract' in data.get('data', {}):
        from .tariff_import import from_cdr
        plan = from_cdr(data['data'])
    else:
        plan = TariffPlan.model_validate(data)
    plan = plan.model_copy(update={'status': 'pending', 'active_from': None})
    settings = db.settings()
    settings.tariffs = [t for t in settings.tariffs if t.id != plan.id] + [plan]
    db.save_settings(Settings.model_validate(settings.model_dump()), 'console')
    print(f'Added {plan.name} ({plan.id}) as pending. Activate it in the dashboard once the retailer confirms the start date.')


def cmd_import_settings(args):
    """Load site/systems/vehicles/tariffs (and optional exclusions) from a JSON file into this install."""
    from .plugins import registry
    runtime, db, security = _open(args)
    doc = json.loads(Path(args.file).read_text())
    current = db.settings().model_dump()
    sections = [k for k in ('site', 'systems', 'vehicles', 'tariffs', 'forecast', 'alerts') if k in doc]
    merged = Settings.model_validate({**current, **{k: doc[k] for k in sections}, 'mode': current['mode']})
    registry.validate_settings(merged)
    db.save_settings(merged, 'console')
    added = 0
    for item in doc.get('exclusions', []):
        if not db.one('SELECT 1 FROM exclusions WHERE start=? AND end=?', (item['start'], item['end'])):
            db.execute('INSERT INTO exclusions(start, end, reason) VALUES(?,?,?)', (item['start'], item['end'], item['reason'][:300]))
            added += 1
    db.audit('settings_imported', {'sections': sections, 'exclusions_added': added}, 'console')
    print(f'Imported {", ".join(sections) or "nothing"}; {added} exclusion period(s) added. Restart the app if it is running.')


def cmd_export_settings(args):
    """Write this install's settings (no credentials) and exclusions to a JSON file."""
    runtime, db, security = _open(args)
    s = db.settings().model_dump()
    doc = {k: s[k] for k in ('site', 'systems', 'vehicles', 'tariffs', 'forecast', 'alerts')}
    doc['exclusions'] = [dict(start=r['start'], end=r['end'], reason=r['reason']) for r in db.rows('SELECT * FROM exclusions ORDER BY start')]
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(doc, f, indent=2)
    print(f'Wrote {args.out} (no passwords or API keys are included).')


def cmd_plan(args):
    from . import forecast, optimizer
    runtime, db, security = _open(args)
    settings = db.settings()
    fid, points, model = forecast.build(db, settings)
    decision = optimizer.plan(db, settings, fid, points, model)
    print(json.dumps({k: v for k, v in decision['summary'].items() if k != 'limitations'}, indent=2, default=str))


def cmd_backtest(args):
    from .backtest import run
    runtime, db, security = _open(args)
    print(json.dumps(run(db, db.settings(), args.days, args.tariff), indent=2, default=str))


def cmd_export(args):
    from .exports import lines
    runtime, db, security = _open(args)
    with open(args.out, 'w', encoding='utf-8') as f:
        for line in lines(db, args.days):
            f.write(line)
    Path(args.out).chmod(0o600)
    print(f'Wrote {args.out}')


def cmd_backup(args):
    runtime, db, security = _open(args)
    db.backup(args.out)
    print(f'Wrote {args.out}. Also copy {runtime.data_dir / "master.key"} — without it stored credentials cannot be read.')


def cmd_stop_control(args):
    from .control import Controller
    from .devices import Devices
    from .notifications import Notifications
    runtime, db, security = _open(args)
    devices = Devices(db, security)
    controller = Controller(db, devices, Notifications(db, security), runtime)

    async def run():
        problems = await controller.stop(db.settings(), 'Stopped from the console', actor='console')
        await devices.close()
        return problems
    problems = asyncio.run(run())
    print('Monitor mode. ' + ('Check the vendor app: ' + '; '.join(problems) if problems else 'Devices released.'))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='kwattson', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('serve').set_defaults(func=cmd_serve)
    sub.add_parser('setup-token').set_defaults(func=cmd_setup_token)
    p = sub.add_parser('reset-user', help='set a new password for a user (console recovery)')
    p.add_argument('username')
    p.add_argument('--disable-mfa', action='store_true')
    p.add_argument('--make-admin', action='store_true', help='also give this user the admin role')
    p.set_defaults(func=cmd_reset_user)
    sub.add_parser('list-users').set_defaults(func=cmd_list_users)
    p = sub.add_parser('set-secret')
    p.add_argument('target', help='system:<id> or vehicle:<id>')
    p.add_argument('name')
    p.set_defaults(func=cmd_set_secret)
    p = sub.add_parser('import-foxess-history')
    p.add_argument('files', nargs='+')
    p.add_argument('--system', default='main')
    p.set_defaults(func=cmd_import_foxess_history)
    p = sub.add_parser('import-foxess-reports')
    p.add_argument('files', nargs='+')
    p.set_defaults(func=cmd_import_foxess_reports)
    p = sub.add_parser('backfill')
    p.add_argument('--days', type=int, default=30)
    p.set_defaults(func=cmd_backfill)
    p = sub.add_parser('import-tariff')
    p.add_argument('file')
    p.set_defaults(func=cmd_import_tariff)
    p = sub.add_parser('import-settings', help='load site/systems/vehicles/tariffs from a JSON file')
    p.add_argument('file')
    p.set_defaults(func=cmd_import_settings)
    p = sub.add_parser('export-settings', help='save site/systems/vehicles/tariffs to a JSON file (no credentials)')
    p.add_argument('--out', required=True)
    p.set_defaults(func=cmd_export_settings)
    sub.add_parser('plan').set_defaults(func=cmd_plan)
    p = sub.add_parser('backtest')
    p.add_argument('--days', type=int, default=14)
    p.add_argument('--tariff', action='append', help='tariff id(s) to compare (default: all non-retired)')
    p.set_defaults(func=cmd_backtest)
    p = sub.add_parser('export')
    p.add_argument('--days', type=int, default=90)
    p.add_argument('--out', required=True)
    p.set_defaults(func=cmd_export)
    p = sub.add_parser('backup')
    p.add_argument('--out', required=True)
    p.set_defaults(func=cmd_backup)
    sub.add_parser('stop-control').set_defaults(func=cmd_stop_control)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
