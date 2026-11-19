"""JSON API used by the dashboard, Home Assistant and push integrations.

Authentication is enforced by the middleware in app.py; handlers here can assume
an authenticated owner session (or a scoped bearer token on /api/ha and /api/ingest).
Responses never include secrets, password/session/token hashes or decrypted
channel configuration.
"""
from __future__ import annotations

import json
import secrets as pysecrets
from datetime import datetime, timedelta

import pyotp
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import __version__, advice, ev_charging, exports, forecast, outcomes, weather
from ..devices import DEMO_PLUGINS, check_demo_plugin
from ..config import EnergySystem, Vehicle, Settings, stamp, utcnow
from ..control import commissioning_hash
from ..db import decode, encode
from ..ingest import select_intervals
from ..notifications import EVENTS, check_template
from ..notifications.channels import CHANNELS, describe as describe_channels
from ..plugins import registry
from ..plugins.push import system_reading, vehicle_reading
from ..security import PASSWORDS, REMEMBER_DAYS, SESSION_HOURS, digest
from ..tariffs import TariffPlan, default_tariffs, pending_plans, plan_at, rate_at

router = APIRouter(prefix='/api')


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Login(Body):
    remember: bool = False
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)
    otp: str = Field('', max_length=10)


class Setup(Body):
    token: str = Field(min_length=20, max_length=200)
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=14, max_length=256)


class Confirm(Body):
    password: str = Field(min_length=1, max_length=256)
    otp: str = Field('', max_length=10)


class PasswordChange(Confirm):
    new_password: str = Field(min_length=14, max_length=256)


class Secrets(Body):
    values: dict[str, str] = Field(default_factory=dict)


class HolidayIn(Body):
    name: str = Field(min_length=1, max_length=100)
    start: str
    end: str
    load_factor: float | None = Field(None, ge=0, le=2)
    ev_away: bool = True


class OverrideIn(Body):
    kind: str = Field(pattern='^(hold|no_battery_export|reserve|ev_charge_now|ev_away)$')
    minutes: int = Field(120, ge=5, le=60 * 24 * 14)
    pct: float | None = Field(None, ge=0, le=100)
    vehicle_id: str | None = Field(None, max_length=40)
    target_pct: float | None = Field(None, gt=0, le=100)
    starting_soc: float | None = Field(None, ge=0, le=100)
    stop_at_limit: bool = False


class AnnotationIn(Body):
    start: str
    end: str | None = None
    kind: str = Field('note', pattern='^(note|guests|appliance|outage|maintenance|exclude)$')
    text: str = Field(min_length=1, max_length=1000)


class ExclusionIn(Body):
    start: str
    end: str
    reason: str = Field(min_length=3, max_length=300)


class ChannelIn(Body):
    name: str = Field(min_length=1, max_length=60)
    kind: str
    enabled: bool = True
    config: dict[str, str] = Field(default_factory=dict)


class RuleIn(Body):
    enabled: bool = True
    channels: list[int] = Field(default_factory=list, max_length=20)
    cooldown_minutes: int = Field(60, ge=0, le=10080)
    respect_quiet: bool = True
    title: str = Field(max_length=200)
    body: str = Field(max_length=2000)


class TokenIn(Confirm):
    name: str = Field(min_length=1, max_length=80)
    scope: str = Field(pattern='^(read|ingest)$')


class ActivateTariff(Confirm):
    active_from: str


class EnableControl(Body):
    password: str = Field('', max_length=256)
    otp: str = Field('', max_length=10)
    confirmation: str = Field('', max_length=64)
    acknowledge: bool = False


def ctx(request: Request):
    s = request.app.state
    return s.db, s.security, s.engine, s.runtime


def client_ip(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


def local_time(text: str, settings: Settings) -> str:
    """Accept ISO with or without offset; naive values are the site's local time."""
    value = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=settings.zone)
    return stamp(value)


# ------------------------------------------------------------------ auth
@router.get('/auth/status')
async def auth_status(request: Request):
    db, security, _, runtime = ctx(request)
    signed_in = False
    token = request.cookies.get(runtime.cookie)
    if token:
        try:
            security.authenticate(token)
            signed_in = True
        except HTTPException:
            pass
    return {'setup_required': not security.owner_exists(), 'signed_in': signed_in, 'demo': runtime.demo, 'version': __version__}


@router.post('/auth/setup')
async def setup(body: Setup, request: Request):
    db, security, *_ = ctx(request)
    security.setup(body.token, body.username, body.password, client_ip(request))
    return {'created': True}


@router.post('/auth/login')
async def login(body: Login, request: Request):
    db, security, _, runtime = ctx(request)
    token, csrf = security.login(body.username, body.password, body.otp, client_ip(request), remember=body.remember)
    response = JSONResponse({'csrf': csrf})
    response.set_cookie(runtime.cookie, token, secure=not runtime.dev_http, httponly=True, samesite='strict', max_age=REMEMBER_DAYS * 86400 if body.remember else SESSION_HOURS * 3600, path='/')
    return response


@router.get('/auth/session')
async def session(request: Request):
    db, *_ = ctx(request)
    me = request.state.user
    user = db.one('SELECT id, username, role, totp_cipher FROM users WHERE id=?', (me['user_id'],))
    return {'user_id': user['id'], 'username': user['username'], 'role': user['role'], 'csrf': me['csrf'],
            'mfa_enabled': bool(user['totp_cipher']), 'version': __version__}


@router.post('/auth/logout')
async def logout(request: Request):
    db, _, _, runtime = ctx(request)
    db.execute('DELETE FROM sessions WHERE token_hash=?', (digest(request.cookies.get(runtime.cookie, '')),))
    response = JSONResponse({'signed_out': True})
    response.delete_cookie(runtime.cookie, path='/', secure=not runtime.dev_http, httponly=True, samesite='strict')
    return response


def me(request: Request) -> int:
    return request.state.user['user_id']


def actor(request: Request) -> str:
    return 'user:' + request.state.user['username']


@router.post('/auth/mfa/start')
async def mfa_start(body: Confirm, request: Request):
    db, security, *_ = ctx(request)
    user = security.reauthenticate(me(request), body.password, body.otp)
    if user['totp_cipher']:
        raise HTTPException(409, 'Two-step sign-in is already on')
    seed = pyotp.random_base32()
    security.save_secret(f'totp_pending:{user["id"]}', encode({'seed': seed, 'expires': stamp(utcnow() + timedelta(minutes=10))}))
    return {'secret': seed, 'uri': pyotp.TOTP(seed).provisioning_uri(name=user['username'], issuer_name='kWattson')}


