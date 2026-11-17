"""Time-of-use tariffs described as data, plus daily allowance accounting.

A tariff is a list of periods (local wall-clock time windows with import and
export rates) and optional daily allowances, e.g. "first 50 kWh/day imported in
the free window cost $0, after that 26.91c" or "first 30 kWh/day exported in the
evening earns 15c, after that 5c". All rates are AUD per kWh; usage and supply
rates include GST; feed-in rates are as published.

Plans are selected by time: the newest *active* plan whose ``active_from`` is at
or before the instant. A plan with status ``pending`` (signed but not confirmed
by the retailer) is never used for billing or control, only for previews.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


def _minutes(text: str) -> int:
    hour, minute = text.split(':')
    value = int(hour) * 60 + int(minute)
    if not 0 <= value <= 1440 or int(minute) % 5:
        raise ValueError('Times must be HH:MM on a 5-minute boundary between 00:00 and 24:00')
    return value


class Period(_Strict):
    name: str = Field(min_length=1, max_length=40, pattern=r'^[a-z0-9_]+$')
    label: str = Field('', max_length=60)
    start: str = Field(pattern=r'^\d\d:\d\d$')
    end: str = Field(pattern=r'^\d\d:\d\d$')
    days: list[int] = Field(default_factory=lambda: list(range(7)))
    import_rate: float = Field(ge=-2, le=5)
    export_rate: float = Field(ge=-2, le=5)

    @field_validator('start', 'end')
    @classmethod
    def time(cls, value):
        _minutes(value)
        return value

    @field_validator('days')
    @classmethod
    def weekdays(cls, value):
        if not value or any(d not in range(7) for d in value):
            raise ValueError('Period days must be 0 (Monday) to 6 (Sunday)')
        return sorted(set(value))

    def contains(self, local: datetime) -> bool:
        if local.weekday() not in self.days:
            return False
        minute = local.hour * 60 + local.minute
        start, end = _minutes(self.start), _minutes(self.end)
        return start <= minute < end if start < end else (minute >= start or minute < end)


class Allowance(_Strict):
    """A daily kWh allowance at the period's rate; beyond it ``rate_after`` applies."""
    kind: str = Field(pattern='^(import|export)$')
    periods: list[str] = Field(min_length=1)
    kwh_per_day: float = Field(ge=0, le=1000)
    rate_after: float = Field(ge=-2, le=5)


class TariffPlan(_Strict):
    id: str = Field(min_length=1, max_length=40, pattern=r'^[a-z0-9_-]+$')
    name: str = Field(min_length=1, max_length=80)
    retailer: str = Field('', max_length=80)
    plan_code: str = Field('', max_length=40)
    status: str = Field('active', pattern='^(active|pending|retired)$')
    active_from: str | None = Field(None, description='UTC ISO timestamp the retailer confirmed; None = base plan')
    supply_daily: float = Field(ge=0, le=20)
    periods: list[Period] = Field(min_length=1, max_length=48)
    allowances: list[Allowance] = Field(default_factory=list, max_length=10)
    notes: str = Field('', max_length=1000)

    @field_validator('active_from')
    @classmethod
    def offset(cls, value):
        if value is not None:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('Tariff start needs a timezone offset')
        return value

    @model_validator(mode='after')
    def coverage(self):
        names = {p.name for p in self.periods}
        if len(names) != len(self.periods):
            raise ValueError(f'{self.name}: period names must be unique')
        # Every 5 minutes of every weekday must be covered by at least one period.
        base = datetime(2024, 1, 1)  # a Monday
        for day in range(7):
            for minute in range(0, 1440, 5):
                moment = base + timedelta(days=day, minutes=minute)
                if not any(p.contains(moment) for p in self.periods):
                    raise ValueError(f'{self.name}: no rate covers {moment:%a %H:%M}')
        for allowance in self.allowances:
            missing = set(allowance.periods) - names
            if missing:
                raise ValueError(f'{self.name}: allowance refers to unknown period(s) {sorted(missing)}')
            for period in self.periods:
                if period.name not in allowance.periods:
                    continue
                rate = period.import_rate if allowance.kind == 'import' else period.export_rate
                # Tier 1 must be the better tier, otherwise the optimiser would
                # prefer the "after" rate and the model would be wrong.
                if allowance.kind == 'import' and rate > allowance.rate_after:
                    raise ValueError(f'{self.name}: import allowance rate must not exceed rate_after')
                if allowance.kind == 'export' and rate < allowance.rate_after:
                    raise ValueError(f'{self.name}: export allowance rate must not be below rate_after')
        return self

    def period_at(self, local: datetime) -> Period:
        for period in self.periods:
            if period.contains(local):
                return period
        raise ValueError('Tariff does not cover this time')  # unreachable after validation

    def start(self) -> datetime | None:
        return datetime.fromisoformat(self.active_from.replace('Z', '+00:00')) if self.active_from else None


