"""Receding-horizon battery planner (mixed-integer linear program, HiGHS).

Physical model per interval i and system s (all energies kWh in the interval):

    pv_ac + pv_bat + curtailed = pv_forecast             DC PV split
    pv_ac + discharge (+ pv_bat if AC-coupled) <= ac_kw*h   inverter AC output limit
    grid_bat <= ac_kw*h                                    inverter AC input (grid charging)
    pv_bat + grid_bat <= charge_kw*h * y_charge           battery charge limit (DC)
    discharge <= discharge_kw*h * (1 - y_charge)          no simultaneous charge/discharge
    soc_i = soc_(i-1) + eta*(pv_bat + grid_bat) - discharge/eta

Site:
    sum_s(pv_ac + discharge - grid_bat) + import = load + export
    import <= grid_import_kw*h * y_import ; export <= export_kw*h * (1 - y_import)

Because PV can reach the battery on the DC side, PV above an inverter's AC
limit is only curtailed when the battery is full or at its charge limit. The
optimiser therefore keeps battery headroom ahead of peak sun when that is worth
more than filling it early ("export_priority" action).

Objective: import cost - export credit + wear*discharge - value of energy left
at the horizon end (+ tiny tie-breakers). Daily allowances (e.g. 50 kWh free,
30 kWh premium export) are modelled as two price tiers with the cheaper/better
tier capped by what is left of today's allowance (measured, gaps counted
conservatively).

Results are *modelled* outcomes against a simulated self-use baseline on the
same forecast; they are not measured savings.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

from .config import Settings, stamp, utcnow
from .db import Database, encode
from .dispatch import overnight_need, split_points
from .ingest import select_intervals
from .tariffs import plan_at

VERSION = 'milp-dc-coupled-v1.5'
EPS = 1e-6


def dispatch_policy(settings: Settings) -> str:
    """Identify the tariff and reserve settings used to schedule live commands."""
    dispatch = (settings.site.timezone, settings.forecast.tariff_buffer_minutes,
                settings.forecast.overnight_buffer_pct, [t.model_dump() for t in settings.tariffs])
    return hashlib.sha256(encode(dispatch).encode()).hexdigest()


# ------------------------------------------------------------------ prices
def price_points(settings: Settings, points: list[dict]) -> list[dict]:
    """Per-interval import/export price tiers and allowance group keys."""
    zone = settings.zone
    out = []
    for p in points:
        at = datetime.fromisoformat(p['ts'])
        local = at.astimezone(zone)
        plan = plan_at(settings.tariffs, at)
        period = plan.period_at(local)
        imports = [dict(rate=period.import_rate, group=None)]
        exports = [dict(rate=period.export_rate, group=None)]
        for index, allowance in enumerate(plan.allowances):
            if period.name not in allowance.periods:
                continue
            group = (local.date().isoformat(), plan.id, index)
            tiers = imports if allowance.kind == 'import' else exports
            tiers[0]['group'] = group
            tiers[0]['limit'] = allowance.kwh_per_day  # plan() lowers this to what is left today
            tiers.append(dict(rate=allowance.rate_after, group=None))
        out.append(dict(plan=plan.id, plan_name=plan.name, period=period.name, label=period.label or period.name,
                        imports=imports, exports=exports, buy=period.import_rate, sell=period.export_rate))
    return out


def allowance_used(db: Database, settings: Settings, now: datetime) -> dict[tuple, float]:
    """kWh of each of today's allowances already used, from measured intervals.

    Unmeasured time inside an allowance window counts as used at the connection
    limit, so missing data can only make the planner more cautious.
    """
    zone = settings.zone
    local_midnight = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = {r['ts']: r for r in select_intervals(db, stamp(local_midnight), stamp(now))}
    used: dict[tuple, float] = defaultdict(float)
    cursor = local_midnight
    while cursor + timedelta(minutes=30) <= now:
        at = cursor
        plan = plan_at(settings.tariffs, at)
        local = at.astimezone(zone)
        period = plan.period_at(local)
        row = rows.get(stamp(at))
        for index, allowance in enumerate(plan.allowances):
            if period.name not in allowance.periods:
                continue
            key = (local.date().isoformat(), plan.id, index)
            measured = (row['import_kwh'] if allowance.kind == 'import' else row['export_kwh']) if row else 0.0
            missing_h = (1800 - (row['coverage_s'] if row else 0)) / 3600
            limit = settings.site.grid_import_kw if allowance.kind == 'import' else settings.site.export_kw
            used[key] += measured + max(0.0, missing_h) * limit
        cursor += timedelta(minutes=30)
    return dict(used)


# --------------------------------------------------------------- builder
@dataclass
class Model:
    cost: list = field(default_factory=list)
    lo: list = field(default_factory=list)
    hi: list = field(default_factory=list)
    integer: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    row_lo: list = field(default_factory=list)
    row_hi: list = field(default_factory=list)

    def var(self, cost=0.0, lo=0.0, hi=math.inf, integer=False) -> int:
        self.cost.append(cost)
        self.lo.append(lo)
        self.hi.append(max(lo, hi))
        self.integer.append(1 if integer else 0)
        return len(self.cost) - 1

    def add(self, terms: dict[int, float], lo=-math.inf, hi=math.inf):
        self.rows.append(terms)
        self.row_lo.append(lo)
        self.row_hi.append(hi)

    def solve(self, seconds=10.0):
        n = len(self.cost)
        matrix = lil_matrix((len(self.rows), n))
        for j, terms in enumerate(self.rows):
            for k, value in terms.items():
                matrix[j, k] += value
        matrix = matrix.tocsr()
        lo, hi = np.array(self.lo), np.array(self.hi)
        integer = np.array(self.integer)
        result = milp(np.array(self.cost), integrality=integer, bounds=Bounds(lo, hi),
                      constraints=LinearConstraint(matrix, self.row_lo, self.row_hi),
                      options={'time_limit': seconds, 'mip_rel_gap': 0.005})
        if result.x is None:
            return None, result
        x = result.x
        ax = matrix @ x
        tol = 1e-5
        ok = (np.all(x >= lo - tol) and np.all(x <= hi + tol) and np.all(ax >= np.array(self.row_lo) - tol)
              and np.all(ax <= np.array(self.row_hi) + tol) and np.all(np.abs(x[integer == 1] - np.round(x[integer == 1])) < tol))
        return (x if ok else None), result


# ------------------------------------------------------------- baseline
def self_use(settings: Settings, points, prices, initial: dict[str, float]) -> list[dict]:
    """Simulate the inverter's normal self-use mode on the same forecast."""
    systems = settings.active_systems()
    soc = dict(initial)
    used: dict[tuple, float] = defaultdict(float)
    out = []
    for p, price in zip(points, prices):
        h = p['hours']
        load = p['load_kwh']
        per = {}
        imp = exp = 0.0
        for s in systems:
            pv = p['solar_by_system'].get(s.id, 0.0)
            to_load = min(pv, load, s.ac_kw * h)
            load -= to_load
            per[s.id] = dict(pv=pv, pv_ac=to_load, pv_bat=0.0, dis=0.0, curt=0.0)
        for s in systems:
            d = per[s.id]
            surplus = d['pv'] - d['pv_ac']
            if s.battery:
                b = s.battery
                eta = math.sqrt(b.round_trip_efficiency)
                room = max(0.0, (b.max_soc_pct / 100 * b.capacity_kwh - soc[s.id]) / eta)
                d['pv_bat'] = min(surplus, b.charge_kw * h, room)
                soc[s.id] += d['pv_bat'] * eta
                surplus -= d['pv_bat']
            ac_room = s.ac_kw * h - d['pv_ac'] - (d['pv_bat'] if s.coupling == 'ac' else 0)
            out_kwh = min(surplus, max(0.0, ac_room), max(0.0, settings.site.export_kw * h - exp))
            exp += out_kwh
            d['pv_ac'] += out_kwh
            d['curt'] = surplus - out_kwh
        for s in systems:
            if not s.battery or load <= 0:
                continue
            b = s.battery
            eta = math.sqrt(b.round_trip_efficiency)
            d = per[s.id]
            available = max(0.0, (soc[s.id] - b.reserve_pct / 100 * b.capacity_kwh) * eta)
            d['dis'] = min(load, b.discharge_kw * h, available, max(0.0, s.ac_kw * h - d['pv_ac']))
            soc[s.id] -= d['dis'] / eta
            load -= d['dis']
        imp = max(0.0, load)
        cost = _tiered(price['imports'], imp, used) - _tiered(price['exports'], exp, used, export=True)
        out.append(dict(import_kwh=imp, export_kwh=exp, cost=cost, curtailed=sum(d['curt'] for d in per.values()),
                        soc=dict(soc), systems=per))
    return out