@router.post('/auth/mfa/confirm')
async def mfa_confirm(body: Confirm, request: Request):
    db, security, *_ = ctx(request)
    user_id = me(request)
    security.reauthenticate(user_id, body.password)
    pending = decode(security.secret(f'totp_pending:{user_id}'), {})
    if not pending or datetime.fromisoformat(pending['expires']) < utcnow() or not pyotp.TOTP(pending['seed']).verify(body.otp, valid_window=1):
        raise HTTPException(422, 'Code is wrong or enrolment expired; start again')
    db.execute('UPDATE users SET totp_cipher=?, totp_last=? WHERE id=?',
               (security.fernet.encrypt(pending['seed'].encode()).decode(), int(utcnow().timestamp()) // 30, user_id))
    security.save_secret(f'totp_pending:{user_id}', None)
    db.audit('mfa_enabled', {'user_id': user_id}, actor(request))
    return {'enabled': True}


@router.post('/auth/mfa/disable')
async def mfa_disable(body: Confirm, request: Request):
    db, security, *_ = ctx(request)
    security.reauthenticate(me(request), body.password, body.otp)
    db.execute('UPDATE users SET totp_cipher=NULL, totp_last=-1 WHERE id=?', (me(request),))
    db.audit('mfa_disabled', {'user_id': me(request)}, actor(request))
    return {'enabled': False}


@router.post('/auth/password')
async def change_password(body: PasswordChange, request: Request):
    db, security, *_ = ctx(request)
    security.reauthenticate(me(request), body.password, body.otp)
    security.validate_password(body.new_password)
    with db.connect() as c:
        c.execute('UPDATE users SET password_hash=? WHERE id=?', (PASSWORDS.hash(body.new_password), me(request)))
        c.execute('DELETE FROM sessions WHERE user_id=?', (me(request),))
    db.audit('password_changed', {'user_id': me(request)}, actor(request))
    return {'sign_in_again': True}


# ----------------------------------------------------------------- users
class UserIn(Confirm):
    username: str = Field(min_length=3, max_length=80)
    role: str = Field(pattern='^(admin|member|viewer)$')
    initial_password: str = Field(min_length=14, max_length=256)


class RoleIn(Confirm):
    role: str = Field(pattern='^(admin|member|viewer)$')


@router.get('/users')
async def users(request: Request):
    db, *_ = ctx(request)
    rows = db.rows('SELECT id, username, role, totp_cipher, created_at, last_login FROM users ORDER BY username')
    return [dict(id=r['id'], username=r['username'], role=r['role'], mfa_enabled=bool(r['totp_cipher']),
                 created_at=r['created_at'], last_login=r['last_login'], you=r['id'] == me(request)) for r in rows]


@router.post('/users')
async def add_user(body: UserIn, request: Request):
    db, security, *_ = ctx(request)
    security.reauthenticate(me(request), body.password, body.otp)
    user_id = security.create_user(body.username, body.initial_password, body.role, actor=actor(request))
    return {'id': user_id}


@router.put('/users/{user_id}')
async def change_role(user_id: int, body: RoleIn, request: Request):
    db, security, *_ = ctx(request)
    security.reauthenticate(me(request), body.password, body.otp)
    user = db.one('SELECT * FROM users WHERE id=?', (user_id,))
    if not user:
        raise HTTPException(404, 'User not found')
    if user['role'] == 'admin' and body.role != 'admin' and security.admins() <= 1:
        raise HTTPException(409, 'There must always be at least one admin')
    with db.connect() as c:
        c.execute('UPDATE users SET role=? WHERE id=?', (body.role, user_id))
        c.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))  # new role applies at next sign-in
    db.audit('user_role_changed', {'user_id': user_id, 'role': body.role}, actor(request))
    return {'saved': True}


@router.post('/users/{user_id}/delete')
async def delete_user(user_id: int, body: Confirm, request: Request):
    db, security, *_ = ctx(request)
    security.reauthenticate(me(request), body.password, body.otp)
    if user_id == me(request):
        raise HTTPException(409, "You can't remove yourself; ask another admin")
    user = db.one('SELECT * FROM users WHERE id=?', (user_id,))
    if not user:
        raise HTTPException(404, 'User not found')
    if user['role'] == 'admin' and security.admins() <= 1:
        raise HTTPException(409, 'There must always be at least one admin')
    with db.connect() as c:
        c.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
        c.execute('DELETE FROM users WHERE id=?', (user_id,))
    security.save_secret(f'totp_pending:{user_id}', None)
    db.audit('user_removed', {'user_id': user_id}, actor(request))
    return {'deleted': True}


# ---------------------------------------------------------------- status
@router.get('/status')
async def status(request: Request):
    db, security, engine, runtime = ctx(request)
    settings = db.settings()
    now = utcnow()
    decision = engine.latest_decision()
    site = db.latest_site()
    systems = []
    for s in settings.systems:
        latest = db.latest_system(s.id)
        systems.append(dict(id=s.id, name=s.name, plugin=s.plugin, enabled=s.enabled, controllable=s.controllable,
                            reading=latest, poll=engine.devices.status.get(f'system:{s.id}')))
    vehicles = [dict(id=v.id, name=v.name, plugin=v.plugin, enabled=v.enabled, reading=db.latest_vehicle(v.id), manual_soc=v.manual_soc,
                     target_pct=v.target_pct, can_stop_charging=ev_charging.can_stop(v), poll=engine.devices.status.get(f'vehicle:{v.id}')) for v in settings.vehicles]
    try:
        today = outcomes.today_so_far(db, settings, now)
    except Exception:
        today = None
    money = outcomes.money_periods(db, settings, now) if systems else {}
    if systems:
        money['projected_today'] = outcomes.projected_today(db, settings, now, decision, money.get('today'))
    return {
        'mode': settings.mode, 'server_time': stamp(now), 'timezone': settings.site.timezone, 'site_name': settings.site.name,
        'tariff': rate_at(settings.tariffs, now, settings.zone),
        'pending_tariffs': [dict(id=p.id, name=p.name, retailer=p.retailer) for p in pending_plans(settings.tariffs)],
        'site': site, 'systems': systems, 'vehicles': vehicles,
        'decision': {k: decision[k] for k in ('id', 'created_at', 'summary')} if decision else None,
        'guards': engine.controller.guards(settings, now) + engine.controller.recovery_reasons(now),
        'control_commissioned': engine.controller.commissioned(settings), 'control_active': db.meta('control_active', {}),
        'control_retry': engine.controller.recovery_status(),
        'heartbeat': db.meta('worker_heartbeat'), 'advice': engine.advice(), 'today': today,
        'money': money,
        'unread': db.one('SELECT COUNT(*) AS n FROM notifications WHERE seen=0')['n'], 'demo': runtime.demo,
        'battery_kwh': settings.battery_kwh(), 'battery_reserve_pct': _reserve_pct(settings),
        'using_example_tariff': plan_at(settings.tariffs, now) == default_tariffs()[0],
        'weather_enabled': settings.site.weather_enabled,
        'weather': weather.current_hour(db, now) if settings.site.weather_enabled or runtime.demo else None,
        'solar_estimate': forecast.solar_snapshot(db, settings, now) if systems else {},
        'site_located': settings.site.latitude is not None and settings.site.longitude is not None,
        'tariff_today': _tariff_today(settings, now)}


def _reserve_pct(settings: Settings):
    batteries = [s.battery for s in settings.active_systems() if s.battery]
    capacity = sum(b.capacity_kwh for b in batteries)
    return round(sum(b.capacity_kwh * b.reserve_pct for b in batteries) / capacity, 1) if capacity else None


