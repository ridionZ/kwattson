# Security model

The dashboard is meant to be reachable from the internet (behind your HTTPS reverse proxy) and can
control a large battery, so it is built defensively. Measures follow Chrome's
[Modern Web Guidance](https://developer.chrome.com/docs/modern-web-guidance/get-started) for security,
privacy and forms, plus OWASP basics.

## Authentication
- Argon2id password hashes; 14–256 character passphrases.
- Optional TOTP two-step sign-in (strongly recommended); each code is accepted once.
- First boot can create `admin` from `INITIAL_ADMIN_PASSWORD_FILE` or `INITIAL_ADMIN_PASSWORD`. The password is used only when there are no users. Docker administrators can read container variables, so clear the installer variable after sign-in. Without either setting, the generated console token proves ownership before the first account can be created.
- Sessions: random 384-bit token in a `__Host-` cookie (`Secure`, `HttpOnly`, `SameSite=Strict`,
  `Path=/`), stored hashed; 12 h absolute / 60 min idle lifetime.
- Login: 5 failures per client IP in 15 min locks that IP for 15 min, plus a global brake.
  Configure `FORWARDED_ALLOW_IPS` so the real client IP is seen behind nginx.
- Sensitive actions (enable control, activate tariff, API tokens, MFA, password) re-check the password
  (and code).
- Recovery is console-only: `kwattson reset-user <username> [--disable-mfa] [--make-admin]`.
- Multiple users with roles: **admin** (everything), **member** (day-to-day actions), **viewer** (read-only). Role rules live in `kwattson/web/permissions.py`.

## Request protection
- Exact `Host` check against `APP_URL`; unsafe requests need a matching `Origin`, a CSRF header bound to
  the session, and are refused if `Sec-Fetch-Site: cross-site`.
- Bodies over 2 MB refused. All inputs validated with strict Pydantic models (unknown fields rejected,
  NaN/Infinity rejected, ranges checked); error messages never echo secrets.
- API bearer tokens are scoped (`read` for Home Assistant state, `ingest` for pushed readings) and only
  work on those endpoints.

## Browser hardening
- CSP: nonce-based `script-src` with `strict-dynamic`, `require-trusted-types-for 'script'` with no
  policies allowed (`trusted-types 'none'`), `default-src 'none'`, no inline styles, `frame-ancestors 'none'`,
  `object-src 'none'`, `base-uri 'none'`. The dashboard builds DOM with `textContent`/`createElement` only.
- No third-party scripts, fonts, analytics or CDNs. `Referrer-Policy: no-referrer`, COOP/CORP same-origin,
  restrictive `Permissions-Policy`, `nosniff`, `Cache-Control: no-store` for pages and API.
- HSTS starts at 300 s (`HSTS_SECONDS`) so a misconfigured proxy is recoverable; raise it once stable.
- The CSRF token lives in memory only; nothing sensitive in `localStorage`.

## Secrets and privacy

Address search is optional. Submitting a query sends it to Photon. Selecting a result sends its approximate coordinates to Open-Meteo to look up the time zone. kWattson stores the coordinates and time zone when you save. You can also enter them manually.

- Device and notification credentials are encrypted with Fernet using `DATA_DIR/master.key`
  (created 0600). Back it up with the database; don't commit it.
- Credentials are write-only in the UI/API; responses show only whether a field is set.
- Errors from integrations are reduced to short, credential-free messages.
- Exports exclude credentials, hashes, channel destinations, plugin connection settings and raw vendor
  payloads; location is rounded to 0.1°. Vehicle plugins store no GPS, trips or VIN.
- Outbound requests: HTTPS only, no redirects, no proxy env, and private/LAN addresses blocked unless
  listed in `OUTBOUND_ALLOW_HOSTS` (SSRF protection). DNS is resolved at check time; a hostile DNS server
  could still race this (acceptable for a single-owner home app).

## Container
Non-root user, read-only root filesystem, `/tmp` tmpfs, all capabilities dropped, `no-new-privileges`,
memory/pid limits, pinned dependency versions (`deploy/requirements.lock`). Run a single worker.

## Reporting
Browser CSP violations are recorded in the audit log (`browser_policy_violation`). Please report
security issues privately to the repository owner rather than in public issues.
