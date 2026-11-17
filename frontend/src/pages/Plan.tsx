import { useMemo, useState, type ReactNode } from 'react';
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
import { ErrorState, Empty, Facts, HelpTip, LoadingBlock, PageHeader, Section, useToast } from '../components/common';
import { BarTimeChart, ChartBlock, LineTimeChart, MARGIN, useTimeAxis, type LegendItem } from '../components/TimeCharts';
import { useTokens } from './Overview';

interface Interval {
  ts: string; hours: number; action: string; load_kwh: number; solar_kwh: number; import_kwh: number; export_kwh: number;
  curtailed_kwh: number; soc_pct: number; buy_rate: number; sell_rate: number; cost: number; baseline_soc_pct: number; baseline_cost: number;
  baseline_curtailed_kwh: number; solar_p10: number | null; solar_p90: number | null; reason: string;
}

export default function Plan() {
  const { status, reloadStatus, can } = useApp();
  const plan = useLoad<{ decision: any; intervals: Interval[] }>(() => get('/api/plan'));
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
  return (
    <Box>
      <PageHeader title="Plan" sub={d ? `Plan made ${f.relative(d.created_at, now)} · next 48 hours` : 'The next 48 hours'}
        actions={can('member') && <Button variant="contained" startIcon={<RefreshRounded />} onClick={replan} disabled={busy}>{busy ? 'Planning…' : 'Re-plan'}</Button>} />
      {plan.error ? <ErrorState message={plan.error} onRetry={plan.reload} /> : !plan.data ? <LoadingBlock height={480} /> :
        !d ? <Empty>No plan has been made yet. The planner runs every few minutes once readings arrive.</Empty> : (
          <Stack spacing={2.5}>
            <Summary d={d} rows={plan.data.intervals} />
            <Charts rows={plan.data.intervals} now={now} />
            <Schedule blocks={status?.advice?.schedule ?? []} now={now} plugins={(status?.systems ?? []).map((s: any) => s.plugin)} />
          </Stack>
        )}
    </Box>
  );
}

function Summary({ d, rows }: { d: any; rows: Interval[] }) {
  const [open, setOpen] = useState(false);
  const { t } = useTokens();
  const s = d.summary;
  const end = new Date(d.created_at).getTime() + 24 * 3600e3;
  const day = rows.filter((r) => new Date(r.ts).getTime() < end);
  const cost = day.reduce((a, r) => a + (r.cost ?? 0), 0);
  const kept = day.reduce((a, r) => a + (r.baseline_curtailed_kwh ?? 0) - (r.curtailed_kwh ?? 0), 0);
  const saving = s.modelled_saving_24h_aud;
  const fig = (label: string, value: string, color?: string, sub?: ReactNode) => (
    <Box sx={{ minWidth: 0 }}>
      <Typography variant="caption" component="h3" color="text.secondary" sx={{ fontWeight: 600 }}>{label}</Typography>
      <Typography variant="h4" component="p" sx={{ color: color ?? 'text.primary', fontSize: { xs: '1.5rem', sm: '1.75rem' } }}>{value}</Typography>
      {sub && <Typography variant="caption" color="text.secondary">{sub}</Typography>}
    </Box>
  );
  return (
    <Section title="Summary" help={<HelpTip title="Modelled, not measured" label="modelled figures">
      These come from the planner’s forecast of your solar and home use. “Plain self-use” is the same forecast run through an inverter left on
      its default mode. Real results are measured in History.
    </HelpTip>}>
      <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: '1fr', sm: 'repeat(3, 1fr)' } }}>
        {fig('Expected cost, next 24 h', cost < -0.005 ? `${f.money(-cost)} credit` : f.money(cost), cost < -0.005 ? t.batteryText : undefined, cost < -0.005 ? 'exports earn more than imports cost · modelled' : 'energy only, modelled')}
        {fig('Saving vs plain self-use', f.money(saving), saving > 0.005 ? t.batteryText : undefined, 'next 24 h, modelled')}
        {fig('Solar kept from clipping', f.kwh(Math.max(0, kept)), kept > 0.05 ? t.solarText : undefined, 'next 24 h, modelled')}
      </Box>
      <Button size="small" onClick={() => setOpen(!open)} aria-expanded={open} aria-controls="plan-details" sx={{ mt: 1.5, ml: -1 }}
        endIcon={<ExpandMoreRounded sx={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .2s' }} />}>Details</Button>
      <Collapse in={open} id="plan-details">
        <Box sx={{ pt: 1.5, display: 'grid', gap: 2, gridTemplateColumns: { xs: '1fr', md: '1fr 1fr' } }}>
          <Facts rows={[
            ['Plan cost, 48 h', `${f.money(-s.plan_cash_aud)} (self-use ${f.money(-s.baseline_cash_aud)})`],
            ['Saving, 48 h', f.money(s.modelled_saving_aud)],
            ['Import / export', `${f.kwh(s.import_kwh)} / ${f.kwh(s.export_kwh)}`],
            ['Battery wear', f.money(s.wear_aud)],
            ['Battery at end', `${f.kwh(s.end_stored_kwh)} (self-use ${f.kwh(s.baseline_end_stored_kwh)})`],
            ['Stored energy valued at', `${f.cents(s.terminal_value_per_kwh)}/kWh`],
          ]} />
          <Facts rows={[
            ['Solver', `${s.solver} · ${s.solver_status === 0 ? 'optimal' : s.solver_message} · ${f.n(s.solve_seconds, 2)} s`],
            ['Planner version', d.version],
            ['Tariff', d.tariff_id],
            ['Forecast', s.confidence],
            ['Limitations', <Box component="ul" sx={{ m: 0, pl: 2 }}>{(s.limitations ?? []).map((l: string) => <li key={l}>{l}</li>)}</Box>],
          ]} />
        </Box>
      </Collapse>
    </Section>
  );
}

