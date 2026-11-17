"""Strict coercion helpers for values arriving from devices and HTTP clients."""
from __future__ import annotations

import math

TRUE = {'true', 'on', 'yes', '1', 'home', 'charging'}
FALSE = {'false', 'off', 'no', '0', 'not_home', 'away', 'idle'}


def as_bool(value, name='value') -> bool:
    """Accept real booleans, 0/1 and common Home Assistant state words. Never bool('false')."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in TRUE | FALSE:
        return value.strip().lower() in TRUE
    raise ValueError(f'{name} must be true or false')


def as_number(value, name='value', low=-math.inf, high=math.inf) -> float:
    if isinstance(value, bool):
        raise ValueError(f'{name} must be a number')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be a number') from None
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f'{name} must be between {low:g} and {high:g}')
    return number
