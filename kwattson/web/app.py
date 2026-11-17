"""FastAPI application factory, security middleware and static dashboard.

Run with ONE worker process (the background engine holds a file lock):
    uvicorn kwattson.web.app:create_app --factory --workers 1 --proxy-headers
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import ValidationError

from .. import __version__
from ..config import Runtime, stamp
from ..db import Database
from ..engine import Engine
from ..security import Security, digest
from .api import router
from .permissions import allowed

STATIC = Path(__file__).parent / 'static'
PUBLIC = {'/api/auth/status', '/api/auth/setup', '/api/auth/login', '/api/security/report'}
BEARER = {'/api/ha/state': 'read', '/api/ingest/': 'ingest'}
UNSAFE = {'POST', 'PUT', 'PATCH', 'DELETE'}
MAX_BODY = 2_000_000
log = logging.getLogger('kwattson')


# The dashboard build may declare CSP needs in static/csp.json, e.g. {"style_nonce": true} for
# MUI/Emotion runtime <style> tags. Only these Trusted Types policy names could ever be allowed.
ALLOWED_TT_POLICIES = {'svelte-trusted-html', 'lit-html'}


def csp(nonce: str, style_nonce: bool = False, trusted_types: tuple[str, ...] = ()) -> str:
    styles = f"'self' 'nonce-{nonce}'" if style_nonce else "'self'"
    policies = ' '.join(trusted_types) if trusted_types else "'none'"
    return ("default-src 'none'; "
            f"script-src 'nonce-{nonce}' 'strict-dynamic'; style-src {styles}; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none'; "
            f"frame-ancestors 'none'; form-action 'self'; require-trusted-types-for 'script'; trusted-types {policies}; "
            "report-uri /api/security/report")


def build_policy() -> tuple[bool, tuple[str, ...]]:
    """CSP adjustments declared by the dashboard build (static/csp.json), allow-listed."""
    import json
    path = STATIC / 'csp.json'
    if not path.is_file():
        return False, ()
    data = json.loads(path.read_text())
    names = tuple(n for n in data.get('trusted_types', []) if n in ALLOWED_TT_POLICIES)
    return bool(data.get('style_nonce')), names


def create_app(runtime: Runtime | None = None) -> FastAPI:
    runtime = runtime or Runtime()
    db = Database(runtime.data_dir / 'kwattson.sqlite3', demo=runtime.demo)
    security = Security(db, runtime)
    engine = Engine(db, security, runtime)
    if runtime.demo:
        from ..demo import seed
        seed(db, security)

    @asynccontextmanager
    async def lifespan(app):
        lock = task = None
        if runtime.worker:
            lock = open(runtime.data_dir / 'worker.lock', 'a')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError('Another worker owns this data directory. Run uvicorn with --workers 1.') from None
            task = asyncio.create_task(engine.run())
        yield
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await engine.close()
        if lock:
            lock.close()

    app = FastAPI(title='kWattson', version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.security, app.state.engine, app.state.runtime = db, security, engine, runtime

    @app.middleware('http')
    async def guard(request: Request, call_next):
        nonce = secrets.token_urlsafe(18)
        request.state.nonce = nonce
        request.state.user = None
        path = request.url.path
        try:
            if path != '/healthz' and request.headers.get('host') != runtime.host:
                raise HTTPException(400, 'Unexpected host; set APP_URL to the address you use')
            if path.startswith('/api/') and request.headers.get('sec-fetch-site') == 'cross-site':
                raise HTTPException(403, 'Cross-site request refused')
            if request.method in UNSAFE:
                length = request.headers.get('content-length')
                if length is None and request.headers.get('transfer-encoding'):
                    raise HTTPException(411, 'Content-Length required')
                if length and (not length.isdigit() or int(length) > MAX_BODY):
                    raise HTTPException(413, 'Request too large (2 MB limit); use the CLI for bulk imports')
            if path.startswith('/api/') and path not in PUBLIC:
                authorize(request, path)
            response = await call_next(request)
        except HTTPException as error:
            response = JSONResponse({'detail': error.detail}, status_code=error.status_code)
        style_nonce, policies = getattr(request.state, 'csp', (False, ()))
        headers = {
            'Content-Security-Policy': csp(nonce, style_nonce, policies), 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()',
            'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Resource-Policy': 'same-origin',
            'X-Frame-Options': 'DENY'}
        if not path.startswith('/static/'):
            headers['Cache-Control'] = 'no-store'
        if not runtime.dev_http:
            headers['Strict-Transport-Security'] = f'max-age={runtime.hsts_seconds}'
        response.headers.update(headers)
        return response

    def authorize(request: Request, path: str):
        auth = request.headers.get('authorization', '')
        scope = next((s for prefix, s in BEARER.items() if path.startswith(prefix)), None)
        if scope and auth.startswith('Bearer '):
            token = db.one('SELECT * FROM api_tokens WHERE hash=?', (digest(auth[7:].strip()),))
            if not token or (token['scope'] != scope and not (scope == 'read' and token['scope'] == 'ingest')):
                raise HTTPException(401, 'Invalid or insufficient API token')
            db.execute('UPDATE api_tokens SET last_used=? WHERE id=?', (stamp(), token['id']))
            request.state.user = {'username': 'token:' + token['name'], 'token': True}
            return
        session = security.authenticate(request.cookies.get(runtime.cookie, ''))
        if not allowed(session['role'], request.method, path):
            raise HTTPException(403, 'Your account role does not allow this; ask an admin')
        if request.method in UNSAFE:
            if request.headers.get('origin') != runtime.app_url:
                raise HTTPException(403, 'Request origin does not match APP_URL')
            security.check_csrf(session, request.headers.get('x-csrf-token', ''))
        request.state.user = session

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error: RequestValidationError):
        first = error.errors()[0] if error.errors() else {}
        where = '.'.join(str(p) for p in first.get('loc', [])[1:])
        return JSONResponse({'detail': f"{where + ': ' if where else ''}{first.get('msg', 'Invalid request')}"[:300]}, status_code=422)

    @app.exception_handler(ValidationError)
    async def invalid_model(request, error: ValidationError):
        first = error.errors()[0] if error.errors() else {}
        where = '.'.join(str(p) for p in first.get('loc', []))
        return JSONResponse({'detail': f"{where + ': ' if where else ''}{first.get('msg', 'Invalid value')}"[:300]}, status_code=422)

    @app.exception_handler(ValueError)
    async def invalid_value(request, error: ValueError):
        return JSONResponse({'detail': str(error)[:300]}, status_code=422)

    @app.get('/healthz')
    async def health():
        return {'status': 'ok', 'version': __version__}

    index_template = (STATIC / 'index.html').read_text() if (STATIC / 'index.html').exists() else '<p>Dashboard not built</p>'
    policy = build_policy()

    @app.get('/', response_class=HTMLResponse)
    async def index(request: Request):
        request.state.csp = policy
        return HTMLResponse(index_template.replace('__CSP_NONCE__', request.state.nonce))

    @app.get('/static/{name:path}')
    async def static(name: str):
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            raise HTTPException(404, 'Not found')
        response = FileResponse(target)
        response.headers['Cache-Control'] = 'no-cache'
        return response

    app.include_router(router)
    return app