def _tariff_today(settings: Settings, now):
    """Today's price periods (local time) for the active plan, for read-only displays."""
    from ..tariffs import plan_at
    zone = settings.zone
    local = now.astimezone(zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for i in range(48):
        at = start + timedelta(minutes=30 * i)
        plan = plan_at(settings.tariffs, at)
        period = plan.period_at(at)
        if out and out[-1]['period'] == period.name and out[-1]['plan'] == plan.id:
            out[-1]['end'] = (at + timedelta(minutes=30)).strftime('%H:%M')
        else:
            out.append(dict(plan=plan.id, period=period.name, label=period.label or period.name, start=at.strftime('%H:%M'),
                            end=(at + timedelta(minutes=30)).strftime('%H:%M'), buy=period.import_rate, sell=period.export_rate))
    if out:
        out[-1]['end'] = '24:00'
    return out


@router.get('/plan')
async def latest_plan(request: Request):
    db, _, engine, _ = ctx(request)
    settings = db.settings()
    now = utcnow()
    cutoff = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    start = cutoff - timedelta(hours=24)
    observations = {r['ts']: r for r in select_intervals(db, stamp(start), stamp(cutoff))}
    history = []
    for i in range(48):
        at = start + timedelta(minutes=30 * i)
        ts = stamp(at)
        r = observations.get(ts)
        p = rate_at(settings.tariffs, at, settings.zone)
        actual = {}
        if r and r['coverage_s'] >= 1500:
            actual = {key: r[key] for key in ('solar_kwh', 'load_kwh', 'import_kwh', 'export_kwh', 'soc_end', 'coverage_s')}
        history.append(dict(ts=ts, buy_rate=p['buy'], sell_rate=p['sell'], **actual))
    decision = engine.latest_decision()
    return {'decision': {k: decision[k] for k in ('id', 'created_at', 'summary', 'version', 'forecast_id', 'tariff_id')} if decision else None,
            'intervals': _intervals(db, decision['id']) if decision else [], 'history': history, 'as_of': stamp(now)}


def _intervals(db, decision_id):
    rows = db.rows('SELECT di.*, fp.house_kwh*di.hours/fp.hours AS house_kwh, fp.house_p10*di.hours/fp.hours AS house_p10, '
                   'fp.house_p90*di.hours/fp.hours AS house_p90, fp.ev_kwh*di.hours/fp.hours AS ev_kwh, '
                   'fp.solar_p10*di.hours/fp.hours AS solar_p10, fp.solar_p90*di.hours/fp.hours AS solar_p90, fp.temperature, '
                   'fp.radiation, fp.load_basis, fp.solar_basis FROM decision_intervals di JOIN decisions d ON d.id=di.decision_id '
                   'LEFT JOIN forecast_points fp ON fp.forecast_id=d.forecast_id AND fp.ts=('
                   'SELECT ts FROM forecast_points WHERE forecast_id=d.forecast_id AND ts<=di.ts ORDER BY ts DESC LIMIT 1) WHERE di.decision_id=? ORDER BY di.ts',
                   (decision_id,))
    for r in rows:
        r['systems'] = decode(r['systems'])
    return rows


@router.post('/plan')
async def replan(request: Request):
    _, _, engine, _ = ctx(request)
    decision = await engine.replan()
    return {'id': decision['id'], 'summary': decision['summary']}


@router.get('/decisions')
async def decisions(request: Request, limit: int = 96):
    db, *_ = ctx(request)
    rows = db.rows('SELECT id, created_at, version, forecast_id, mode, tariff_id, input_hash, summary FROM decisions ORDER BY id DESC LIMIT ?',
                   (max(1, min(limit, 1000)),))
    for r in rows:
        r['summary'] = decode(r['summary'])
    return rows


@router.get('/decisions/{decision_id}')
async def decision_detail(decision_id: int, request: Request):
    db, *_ = ctx(request)
    row = db.one('SELECT * FROM decisions WHERE id=?', (decision_id,))
    if not row:
        raise HTTPException(404, 'Decision not found')
    row['summary'] = decode(row['summary'])
    inputs = decode(row['inputs'])
    inputs.pop('settings', None)  # large; available via /api/settings/history
    row['inputs'] = inputs
    row['intervals'] = _intervals(db, decision_id)
    return row


@router.get('/history')
async def history(request: Request, days: int = 7):
    db, *_ = ctx(request)
    days = max(1, min(days, 400))
    start = stamp(utcnow() - timedelta(days=days))
    rows = select_intervals(db, start)
    return {'intervals': [{k: r[k] for k in ('ts', 'source', 'solar_kwh', 'load_kwh', 'ev_kwh', 'import_kwh', 'export_kwh',
                                               'charge_kwh', 'discharge_kwh', 'soc_end', 'limited_s', 'coverage_s', 'quality')} for r in rows],
            'note': 'Uses energy counters where available and power readings otherwise. Missing time is left out.'}


@router.get('/live')
async def live(request: Request, hours: int = 24):
    db, *_ = ctx(request)
    hours = max(1, min(hours, 72))
    rows = db.rows('SELECT ts, soc, solar_kw, load_kw, import_kw, export_kw, charge_kw, discharge_kw, ev_kw, output_limited FROM site_readings '
                   'WHERE ts>=? ORDER BY ts', (stamp(utcnow() - timedelta(hours=hours)),))
    return {'readings': rows}


@router.get('/outcomes')
async def daily_outcomes(request: Request, days: int = 60):
    db, *_ = ctx(request)
    rows = db.rows('SELECT data FROM daily_outcomes ORDER BY day DESC LIMIT ?', (max(1, min(days, 1000)),))
    return {'days': [decode(r['data']) for r in rows][::-1],
            'daily_history': [dict(day=r['day'], source=r['source'], **decode(r['data'])) for r in
                              db.rows('SELECT * FROM daily_history ORDER BY day DESC LIMIT ?', (max(1, min(days, 1000)),))][::-1]}


@router.get('/forecast/accuracy')
async def accuracy(request: Request, limit: int = 200):
    db, *_ = ctx(request)
    rows = db.rows('SELECT e.forecast_id, f.created_at, e.metrics FROM evaluations e JOIN forecasts f ON f.id=e.forecast_id '
                   'ORDER BY e.forecast_id DESC LIMIT ?', (max(1, min(limit, 2000)),))
    params = {r['name']: dict(version=r['version'], fitted_at=r['fitted_at'], data=decode(r['data'])) for r in db.rows('SELECT * FROM model_params')}
    load = params.get('load', {}).get('data', {})
    now = utcnow()
    recent = db.one('SELECT id, created_at FROM forecasts WHERE created_at>=? AND created_at<=? ORDER BY id LIMIT 1',
                    (stamp(now - timedelta(days=1)), stamp(now - timedelta(minutes=30))))
    provisional = None
    if recent:
        metrics = forecast.score_run(db, recent['id'], now)
        if metrics['samples']:
            provisional = dict(created_at=recent['created_at'], **metrics)
    return {'provisional': provisional, 'evaluations': [dict(forecast_id=r['forecast_id'], created_at=r['created_at'], **decode(r['metrics'])) for r in rows][::-1],
            'load_model': {k: v for k, v in load.items() if k not in ('counts', 'bands')},
            'solar_model': params.get('solar', {}).get('data')}


@router.get('/forecast/comparison')
def forecast_comparison(request: Request, run_id: int | None = None, today: bool = False):
    db, *_ = ctx(request)
    now = utcnow()
    available = ('SELECT id, created_at FROM forecasts f WHERE EXISTS '
                 '(SELECT 1 FROM forecast_points p WHERE p.forecast_id=f.id)')
    runs = db.rows(available + ' ORDER BY id DESC LIMIT 200')
    if today and run_id is None:
        return {'runs': runs, **forecast.today_comparison(db, db.settings(), now)}
    if not runs:
        return {'runs': [], 'points': [], 'forecast_id': None}
    if run_id is None:
        # Prefer a run with most of its first day elapsed, including on a new installation.
        first_day = stamp(now - timedelta(hours=24))
        run_id = next((r['id'] for r in reversed(runs) if r['created_at'] >= first_day), runs[0]['id'])
    elif all(r['id'] != run_id for r in runs):
        selected = db.one(available + ' AND id=?', (run_id,))
        if not selected:
            raise HTTPException(404, 'Forecast run no longer available')
        runs.append(selected)
    return {'runs': runs, **forecast.comparison(db, run_id, now)}


# -------------------------------------------------------------- settings
def _secret_status(security, settings: Settings):
    out = {}
    for kind, items in (('system', settings.systems), ('vehicle', settings.vehicles)):
        for item in items:
            out[f'{kind}:{item.id}'] = sorted(security.secret_names(f'{kind}:{item.id}:'))
    return out


@router.get('/settings')
async def get_settings(request: Request):
    db, security, _, runtime = ctx(request)
    settings = db.settings()
    plugins = registry.describe()
    if runtime.demo:
        plugins = [p for p in plugins if p['name'] in DEMO_PLUGINS]
    return {'settings': settings.model_dump(), 'schema': Settings.model_json_schema(), 'plugins': plugins,
            'configured_secrets': _secret_status(security, settings), 'channel_kinds': describe_channels(),
            'commissioned': db.meta('control_commissioned'),
            'last_tests': {f'{k}:{i.id}': db.meta(f'last_test:{k}:{i.id}') for k, items in (('system', settings.systems), ('vehicle', settings.vehicles)) for i in items}}


@router.put('/settings')
async def put_settings(request: Request):
    db, security, engine, _ = ctx(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, 'Expected a settings object')
    old = db.settings()
    body['mode'] = old.mode  # mode only changes via /control
    new = Settings.model_validate(body)
    for item in [*new.systems, *new.vehicles]:
        check_demo_plugin(security, item.plugin)
    registry.validate_settings(new)
    if old.mode == 'automatic' and commissioning_hash(new) != commissioning_hash(old):
        raise HTTPException(409, 'Stop automatic control before changing controllable systems')
    changed = db.save_settings(new, actor(request))
    db.audit('settings_changed', {'sections': changed}, actor(request))
    if (old.site.latitude, old.site.longitude) != (new.site.latitude, new.site.longitude):
        db.execute('DELETE FROM weather_hours WHERE ts>=?', (stamp(utcnow().replace(minute=0, second=0, microsecond=0)),))
        db.set_meta('location_changed_at', stamp())
        engine.next['weather'] = 0
    if old.site.weather_enabled != new.site.weather_enabled:
        engine.next['weather'] = 0
    if old.site.poll_seconds != new.site.poll_seconds:
        engine.next['poll'] = 0
    engine.request_plan()
    return {'saved': True, 'changed': changed}


class AddressQuery(Body):
    query: str = Field(min_length=3, max_length=200)


class Coordinates(Body):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


@router.post('/location/search')
async def address_lookup(body: AddressQuery):
    try:
        return await weather.addresses(body.query)
    except Exception:
        raise HTTPException(424, 'Address search is unavailable. Try again or enter coordinates below.') from None


@router.post('/location/timezone')
async def lookup_zone(body: Coordinates):
    try:
        return {'timezone': await weather.timezone_at(body.latitude, body.longitude)}
    except Exception:
        raise HTTPException(424, 'Time zone lookup is unavailable. Check the time zone manually.') from None


@router.post('/weather/refresh')
async def refresh_weather(request: Request):
    db, _, engine, runtime = ctx(request)
    settings = db.settings()
    if not runtime.demo:
        if not weather.located(settings) or not settings.site.weather_enabled:
            raise HTTPException(409, 'Save a home location and enable weather in Planning first.')
        try:
            await weather.refresh(db, settings)
        except Exception:
            raise HTTPException(424, 'Open-Meteo could not supply a forecast. Check outbound HTTPS and retry.') from None
    age = weather.freshness_hours(db)
    if age is None or age > 6:
        raise HTTPException(424, 'No recent forecast covers the current hour.')
    engine.request_plan()
    return {'weather': weather.current_hour(db, utcnow()), 'simulated': runtime.demo}


@router.get('/settings/history')
async def settings_history(request: Request):
    db, *_ = ctx(request)
    return [dict(id=r['id'], at=r['at'], actor=r['actor'], changed=decode(r['changed'])) for r in
            db.rows('SELECT id, at, actor, changed FROM settings_history ORDER BY id DESC LIMIT 200')]


@router.put('/secrets/{target}')
async def put_secrets(target: str, body: Secrets, request: Request):
    db, security, engine, _ = ctx(request)
    settings = db.settings()
    kind, _, item_id = target.partition(':')
    items = {'system': settings.systems, 'vehicle': settings.vehicles}.get(kind, [])
    spec = next((i for i in items if i.id == item_id), None)
    if not spec:
        raise HTTPException(404, 'Unknown system or vehicle (save settings first)')
    allowed = {f.name for f in registry.get(spec.plugin, kind).secrets}
    unknown = set(body.values) - allowed
    if unknown:
        raise HTTPException(422, f'Unknown credential field(s): {", ".join(sorted(unknown))}')
    if settings.mode == 'automatic' and kind == 'system' and spec.controllable:
        raise HTTPException(409, 'Stop automatic control before changing credentials of a controlled system')
    for name, value in body.values.items():
        if len(value) > 8000:
            raise HTTPException(422, 'Value too long')
        security.save_secret(f'{target}:{name}', value.strip())
    db.audit('credentials_changed', {'target': target, 'fields': sorted(body.values)}, actor(request))
    engine.next['poll'] = engine.next['vehicles'] = 0
    return {'saved': sorted(body.values)}


@router.post('/test/{kind}/{item_id}')
async def test_device(kind: str, item_id: str, request: Request):
    db, _, engine, _ = ctx(request)
    settings = db.settings()
    try:
        plugin = engine.devices.system(settings, item_id) if kind == 'system' else engine.devices.vehicle(settings, item_id)
        result = await plugin.test()
    except StopIteration:
        raise HTTPException(404, 'Unknown system or vehicle') from None
    except Exception as error:
        from ..devices import _message
        db.audit('device_test_failed', {'target': f'{kind}:{item_id}', 'error': type(error).__name__}, actor(request))
        raise HTTPException(424, f'Test failed: {_message(error)}') from None
    db.audit('device_test', {'target': f'{kind}:{item_id}'}, actor(request))
    db.set_meta(f'last_test:{kind}:{item_id}', stamp())
    return json.loads(json.dumps(result, default=str))


class DraftTest(Body):
    spec: dict
    secrets: dict[str, str] = Field(default_factory=dict)


class DeviceSave(DraftTest):
    original: dict | None = None


@router.put('/devices/{kind}')
async def save_device(kind: str, body: DeviceSave, request: Request):
    """Save one device and its encrypted credentials in the same transaction."""
    db, security, engine, _ = ctx(request)
    if kind not in ('system', 'vehicle'):
        raise HTTPException(404, 'Unknown kind')
    spec = (EnergySystem if kind == 'system' else Vehicle).model_validate(body.spec)
    check_demo_plugin(security, spec.plugin)
    cls = registry.get(spec.plugin, kind)
    target = f'{kind}:{spec.id}'
    fields = {field.name for field in cls.secrets}
    if set(body.secrets) - fields or any(len(v) > 8000 for v in body.secrets.values()):
        raise HTTPException(422, 'Invalid credential field or length')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old = Settings.model_validate_json(c.execute('SELECT data FROM settings WHERE id=1').fetchone()['data'])
        items = getattr(old, kind + 's')
        existing = next((item for item in items if item.id == spec.id), None)
        if (existing.model_dump() if existing else None) != body.original:
            raise HTTPException(409, 'This device has changed or its name is taken. Reopen the wizard.')
        if existing and existing.plugin != spec.plugin:
            raise HTTPException(422, 'Choose a new device name for a different plugin.')
        new = old.model_copy(deep=True)
        setattr(new, kind + 's', [spec if i.id == spec.id else i for i in items] if existing else [*items, spec])
        registry.validate_settings(new)
        if old.mode == 'automatic' and (commissioning_hash(old) != commissioning_hash(new)
                or (kind == 'system' and spec.controllable and body.secrets)):
            raise HTTPException(409, 'Stop automatic control before changing a controlled system.')
        saved = set(security.secret_names(target + ':')) if existing else set()
        values = {k: v.strip() for k, v in body.secrets.items() if v.strip()}
        for field in cls.secrets:
            if field.required and field.name not in saved and field.name not in values:
                raise HTTPException(422, f'Enter {field.label}.')
        if not existing:
            prefix = target + ':'
            c.execute('DELETE FROM secrets WHERE substr(name,1,?)=?', (len(prefix), prefix))
        for name, value in values.items():
            security.save_secret_in(c, f'{target}:{name}', value)
        if not existing or spec != existing or values:
            c.execute('DELETE FROM meta WHERE key=?', (f'last_test:{target}',))
        db.save_settings_in(c, new, actor(request))
        c.execute('INSERT INTO audit(at, actor, event, data) VALUES(?,?,?,?)',
                  (stamp(), actor(request), 'device_saved', encode({'target': target, 'credential_fields': sorted(values)})))
    engine.next['poll' if kind == 'system' else 'vehicles'] = 0
    engine.request_plan()
    return {'saved': True}


@router.post('/test-draft/{kind}')
async def test_draft(kind: str, body: DraftTest, request: Request):
    """Read-only connection test for a system/vehicle that hasn't been saved (wizard step 2)."""
    from ..devices import _message, draft_instance
    db, security, *_ = ctx(request)
    if kind not in ('system', 'vehicle'):
        raise HTTPException(404, 'Unknown kind')
    # Read-only probes construct a plugin before hardware ratings are entered.
    # These constructor defaults are never saved or reported as discoveries.
    defaults = {'ac_kw': 1} if kind == 'system' else {'capacity_kwh': 60}
    values = {**defaults, **body.spec}
    spec = (EnergySystem if kind == 'system' else Vehicle).model_validate(values)
    cls = registry.get(spec.plugin, kind)
    unknown = set(body.secrets) - {f.name for f in cls.secrets}
    if unknown:
        raise HTTPException(422, f'Unknown credential field(s): {", ".join(sorted(unknown))}')
    if any(len(v) > 8000 for v in body.secrets.values()):
        raise HTTPException(422, 'Value too long')
    plugin = None
    try:
        plugin = draft_instance(db, security, kind, spec, db.settings().site.timezone, body.secrets)
        result = await plugin.test()
    except Exception as error:
        raise HTTPException(424, f'Test failed: {_message(error)}') from None
    finally:
        if plugin is not None:
            try:
                await plugin.close()
            except Exception:
                pass
    db.audit('device_test', {'target': f'{kind}:{spec.id}', 'draft': True}, actor(request))
    return json.loads(json.dumps(result, default=str))


# --------------------------------------------------------------- tariffs
class FlatRates(Body):
    original: TariffPlan
    name: str = Field(min_length=1, max_length=80)
    buy: float
    sell: float
    daily: float


@router.post('/tariffs/{tariff_id}/rates')
async def edit_rates(tariff_id: str, body: FlatRates, request: Request):
    db, _, engine, _ = ctx(request)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        settings = Settings.model_validate_json(c.execute('SELECT data FROM settings WHERE id=1').fetchone()['data'])
        plan = next((t for t in settings.tariffs if t.id == tariff_id), None)
        if plan != body.original:
            raise HTTPException(409, 'This tariff changed. Reopen the editor.')
        if len(plan.periods) != 1:
            raise HTTPException(422, 'Select a flat-rate plan.')
        if plan == default_tariffs()[0]:
            plan.notes = ''
        plan.name, plan.supply_daily = body.name, body.daily
        plan.periods[0].import_rate, plan.periods[0].export_rate = body.buy, body.sell
        db.save_settings_in(c, Settings.model_validate(settings.model_dump()), actor(request))
    engine.request_plan()
    return {'saved': True}


@router.post('/tariffs/import')
async def import_tariff(request: Request):
    db, *_ = ctx(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, 'Expected a JSON object')
    if 'data' in body and isinstance(body['data'], dict) and 'electricityContract' in body['data']:
        from ..tariff_import import from_cdr
        plan = from_cdr(body['data'])
    else:
        plan = TariffPlan.model_validate(body)
    settings = db.settings()
    if any(t.id == plan.id for t in settings.tariffs):
        raise HTTPException(409, f'A tariff with id "{plan.id}" already exists')
    plan = plan.model_copy(update={'status': 'pending', 'active_from': None})
    settings.tariffs = [*settings.tariffs, plan]
    db.save_settings(Settings.model_validate(settings.model_dump()), actor(request))
    db.audit('tariff_imported', {'id': plan.id}, actor(request))
    return {'imported': plan.model_dump()}


@router.post('/tariffs/{tariff_id}/activate')
async def activate_tariff(tariff_id: str, body: ActivateTariff, request: Request):
    db, security, engine, _ = ctx(request)
    security.reauthenticate(me(request), body.password, body.otp)
    settings = db.settings()
    plan = next((t for t in settings.tariffs if t.id == tariff_id), None)
    if not plan:
        raise HTTPException(404, 'Tariff not found')
    when = local_time(body.active_from, settings)
    plans = [t.model_copy(update={'status': 'active', 'active_from': when}) if t.id == tariff_id else t for t in settings.tariffs]
    settings.tariffs = plans
    db.save_settings(Settings.model_validate(settings.model_dump()), actor(request))
    db.audit('tariff_activated', {'id': tariff_id, 'active_from': when}, actor(request))
    engine.request_plan()
    return {'activated': tariff_id, 'active_from': when}


@router.post('/tariffs/{tariff_id}/retire')
async def retire_tariff(tariff_id: str, request: Request):
    db, _, engine, _ = ctx(request)
    settings = db.settings()
    settings.tariffs = [t.model_copy(update={'status': 'retired'}) if t.id == tariff_id else t for t in settings.tariffs]
    db.save_settings(Settings.model_validate(settings.model_dump()), actor(request))
    engine.request_plan()
    return {'retired': tariff_id}


# ----------------------------------------------------- holidays/overrides
@router.get('/holidays')
async def holidays(request: Request):
    db, *_ = ctx(request)
    return db.rows('SELECT * FROM holidays ORDER BY start DESC LIMIT 200')


@router.post('/holidays')
async def add_holiday(body: HolidayIn, request: Request):
    db, _, engine, _ = ctx(request)
    settings = db.settings()
    start, end = local_time(body.start, settings), local_time(body.end, settings)
    if start >= end:
        raise HTTPException(422, 'End must be after start')
    holiday_id = db.execute('INSERT INTO holidays(name, start, end, load_factor, ev_away, created_at) VALUES(?,?,?,?,?,?)',
                            (body.name, start, end, body.load_factor, int(body.ev_away), stamp()))
    db.audit('holiday_added', {'id': holiday_id}, actor(request))
    engine.request_plan()
    return {'id': holiday_id}


@router.delete('/holidays/{holiday_id}')
async def delete_holiday(holiday_id: int, request: Request):
    db, _, engine, _ = ctx(request)
    db.execute('DELETE FROM holidays WHERE id=?', (holiday_id,))
    engine.request_plan()
    return {'deleted': True}


def charging_vehicle(settings: Settings, vehicle_id: str | None) -> Vehicle:
    vehicles = settings.active_vehicles()
    vehicle = next((v for v in vehicles if v.id == vehicle_id), None)
    if vehicle_id is None and len(vehicles) == 1:
        return vehicles[0]
    if vehicle is None:
        raise HTTPException(422, 'Choose an enabled vehicle.')
    return vehicle


@router.get('/overrides/charge-preview')
def preview_charge(request: Request, vehicle_id: str, minutes: int, target_pct: float | None = None,
                   starting_soc: float | None = None):
    db, _, engine, _ = ctx(request)
    if (not 5 <= minutes <= 60 * 24 * 14 or (target_pct is not None and not 0 < target_pct <= 100)
            or (starting_soc is not None and not 0 <= starting_soc <= 100)):
        raise HTTPException(422, 'Enter a valid duration and battery percentage.')
    settings = db.settings()
    vehicle = charging_vehicle(settings, vehicle_id)
    now = utcnow().replace(microsecond=0)
    data = {'target_pct': target_pct}
    if starting_soc is not None:
        data['starting_soc'] = starting_soc
    return advice.charging_preview(db, settings, vehicle, now, now + timedelta(minutes=minutes), data,
                                   stamp(now), engine.latest_decision())


@router.get('/overrides')
def overrides(request: Request):
    db, _, engine, _ = ctx(request)
    now = utcnow().replace(microsecond=0)
    rows = db.rows("SELECT * FROM overrides WHERE cancelled_at IS NULL AND (expires>? OR "
                   "(expires>? AND kind='ev_charge_now' AND json_extract(data,'$.state')='stopping')) ORDER BY id DESC",
                   (stamp(now), stamp(now - 2 * ev_charging.END_GRACE)))
    settings = db.settings()
    vehicles = {v.id: v for v in settings.active_vehicles()}
    decision = None
    for r in rows:
        r['data'] = decode(r['data'])
        v = vehicles.get(r['data'].get('vehicle_id'))
        if r['kind'] == 'ev_charge_now' and v and not r['data'].get('state') and r['starts'] <= stamp(now):
            decision = decision or engine.latest_decision()
            r['estimate'] = advice.charging_preview(db, settings, v, now, datetime.fromisoformat(r['expires']),
                                                    r['data'], r['starts'], decision)
    return rows


@router.post('/overrides')
async def add_override(body: OverrideIn, request: Request):
    db, _, engine, _ = ctx(request)
    if body.kind == 'reserve' and body.pct is None:
        raise HTTPException(422, 'Give the reserve percentage to hold')
    data = {k: v for k, v in (('pct', body.pct), ('vehicle_id', body.vehicle_id)) if v is not None}
    if body.kind != 'ev_charge_now' and (body.target_pct is not None or body.starting_soc is not None or body.stop_at_limit):
        raise HTTPException(422, 'Charging targets apply to a charging session.')
    async with engine.ev_lock:
        now = utcnow()
        if body.kind == 'ev_charge_now':
            v = charging_vehicle(db.settings(), body.vehicle_id)
            data['vehicle_id'] = v.id
            if body.stop_at_limit and (not ev_charging.can_stop(v) or body.minutes > 24 * 60):
                raise HTTPException(422, 'Automatic stopping needs a compatible connection and a session of 24 hours or less.')
            data.update({k: val for k, val in (('target_pct', body.target_pct), ('starting_soc', body.starting_soc)) if val is not None})
            data['stop_at_limit'] = body.stop_at_limit
        with db.connect() as c:
            if body.kind == 'ev_charge_now':
                c.execute("UPDATE overrides SET cancelled_at=? WHERE kind='ev_charge_now' AND cancelled_at IS NULL "
                          "AND expires>? AND json_extract(data,'$.vehicle_id')=?",
                          (stamp(now), stamp(now - 2 * ev_charging.END_GRACE), data['vehicle_id']))
            override_id = c.execute('INSERT INTO overrides(created_at, starts, expires, kind, data) VALUES(?,?,?,?,?)',
                                    (stamp(now), stamp(now), stamp(now + timedelta(minutes=body.minutes)), body.kind, encode(data))).lastrowid
    db.audit('override_added', {'id': override_id, 'kind': body.kind}, actor(request))
    engine.request_plan()
    return {'id': override_id}


@router.delete('/overrides/{override_id}')
async def cancel_override(override_id: int, request: Request):
    db, _, engine, _ = ctx(request)
    async with engine.ev_lock:
        db.execute('UPDATE overrides SET cancelled_at=? WHERE id=?', (stamp(), override_id))
    engine.request_plan()
    return {'cancelled': True}


@router.get('/annotations')
async def annotations(request: Request):
    db, *_ = ctx(request)
    return db.rows('SELECT * FROM annotations ORDER BY start DESC LIMIT 300')


@router.post('/annotations')
async def add_annotation(body: AnnotationIn, request: Request):
    db, *_ = ctx(request)
    settings = db.settings()
    start = local_time(body.start, settings)
    end = local_time(body.end, settings) if body.end else None
    annotation_id = db.execute('INSERT INTO annotations(created_at, start, end, kind, text) VALUES(?,?,?,?,?)',
                               (stamp(), start, end, body.kind, body.text))
    if body.kind == 'exclude' and end:
        db.execute('INSERT INTO exclusions(start, end, reason) VALUES(?,?,?)', (start, end, body.text[:300]))
    return {'id': annotation_id}


@router.delete('/annotations/{annotation_id}')
async def delete_annotation(annotation_id: int, request: Request):
    db, *_ = ctx(request)
    db.execute('DELETE FROM annotations WHERE id=?', (annotation_id,))
    return {'deleted': True, 'note': 'Any exclusion created from this note is kept; delete it under exclusions if needed.'}


@router.get('/exclusions')
async def exclusions(request: Request):
    db, *_ = ctx(request)
    return db.rows('SELECT * FROM exclusions ORDER BY start DESC')


@router.post('/exclusions')
async def add_exclusion(body: ExclusionIn, request: Request):
    db, *_ = ctx(request)
    settings = db.settings()
    start, end = local_time(body.start, settings), local_time(body.end, settings)
    if start >= end:
        raise HTTPException(422, 'End must be after start')
    return {'id': db.execute('INSERT INTO exclusions(start, end, reason) VALUES(?,?,?)', (start, end, body.reason))}


@router.delete('/exclusions/{exclusion_id}')
async def delete_exclusion(exclusion_id: int, request: Request):
    db, *_ = ctx(request)
    db.execute('DELETE FROM exclusions WHERE id=?', (exclusion_id,))
    return {'deleted': True}


# --------------------------------------------------------------- control
CONFIRMATION = 'ENABLE BATTERY CONTROL'


@router.post('/control/enable')
async def enable_control(body: EnableControl, request: Request):
    db, security, engine, runtime = ctx(request)
    if not runtime.worker:
        raise HTTPException(409, 'Not enabled. The background worker must be running.')
    async with engine.control_lock:
        current = db.settings()
        if current.mode == 'automatic':
            return {'mode': 'automatic'}
        first = not engine.controller.commissioned(current)
        if first:
            if body.confirmation.strip() != CONFIRMATION or not body.acknowledge:
                raise HTTPException(422, 'Read the commissioning checklist, tick the acknowledgement and type ' + CONFIRMATION)
            security.reauthenticate(me(request), body.password, body.otp)

        settings = current.model_copy(update={'mode': 'automatic'})
        blocking = engine.controller.guards(settings, commissioning=first, check_plan=False)
        decision = None
        if not blocking:
            try:
                decision = await engine.replan(settings)
            except Exception as error:
                if first:
                    raise HTTPException(503, 'Not enabled. A fresh plan could not be built.') from error
                engine.request_plan()
                blocking = ['Waiting for a fresh plan']
            else:
                blocking = engine.controller.guards(settings, commissioning=first)
        if db.settings() != current:
            raise HTTPException(409, 'Not enabled. Settings changed while planning. Try again.')
        if first and blocking:
            raise HTTPException(409, 'Not enabled. Fix these first: ' + '; '.join(blocking))
        if first:
            db.set_meta('control_commissioned', {'at': stamp(), 'hash': commissioning_hash(settings)})
        db.save_settings(settings, actor(request))
        db.audit('control_enabled', {'systems': [s.id for s in settings.systems if s.controllable]}, actor(request))
        if blocking:
            return {'mode': 'automatic', 'initial_control': {'executed': False, 'reasons': blocking}}
        result = await engine.controller.cycle(settings)
        return {'mode': 'automatic', 'decision_id': decision['id'], 'initial_control': result}


@router.post('/control/retry')
async def retry_control(request: Request):
    db, _, engine, runtime = ctx(request)
    if not runtime.worker:
        raise HTTPException(409, 'The background worker must be running.')
    async with engine.control_lock:
        recovery = engine.controller.recovery_status()
        settings = db.settings()
        if not recovery or (settings.mode != 'automatic' and recovery['phase'] != 'release'):
            raise HTTPException(409, 'There is no paused command or release to retry.')
        now = utcnow()
        last = db.meta('control_manual_retry_at')
        if last and now - datetime.fromisoformat(last) < timedelta(seconds=15):
            raise HTTPException(429, 'A manual retry just ran. Try again in a few seconds.')
        db.set_meta('control_manual_retry_at', stamp(now))
        db.audit('control_retry_requested', {'phase': recovery['phase']}, actor(request))
        result = await engine.controller.cycle(settings, retry_now=True)
        return {'recovery': engine.controller.recovery_status(), **result}


@router.post('/control/stop')
async def stop_control(request: Request):
    db, _, engine, _ = ctx(request)
    async with engine.control_lock:
        problems = await engine.controller.stop(db.settings(), 'Stopped by owner', actor=actor(request))
    return {'mode': 'monitor', 'problems': problems}


@router.get('/commands')
async def commands(request: Request):
    db, *_ = ctx(request)
    rows = db.rows('SELECT * FROM commands ORDER BY id DESC LIMIT 200')
    for r in rows:
        r['payload'], r['result'] = decode(r['payload']), decode(r['result'])
    return rows


# --------------------------------------------------------- notifications
@router.get('/notifications')
async def notifications(request: Request, limit: int = 100, unread: bool = False):
    db, *_ = ctx(request)
    where = ' WHERE seen=0' if unread else ''
    rows = db.rows('SELECT id, created_at, event, severity, title, body, seen FROM notifications' + where + ' ORDER BY id DESC LIMIT ?',
                   (max(1, min(limit, 500)),))
    deliveries = db.rows('SELECT d.id, d.notification_id, d.channel_id, d.status, d.attempts, d.next_at, d.result, c.name AS channel '
                         'FROM deliveries d JOIN channels c ON c.id=d.channel_id ORDER BY d.id DESC LIMIT 200')
    for d in deliveries:
        d['result'] = decode(d['result'])
    return {'notifications': rows, 'deliveries': deliveries}


@router.post('/notifications/seen')
async def mark_seen(request: Request, notification_id: int | None = None):
    db, *_ = ctx(request)
    if notification_id is None:
        db.execute('UPDATE notifications SET seen=1 WHERE seen=0')
    else:
        db.execute('UPDATE notifications SET seen=1 WHERE id=?', (notification_id,))
    return {'seen': True}


@router.get('/notification-rules')
async def rules(request: Request):
    db, _, engine, _ = ctx(request)
    return [dict(event=event, label=meta[0], severity=meta[1], **engine.notifications.rule(event)) for event, meta in EVENTS.items()]


@router.put('/notification-rules/{event}')
async def save_rule(event: str, body: RuleIn, request: Request):
    db, *_ = ctx(request)
    if event not in EVENTS:
        raise HTTPException(404, 'Unknown event')
    check_template(body.title)
    check_template(body.body)
    db.execute('INSERT OR REPLACE INTO notification_rules VALUES(?,?)', (event, encode(body.model_dump())))
    return {'saved': True}


class GroupIn(Body):
    events: dict[str, bool] = Field(default_factory=dict)
    channels: list[int] = Field(default_factory=list, max_length=20)
    cooldown_minutes: int = Field(60, ge=0, le=10080)
    respect_quiet: bool = True


@router.get('/notification-groups')
async def notification_groups(request: Request):
    _, _, engine, _ = ctx(request)
    return engine.notifications.groups()


@router.put('/notification-groups/{group_id}')
async def save_notification_group(group_id: str, body: GroupIn, request: Request):
    db, _, engine, _ = ctx(request)
    engine.notifications.save_group(group_id, body.events, body.channels, body.cooldown_minutes, body.respect_quiet)
    db.audit('alert_group_saved', {'group': group_id}, actor(request))
    return {'saved': True}


@router.get('/channels')
async def channels(request: Request):
    db, security, *_ = ctx(request)
    rows = db.rows('SELECT id, name, kind, enabled, created_at FROM channels ORDER BY id')
    for r in rows:
        config = decode(security.secret(f"channel:{r['id']}"), {})
        fields = CHANNELS[r['kind']].fields
        # Non-secret values are shown; secret ones only as "set".
        r['config'] = {f.name: ('' if f.secret else config.get(f.name, '')) for f in fields}
        r['configured'] = sorted(f.name for f in fields if config.get(f.name))
    return {'channels': rows, 'kinds': describe_channels()}


@router.post('/channels')
async def add_channel(body: ChannelIn, request: Request):
    db, security, *_ = ctx(request)
    if body.kind not in CHANNELS:
        raise HTTPException(422, 'Unknown channel type')
    config = CHANNELS[body.kind].validate(body.config)
    channel_id = db.execute('INSERT INTO channels(name, kind, enabled, created_at) VALUES(?,?,?,?)', (body.name, body.kind, int(body.enabled), stamp()))
    security.save_secret(f'channel:{channel_id}', encode(config))
    db.audit('channel_added', {'id': channel_id, 'kind': body.kind}, actor(request))
    return {'id': channel_id}


@router.put('/channels/{channel_id}')
async def update_channel(channel_id: int, body: ChannelIn, request: Request):
    db, security, *_ = ctx(request)
    row = db.one('SELECT * FROM channels WHERE id=?', (channel_id,))
    if not row:
        raise HTTPException(404, 'Channel not found')
    if row['kind'] != body.kind:
        raise HTTPException(422, 'Create a new channel to change its type')
    merged = {**decode(security.secret(f'channel:{channel_id}'), {}), **{k: v for k, v in body.config.items() if v != ''}}
    config = CHANNELS[body.kind].validate(merged)
    db.execute('UPDATE channels SET name=?, enabled=? WHERE id=?', (body.name, int(body.enabled), channel_id))
    security.save_secret(f'channel:{channel_id}', encode(config))
    return {'saved': True}


@router.delete('/channels/{channel_id}')
async def delete_channel(channel_id: int, request: Request):
    db, security, *_ = ctx(request)
    with db.connect() as c:
        c.execute('DELETE FROM deliveries WHERE channel_id=?', (channel_id,))
        c.execute('DELETE FROM channels WHERE id=?', (channel_id,))
    security.save_secret(f'channel:{channel_id}', None)
    return {'deleted': True}


@router.post('/channels/{channel_id}/test')
async def test_channel(channel_id: int, request: Request):
    _, _, engine, _ = ctx(request)
    try:
        await engine.notifications.send_test(channel_id)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except Exception as error:
        from ..notifications import _safe
        raise HTTPException(502, f'Test failed ({type(error).__name__}): {_safe(error)}') from None
    return {'sent': True}


# ---------------------------------------------------------- tokens & HA
@router.get('/tokens')
async def tokens(request: Request):
    db, *_ = ctx(request)
    return db.rows('SELECT id, name, scope, created_at, last_used FROM api_tokens ORDER BY id')


@router.post('/tokens')
async def create_token(body: TokenIn, request: Request):
    db, security, *_ = ctx(request)
    security.reauthenticate(me(request), body.password, body.otp)
    value = 'kw_' + pysecrets.token_urlsafe(40)
    token_id = db.execute('INSERT INTO api_tokens(name, hash, scope, created_at) VALUES(?,?,?,?)', (body.name, digest(value), body.scope, stamp()))
    db.audit('token_created', {'id': token_id, 'scope': body.scope}, actor(request))
    return {'id': token_id, 'token': value, 'note': 'Shown once. Store it in Home Assistant secrets.yaml or your device config.'}


@router.delete('/tokens/{token_id}')
async def revoke_token(token_id: int, request: Request):
    db, *_ = ctx(request)
    db.execute('DELETE FROM api_tokens WHERE id=?', (token_id,))
    db.audit('token_revoked', {'id': token_id}, actor(request))
    return {'revoked': True}


@router.get('/ha/state')
async def ha_state(request: Request):
    """Polled by a Home Assistant REST sensor (see homeassistant/ in the repo)."""
    db, _, engine, _ = ctx(request)
    settings = db.settings()
    now = utcnow()
    site = db.latest_site()
    fresh = bool(site and now - datetime.fromisoformat(site['ts']) < timedelta(minutes=15))
    decision = engine.latest_decision()
    info = engine.advice()
    rate = rate_at(settings.tariffs, now, settings.zone)
    return {
        'available': fresh, 'updated_at': stamp(now), 'mode': settings.mode,
        'battery_soc': site['soc'] if fresh else None, 'solar_kw': site['solar_kw'] if fresh else None,
        'load_kw': site['load_kw'] if fresh else None, 'grid_kw': (site['import_kw'] - site['export_kw']) if fresh else None,
        'price_cents': round(rate['buy'] * 100, 2), 'feed_in_cents': round(rate['sell'] * 100, 2), 'tariff_period': rate['label'],
        'action': decision['summary']['action'] if decision else None, 'reason': decision['summary']['reason'] if decision else None,
        'modelled_saving_24h': round(decision['summary']['modelled_saving_24h_aud'], 2) if decision else None,
        'climate': info.get('climate'), 'ev': info.get('vehicles'), 'clipping': info.get('clipping')}


@router.post('/ingest/system/{system_id}')
async def ingest_system(system_id: str, request: Request):
    db, *_ = ctx(request)
    settings = db.settings()
    spec = next((s for s in settings.systems if s.id == system_id and s.plugin == 'push'), None)
    if not spec:
        raise HTTPException(404, 'No system with this id uses the "push" plugin')
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(422, 'Expected a JSON object')
    system_reading(payload)  # validate now so the sender gets the error
    db.execute('INSERT OR REPLACE INTO pushed_readings VALUES(?,?,?)', (f'system:{system_id}', stamp(), encode(payload)))
    return {'accepted': True}


@router.post('/ingest/vehicle/{vehicle_id}')
async def ingest_vehicle(vehicle_id: str, request: Request):
    db, *_ = ctx(request)
    settings = db.settings()
    if not any(v.id == vehicle_id and v.plugin == 'push_vehicle' for v in settings.vehicles):
        raise HTTPException(404, 'No vehicle with this id uses the "push_vehicle" plugin')
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(422, 'Expected a JSON object')
    vehicle_reading(payload)
    db.execute('INSERT OR REPLACE INTO pushed_readings VALUES(?,?,?)', (f'vehicle:{vehicle_id}', stamp(), encode(payload)))
    return {'accepted': True}


# ------------------------------------------------------------ data/audit
@router.get('/export')
async def export(request: Request, days: int = 90):
    db, *_ = ctx(request)
    days = max(1, min(days, 3650))
    db.audit('data_exported', {'days': days}, actor(request))
    return StreamingResponse(exports.lines(db, days), media_type='application/x-ndjson',
                             headers={'Content-Disposition': f'attachment; filename="kwattson-{utcnow():%Y%m%d}.jsonl"'})


@router.get('/audit')
async def audit(request: Request, limit: int = 200):
    db, *_ = ctx(request)
    rows = db.rows('SELECT * FROM audit ORDER BY id DESC LIMIT ?', (max(1, min(limit, 1000)),))
    for r in rows:
        r['data'] = decode(r['data'])
    return rows


@router.post('/security/report')
async def security_report(request: Request):
    db, security, *_ = ctx(request)
    security.throttle('report:' + client_ip(request), 30)
    try:
        data = json.loads((await request.body())[:20000] or b'{}')
        reports = data if isinstance(data, list) else [data]
        for r in reports[:5]:
            body = r.get('body') or r.get('csp-report') or {}
            directive = str(body.get('effectiveDirective') or body.get('violated-directive') or 'unknown')[:80]
            db.audit('browser_policy_violation', {'directive': directive}, 'browser')
    except Exception:
        pass
    from fastapi.responses import Response
    return Response(status_code=204)
