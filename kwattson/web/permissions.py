"""Who may call what. One table, checked by the middleware for every session request.

Roles
- admin:  everything.
- member: everything a viewer can, plus day-to-day actions (quick actions/overrides, holidays,
          notes, re-plan, mark alerts read, stop automatic control).
- viewer: read-only dashboards (overview, plan, history, alerts feed).
Everyone can manage their own account (password, two-step sign-in, sign out).
"""
from __future__ import annotations

RANK = {'viewer': 0, 'member': 1, 'admin': 2}

# (method or '*', path prefix) → minimum role. First match wins; default: admin.
RULES: list[tuple[str, str, str]] = [
    # own account
    ('GET', '/api/auth/session', 'viewer'),
    ('POST', '/api/auth/logout', 'viewer'),
    ('POST', '/api/auth/password', 'viewer'),
    ('POST', '/api/auth/mfa/', 'viewer'),
    # what's scheduled is visible to everyone; changing it is a day-to-day action
    ('GET', '/api/overrides', 'viewer'),
    ('GET', '/api/holidays', 'viewer'),
    ('GET', '/api/annotations', 'viewer'),
    # day-to-day actions
    ('*', '/api/overrides', 'member'),
    ('*', '/api/holidays', 'member'),
    ('*', '/api/annotations', 'member'),
    ('POST', '/api/notifications/seen', 'member'),
    ('POST', '/api/plan', 'member'),
    ('POST', '/api/control/stop', 'member'),   # stopping is always safe
    # admin-only reads (credentials-adjacent or configuration detail)
    ('GET', '/api/settings', 'admin'),
    ('GET', '/api/users', 'admin'),
    ('GET', '/api/tokens', 'admin'),
    ('GET', '/api/channels', 'admin'),
    ('GET', '/api/notification-', 'admin'),
    ('GET', '/api/audit', 'admin'),
    ('GET', '/api/export', 'admin'),
    # every other read is fine for viewers
    ('GET', '/api/', 'viewer'),
]


def required_role(method: str, path: str) -> str:
    for rule_method, prefix, role in RULES:
        if rule_method in ('*', method) and path.startswith(prefix):
            return role
    return 'admin'


def allowed(role: str, method: str, path: str) -> bool:
    return RANK.get(role, -1) >= RANK[required_role(method, path)]