def _tiered(tiers, amount, used, export=False):
    """Cost of an amount through allowance tiers, consuming the allowance (baseline only)."""
    total = 0.0
    for tier in tiers:
        take = amount
        if tier['group'] is not None:
            take = min(amount, max(0.0, tier.get('limit', math.inf) - used[tier['group']]))
            used[tier['group']] += take
        total += take * tier['rate']
        amount -= take
        if amount <= EPS:
            break
    return total


# --------------------------------------------------------------- planner
def plan(db: Database, settings: Settings, forecast_id: int, points: list[dict], forecast_model: dict,
         now: datetime | None = None, initial: dict[str, float] | None = None, overrides: list[dict] | None = None,
         store: bool = True) -> dict:
    now = now or utcnow()
    systems = settings.active_systems()
    points = split_points(settings, points)
    prices = price_points(settings, points)
    overnight = overnight_need(points, settings)
    battery_capacity = settings.battery_kwh()
    capacity = battery_capacity or 1
    used = allowance_used(db, settings, now)
    if overrides is None:
        overrides = db.rows('SELECT * FROM overrides WHERE cancelled_at IS NULL AND starts<=? AND expires>?', (stamp(now), stamp(now)))
    hold = any(o['kind'] == 'hold' for o in overrides)
    no_battery_export = any(o['kind'] == 'no_battery_export' for o in overrides)
    data = lambda o: o['data'] if isinstance(o['data'], dict) else json.loads(o['data'] or '{}')
    reserve_boost = max((float(data(o).get('pct', 0)) for o in overrides if o['kind'] == 'reserve'), default=0.0)

    # Initial stored energy (kWh) per battery from fresh telemetry; never invented.
    telemetry_fresh = True
    if initial is None:
        initial = {}
        for s in systems:
            if not s.battery:
                continue
            latest = db.latest_system(s.id)
            fresh = bool(latest and latest['soc'] is not None and now - datetime.fromisoformat(latest['ts']) < timedelta(minutes=20))
            telemetry_fresh &= fresh
            # Unknown state: assume only the reserve is stored (cautious, never invents energy).
            initial[s.id] = s.battery.capacity_kwh * (latest['soc'] if fresh else s.battery.reserve_pct) / 100

    allowance_limits = {}
    for price in prices:
        for tier in price['imports'] + price['exports']:
            if tier['group'] is not None:
                day, plan_id, index = tier['group']
                tariff = next(t for t in settings.tariffs if t.id == plan_id)
                limit = max(0.0, tariff.allowances[index].kwh_per_day - used.get(tier['group'], 0.0))
                allowance_limits[tier['group']] = limit
                tier['limit'] = limit

    final = [p['buy'] for p, pt in zip(prices, points) if pt['lead_minutes'] >= max(0, (points[-1]['lead_minutes'] - 24 * 60))]
    terminal_value = settings.forecast.terminal_value_factor * float(np.quantile(final, 0.2)) if final else 0.0

    m = Model()
    idx: list[dict] = []
    groups: dict[tuple, list[int]] = defaultdict(list)
    previous_soc: dict[str, int | None] = {s.id: None for s in systems}
    for i, (p, price) in enumerate(zip(points, prices)):
        h = p['hours']
        v: dict = {'imp': [], 'exp': [], 'sys': {}}
        for tier in price['imports']:
            j = m.var(cost=tier['rate'], hi=settings.site.grid_import_kw * h)
            v['imp'].append(j)
            if tier['group'] is not None:
                groups[('imp',) + tier['group']].append(j)
        for tier in price['exports']:
            j = m.var(cost=-tier['rate'], hi=settings.site.export_kw * h)
            v['exp'].append(j)
            if tier['group'] is not None:
                groups[('exp',) + tier['group']].append(j)
        y_import = m.var(lo=0, hi=1, integer=True)
        night = overnight[i]
        sale = None
        if battery_capacity and price['buy'] > 0 and p['export_allowed'] and night is not None and not no_battery_export:
            sale = m.var(integer=True, hi=1)
        m.add({**{j: 1 for j in v['imp']}, y_import: -settings.site.grid_import_kw * h}, hi=0)
        m.add({**{j: 1 for j in v['exp']}, y_import: settings.site.export_kw * h}, hi=settings.site.export_kw * h)
        balance = {**{j: 1 for j in v['imp']}, **{j: -1 for j in v['exp']}}
        pv_ac_all = []
        for s in systems:
            pv = max(0.0, p['solar_by_system'].get(s.id, 0.0))
            sv = {}
            sv['pv_ac'] = m.var(hi=pv)
            sv['curt'] = m.var(cost=1e-4, hi=pv)
            pv_ac_all.append(sv['pv_ac'])
            balance[sv['pv_ac']] = 1
            output = {sv['pv_ac']: 1}
            if s.battery:
                b = s.battery
                eta = math.sqrt(b.round_trip_efficiency)
                floor = b.capacity_kwh * max(b.reserve_pct, reserve_boost) / 100
                ceiling = b.capacity_kwh * b.max_soc_pct / 100
                start = initial.get(s.id, floor)
                sv['pv_bat'] = m.var(hi=pv)
                sv['grid_bat'] = m.var(cost=1e-5, hi=min(s.ac_kw, b.charge_kw) * h if p['charge_allowed'] else 0)
                sv['dis'] = m.var(cost=b.wear_cents_per_kwh / 100, hi=0 if hold else min(b.discharge_kw, s.ac_kw) * h)
                # Never plan below the current level if already under the floor (no invented energy).
                # Tiny reward for stored energy, decaying with lead time: among equally good
                # plans prefer charging sooner (more robust to forecast error), without
                # outweighing real price differences (max ~0.5c/kWh over a whole day).
                sv['soc'] = m.var(cost=-1e-4 * 0.99 ** i, lo=min(floor, start), hi=max(ceiling, start))
                y = m.var(lo=0, hi=1, integer=True)
                m.add({sv['pv_bat']: 1, sv['grid_bat']: 1, y: -b.charge_kw * h}, hi=0)
                m.add({sv['dis']: 1, y: b.discharge_kw * h}, hi=b.discharge_kw * h)
                max_discharge = min(s.ac_kw, b.discharge_kw) * h
                with_export = {sv['dis']: 1, y_import: -max_discharge}
                if sale is not None:
                    with_export[sale] = -max_discharge
                    expected, uncertainty, _ = night
                    # Share the net household load by battery capacity and convert it to stored energy.
                    extra = expected / (capacity * eta)
                    extra += min(settings.forecast.overnight_buffer_pct / 100, uncertainty / (capacity * eta))
                    export_floor = floor + extra * b.capacity_kwh
                    sv['export_floor_pct'] = export_floor / b.capacity_kwh * 100
                    m.add({sv['soc']: 1, sale: -export_floor}, lo=0)
                m.add(with_export, hi=0)
                balance[sv['dis']] = 1
                balance[sv['grid_bat']] = -1
                output[sv['dis']] = 1
                if s.coupling == 'ac':
                    output[sv['pv_bat']] = 1
                soc_terms = {sv['soc']: 1, sv['pv_bat']: -eta, sv['grid_bat']: -eta, sv['dis']: 1 / eta}
                if previous_soc[s.id] is None:
                    m.add(soc_terms, start, start)
                else:
                    soc_terms[previous_soc[s.id]] = -1
                    m.add(soc_terms, 0, 0)
                previous_soc[s.id] = sv['soc']
                m.add({sv['pv_ac']: 1, sv['pv_bat']: 1, sv['curt']: 1}, pv, pv)
            else:
                m.add({sv['pv_ac']: 1, sv['curt']: 1}, pv, pv)
            m.add(output, hi=s.ac_kw * h)
            v['sys'][s.id] = sv
        m.add(balance, p['load_kwh'], p['load_kwh'])
        if no_battery_export:
            m.add({**{j: 1 for j in v['exp']}, **{j: -1 for j in pv_ac_all}}, hi=0)
        idx.append(v)

    for key, members in groups.items():
        m.add({j: 1 for j in members}, hi=allowance_limits[key[1:]])
    for s in systems:
        if s.battery and previous_soc[s.id] is not None:
            eta = math.sqrt(s.battery.round_trip_efficiency)
            m.cost[previous_soc[s.id]] -= terminal_value * eta

    began = time.monotonic()
    x, result = m.solve()
    solve_seconds = round(time.monotonic() - began, 3)
    baseline = self_use(settings, points, prices, initial)
    intervals = []
    if x is not None:
        x = np.maximum(x, 0)
        for p, price, v, base, night in zip(points, prices, idx, baseline, overnight):
            imp = float(sum(x[j] for j in v['imp']))
            exp = float(sum(x[j] for j in v['exp']))
            cost = float(sum(x[j] * m.cost[j] for j in v['imp'] + v['exp']))
            per = {}
            for s in systems:
                sv = v['sys'][s.id]
                g = lambda k: float(x[sv[k]]) if k in sv else 0.0
                per[s.id] = dict(pv_ac=g('pv_ac'), pv_bat=g('pv_bat'), grid_bat=g('grid_bat'), dis=g('dis'), curt=g('curt'),
                                 soc_kwh=g('soc'), soc_pct=g('soc') / s.battery.capacity_kwh * 100 if s.battery else None,
                                 export_floor_pct=sv.get('export_floor_pct'))
            intervals.append(dict(p=p, price=price, imp=imp, exp=exp, cost=cost, systems=per, base=base,
                                  overnight_ev_kwh=night.planned_ev_kwh if night else 0.0))
        solver = 'HiGHS MILP'
    else:
        for p, price, base in zip(points, prices, baseline):
            per = {sid: dict(pv_ac=d['pv_ac'], pv_bat=d['pv_bat'], grid_bat=0.0, dis=d['dis'], curt=d['curt'],
                             soc_kwh=base['soc'].get(sid, 0.0),
                             soc_pct=base['soc'][sid] / next(s for s in systems if s.id == sid).battery.capacity_kwh * 100 if sid in base['soc'] else None)
                   for sid, d in base['systems'].items()}
            intervals.append(dict(p=p, price=price, imp=base['import_kwh'], exp=base['export_kwh'], cost=base['cost'], systems=per, base=base))
        solver = 'self-use fallback'

    label_actions(settings, intervals)
    end_plan = sum(intervals[-1]['systems'][s.id]['soc_kwh'] for s in systems if s.battery) if intervals else 0
    end_base = sum(baseline[-1]['soc'].values()) if baseline else 0
    plan_cash = sum(r['cost'] for r in intervals)
    base_cash = sum(b['cost'] for b in baseline)
    wear = sum(sum(d['dis'] for d in r['systems'].values()) for r in intervals) * \
        (np.mean([s.battery.wear_cents_per_kwh for s in systems if s.battery]) / 100 if any(s.battery for s in systems) else 0)
    stored_value = terminal_value * (end_plan - end_base)
    first_day = [r for r in intervals if r['p']['lead_minutes'] < 24 * 60]
    # Credit (or charge) the difference in stored energy at the 24 h mark, as for the full horizon,
    # so a plan that ends the day fuller than self-use isn't shown as a loss.
    day_end_gap = 0.0
    if first_day:
        last = first_day[-1]
        day_end_gap = (sum(last['systems'][s.id]['soc_kwh'] for s in systems if s.battery) - sum(last['base']['soc'].values()))
    summary = dict(
        solver=solver, solver_status=int(result.status), solver_message=str(result.message)[:200], solve_seconds=solve_seconds,
        objective='import cost − export credit + battery wear − value of energy left at horizon end',
        plan_cash_aud=plan_cash, baseline_cash_aud=base_cash, wear_aud=float(wear),
        end_stored_kwh=end_plan, baseline_end_stored_kwh=end_base, terminal_value_per_kwh=terminal_value,
        modelled_saving_aud=base_cash - plan_cash + stored_value,
        modelled_saving_24h_aud=sum(b['base']['cost'] for b in first_day) - sum(r['cost'] for r in first_day) + terminal_value * day_end_gap,
        curtailed_kwh=sum(sum(d['curt'] for d in r['systems'].values()) for r in intervals),
        baseline_curtailed_kwh=sum(b['curtailed'] for b in baseline),
        import_kwh=sum(r['imp'] for r in intervals), export_kwh=sum(r['exp'] for r in intervals),
        allowance_used=[dict(day=k[0], plan=k[1], index=k[2], used_kwh=v) for k, v in used.items()],
        allowance_remaining=[dict(day=k[0], plan=k[1], index=k[2], kwh=v) for k, v in allowance_limits.items()],
        dispatch_policy=dispatch_policy(settings), telemetry_fresh=telemetry_fresh, initial_soc_pct=sum(initial.values()) / capacity * 100 if initial else None,
        action=intervals[0]['action'] if intervals else 'self_use', reason=intervals[0]['reason'] if intervals else '',
        overrides=[o['kind'] for o in overrides], confidence=forecast_model.get('confidence'),
        limitations=['Savings are modelled against a simulated self-use baseline on the same forecast, not measured.',
                     'Forecast error, device behaviour and tariff interpretation can all differ from reality.'])
    inputs = dict(settings=settings.model_dump(), initial_kwh=initial, forecast_id=forecast_id, forecast=forecast_model,
                  overrides=[{k: o.get(k) for k in ('id', 'kind', 'data', 'expires')} for o in overrides], used=summary['allowance_used'])
    digest = hashlib.sha256(encode(inputs).encode()).hexdigest()
    plan_id = plan_at(settings.tariffs, now).id
    if not store:  # backtests and what-if runs
        return dict(id=None, created_at=stamp(now), summary=summary, intervals=[flatten(r) for r in intervals])
    with db.connect() as c:
        decision_id = c.execute('INSERT INTO decisions(created_at, version, forecast_id, mode, tariff_id, input_hash, inputs, summary) '
                                'VALUES(?,?,?,?,?,?,?,?)', (stamp(now), VERSION, forecast_id, settings.mode, plan_id, digest,
                                                            encode(inputs), encode(summary))).lastrowid
        c.executemany('INSERT INTO decision_intervals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', [
            (decision_id, r['p']['ts'], r['p']['hours'], r['action'], r['fox_mode'], r['p']['load_kwh'], r['p']['solar_kwh'],
             sum(d['pv_ac'] for d in r['systems'].values()), sum(d['pv_bat'] for d in r['systems'].values()),
             sum(d['grid_bat'] for d in r['systems'].values()), sum(d['dis'] for d in r['systems'].values()),
             r['imp'], r['exp'], sum(d['curt'] for d in r['systems'].values()), r['soc_pct'],
             r['price']['buy'], r['price']['sell'], r['cost'], sum(r['base']['soc'].values()) / capacity * 100,
             r['base']['cost'], r['base']['curtailed'], r['reason'], encode(r['systems']))
            for r in intervals])
    return dict(id=decision_id, created_at=stamp(now), summary=summary, intervals=[flatten(r) for r in intervals])


