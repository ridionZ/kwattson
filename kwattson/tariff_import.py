"""Import retail electricity plans from the AER Consumer Data Right (CDR) API.

Energy Made Easy publishes every residential offer as "Energy Product Reference
Data" (``GET https://cdr.energymadeeasy.gov.au/<brand>/cds-au/v1/energy/plans/<planId>``).
This module turns one such plan-detail response into a :class:`TariffPlan`.

Interpretation rules (see ``tariffs/README.md`` for the user-facing version):

* **GST.** CDR prices are published *exclusive* of GST. Usage rates and the
  daily supply charge are multiplied by 1.1 so they match the bill. Solar
  feed-in rates are used as published (feed-in credits are normally GST-free
  for households).
* **Times.** CDR end times are *inclusive* (``15:00``-``20:59`` means 3pm to
  9pm), so an end minute just before a 5-minute boundary (``:59``, ``:29``...)
  is rounded up. ``00:00`` and ``23:59`` as an end mean midnight (``24:00``).
  A window whose end is before its start wraps past midnight and is split.
* **Tiers.** A usage block whose first rate has a ``volume`` and is cheaper than
  the next rate becomes an import :class:`Allowance` when the volume is daily
  (``period`` ``P1D``, or the text says "per day"). Otherwise the first rate is
  used and the omission is recorded in ``notes``. Feed-in tiers are usually
  published as separate ``solarFeedInTariff`` entries over the same time window
  (one with a ``volume``, one without); they are combined into an export
  allowance under the same daily rule.
* **Gaps.** Times not covered by any usage block are filled with the plan's
  single OFF_PEAK (or else single SHOULDER) rate; otherwise ``ValueError``.
  Times without a retailer feed-in rate earn 0.
* Demand charges, controlled-load circuits, government/premium feed-in schemes,
  discounts and fees are not modelled; they are mentioned in ``notes``.

Imported plans always have ``status='pending'``: the owner activates one by
setting ``status='active'`` and the retailer-confirmed ``active_from``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .tariffs import Allowance, Period, TariffPlan

GST = 1.1
CDR_BASE_URL = 'https://cdr.energymadeeasy.gov.au/{brand}/cds-au/v1/energy/plans/{plan_id}'
PLAN_ID_PATTERN = re.compile(r'^[A-Za-z0-9@._-]{1,80}$')
BRAND_PATTERN = re.compile(r'^[a-z0-9-]{1,60}$')

SLOT = 5  # minutes; the TariffPlan grid resolution
SLOTS_PER_DAY = 1440 // SLOT
MAX_PERIODS = 48  # TariffPlan.periods max_length
NOTES_LIMIT = 1000  # TariffPlan.notes max_length

_DAY_CODES = {'MON': 0, 'TUE': 1, 'WED': 2, 'THU': 3, 'FRI': 4, 'SAT': 5, 'SUN': 6}
_DAY_GROUPS = {'BUSINESS_DAYS': [0, 1, 2, 3, 4], 'WEEKDAYS': [0, 1, 2, 3, 4], 'WEEKENDS': [5, 6], 'ALL': list(range(7))}
_CAP_TEXT = re.compile(r'first\s+\d+(\.\d+)?\s*kwh|limit|\bcap(ped)?\b', re.IGNORECASE)
_DAILY_TEXT = re.compile(r'\b(per|a|each)\s+day\b|/\s*day\b|\bdaily\b', re.IGNORECASE)


# --------------------------------------------------------------------------- helpers


def _slug(text: str, fallback: str) -> str:
    """Lower-case identifier usable as a Period name (``^[a-z0-9_]+$``)."""
    slug = re.sub(r'[^a-z0-9]+', '_', (text or '').lower()).strip('_')
    return (slug or fallback)[:30]


def _price(value, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{what}: unit price {value!r} is not a number') from None


def _clock(text: str, *, is_end: bool) -> int:
    """Parse ``HH:MM`` / ``HHMM`` (optionally with seconds) to minutes after midnight.

    End times are inclusive in CDR data, so ``20:59`` becomes 21:00 and
    ``23:59`` / ``00:00`` become 24:00. Start times must sit on a 5-minute grid.
    """
    raw = str(text).strip()
    match = re.fullmatch(r'(\d{1,2}):?(\d{2})(?::?\d{2})?', raw)
    if not match:
        raise ValueError(f'Unrecognised time {text!r}')
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 24 or minute > 59 or (hour == 24 and minute):
        raise ValueError(f'Unrecognised time {text!r}')
    value = hour * 60 + minute
    if is_end:
        if value % SLOT == SLOT - 1:  # inclusive end such as 20:59 or 21:29
            value += 1
        if value == 0:
            value = 1440
    if value % SLOT:
        raise ValueError(f'Time {text!r} is not on a {SLOT}-minute boundary')
    return value


def _days(codes) -> list[int]:
    """Translate CDR day codes to 0=Monday..6=Sunday; public holidays are ignored."""
    if not codes:
        return list(range(7))
    days: set[int] = set()
    for code in codes:
        code = str(code).upper()
        if code in _DAY_CODES:
            days.add(_DAY_CODES[code])
        elif code in _DAY_GROUPS:
            days.update(_DAY_GROUPS[code])
        elif code != 'PUBLIC_HOLIDAYS':
            raise ValueError(f'Unrecognised day code {code!r}')
    return sorted(days)


def _slots(windows: list[dict] | None) -> list[tuple[int, int]]:
    """Expand CDR time windows into (weekday, 5-minute slot) pairs.

    ``None`` (no windows) means all day, every day. A window ending before it
    starts wraps past midnight onto the following day's early hours, which for
    a weekly pattern simply means the early hours of the same listed days.
    """
    if windows is None:
        return [(day, slot) for day in range(7) for slot in range(SLOTS_PER_DAY)]
    result: list[tuple[int, int]] = []
    for window in windows:
        start = _clock(window.get('startTime', '00:00'), is_end=False)
        end = _clock(window.get('endTime', '00:00'), is_end=True)
        days = _days(window.get('days'))
        if start == 1440:
            start = 0
        ranges = [(start, end)] if start < end else [(start, 1440), (0, end)]
        for day in days:
            for lo, hi in ranges:
                result.extend((day, minute // SLOT) for minute in range(lo, hi, SLOT))
    return result


def _is_daily(period: str | None, *texts: str | None) -> bool:
    """True when a tier volume is a daily allowance (period P1D or text says per day)."""
    return period == 'P1D' or any(text and _DAILY_TEXT.search(text) for text in texts)


def _fmt(rate: float) -> str:
    return f'{rate * 100:.2f}c'


# --------------------------------------------------------------------------- import side


@dataclass
class _ImportBlock:
    """One usage-rate block (a TOU band or the single rate), GST-inclusive."""
    name: str
    label: str
    type: str
    rate: float
    allowance_kwh: float | None = None  # daily tier-1 volume at ``rate``
    rate_after: float | None = None


def _import_block(block: dict, index: int, notes: list[str]) -> _ImportBlock:
    label = block.get('displayName') or block.get('type') or f'Rate {index + 1}'
    kind = (block.get('type') or '').upper()
    name = _slug(block.get('displayName') or kind, f'rate_{index + 1}')
    rates = block.get('rates') or []
    if not rates:
        raise ValueError(f'Usage block {label!r} has no rates')
    prices = [_price(r.get('unitPrice'), label) * GST for r in rates]
    result = _ImportBlock(name=name, label=label[:60], type=kind, rate=round(prices[0], 6))
    if len(rates) > 1 and len(set(prices)) > 1:
        volume = rates[0].get('volume')
        daily = _is_daily(block.get('period'), block.get('description'), block.get('displayName'))
        if volume and prices[0] < prices[1] and daily:
            result.allowance_kwh = float(volume)
            result.rate_after = round(prices[1], 6)
            if len(rates) > 2:
                notes.append(f'{label}: only the first two of {len(rates)} usage tiers are modelled.')
        else:
            tiers = ', '.join(f'{_fmt(p)}' + (f' for {r.get("volume")} kWh' if r.get('volume') else '')
                              for p, r in zip(prices, rates))
            notes.append(f'{label}: tiered usage ({tiers} per {block.get("period") or "period"}) '
                         f'not modelled; first rate used.')
    return result


def _fill_block(blocks: list[_ImportBlock]) -> _ImportBlock | None:
    """The block to use for times no window covers, if the choice is obvious."""
    for kind in ('OFF_PEAK', 'SHOULDER'):
        matches = [b for b in blocks if b.type == kind]
        if len(matches) == 1:
            return matches[0]
    return None


def _import_grid(tariff: dict, notes: list[str]) -> tuple[list[list[int]], list[_ImportBlock]]:
    """Map every (weekday, slot) to an index into the returned import blocks."""
    kind = tariff.get('rateBlockUType')
    if kind == 'singleRate' or (kind is None and 'singleRate' in tariff):
        raw = [tariff['singleRate']]
        windows = [None]
    elif kind == 'timeOfUseRates' or (kind is None and 'timeOfUseRates' in tariff):
        raw = tariff.get('timeOfUseRates') or []
        windows = [b.get('timeOfUse') or None for b in raw]
    elif kind == 'demandCharges':
        raise ValueError('Tariff period has only demand charges and no usage rates')
    else:
        raise ValueError(f'Unsupported rateBlockUType {kind!r}')
    if not raw:
        raise ValueError('Tariff period has no usage rates')
    blocks = [_import_block(b, i, notes) for i, b in enumerate(raw)]

    grid: list[list[int]] = [[-1] * SLOTS_PER_DAY for _ in range(7)]
    clashes = 0
    for index, slots in enumerate(_slots(w) for w in windows):
        for day, slot in slots:
            current = grid[day][slot]
            if current == -1:
                grid[day][slot] = index
            elif blocks[current].rate != blocks[index].rate:
                clashes += 1
    if clashes:
        raise ValueError(f'Usage windows overlap with different rates ({clashes * SLOT} minutes per week)')

    gaps = sum(row.count(-1) for row in grid)
    if gaps:
        fill = _fill_block(blocks)
        if fill is None:
            raise ValueError(f'Usage windows leave {gaps * SLOT} minutes per week uncovered '
                             'and there is no single off-peak/shoulder rate to fill them')
        position = blocks.index(fill)
        for row in grid:
            for slot, value in enumerate(row):
                if value == -1:
                    row[slot] = position
        notes.append(f'{gaps * SLOT // 60} h/week not covered by any usage window charged at {fill.label}.')
    return grid, blocks


# --------------------------------------------------------------------------- export side


@dataclass
class _ExportBand:
    """Feed-in rates for one time window, possibly with a daily tier."""
    name: str
    label: str
    rate: float
    allowance_kwh: float | None = None
    rate_after: float | None = None


def _feed_in_components(contract: dict, on_date: date, notes: list[str]) -> list[dict]:
    """Flatten retailer feed-in tariffs valid on ``on_date`` into rate components."""
    components = []
    skipped_government, skipped_dated = 0, 0
    for tariff in contract.get('solarFeedInTariff') or []:
        if tariff.get('payerType') == 'GOVERNMENT' or tariff.get('scheme') == 'PREMIUM':
            skipped_government += 1
            continue
        start, end = tariff.get('startDate'), tariff.get('endDate')
        try:
            if (start and date.fromisoformat(start) > on_date) or (end and date.fromisoformat(end) < on_date):
                skipped_dated += 1
                continue
        except ValueError:
            pass  # unparseable dates: keep the tariff rather than silently drop it
        if 'timeVaryingTariffs' in tariff:
            bands = tariff['timeVaryingTariffs'] or []
        elif 'singleTariff' in tariff:
            bands = [tariff['singleTariff']]
        else:
            raise ValueError(f'Unsupported feed-in tariff type {tariff.get("tariffUType")!r}')
        for band in bands:
            windows = band.get('timeVariations') or None
            label = band.get('type') or tariff.get('displayName') or 'feed-in'
            text = (tariff.get('description'), tariff.get('displayName'), band.get('displayName'))
            for rate in band.get('rates') or []:
                components.append(dict(
                    windows=windows, label=label, type=(band.get('type') or '').upper(), texts=text,
                    period=band.get('period'), rate=_price(rate.get('unitPrice'), 'feed-in'),
                    volume=float(rate['volume']) if rate.get('volume') else None))
    if skipped_government:
        notes.append(f'{skipped_government} government/premium feed-in scheme(s) ignored.')
    if skipped_dated:
        notes.append(f'{skipped_dated} feed-in tariff(s) not valid on {on_date} ignored.')
    return components


def _window_key(windows: list[dict] | None) -> tuple:
    """Canonical key so tiers over the same time window can be grouped."""
    if windows is None:
        return ('all',)
    return tuple(sorted((tuple(_days(w.get('days'))),
                         _clock(w.get('startTime', '00:00'), is_end=False),
                         _clock(w.get('endTime', '00:00'), is_end=True)) for w in windows))


def _export_grid(contract: dict, on_date: date, notes: list[str]) -> tuple[list[list[int]], list[_ExportBand]]:
    """Map every (weekday, slot) to an index into the returned export bands (-1 = no feed-in)."""
    groups: dict[tuple, list[dict]] = {}
    for component in _feed_in_components(contract, on_date, notes):
        groups.setdefault(_window_key(component['windows']), []).append(component)

    bands: list[_ExportBand] = []
    cap_texts: set[str] = set()
    grid: list[list[int]] = [[-1] * SLOTS_PER_DAY for _ in range(7)]
    # Windowed bands first so an all-day tariff only fills what they leave.
    for key in sorted(groups, key=lambda k: k == ('all',)):
        parts = groups[key]
        tiered = [p for p in parts if p['volume']]
        plain = [p for p in parts if not p['volume']]
        first = parts[0]
        name = _slug(first['type'] or 'feed_in', 'feed_in')
        band = _ExportBand(name=name, label=str(first['label']).replace('_', ' ').title()[:30], rate=first['rate'])
        if tiered and plain:
            tier, after = tiered[0], plain[0]
            if not _is_daily(tier['period'], *tier['texts']):
                notes.append(f'Feed-in tier of {tier["volume"]:g} kWh per {tier["period"]} not modelled; '
                             f'{_fmt(after["rate"])} used.')
                band.rate = after['rate']
            elif tier['rate'] >= after['rate']:
                band.rate, band.allowance_kwh, band.rate_after = tier['rate'], tier['volume'], after['rate']
            else:
                notes.append('Feed-in tier cheaper than the rate after it; tier ignored.')
                band.rate = after['rate']
        elif tiered:
            tier = tiered[0]
            notes.append(f'Feed-in {_fmt(tier["rate"])} for the first {tier["volume"]:g} kWh '
                         f'has no published rate after it; assumed 0c beyond the limit.')
            if _is_daily(tier['period'], *tier['texts']) and tier['rate'] > 0:
                band.allowance_kwh, band.rate_after = tier['volume'], 0.0
        elif len({p['rate'] for p in plain}) > 1:
            # Usually an unlabelled tier ("first 15 kWh at 35c, then 10c") published
            # without a volume; the cap is unknown, so be conservative.
            band.rate = min(p['rate'] for p in plain)
            notes.append(f'Feed-in window {name} lists several rates without a volume '
                         f'({"/".join(_fmt(p["rate"]) for p in plain)}); lowest used.')
        if len(tiered) > 1:
            notes.append(f'Several feed-in tiers share one window ({name}); only the first is modelled.')
        cap_texts.update(t for p in parts for t in p['texts'] if t and _CAP_TEXT.search(t))
        index = len(bands)
        bands.append(band)
        for day, slot in _slots(first['windows']):
            if grid[day][slot] == -1:
                grid[day][slot] = index
    if not any(b.allowance_kwh for b in bands):
        for text in sorted(cap_texts):
            notes.append(f'Feed-in text mentions a cap not in the structured data, check it: "{text[:140]}".')
    uncovered = sum(row.count(-1) for row in grid)
    if bands and uncovered:
        notes.append(f'{uncovered * SLOT // 60} h/week have no retailer feed-in rate; 0c assumed.')
    return grid, bands


# --------------------------------------------------------------------------- assembly


def _select_tariff_period(contract: dict, on_date: date, index: int, notes: list[str]) -> dict:
    """Pick the tariff period covering ``on_date`` (MM-DD ranges may wrap the year end)."""
    periods = contract.get('tariffPeriod') or []
    if not periods:
        raise ValueError('Plan has no tariffPeriod')
    key = (on_date.month, on_date.day if (on_date.month, on_date.day) != (2, 29) else 28)

    def covers(tariff: dict) -> bool:
        try:
            start = tuple(int(x) for x in str(tariff.get('startDate', '01-01')).split('-')[-2:])
            end = tuple(int(x) for x in str(tariff.get('endDate', '12-31')).split('-')[-2:])
        except ValueError:
            return True
        return start <= key <= end if start <= end else (key >= start or key <= end)

    matching = [t for t in periods if covers(t)]
    if not matching:
        raise ValueError(f'No tariff period covers {on_date:%m-%d}')
    if not 0 <= index < len(matching):
        raise ValueError(f'network_tariff_index {index} out of range: {len(matching)} tariff period(s) cover {on_date}')
    others = [t for t in periods if t is not matching[index]]
    if others:
        notes.append('Other tariff periods not used: ' + '; '.join(
            f'{t.get("displayName", "?")} {t.get("startDate")}..{t.get("endDate")}' for t in others) + '.')
    return matching[index]


def _supply(tariff: dict, notes: list[str]) -> float:
    charge = tariff.get('dailySupplyCharge')
    if charge is None:
        bands = tariff.get('dailySupplyCharges') or []
        if not bands:
            raise ValueError('Tariff period has no daily supply charge')
        charge = bands[0].get('unitPrice')
        notes.append('Banded daily supply charge: first band used.')
    return round(_price(charge, 'daily supply charge') * GST, 5)


def _build_periods(import_grid, import_blocks, export_grid, export_bands) -> list[tuple[Period, int, int]]:
    """Compress the per-slot grids into named periods, merging identical days.

    Returns ``(period, import_block_index, export_band_index)`` triples.
    """
    # 1. Runs of identical (import, export) per weekday.
    runs: dict[tuple[int, int, int, int], list[int]] = {}  # (start, end, imp, exp) -> days
    for day in range(7):
        slot = 0
        while slot < SLOTS_PER_DAY:
            key = (import_grid[day][slot], export_grid[day][slot])
            end = slot
            while end < SLOTS_PER_DAY and (import_grid[day][end], export_grid[day][end]) == key:
                end += 1
            runs.setdefault((slot, end, *key), []).append(day)
            slot = end
    # 2. Name them after the import block. Where one import block spans several
    #    feed-in bands, the block's main band (most time, then lowest rate)
    #    keeps the plain name and the others get a suffix, e.g. peak and
    #    peak_fit_peak for CovaU SolarMax's 15-18h and 18-21h.
    coverage: dict[int, dict[int, int]] = {}
    for (start, end, imp, exp), days in runs.items():
        coverage.setdefault(imp, {}).setdefault(exp, 0)
        coverage[imp][exp] += (end - start) * len(days)
    main_band = {imp: max(bands, key=lambda e: (bands[e], -(export_bands[e].rate if e >= 0 else 0)))
                 for imp, bands in coverage.items()}
    used: set[str] = set()
    periods = []
    for (start, end, imp, exp), days in sorted(runs.items(), key=lambda item: (item[1][0], item[0][0])):
        block = import_blocks[imp]
        band = export_bands[exp] if exp >= 0 else None
        base = block.name
        suffixed = exp != main_band[imp] and band is not None
        if suffixed:
            base = f'{base}_fit_{band.name}'[:36]
        name, counter = base, 1
        while name in used:  # off_peak, off_peak_2, off_peak_3 ...
            counter += 1
            name = f'{base}_{counter}'
        used.add(name)
        label = f'{block.label} / {band.label} FiT' if suffixed else block.label
        period = Period(
            name=name, label=label[:60], start=f'{start * SLOT // 60:02d}:{start * SLOT % 60:02d}',
            end=f'{end * SLOT // 60:02d}:{end * SLOT % 60:02d}', days=days,
            import_rate=block.rate, export_rate=band.rate if band else 0.0)
        periods.append((period, imp, exp))
    return periods


def _allowances(periods, import_blocks, export_bands) -> list[Allowance]:
    """Daily tier allowances over every period derived from a tiered block/band."""
    result = []
    for index, block in enumerate(import_blocks):
        if block.allowance_kwh is not None:
            names = [p.name for p, imp, _ in periods if imp == index]
            if names:
                result.append(Allowance(kind='import', periods=names, kwh_per_day=block.allowance_kwh,
                                        rate_after=block.rate_after))
    for index, band in enumerate(export_bands):
        if band.allowance_kwh is not None:
            names = [p.name for p, _, exp in periods if exp == index]
            if names:
                result.append(Allowance(kind='export', periods=names, kwh_per_day=band.allowance_kwh,
                                        rate_after=band.rate_after))
    return result


def _plan_id(detail: dict) -> str:
    brand = _slug(detail.get('brand') or detail.get('brandName') or '', 'plan').replace('_', '-')
    code = re.sub(r'[^a-z0-9]+', '-', str(detail.get('planId', '')).split('@')[0].lower()).strip('-')
    return f'{brand}-{code}'.strip('-')[:40] or 'imported-plan'


def _join_notes(notes: list[str]) -> str:
    text = ' '.join(dict.fromkeys(n for n in notes if n))  # de-duplicate, keep order
    return text if len(text) <= NOTES_LIMIT else text[:NOTES_LIMIT - 1] + '…'


def from_cdr(detail: dict, *, network_tariff_index: int = 0, include_controlled_load: bool = False,
             on_date: date | None = None) -> TariffPlan:
    """Convert a CDR energy plan-detail payload into a pending :class:`TariffPlan`.

    ``detail`` is either the full response (``{"data": {...}}``) or its ``data``
    object. ``on_date`` (default today) selects the seasonal tariff period and
    the feed-in tariffs in force. ``network_tariff_index`` chooses between
    several tariff periods covering the same date (alternative network
    tariffs). ``include_controlled_load`` lists controlled-load circuit rates in
    the notes; they are never part of the periods because a TariffPlan models a
    single metered import circuit.

    Raises ``ValueError`` when the plan cannot be represented faithfully.
    """
    if not isinstance(detail, dict):
        raise ValueError('Plan detail must be a JSON object')
    if isinstance(detail.get('data'), dict):
        detail = detail['data']
    contract = detail.get('electricityContract')
    if not isinstance(contract, dict):
        raise ValueError('Not an electricity plan (no electricityContract)')
    on_date = on_date or date.today()
    notes: list[str] = []

    tariff = _select_tariff_period(contract, on_date, network_tariff_index, notes)
    supply = _supply(tariff, notes)
    import_grid, import_blocks = _import_grid(tariff, notes)
    export_grid, export_bands = _export_grid(contract, on_date, notes)
    if not export_bands:
        notes.append('No retailer solar feed-in tariff; 0c assumed.')

    built = _build_periods(import_grid, import_blocks, export_grid, export_bands)
    if len(built) > MAX_PERIODS:
        raise ValueError(f'Plan needs {len(built)} periods; at most {MAX_PERIODS} are supported')
    allowances = _allowances(built, import_blocks, export_bands)
    periods = [period for period, _, _ in built]

    if tariff.get('demandCharges'):
        charges = ', '.join(f'{c.get("displayName", "demand")} ${_price(c.get("amount"), "demand") * GST:.4f}/'
                            f'{c.get("measureUnit", "kW").lower()}/{c.get("chargePeriod", "period").lower()}'
                            for c in tariff['demandCharges'])
        notes.append(f'Demand charges not modelled: {charges} (GST incl).')
    controlled = contract.get('controlledLoad') or []
    if controlled:
        if include_controlled_load:
            for load in controlled:
                block = load.get('singleRate') or {}
                rates = [_fmt(_price(r.get('unitPrice'), 'controlled load') * GST) for r in block.get('rates') or []]
                if 'timeOfUseRates' in load:
                    rates = [_fmt(_price(r.get('unitPrice'), 'controlled load') * GST)
                             for b in load['timeOfUseRates'] for r in b.get('rates') or []]
                notes.append(f'{load.get("displayName", "Controlled load")}: {"/".join(rates) or "?"} per kWh '
                             'incl GST on a separate circuit (not modelled).')
        else:
            notes.append('Plan also offers a controlled-load circuit (ignored).')
    if contract.get('timeZone') == 'AEST':
        notes.append('Retailer times are AEST (no daylight saving).')
    if contract.get('discounts') or contract.get('incentives'):
        names = [str(i.get('displayName', '?')) for i in (contract.get('discounts') or []) + (contract.get('incentives') or [])]
        notes.append('Discounts/incentives not modelled: ' + '; '.join(names)[:200] + '.')

    plan_code = str(detail.get('planId', '')).split('@')[0][:40]
    header = f'Imported from AER CDR {detail.get("planId", "?")} (effective {str(detail.get("effectiveFrom") or "?")[:10]}); ' \
             'rates incl GST, feed-in as published.'
    return TariffPlan(
        id=_plan_id(detail),
        name=str(detail.get('displayName') or plan_code or 'Imported plan')[:80],
        retailer=str(detail.get('brandName') or detail.get('brand') or '')[:80],
        plan_code=plan_code,
        status='pending',
        active_from=None,
        supply_daily=supply,
        periods=periods,
        allowances=allowances,
        notes=_join_notes([header, *notes]),
    )


# --------------------------------------------------------------------------- I/O


def load_file(path: str | Path, **options) -> TariffPlan:
    """Load a tariff from JSON: either a CDR plan-detail response or a native TariffPlan.

    Extra keyword options are passed to :func:`from_cdr` for CDR files.
    """
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(payload, dict) and ('electricityContract' in payload or
                                      isinstance(payload.get('data'), dict) and 'electricityContract' in payload['data']):
        return from_cdr(payload, **options)
    return TariffPlan.model_validate(payload)


async def fetch_cdr(plan_id: str, retailer_brand: str) -> dict:
    """Download one plan-detail response from the Energy Made Easy CDR endpoint.

    ``retailer_brand`` is the CDR brand slug (e.g. ``covau``) and ``plan_id``
    the full plan id (e.g. ``COV1117614MRE3@EME``). Both are validated strictly
    because they are interpolated into the URL. Redirects are not followed.
    """
    import httpx  # imported lazily so parsing does not require httpx

    if not isinstance(plan_id, str) or not PLAN_ID_PATTERN.fullmatch(plan_id) or plan_id in ('.', '..'):
        raise ValueError('Invalid plan id')
    if not isinstance(retailer_brand, str) or not BRAND_PATTERN.fullmatch(retailer_brand):
        raise ValueError('Invalid retailer brand')
    url = CDR_BASE_URL.format(brand=retailer_brand, plan_id=plan_id)
    async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
        response = await client.get(url, headers={'x-v': '3', 'x-min-v': '1', 'Accept': 'application/json'})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
        raise ValueError('Unexpected CDR response shape')
    return payload