function Charts({ rows, now }: { rows: Interval[]; now: number }) {
  const { t, actions } = useTokens();
  const compact = useMediaQuery((th: Theme) => th.breakpoints.down('sm'));
  const [hi, setHi] = useState<number | null>(null);
  const ts = useMemo(() => rows.map((r) => new Date(r.ts).getTime()), [rows]);
  const { axis, boundaries } = useTimeAxis(ts, now, compact);
  const kw = (kwh: number | null | undefined, r: Interval) => (r.hours > 0 && kwh != null ? kwh / r.hours : 0);
  const data = useMemo(() => {
    const solar = rows.map((r) => kw(r.solar_kwh, r));
    const p10 = rows.map((r) => kw(r.solar_p10 ?? r.solar_kwh, r));
    const p90 = rows.map((r) => kw(r.solar_p90 ?? r.solar_kwh, r));
    return {
      solar, p10, p90, band: p90.map((v, i) => Math.max(0, v - p10[i])),
      home: rows.map((r) => kw(r.load_kwh, r)),
      imp: rows.map((r) => kw(r.import_kwh, r)),
      exp: rows.map((r) => -kw(r.export_kwh, r)),
      soc: rows.map((r) => r.soc_pct),
      base: rows.map((r) => r.baseline_soc_pct),
      buy: rows.map((r) => r.buy_rate * 100),
      sell: rows.map((r) => r.sell_rate * 100),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows]);
  const idx = hi ?? 0;
  const timeLabel = (i: number) => `${f.day(ts[i])} ${f.time(ts[i])}`;
  const common = { xAxis: axis, boundaries, hi, setHi, muted: t.baseline };
  const kwFmt = (v: number) => `${f.n(Math.abs(v), Math.abs(v) < 10 ? 1 : 0)}`;
  const cur = rows[idx];
  const a = action(cur?.action);

  // Action strip: merge consecutive equal actions.
  const blocks: { action: string; n: number; start: number }[] = [];
  rows.forEach((r, i) => {
    const last = blocks[blocks.length - 1];
    if (last && last.action === r.action) last.n += 1;
    else blocks.push({ action: r.action, n: 1, start: i });
  });

  const legendSolar: LegendItem[] = [
    { label: 'Solar forecast', color: t.solar, kind: 'line', value: (i) => f.kw(data.solar[i]) },
    { label: 'Likely range', color: t.solar, kind: 'band', value: (i) => `${f.n(data.p10[i])}–${f.n(data.p90[i])}` },
    { label: 'Home use', color: t.home, kind: 'line', value: (i) => f.kw(data.home[i]) },
  ];
  const legendGrid: LegendItem[] = [
    { label: 'Import', color: t.gridIn, kind: 'bar', value: (i) => f.kw(data.imp[i]) },
    { label: 'Export', color: t.gridOut, kind: 'bar', value: (i) => f.kw(-data.exp[i]) },
  ];
  const legendBat: LegendItem[] = [
    { label: 'Planned', color: t.battery, kind: 'line', value: (i) => f.pct(data.soc[i]) },
    { label: 'Plain self-use', color: t.baseline, kind: 'dashed', value: (i) => f.pct(data.base[i]) },
  ];
  const legendPrice: LegendItem[] = [
    { label: 'Buy', color: t.buy, kind: 'step', value: (i) => `${f.n(data.buy[i])}c` },
    { label: 'Feed-in', color: t.sell, kind: 'step', value: (i) => `${f.n(data.sell[i])}c` },
  ];

  return (
    <Section title="Next 48 hours" sx={{ overflow: 'visible' }}>
      <Box sx={{ position: 'sticky', top: { xs: 56, md: 64 }, zIndex: 2, bgcolor: 'background.paper', py: 1, mx: -1, px: 1, borderBottom: 1, borderColor: 'divider' }}
>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap', rowGap: 0.5 }}>
          <Typography variant="body2" sx={{ fontWeight: 700 }}>{hi == null ? `Now · ${f.time(ts[0])}` : timeLabel(idx)}</Typography>
          <Chip size="small" icon={<a.Icon />} label={a.label}
            sx={{ bgcolor: (actions as Record<string, string>)[cur?.action] + '33', '& .MuiChip-icon': { color: 'text.primary' } }} />
          <Typography variant="caption" color="text.secondary" sx={{ display: { xs: 'none', sm: 'block' } }}>
            {hi == null ? 'Point at or tap a chart to read any half-hour.' : ''}
          </Typography>
        </Stack>
      </Box>

      <ChartBlock id="c-solar" title="Solar & home" unit="kW" legend={legendSolar} index={idx}
        table={{ columns: ['Time', 'Solar kW', 'Range kW', 'Home kW'], rows: rows.map((_, i) => [timeLabel(i), f.n(data.solar[i]), `${f.n(data.p10[i])}–${f.n(data.p90[i])}`, f.n(data.home[i])]) }}>
        <LineTimeChart {...common} height={compact ? 170 : 200} yFormat={kwFmt} yMin={0} desc="Forecast solar generation with its likely range, and forecast home use, in kilowatts."
          series={[
            { id: 'p10', data: data.p10, stack: 'band', area: false, showMark: false, color: 'transparent', disableHighlight: true },
            { id: 'band', data: data.band, stack: 'band', area: true, showMark: false, color: t.solar, disableHighlight: true },
            { id: 'solar', data: data.solar, showMark: false, color: t.solar, curve: 'monotoneX' },
            { id: 'home', data: data.home, showMark: false, color: t.home, curve: 'monotoneX' },
          ]}
          sx={{ '& .MuiLineChart-area[data-series="band"]': { fillOpacity: 0.24 }, '& .MuiLineChart-line': { strokeWidth: 2.25 }, '& .MuiLineChart-line[data-series="band"], & .MuiLineChart-line[data-series="p10"]': { strokeWidth: 0 } }} />
      </ChartBlock>

      <ChartBlock id="c-grid" title="Grid" unit="kW, imports up, exports down" legend={legendGrid} index={idx}
        table={{ columns: ['Time', 'Import kW', 'Export kW'], rows: rows.map((_, i) => [timeLabel(i), f.n(data.imp[i]), f.n(-data.exp[i])]) }}>
        <BarTimeChart {...common} height={compact ? 120 : 140} yFormat={kwFmt} desc="Planned grid import as bars above zero and export below zero, in kilowatts."
          series={[
            { id: 'imp', data: data.imp, stack: 'g', color: t.gridIn },
            { id: 'exp', data: data.exp, stack: 'g', color: t.gridOut },
          ]} />
      </ChartBlock>

      <ChartBlock id="c-bat" title="Battery" unit="%" legend={legendBat} index={idx}
        table={{ columns: ['Time', 'Planned %', 'Self-use %'], rows: rows.map((_, i) => [timeLabel(i), f.n(data.soc[i], 0), f.n(data.base[i], 0)]) }}>
        <LineTimeChart {...common} height={compact ? 130 : 150} yFormat={(v) => `${v}%`} yMin={0} yMax={100}
          desc="Planned battery level compared with plain self-use, in percent."
          series={[
            { id: 'soc', data: data.soc, showMark: false, color: t.battery, area: true, curve: 'monotoneX' },
            { id: 'base', data: data.base, showMark: false, color: t.baseline, curve: 'monotoneX' },
          ]}
          sx={{ '& .MuiLineChart-line': { strokeWidth: 2.25 }, '& .MuiLineChart-line[data-series="base"]': { strokeDasharray: '6 4' }, '& .MuiLineChart-area[data-series="soc"]': { fillOpacity: 0.18 } }} />
      </ChartBlock>

      <ChartBlock id="c-price" title="Prices" unit="c/kWh" legend={legendPrice} index={idx}
        table={{ columns: ['Time', 'Buy c', 'Feed-in c', 'Action'], rows: rows.map((r, i) => [timeLabel(i), f.n(data.buy[i]), f.n(data.sell[i]), action(r.action).label]) }}>
        <LineTimeChart {...common} height={compact ? 120 : 140} yFormat={(v) => `${v}c`} yMin={0}
          desc="Buy and feed-in prices in cents per kilowatt hour."
          series={[
            { id: 'buy', data: data.buy, showMark: false, color: t.buy, curve: 'stepAfter' },
            { id: 'sell', data: data.sell, showMark: false, color: t.sell, curve: 'stepAfter' },
          ]}
          sx={{ '& .MuiLineChart-line': { strokeWidth: 2 } }} />
        <Box sx={{ pl: `${MARGIN.left + 44}px`, pr: `${MARGIN.right}px`, mt: 0.5 }}>
          <Box role="img" aria-label="Planned action over time, see the schedule below" sx={{ display: 'flex', height: 12, borderRadius: 1, overflow: 'hidden' }}>
            {blocks.map((b) => (
              <Box key={b.start} title={`${action(b.action).label} from ${timeLabel(b.start)}`}
                sx={{ flexGrow: b.n, flexBasis: 0, bgcolor: (actions as Record<string, string>)[b.action] ?? t.baseline, borderRight: '1px solid', borderColor: 'background.paper' }} />
            ))}
          </Box>
        </Box>
        <Box component="ul" aria-label="Action colours" sx={{ listStyle: 'none', m: 0, mt: 1, p: 0, display: 'flex', flexWrap: 'wrap', columnGap: 2, rowGap: 0.5 }}>
          {Object.entries(ACTIONS).map(([k, v]) => (
            <Box component="li" key={k} sx={{ display: 'flex', alignItems: 'center', gap: 0.5, fontSize: '0.75rem', color: 'text.secondary' }}>
              <Box component="span" aria-hidden sx={{ width: 10, height: 10, borderRadius: 0.5, bgcolor: (actions as Record<string, string>)[k] }} />
              {v.label}
            </Box>
          ))}
        </Box>
      </ChartBlock>
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