def flatten(r: dict) -> dict:
    p = r['p']
    return dict(ts=p['ts'], hours=p['hours'], action=r['action'], commands=r['commands'], reason=r['reason'],
                load_kwh=p['load_kwh'], house_kwh=p['house_kwh'], ev_kwh=p['ev_kwh'], solar_kwh=p['solar_kwh'],
                solar_p10=p['solar_p10'], solar_p90=p['solar_p90'], house_p10=p['house_p10'], house_p90=p['house_p90'],
                temperature=p['temperature'], import_kwh=r['imp'], export_kwh=r['exp'], cost=r['cost'],
                buy=r['price']['buy'], sell=r['price']['sell'], period=r['price']['label'], plan=r['price']['plan_name'],
                soc_pct=r['soc_pct'], baseline_soc_pct=r.get('baseline_soc_pct'), baseline_cost=r['base']['cost'],
                curtailed_kwh=sum(d['curt'] for d in r['systems'].values()), baseline_curtailed_kwh=r['base']['curtailed'],
                charge_kwh=sum(d['pv_bat'] + d['grid_bat'] for d in r['systems'].values()),
                grid_charge_kwh=sum(d['grid_bat'] for d in r['systems'].values()),
                discharge_kwh=sum(d['dis'] for d in r['systems'].values()), systems=r['systems'])


