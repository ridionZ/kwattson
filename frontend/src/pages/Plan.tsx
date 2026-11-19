import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import Box from '@mui/material/Box';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';
import Button from '@mui/material/Button';
import Collapse from '@mui/material/Collapse';
import IconButton from '@mui/material/IconButton';
import Table from '@mui/material/Table';
import TableHead from '@mui/material/TableHead';
import TableBody from '@mui/material/TableBody';
import TableRow from '@mui/material/TableRow';
import TableCell from '@mui/material/TableCell';
import Chip from '@mui/material/Chip';
import ToggleButton from '@mui/material/ToggleButton';
import ToggleButtonGroup from '@mui/material/ToggleButtonGroup';
import useMediaQuery from '@mui/material/useMediaQuery';
import type { Theme } from '@mui/material/styles';
import RefreshRounded from '@mui/icons-material/RefreshRounded';
import ContentCopyRounded from '@mui/icons-material/ContentCopyRounded';
import ExpandMoreRounded from '@mui/icons-material/ExpandMoreRounded';
import { get, post, errorText } from '../lib/api';
import { useLoad, useNow } from '../lib/hooks';
import { useApp } from '../lib/app';
import { ACTIONS, action, foxMode } from '../lib/actions';
import * as f from '../lib/fmt';
import { ErrorState, Empty, Facts, LoadingBlock, PageHeader, Section, useToast } from '../components/common';
import { ChartBlock, LineTimeChart, MARGIN, useTimeAxis, type LegendItem } from '../components/TimeCharts';
import { useTokens } from './Overview';
import EvPlan, { type VehicleOutlook } from '../components/EvPlan';
import type { Override } from '../components/Scheduled';

interface Interval {
  ts: string; hours: number; action: string; load_kwh: number; solar_kwh: number; import_kwh: number; export_kwh: number;
  curtailed_kwh: number; ev_kwh?: number | null; soc_pct: number; buy_rate: number; sell_rate: number; cost: number; baseline_soc_pct: number; baseline_cost: number;
  baseline_curtailed_kwh: number; solar_p10: number | null; solar_p90: number | null; reason: string;
}

interface MeasuredInterval {
  ts: string; buy_rate: number; sell_rate: number; coverage_s?: number; solar_kwh?: number; load_kwh?: number;
  import_kwh?: number; export_kwh?: number; soc_end?: number | null;
}

export default function Plan() {
  const { status, reloadStatus, can } = useApp();
  const plan = useLoad<{ decision: any; intervals: Interval[]; history: MeasuredInterval[]; as_of: string; ev_charges?: Override[] }>(
    () => get('/api/plan'), [status?.decision?.id]);
  useEffect(() => {
    const timer = setInterval(() => { if (!document.hidden) void plan.reload(); }, 60000);
    return () => clearInterval(timer);
  }, [plan.reload]);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const now = useNow();

  async function replan() {
    setBusy(true);
    try {
      const r = await post('/api/plan');
      toast(`New plan made: ${action(r.summary?.action).label} now.`);
      await Promise.all([plan.reload(), reloadStatus()]);
    } catch (e) {
      toast(errorText(e), 'error');
    } finally {
      setBusy(false);
    }
  }
  const d = plan.data?.decision;
  const vehicles = (status?.vehicles ?? []).filter((v: { enabled: boolean }) => v.enabled);
  const last = plan.data?.intervals.at(-1);
  const horizonEnd = last ? Date.parse(last.ts) + last.hours * HOUR : undefined;
  const ev = vehicles.length > 0 && <EvPlan vehicles={vehicles} outlook={(status?.advice?.vehicles ?? []) as VehicleOutlook[]}
    charges={plan.data?.ev_charges ?? []} canEdit={can('member')} now={now} horizonEnd={horizonEnd}
    onChanged={() => { void plan.reload(); void reloadStatus(); }} />;
  return (
    <Box>
      <PageHeader title="Plan" sub={d ? `Plan made ${f.relative(d.created_at, now)}` : undefined}
        actions={can('member') && <Button variant="contained" startIcon={<RefreshRounded />} onClick={replan} disabled={busy}>{busy ? 'Planning…' : 'Re-plan'}</Button>} />
      {plan.error ? <ErrorState message={plan.error} onRetry={plan.reload} /> : !plan.data ? <LoadingBlock height={480} /> : (
        <Stack spacing={2.5}>
          {d ? <Summary d={d} rows={plan.data.intervals} batteryKwh={status?.battery_kwh ?? 0} />
            : <Empty>No plan has been made yet. The planner runs every few minutes once readings arrive.</Empty>}
          {ev}
          {d && <>
            <Charts rows={plan.data.intervals} history={plan.data.history ?? []} now={Date.parse(plan.data.as_of) || now}
              initialSoc={d.summary.initial_soc_pct ?? null} showEv={vehicles.length > 0} />
            <Schedule blocks={status?.advice?.schedule ?? []} now={now} plugins={(status?.systems ?? []).map((s: any) => s.plugin)} />
          </>}
        </Stack>
      )}
    </Box>
  );
}