def default_tariffs() -> list[TariffPlan]:
    """A placeholder flat-rate plan so a new install works; replace it with your own plan."""
    return [TariffPlan(
        id='flat', name='Flat rate (edit me)', status='active', supply_daily=1.00,
        periods=[Period(name='anytime', label='Any time', start='00:00', end='24:00', import_rate=0.30, export_rate=0.05)],
        notes='Placeholder. Enter your own plan, import one, or (Australia) import from Energy Made Easy.')]


def plan_at(plans: list[TariffPlan], at: datetime) -> TariffPlan:
    candidates = [p for p in plans if p.status == 'active' and (p.start() is None or p.start() <= at)]
    if not candidates:
        raise ValueError('No active tariff covers this time')
    return max(candidates, key=lambda p: p.start() or datetime.min.replace(tzinfo=at.tzinfo))


def rate_at(plans: list[TariffPlan], at: datetime, zone: ZoneInfo) -> dict:
    """Instantaneous tariff summary used by the dashboard and alerts."""
    plan = plan_at(plans, at)
    local = at.astimezone(zone)
    period = plan.period_at(local)
    allowances = [a for a in plan.allowances if period.name in a.periods]
    return dict(plan_id=plan.id, plan=plan.name, period=period.name, label=period.label or period.name,
                buy=period.import_rate, sell=period.export_rate, supply=plan.supply_daily,
                allowances=[a.model_dump() for a in allowances])


def pending_plans(plans: list[TariffPlan]) -> list[TariffPlan]:
    return [p for p in plans if p.status == 'pending']


def bill(plans: list[TariffPlan], intervals: list[dict], zone: ZoneInfo) -> dict[str, dict]:
    """Daily cost of observed intervals, applying allowances in chronological order.

    ``intervals`` need ts, import_kwh, export_kwh. Returns {local_date: totals}.
    Supply is charged once per local day that has any interval.
    """
    days: dict[str, dict] = {}
    used: dict[tuple, float] = defaultdict(float)
    for row in sorted(intervals, key=lambda r: r['ts']):
        at = datetime.fromisoformat(row['ts'])
        local = at.astimezone(zone)
        day = local.date().isoformat()
        plan = plan_at(plans, at)
        period = plan.period_at(local)
        totals = days.setdefault(day, dict(plan=plan.name, supply=plan.supply_daily, import_kwh=0.0, export_kwh=0.0,
                                           import_cost=0.0, export_credit=0.0))
        imported, exported = row['import_kwh'], row['export_kwh']
        import_cost = imported * period.import_rate
        export_credit = exported * period.export_rate
        for index, allowance in enumerate(plan.allowances):
            if period.name not in allowance.periods:
                continue
            key = (day, plan.id, index)
            amount = imported if allowance.kind == 'import' else exported
            eligible = min(amount, max(0.0, allowance.kwh_per_day - used[key]))
            used[key] += eligible
            if allowance.kind == 'import':
                import_cost = eligible * period.import_rate + (amount - eligible) * allowance.rate_after
            else:
                export_credit = eligible * period.export_rate + (amount - eligible) * allowance.rate_after
        totals['import_kwh'] += imported
        totals['export_kwh'] += exported
        totals['import_cost'] += import_cost
        totals['export_credit'] += export_credit
    for totals in days.values():
        totals['net'] = totals['supply'] + totals['import_cost'] - totals['export_credit']
    return days