# ------------------------------------------------------------ explanations
def label_actions(settings: Settings, intervals: list[dict]):
    """Turn each interval's solution into vendor-neutral commands and a plain reason."""
    systems = settings.active_systems()
    capacity = settings.battery_kwh() or 1
    for i, r in enumerate(intervals):
        h = r['p']['hours']
        commands = {}
        pv_to_grid = sum(d['pv_ac'] for d in r['systems'].values())
        battery_export = r['exp'] > 0.05 * h and r['exp'] > pv_to_grid - r['p']['load_kwh'] + 0.05 * h
        for s in systems:
            d = r['systems'][s.id]
            if not s.battery:
                continue
            b = s.battery
            room = b.capacity_kwh * b.max_soc_pct / 100 - d['soc_kwh']
            if d['grid_bat'] > 0.1 * h:
                mode = 'force_charge'
            elif d['dis'] > 0.1 * h and battery_export and d.get('export_floor_pct') is not None:
                mode = 'force_discharge'
            elif r['price']['buy'] > 0 and r['exp'] > 0.1 * h and room > 0.5 and d['pv_bat'] < min(b.charge_kw * h, room) - 0.1 * h:
                mode = 'export_priority'
            elif d['dis'] < 0.02 * h and r['imp'] > 0.1 * h and d['soc_pct'] > b.reserve_pct + 1:
                mode = 'hold'
            else:
                mode = 'self_use'
            power = (d['pv_bat'] + d['grid_bat']) / h if mode == 'force_charge' else d['dis'] / h if mode == 'force_discharge' else 0.0
            target = round(d['soc_pct'], 1)
            if mode == 'force_discharge':
                target = max(target, math.ceil(d['export_floor_pct'] * 10 - EPS) / 10)
            commands[s.id] = dict(mode=mode, power_kw=round(power, 2), target_soc=target)
            d['command'] = commands[s.id]  # stored with the decision; control.py executes exactly this
        order = ['force_charge', 'force_discharge', 'export_priority', 'hold', 'self_use']
        action = min((c['mode'] for c in commands.values()), key=order.index, default='self_use')
        r['action'] = action
        r['commands'] = commands
        r['fox_mode'] = {'force_charge': 'ForceCharge', 'force_discharge': 'ForceDischarge', 'export_priority': 'Feedin',
                         'hold': 'SelfUse(min SoC)', 'self_use': 'SelfUse'}[action]
        stored = sum(d['soc_kwh'] for d in r['systems'].values())
        r['soc_pct'] = stored / capacity * 100
        r['baseline_soc_pct'] = sum(r['base']['soc'].values()) / capacity * 100
        r['reason'] = explain(settings, intervals, i, action)