function Summary({ d, rows, batteryKwh }: { d: any; rows: Interval[]; batteryKwh: number }) {
  const [open, setOpen] = useState(false);
  const [range, setRange] = useState<'day' | 'full'>('day');
  const s = d.summary;
  const start = Date.parse(rows[0]?.ts ?? d.created_at);
  const last = rows[rows.length - 1];
  const hours = last ? (Date.parse(last.ts) + last.hours * HOUR - start) / HOUR : 0;
  const full = range === 'full' || hours <= 24;
  const cutoff = start + 24 * HOUR;
  const period = full ? rows : rows.filter(r => Date.parse(r.ts) < cutoff);
  const fraction = (row: Interval) => full ? 1 : Math.min(1, (cutoff - Date.parse(row.ts)) / (row.hours * HOUR));
  const totals = period.reduce((acc, r) => {
    const share = fraction(r);
    return {
      importKwh: acc.importKwh + r.import_kwh * share,
      exportKwh: acc.exportKwh + r.export_kwh * share,
      importCost: acc.importCost + (r.import_kwh > 1e-6 ? r.cost * share : 0),
      exportCredit: acc.exportCredit - (r.export_kwh > 1e-6 ? r.cost * share : 0),
      net: acc.net + r.cost * share,
      baseline: acc.baseline + r.baseline_cost * share,
      kept: acc.kept + (r.baseline_curtailed_kwh - r.curtailed_kwh) * share,
    };
  }, { importKwh: 0, exportKwh: 0, importCost: 0, exportCredit: 0, net: 0, baseline: 0, kept: 0 });
  const billSaving = totals.baseline - totals.net;
  // Optimised intervals have one grid flow direction. Fallback intervals may only have a combined cost.
  const separateGridCosts = period.every(r => r.import_kwh <= 1e-6 || r.export_kwh <= 1e-6);
  const chargeDifference = (r: Interval | undefined) => r ? r.soc_pct - r.baseline_soc_pct : 0;
  const finalStep = period[period.length - 1];
  const stepShare = finalStep ? fraction(finalStep) : 0;
  const chargeGap = stepShare * chargeDifference(finalStep) + (1 - stepShare) * chargeDifference(period[period.length - 2]);
  const storedGap = full ? s.end_stored_kwh - s.baseline_end_stored_kwh : chargeGap * batteryKwh / 100;
  const adjustment = storedGap * (s.terminal_value_per_kwh ?? 0);
  return (
    <Section title="Plan cost and credits" action={hours > 24 && <ToggleButtonGroup size="small" exclusive value={range}
      onChange={(_, value) => value && setRange(value)} aria-label="Plan cost period"
      sx={{ '& .MuiToggleButton-root': { whiteSpace: 'nowrap' } }}>
      <ToggleButton value="day">First 24 h</ToggleButton>
      <ToggleButton value="full">Full {Math.round(hours)} h</ToggleButton>
    </ToggleButtonGroup>}>
      {period.length ? <>
        <Stack spacing={0.4} sx={{ mb: 1.5 }}>
          <Typography variant="h4" component="p">{f.netCost(totals.net)}</Typography>
          <Typography variant="body2" color="text.secondary">
            Modelled energy only, from {f.dateTime(start)}. Daily supply and battery wear are separate.
          </Typography>
        </Stack>
        <Facts rows={[
          ['Grid imports', separateGridCosts ? `${f.netCost(totals.importCost)} · ${f.kwh(totals.importKwh)}` : f.kwh(totals.importKwh)],
          ['Grid exports', separateGridCosts ? `${f.netCost(-totals.exportCredit)} · ${f.kwh(totals.exportKwh)}` : f.kwh(totals.exportKwh)],
          ['Net with plain self-use', f.netCost(totals.baseline)],
          ['Energy saving vs plain self-use', f.better(billSaving)],
          Math.abs(adjustment) >= 0.005 && ['Battery charge adjustment', f.money(adjustment)],
          Math.abs(adjustment) >= 0.005 && ['Saving after battery adjustment', f.better(billSaving + adjustment)],
          ['Solar kept from clipping', f.kwh(Math.max(0, totals.kept))],
        ]} />
      </> : <Empty>No intervals are available for this plan.</Empty>}
      <Button size="small" onClick={() => setOpen(!open)} aria-expanded={open} aria-controls="plan-details" sx={{ mt: 1.5, ml: -1 }}
        endIcon={<ExpandMoreRounded sx={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .2s' }} />}>Planner details</Button>
      <Collapse in={open} id="plan-details">
        <Box sx={{ pt: 1.5 }}>
          <Facts rows={[
            ['Battery wear, full plan', `${f.money(s.wear_aud)} modelled`],
            ['Battery at full plan end', `${f.kwh(s.end_stored_kwh)} (self-use ${f.kwh(s.baseline_end_stored_kwh)})`],
            ['Stored energy valued at', `${f.cents(s.terminal_value_per_kwh)}/kWh`],
            ['Solver', `${s.solver} · ${s.solver_status === 0 ? 'optimal' : s.solver_message} · ${f.n(s.solve_seconds, 2)} s`],
            ['Planner version', d.version],
            ['Tariff', d.tariff_id],
          ]} />
        </Box>
      </Collapse>
    </Section>
  );
}

type ChartPoint = {
  time: number; kind: 'actual' | 'forecast'; buy: number; sell: number; action?: string;
  solar: number | null; home: number | null; p10: number | null; p90: number | null;
  imp: number | null; exp: number | null; ev: number | null;
};

const HOUR = 3600000;
const power = (energy: number | null | undefined, hours: number) => energy != null && hours > 0 ? energy / hours : null;

function chartPoints(history: MeasuredInterval[], rows: Interval[], now: number, end: number): ChartPoint[] {
  const past = history.map((r): ChartPoint => {
    const duration = (r.coverage_s ?? 0) / 3600;
    const exported = power(r.export_kwh, duration);
    return { time: Date.parse(r.ts), kind: 'actual', solar: power(r.solar_kwh, duration), home: power(r.load_kwh, duration),
      buy: r.buy_rate * 100, sell: r.sell_rate * 100, imp: power(r.import_kwh, duration), exp: exported === null ? null : -exported,
      p10: null, p90: null, ev: null };
  });
  const lastActual = past[past.length - 1];
  if (lastActual) {
    const boundary = Math.min(now - 1, lastActual.time + HOUR / 2);
    if (boundary > lastActual.time) past.push({ ...lastActual, time: boundary });
  }
  const future = rows.flatMap((r): ChartPoint[] => {
    const start = Date.parse(r.ts);
    const stop = start + Math.round(r.hours * HOUR);
    if (stop <= now || start >= end) return [];
    const point: ChartPoint = { time: Math.max(now, start), kind: 'forecast', action: r.action,
      solar: power(r.solar_kwh, r.hours), home: power(r.load_kwh, r.hours), ev: power(r.ev_kwh, r.hours),
      p10: power(r.solar_p10, r.hours), p90: power(r.solar_p90, r.hours),
      buy: r.buy_rate * 100, sell: r.sell_rate * 100, imp: power(r.import_kwh, r.hours), exp: -r.export_kwh / r.hours };
    return r === rows[rows.length - 1] || stop >= end ? [point, { ...point, time: Math.min(stop, end) }] : [point];
  });
  return [...past, ...future];
}

function Charts({ rows, history, now, initialSoc, showEv }: { rows: Interval[]; history: MeasuredInterval[]; now: number; initialSoc: number | null; showEv: boolean }) {
  const { t, actions } = useTokens();
  const compact = useMediaQuery((th: Theme) => th.breakpoints.down('sm'));
  const [hi, setHi] = useState<number | null>(null);
  const container = useRef<HTMLDivElement>(null);
  const start = now - 24 * HOUR;
  const end = now + 48 * HOUR;
  const points = useMemo(() => chartPoints(history, rows, now, end), [history, rows, now, end]);
  const ts = useMemo(() => points.map(r => r.time), [points]);
  const batteryLevels = useMemo(() => {
    const levels = new Map(history.map(r => [Date.parse(r.ts) + HOUR / 2, r.soc_end]));
    const forecasts = new Map(rows.map(r => [Date.parse(r.ts) + Math.round(r.hours * HOUR), r]));
    return points.map(p => {
      if (p.kind === 'actual') return { actual: levels.get(p.time) ?? levels.get(p.time + 1) ?? null, planned: null, baseline: null };
      const last = forecasts.get(p.time);
      return {
        actual: levels.get(p.time) ?? null,
        planned: last?.soc_pct ?? (p.time === Date.parse(rows[0]?.ts) ? initialSoc : null),
        baseline: last?.baseline_soc_pct ?? (p.time === Date.parse(rows[0]?.ts) ? initialSoc : null),
      };
    });
  }, [points, rows, history, initialSoc]);
  const { axis, boundaries } = useTimeAxis(ts, now, compact, start, end);
  const defaultIndex = points.findIndex(r => r.kind === 'forecast');
  const idx = hi != null && hi < points.length ? hi : Math.max(0, defaultIndex);
  const timeLabel = (i: number) => f.dateTime(ts[i]);
  const common = { xAxis: axis, boundaries, hi, setHi, muted: t.baseline };
  const cur = points[idx];
  const a = cur?.action ? action(cur.action) : null;

  useEffect(() => {
    if (!compact) return;
    const setInitialPosition = requestAnimationFrame(() => {
      container.current?.querySelectorAll<HTMLElement>('[data-plan-plot]').forEach(plot => {
        plot.scrollLeft = plot.scrollWidth / 6;
      });
    });
    return () => cancelAnimationFrame(setInitialPosition);
  }, [compact]);

  function plot(title: string, child: ReactNode) {
    return <Box data-plan-plot role={compact ? 'region' : undefined} aria-label={compact ? `${title} timeline` : undefined} tabIndex={compact ? 0 : undefined}
      sx={{ overflowX: compact ? 'auto' : 'hidden' }}
      onScroll={compact ? e => {
        const left = e.currentTarget.scrollLeft;
        container.current?.querySelectorAll<HTMLElement>('[data-plan-plot]').forEach(other => {
          if (Math.abs(other.scrollLeft - left) > 1) other.scrollLeft = left;
        });
      } : undefined}>
      <Box sx={{ width: compact ? '200%' : '100%' }}>{child}</Box>
    </Box>;
  }

  const series = (id: string, key: keyof ChartPoint, color: string, kind?: ChartPoint['kind']) => ({
    id, color, curve: 'stepAfter', data: points.map(p => !kind || p.kind === kind ? p[key] as number | null : null), showMark: false,
  });
  const legend = (label: string, color: string, kind: LegendItem['kind'], value: (i: number) => string): LegendItem => ({ label, color, kind, value });
  const byKind = (key: 'solar' | 'home', kind: ChartPoint['kind']) => (i: number) => f.kw(points[i]?.kind === kind ? points[i]?.[key] : null);
  const seriesStyle = { '& .MuiLineChart-line': { strokeWidth: 2 }, '& .MuiLineChart-line[data-series^="forecast"]': { strokeDasharray: '5 3' } };
  const range = (i: number) => points[i]?.p10 == null || points[i]?.p90 == null ? '–' : `${f.n(points[i].p10)}–${f.n(points[i].p90)}`;
  const legendSolar = [
    legend('Actual solar', t.solar, 'line', byKind('solar', 'actual')),
    legend('Forecast solar', t.solar, 'dashed', byKind('solar', 'forecast')),
    legend('Likely range', t.solar, 'band', range),
    legend('Actual use', t.home, 'line', byKind('home', 'actual')),
    legend('Forecast use', t.home, 'dashed', byKind('home', 'forecast')),
  ];
  const legendEv = [legend('Forecast charging', t.ev, 'dashed', i => f.kw(points[i]?.ev))];
  const legendGrid = [
    legend('Import', t.gridIn, 'line', i => f.kw(points[i]?.imp)),
    legend('Export', t.gridOut, 'line', i => f.kw(points[i]?.exp == null ? null : Math.abs(points[i].exp!))),
  ];
  const legendBat = [
    legend('Actual', t.battery, 'line', i => f.pct(batteryLevels[i]?.actual)),
    legend('Planned', t.battery, 'dashed', i => f.pct(batteryLevels[i]?.planned)),
    legend('Plain self-use', t.baseline, 'dashed', i => f.pct(batteryLevels[i]?.baseline)),
  ];
  const legendPrice = [
    legend('Buy', t.buy, 'step', i => f.n(points[i]?.buy) + 'c'),
    legend('Feed-in', t.sell, 'step', i => f.n(points[i]?.sell) + 'c'),
  ];
  const tableRows = (items: LegendItem[]) => points.map((_, i) => [timeLabel(i), ...items.map(l => l.value(i))]);

  return (
    <Section title="Past 24 hours and next 48 hours" sx={{ minWidth: 0 }}>
      <Box ref={container} sx={{ minWidth: 0 }}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap', rowGap: 0.5, pb: 1, borderBottom: 1, borderColor: 'divider' }}>
          <Typography variant="body2" sx={{ fontWeight: 700 }}>{hi == null ? `Now · ${f.time(now)}` : timeLabel(idx)}</Typography>
          {a ? <Chip size="small" icon={<a.Icon />} label={a.label}
            sx={{ bgcolor: (actions as Record<string, string>)[cur?.action ?? ''] + '33', '& .MuiChip-icon': { color: 'text.primary' } }} />
            : cur?.kind === 'actual' && <Chip size="small" label="Measured" />}
          <Typography variant="caption" color="text.secondary">
            {compact ? 'Swipe any chart to scroll them together.' : 'Point at a chart to compare the same time across timelines.'}
          </Typography>
        </Stack>

        <ChartBlock id="c-solar" title="Solar & home" unit="kW" legend={legendSolar} index={idx}
          table={{ columns: ['Time', ...legendSolar.map(l => l.label)], rows: tableRows(legendSolar) }}>
          {plot('Solar and home', <LineTimeChart {...common} height={compact ? 172 : 210} yFormat={v => f.n(v)} yMin={0}
            desc="Measured and forecast solar and electricity use. Dashed lines are forecasts."
            series={[
              { ...series('floor', 'p10', 'transparent'), stack: 'range', disableHighlight: true },
              { id: 'range', color: t.solar, data: points.map(p => p.p10 == null || p.p90 == null ? null : Math.max(0, p.p90 - p.p10)),
                curve: 'stepAfter', stack: 'range', area: true, showMark: false, disableHighlight: true },
              series('actual-solar', 'solar', t.solar, 'actual'), series('forecast-solar', 'solar', t.solar, 'forecast'),
              series('actual-home', 'home', t.home, 'actual'), series('forecast-home', 'home', t.home, 'forecast'),
            ]}
            sx={{ ...seriesStyle, '& .MuiLineChart-area[data-series="range"]': { fillOpacity: 0.18 },
              '& .MuiLineChart-line[data-series="range"]': { strokeWidth: 0 } }} />)}
        </ChartBlock>

        {showEv && <ChartBlock id="c-ev" title="EV charging forecast" unit="kW" legend={legendEv} index={idx}
          table={{ columns: ['Time', 'Forecast charging'], rows: tableRows(legendEv) }}>
          {plot('EV charging forecast', <LineTimeChart {...common} height={compact ? 130 : 154} yFormat={v => f.n(v)} yMin={0}
            desc="Expected home EV charging power averaged over forecast intervals, including saved future charges."
            series={[series('forecast-ev', 'ev', t.ev, 'forecast')]} sx={seriesStyle} />)}
        </ChartBlock>}

        <ChartBlock id="c-grid" title="Grid" unit="kW, imports up, exports down" legend={legendGrid} index={idx}
          table={{ columns: ['Time', 'Import', 'Export'], rows: tableRows(legendGrid) }}>
          {plot('Grid', <LineTimeChart {...common} height={compact ? 130 : 154} yFormat={v => f.n(Math.abs(v))}
            desc="Grid imports above zero, exports below. Solid lines show measured power; dashed lines show the plan."
            series={[
              series('actual-import', 'imp', t.gridIn, 'actual'), series('forecast-import', 'imp', t.gridIn, 'forecast'),
              series('actual-export', 'exp', t.gridOut, 'actual'), series('forecast-export', 'exp', t.gridOut, 'forecast'),
            ]} sx={seriesStyle} />)}
        </ChartBlock>

        <ChartBlock id="c-bat" title="Battery" unit="%" legend={legendBat} index={idx}
          table={{ columns: ['Time', 'Actual', 'Planned', 'Plain self-use'], rows: tableRows(legendBat) }}>
          {plot('Battery', <LineTimeChart {...common} height={compact ? 132 : 156} yFormat={v => `${v}%`} yMin={0} yMax={100}
            desc="Measured battery level and the two forecast battery levels, in percent."
            series={[
              { id: 'actual-battery', data: batteryLevels.map(p => p.actual), color: t.battery, showMark: false },
              { id: 'forecast-battery', data: batteryLevels.map(p => p.planned), color: t.battery, showMark: false },
              { id: 'forecast-baseline', data: batteryLevels.map(p => p.baseline), color: t.baseline, showMark: false },
            ]} sx={seriesStyle} />)}
        </ChartBlock>

        <ChartBlock id="c-price" title="Prices" unit="c/kWh" legend={legendPrice} index={idx}
          table={{ columns: ['Time', 'Buy', 'Feed-in', 'Action'], rows: points.map((p, i) => [timeLabel(i), ...legendPrice.map(l => l.value(i)), p.action ? action(p.action).label : '–']) }}>
          {plot('Prices', <>
            <LineTimeChart {...common} height={compact ? 125 : 148} yFormat={v => `${v}c`} desc="Buy and feed-in prices in cents per kilowatt hour."
              series={[series('buy', 'buy', t.buy), series('sell', 'sell', t.sell)]} sx={seriesStyle} />
            <Box sx={{ pl: `${MARGIN.left + 44}px`, pr: `${MARGIN.right}px`, mt: 0.5 }}>
              <Box role="img" aria-label="Planned actions on the future part of the timeline" sx={{ position: 'relative', height: 10, borderRadius: 1, overflow: 'hidden' }}>
                {rows.map(r => {
                  const rStart = Date.parse(r.ts);
                  const a = Math.max(now, rStart);
                  const b = Math.min(end, rStart + r.hours * HOUR);
                  if (b <= a) return null;
                  return <Box key={r.ts} title={`${action(r.action).label} from ${f.dateTime(a)}`} sx={{ position: 'absolute', height: '100%',
                    left: `${(a - start) / (end - start) * 100}%`, width: `${(b - a) / (end - start) * 100}%`,
                    bgcolor: (actions as Record<string, string>)[r.action] ?? t.baseline }} />;
                })}
              </Box>
            </Box>
          </>)}
          <Box component="ul" aria-label="Action colours" sx={{ listStyle: 'none', m: 0, mt: 1, p: 0, display: 'flex', flexWrap: 'wrap', columnGap: 2, rowGap: 0.5 }}>
            {Object.entries(ACTIONS).map(([key, value]) => (
              <Box component="li" key={key} sx={{ display: 'flex', alignItems: 'center', gap: 0.5, fontSize: '0.75rem', color: 'text.secondary' }}>
                <Box component="span" aria-hidden sx={{ width: 10, height: 10, borderRadius: 0.5, bgcolor: (actions as Record<string, string>)[key] }} />
                {value.label}
              </Box>
            ))}
          </Box>
        </ChartBlock>
      </Box>
    </Section>
  );
}

function Schedule({ blocks, now, plugins }: { blocks: any[]; now: number; plugins: string[] }) {
  const [open, setOpen] = useState<Set<number>>(new Set());
  const toast = useToast();
  const { actions } = useTokens();
  const vendor = plugins.includes('foxess') ? 'FoxESS' : 'Inverter';
  const toggle = (i: number) => setOpen((s) => { const n = new Set(s); if (n.has(i)) n.delete(i); else n.add(i); return n; });
  async function copy() {
    const text = blocks.map((b, i) => `${f.when(b.start, now, i === 0)} – ${f.when(b.end, now)}: ${action(b.action).label} (${foxMode(b.fox_mode)}), battery ${f.pct(b.soc_end)} at end`).join('\n');
    try {
      await navigator.clipboard.writeText(text);
      toast('Schedule copied.');
    } catch {
      toast('Couldn’t copy. Your browser blocked clipboard access.', 'error');
    }
  }
  return (
    <Section title="Schedule" action={blocks.length > 0 && <Button size="small" startIcon={<ContentCopyRounded />} onClick={copy}>Copy schedule</Button>}>
      {!blocks.length ? <Empty>No schedule yet.</Empty> : (
        <Table size="small" aria-label="Planned schedule" sx={{ '& td, & th': { px: { xs: 1, sm: 2 } } }}>
          <TableHead>
            <TableRow>
              <TableCell sx={{ width: 40 }}><Box component="span" sx={{ position: 'absolute', width: '1px', height: '1px', overflow: 'hidden', clipPath: 'inset(50%)', whiteSpace: 'nowrap' }}>Details</Box></TableCell>
              <TableCell>Starts</TableCell>
              <TableCell>Action</TableCell>
              <TableCell align="right">Battery at end</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {blocks.map((b, i) => {
              const a = action(b.action);
              const isOpen = open.has(i);
              return [
                <TableRow key={i} hover onClick={() => toggle(i)} sx={{ cursor: 'pointer', '& > td': { borderBottom: isOpen ? 0 : undefined } }}>
                  <TableCell>
                    <IconButton size="small" aria-expanded={isOpen} aria-controls={`sched-${i}`} aria-label={`Details for ${a.label} from ${f.when(b.start, now, i === 0)}`}
                      onClick={(e) => { e.stopPropagation(); toggle(i); }}>
                      <ExpandMoreRounded sx={{ transform: isOpen ? 'rotate(180deg)' : 'none', transition: 'transform .2s' }} />
                    </IconButton>
                  </TableCell>
                  <TableCell sx={{ whiteSpace: 'nowrap', fontWeight: i === 0 ? 700 : 400 }}>{f.when(b.start, now, i === 0)}</TableCell>
                  <TableCell>
                    <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
                      <Box aria-hidden sx={{ width: 28, height: 28, borderRadius: 2, display: 'grid', placeItems: 'center', bgcolor: (actions as Record<string, string>)[b.action] + '2E', flexShrink: 0 }}>
                        <a.Icon sx={{ fontSize: 18 }} />
                      </Box>
                      <span>{a.label}</span>
                    </Stack>
                  </TableCell>
                  <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>{f.pct(b.soc_end)}</TableCell>
                </TableRow>,
                <TableRow key={`${i}d`}>
                  <TableCell colSpan={4} sx={{ py: 0, borderBottom: isOpen ? undefined : 0 }}>
                    <Collapse in={isOpen} unmountOnExit id={`sched-${i}`}>
                      <Box sx={{ py: 1.5, pl: { sm: 6 } }}>
                        <Facts rows={[
                          ['Ends', f.when(b.end, now)],
                          ['Why', b.reason],
                          [`${vendor} mode`, foxMode(b.fox_mode)],
                        ]} />
                      </Box>
                    </Collapse>
                  </TableCell>
                </TableRow>,
              ];
            })}
          </TableBody>
        </Table>
      )}
    </Section>
  );
}