def _cents(rate):
    return f'{rate * 100:.1f}c'


def explain(settings: Settings, intervals: list[dict], i: int, action: str) -> str:
    r = intervals[i]
    price = r['price']
    later = intervals[i + 1:i + 49]
    if action == 'force_charge':
        uses = [x for x in later if sum(d['dis'] for d in x['systems'].values()) > 0.05]
        target = max(uses, key=lambda x: max(x['price']['buy'], x['price']['sell']), default=None)
        tail = (f" Stored energy is worth about {_cents(max(target['price']['buy'], target['price']['sell']))} "
                f"in the {target['price']['label'].lower()} period.") if target else ''
        free = ' (free allowance)' if r['price']['imports'][0]['group'] and r['price']['buy'] == 0 else ''
        return f"Charge from the grid at {_cents(price['buy'])}{free} during {price['label'].lower()}.{tail}"
    if action == 'force_discharge':
        refill = min((x['price']['buy'] for x in later), default=price['buy'])
        floors = [d['export_floor_pct'] for sid, d in r['systems'].items()
                  if r['commands'].get(sid, {}).get('mode') == 'force_discharge']
        reserve = ' / '.join(f'{floor:.1f}%' for floor in floors)
        demand = 'household use and planned EV charging' if r.get('overnight_ev_kwh', 0) > EPS else 'household use'
        return (f"Export battery energy at {_cents(price['sell'])}. Keep at least {reserve} for the minimum "
                f"reserve and forecast {demand} through 6 am. Cheapest refill ahead is {_cents(refill)}.")
    if action == 'export_priority':
        clip = sum(x['base']['curtailed'] for x in later[:16])
        if clip > 0.2:
            return (f"Send solar to the grid first and let the battery take only what the inverter can't pass. "
                    f"Keeps room for about {clip:.1f} kWh of peak solar above the inverter's AC limit that a full battery would waste.")
        return 'Send solar to the grid first; the battery can be filled more cheaply later in the plan.'
    if action == 'hold':
        expensive = max(later, key=lambda x: x['price']['buy'], default=None)
        if expensive and expensive['price']['buy'] > price['buy']:
            return (f"Keep the battery for {expensive['price']['label'].lower()} imports at {_cents(expensive['price']['buy'])}; "
                    f"grid power now costs {_cents(price['buy'])}.")
        return 'Keep stored energy for later; using it now is worth less than the battery wear.'
    if r['p'].get('planned_ev_kwh', 0) > EPS:
        return 'Normal self-use includes the EV charge you have planned for this time.'
    if r['exp'] > 0.05 and sum(d['curt'] for d in r['systems'].values()) > 0.05:
        return 'Battery full and inverter at its AC limit: some solar is unavoidably curtailed.'
    return 'Normal self-use: solar to the home first, then the battery, then the grid.'
