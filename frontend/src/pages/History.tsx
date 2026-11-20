import { useEffect, useMemo, useState, type FormEvent } from 'react';
import Box from '@mui/material/Box';
import Tabs from '@mui/material/Tabs';
import Tab from '@mui/material/Tab';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';
import ToggleButton from '@mui/material/ToggleButton';
import ToggleButtonGroup from '@mui/material/ToggleButtonGroup';
import Button from '@mui/material/Button';
import List from '@mui/material/List';
import ListItem from '@mui/material/ListItem';
import ListItemButton from '@mui/material/ListItemButton';
import ListItemText from '@mui/material/ListItemText';
import Dialog from '@mui/material/Dialog';
import DialogTitle from '@mui/material/DialogTitle';
import DialogContent from '@mui/material/DialogContent';
import DialogActions from '@mui/material/DialogActions';
import TextField from '@mui/material/TextField';
import MenuItem from '@mui/material/MenuItem';
import Alert from '@mui/material/Alert';
import Chip from '@mui/material/Chip';
import Switch from '@mui/material/Switch';
import FormControlLabel from '@mui/material/FormControlLabel';
import Table from '@mui/material/Table';
import TableHead from '@mui/material/TableHead';
import TableBody from '@mui/material/TableBody';
import TableRow from '@mui/material/TableRow';
import TableCell from '@mui/material/TableCell';
import useMediaQuery from '@mui/material/useMediaQuery';
import type { Theme } from '@mui/material/styles';
import { BarChart } from '@mui/x-charts/BarChart';
import { LineChart } from '@mui/x-charts/LineChart';
import AddRounded from '@mui/icons-material/AddRounded';
import BlockRounded from '@mui/icons-material/BlockRounded';
import { get, post, del, errorText } from '../lib/api';
import { useLoad, go } from '../lib/hooks';
import { useApp } from '../lib/app';
import { action } from '../lib/actions';
import * as f from '../lib/fmt';
import { ConfirmDialog, Empty, ErrorState, Facts, HelpTip, LoadingBlock, PageHeader, Section, useToast } from '../components/common';
import { useTokens } from './Overview';

const TABS = [
  { key: 'trends', label: 'Trends' },
  { key: 'accuracy', label: 'Forecast accuracy' },
  { key: 'decisions', label: 'Decisions' },
  { key: 'notes', label: 'Notes & exclusions' },
];

export default function History({ tab }: { tab?: string }) {
  const current = TABS.some((t) => t.key === tab) ? tab! : 'trends';
  return (
    <Box>
      <PageHeader title="History" sub="What actually happened, measured from your readings." />
      <Tabs value={current} onChange={(_, v) => go(`history/${v}`)} variant="scrollable" allowScrollButtonsMobile aria-label="History sections"
        sx={{ mb: 2.5, borderBottom: 1, borderColor: 'divider' }}>
        {TABS.map((t) => <Tab key={t.key} value={t.key} label={t.label} id={`htab-${t.key}`} aria-controls={`hpanel-${t.key}`} />)}
      </Tabs>
      <Box role="tabpanel" id={`hpanel-${current}`} aria-labelledby={`htab-${current}`}>
        {current === 'trends' && <Trends />}
        {current === 'accuracy' && <Accuracy />}
        {current === 'decisions' && <Decisions />}
        {current === 'notes' && <Notes />}
      </Box>
    </Box>
  );
}

function Trends() {
  const [days, setDays] = useState(7);
  const { t } = useTokens();
  const compact = useMediaQuery((th: Theme) => th.breakpoints.down('sm'));
  const live = useLoad<{ readings: any[] }>(() => get('/api/live?hours=24'));
  const out = useLoad<{ days: any[] }>(() => get(`/api/outcomes?days=${days}`), [days]);
  const liveRows = live.data?.readings ?? [];
  const thin = useMemo(() => liveRows.filter((_, i) => i % Math.max(1, Math.round(liveRows.length / 144)) === 0), [liveRows]);
  const ds = out.data?.days ?? [];
  const sum = (k: string) => ds.reduce((a, d) => a + (d[k] ?? 0), 0);
  const avg = (rows: any[], k: string) => (rows.length ? rows.reduce((a, d) => a + (d[k] ?? 0), 0) / rows.length : null);
  const weekend = ds.filter((d) => { const wd = new Date(`${d.day}T12:00:00Z`).getUTCDay(); return wd === 0 || wd === 6; });
  const weekday = ds.filter((d) => !weekend.includes(d));

  return (
    <Stack spacing={2.5}>
      <Section title="Last 24 hours" help={<HelpTip label="last 24 hours">Measured power from your system, every reading. Gaps mean no data, not zero.</HelpTip>}>
        {live.error ? <ErrorState message={live.error} onRetry={live.reload} /> : !live.data ? <LoadingBlock height={220} /> : !thin.length ? <Empty>No readings in the last day.</Empty> : (
          <LineChart height={compact ? 200 : 240} margin={{ left: 4, right: 12, top: 10, bottom: 2 }} skipAnimation
            xAxis={[{ scaleType: 'time', data: thin.map((r) => new Date(r.ts)), valueFormatter: (v: Date) => f.hourTick(v), tickNumber: compact ? 4 : 8, height: 24 }]}
            yAxis={[{ width: 40, valueFormatter: (v: number) => `${v}`, label: undefined }]}
            series={[
              { id: 'solar', label: 'Solar kW', data: thin.map((r) => r.solar_kw), color: t.solar, showMark: false, area: true },
              { id: 'home', label: 'Home kW', data: thin.map((r) => r.load_kw), color: t.home, showMark: false },
              { id: 'grid', label: 'Grid import kW', data: thin.map((r) => r.import_kw), color: t.gridIn, showMark: false },
            ]}
            grid={{ horizontal: true }}
            slotProps={{ legend: { position: { vertical: 'top', horizontal: 'start' } } }}
            sx={{ '& .MuiLineChart-area': { fillOpacity: 0.18 } }} />
        )}
      </Section>

      <Section title="Daily cost" action={
        <ToggleButtonGroup size="small" exclusive value={days} onChange={(_, v) => v && setDays(v)} aria-label="Period">
          {[7, 30, 90].map((n) => <ToggleButton key={n} value={n}>{n} days</ToggleButton>)}
        </ToggleButtonGroup>}>
        {out.error ? <ErrorState message={out.error} onRetry={out.reload} /> : !out.data ? <LoadingBlock height={240} /> : !ds.length ? <Empty>No complete days yet.</Empty> : (
          <>
            <BarChart height={240} margin={{ left: 4, right: 12, top: 10, bottom: 2 }} skipAnimation
              xAxis={[{ scaleType: 'band', data: ds.map((d) => d.day), valueFormatter: (v: string) => f.day(`${v}T12:00:00`), height: 24 }]}
              yAxis={[{ width: 48, valueFormatter: (v: number) => `$${v}` }]}
              series={[
                { label: 'Actual', data: ds.map((d) => d.net_aud), color: t.battery, valueFormatter: (v) => f.money(v) },
                { label: 'Plain self-use', data: ds.map((d) => d.self_use_net_aud), color: t.baseline, valueFormatter: (v) => f.money(v) },
                { label: 'No solar', data: ds.map((d) => d.no_solar_net_aud), color: t.gridIn, valueFormatter: (v) => f.money(v) },
              ]}
              grid={{ horizontal: true }}
              slotProps={{ legend: { position: { vertical: 'top', horizontal: 'start' } } }} />
            <Typography variant="caption" color="text.secondary">Estimated from readings, not your bill. Self-use and no-solar are replays of the same days.</Typography>
          </>
        )}
      </Section>

      {ds.length > 0 && (
        <Box sx={{ display: 'grid', gap: 2.5, gridTemplateColumns: { xs: '1fr', md: '1fr 1fr' } }}>
          <Section title={`Totals, last ${ds.length} days`}>
            <Facts rows={[
              ['Net cost', f.money(sum('net_aud'))],
              ['Saving vs self-use', `${f.money(sum('saving_vs_self_use_aud'))} (estimated)`],
              ['Solar', f.kwh(sum('solar_kwh'), 0)],
              ['Home use', f.kwh(sum('load_kwh'), 0)],
              ['Imported', f.kwh(sum('import_kwh'), 0)],
              ['Exported', f.kwh(sum('export_kwh'), 0)],
            ]} />
          </Section>
          <Section title="Weekday vs weekend">
            <Table size="small">
              <TableHead><TableRow><TableCell>Daily average</TableCell><TableCell align="right">Weekday</TableCell><TableCell align="right">Weekend</TableCell></TableRow></TableHead>
              <TableBody>
                {[['Cost', 'net_aud', f.money], ['Home use', 'load_kwh', (v: number) => f.kwh(v)], ['Solar', 'solar_kwh', (v: number) => f.kwh(v)]].map(([l, k, fm]: any) => (
                  <TableRow key={k}><TableCell component="th" scope="row">{l}</TableCell><TableCell align="right">{fm(avg(weekday, k))}</TableCell><TableCell align="right">{fm(avg(weekend, k))}</TableCell></TableRow>
                ))}
              </TableBody>
            </Table>
          </Section>
        </Box>
      )}
    </Stack>
  );
}

interface ComparisonPoint {
  end: string;
  forecast_solar: number | null;
  actual_solar: number | null;
  forecast_load: number | null;
  actual_load: number | null;
  planned_soc: number | null;
  actual_soc: number | null;
}

interface ComparisonData {
  runs: { id: number; created_at: string; day?: string; hour?: number; adopted?: boolean }[];
  current_day?: string;
  forecast_id: number | null;
  mode?: string;
  points: ComparisonPoint[];
}

type EnergyKey = 'forecast_solar' | 'actual_solar' | 'forecast_load' | 'actual_load';

function energyBins(points: ComparisonPoint[], hours: number) {
  const groups = new Map<number, ComparisonPoint[]>();
  for (const point of points) {
    const start = Date.parse(point.end) - 30 * 60000;
    const local = f.parts(start);
    const minutes = (Number(local.hour) % hours) * 60 + Number(local.minute);
    const key = start - minutes * 60000;
    const group = groups.get(key) ?? [];
    group.push(point);
    groups.set(key, group);
  }
  return [...groups.values()].map(slots => {
    const sum = (field: EnergyKey) => slots.some(p => p[field] == null) ? null : slots.reduce((total, p) => total + p[field]!, 0);
    return { start: Date.parse(slots[0].end) - 30 * 60000, end: Date.parse(slots[slots.length - 1].end), sum };
  });
}

function ForecastCharts() {
  const [selected, setSelected] = useState<number | null>(null);
  const [hours, setHours] = useState(1);
  const [allRuns, setAllRuns] = useState(false);
  const query = (selected === null ? 'today=true' : `run_id=${selected}`) + `&all_runs=${allRuns}`;
  const comparison = useLoad<ComparisonData>(() => get('/api/forecast/comparison?' + query), [query]);
  const { t } = useTokens();
  const compact = useMediaQuery((th: Theme) => th.breakpoints.down('sm'));
  useEffect(() => {
    const timer = setInterval(() => { if (!document.hidden) void comparison.reload(); }, 60000);
    return () => clearInterval(timer);
  }, [comparison.reload]);
  const data = comparison.data;
  const bins = useMemo(() => energyBins(data?.points ?? [], hours), [data, hours]);
  const charts = [
    { title: 'Electricity use', forecast: 'forecast_load', measured: 'actual_load', color: t.home },
    { title: 'Solar generation', forecast: 'forecast_solar', measured: 'actual_solar', color: t.solar },
  ] as const;
  const tickEvery = (n: number) => Math.max(1, Math.ceil(n / (compact ? 5 : 12)));

  return <Section title="Estimated and actual">
    {comparison.error ? <ErrorState message={comparison.error} onRetry={comparison.reload} /> : !data ? <LoadingBlock /> : <Stack spacing={2}>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5}>
        <TextField select label="Forecast" value={selected ?? ''} onChange={e => setSelected(e.target.value === '' ? null : Number(e.target.value))}
          sx={{ width: { xs: '100%', sm: 340 } }} slotProps={{ inputLabel: { shrink: true }, select: { native: true } }}>
          <option value="">Latest forecast before each interval</option>
          {data.runs.map(run => <option key={run.id} value={run.id}>{f.dateTime(run.created_at)}{run.adopted ? ' · adopted plan' : ' · checkpoint'}</option>)}
        </TextField>
        <Button variant="outlined" disabled={!data.runs.some(r => r.day === data.current_day && r.hour === 6)}
          onClick={() => setSelected([...data.runs].reverse().find(r => r.day === data.current_day && r.hour === 6)?.id ?? null)}>6 am checkpoint</Button>
        <ToggleButtonGroup size="small" exclusive value={hours} onChange={(_, value) => value && setHours(value)} aria-label="Energy chart interval">
          <ToggleButton value={1}>Hourly</ToggleButton>
          <ToggleButton value={3}>3-hourly</ToggleButton>
        </ToggleButtonGroup>
      </Stack>
      <FormControlLabel control={<Switch checked={allRuns} onChange={e => setAllRuns(e.target.checked)} />} label="Show every background forecast" />
      {(selected !== data.forecast_id) ? <LoadingBlock /> : !bins.length ?
        <Empty>Waiting for the first complete half-hour to finish.</Empty> : <>
        <Typography variant="body2" color="text.secondary">
          {selected === null ? 'Each half-hour uses the latest forecast saved before it began. ' : 'This comparison uses the selected run’s first 24 hours. '}
          Use includes EV charging. Bars are based on elapsed time, and gaps indicate unavailable data.
        </Typography>
        {charts.map(chart => <Box key={chart.title} sx={{ minWidth: 0 }}>
          <Typography component="h3" variant="subtitle1" sx={{ fontWeight: 700 }}>{chart.title} (kWh)</Typography>
          <BarChart height={compact ? 188 : 215} skipAnimation title={chart.title + ' comparison'}
            margin={{ top: 8, right: 16, left: 4, bottom: 0 }}
            xAxis={[{ scaleType: 'band', data: bins.map(b => b.start), tickLabelInterval: (_, i) => i % tickEvery(bins.length) === 0,
              valueFormatter: (v: number, ctx) => ctx.location === 'tick' ? f.time(v) : `${f.dateTime(v)} to ${f.time(bins.find(b => b.start === v)?.end)}`, height: 28 }]}
            yAxis={[{ min: 0, width: 42 }]}
            series={[
              { label: 'Estimated', data: bins.map(b => b.sum(chart.forecast)), color: t.baseline, valueFormatter: v => f.kwh(v, 2) },
              { label: 'Actual', data: bins.map(b => b.sum(chart.measured)), color: chart.color, valueFormatter: v => f.kwh(v, 2) },
            ]} grid={{ horizontal: true }} />
        </Box>)}
        {data.points.some(p => p.planned_soc != null || p.actual_soc != null) && <Box sx={{ minWidth: 0 }}>
          <Typography component="h3" variant="subtitle1" sx={{ fontWeight: 700 }}>Battery level (%)</Typography>
          {data.mode === 'monitor' && <Typography variant="caption" color="text.secondary">Monitoring mode was active for this plan.</Typography>}
          <LineChart height={compact ? 185 : 205} skipAnimation title="Battery level comparison"
            margin={{ top: 8, right: 16, left: 4, bottom: 0 }}
            xAxis={[{ scaleType: 'time', data: data.points.map(p => new Date(p.end)), tickNumber: compact ? 4 : 8,
              ...(data.points.length === 1 ? { min: new Date(Date.parse(data.points[0].end) - 900000), max: new Date(Date.parse(data.points[0].end) + 900000) } : {}),
              valueFormatter: (v: Date, ctx) => ctx.location === 'tick' ? f.time(v) : f.dateTime(v), height: 28 }]}
            yAxis={[{ min: 0, max: 100, width: 42 }]}
            series={[
              ...(selected !== null ? [{ label: 'Planned', data: data.points.map(p => p.planned_soc), color: t.baseline, valueFormatter: (v: number | null) => f.pct(v), showMark: data.points.length < 3 }] : []),
              { label: 'Actual', data: data.points.map(p => p.actual_soc), color: t.battery, valueFormatter: v => f.pct(v), showMark: data.points.length < 3 },
            ]} grid={{ horizontal: true }} />
        </Box>}
      </>}
    </Stack>}
  </Section>;
}

function Accuracy() {
  const [allRuns, setAllRuns] = useState(false);
  const acc = useLoad<any>(() => get(`/api/forecast/accuracy?all_runs=${allRuns}`), [allRuns]);
  useEffect(() => {
    const timer = setInterval(() => { if (!document.hidden) void acc.reload(); }, 60000);
    return () => clearInterval(timer);
  }, [acc.reload]);
  if (acc.error) return <ErrorState message={acc.error} onRetry={acc.reload} />;
  if (!acc.data) return <LoadingBlock height={200} />;
  const ev = acc.data.evaluations as any[];
  const lm = acc.data.load_model ?? {};
  const learning = (lm.days ?? 0) < 14;
  return (
    <Stack spacing={2.5}>
      <ForecastCharts />
      <Section title="Model">
        {learning && <Alert severity="info" sx={{ mb: 1.5 }}>Still learning: {f.n(lm.days ?? 0, 0)} days of data so far. Forecasts settle after about two weeks.</Alert>}
        <Facts rows={[['Load history', `${f.n(lm.days ?? 0, 0)} days`], ['Hot / cold samples', `${lm.hot_samples ?? '–'} / ${lm.cold_samples ?? '–'}`]]} />
      </Section>
      <Section title="Forecast error by run">
        <FormControlLabel control={<Switch checked={allRuns} onChange={e => setAllRuns(e.target.checked)} />} label="Include every background evaluation" />
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>Each error is the average difference in kWh per measured half-hour during the forecast’s first day. Smaller is better.</Typography>
        {!ev.length ? <Empty>No forecasts have been scored yet. Each forecast is checked once its day has passed.</Empty> : (
          <Box role="region" aria-label="Forecast errors" tabIndex={0} sx={{ overflow: 'auto', maxHeight: 420 }}>
            <Table size="small" stickyHeader>
              <TableHead><TableRow><TableCell>Forecast made</TableCell><TableCell>House error (kWh)</TableCell><TableCell>Solar error (kWh)</TableCell><TableCell>Half-hours scored (house / solar)</TableCell></TableRow></TableHead>
              <TableBody>{ev.slice().reverse().map((r) => (
                <TableRow key={r.forecast_id}>
                  <TableCell>{f.dateTime(r.created_at)}</TableCell>
                  <TableCell>{f.n(r.house_mae_kwh, 2)}</TableCell>
                  <TableCell>{f.n(r.solar_mae_kwh, 2)}</TableCell>
                  <TableCell>{r.samples ?? '–'} / {r.solar_samples ?? '–'}</TableCell>
                </TableRow>
              ))}</TableBody>
            </Table>
          </Box>
        )}
      </Section>
    </Stack>
  );
}

function Decisions() {
  const [allRuns, setAllRuns] = useState(false);
  const list = useLoad<any[]>(() => get(`/api/decisions?limit=50&all_runs=${allRuns}`), [allRuns]);
  const [selected, setSel] = useState<number | null>(null);
  const sel = selected ?? list.data?.[0]?.id;
  const detail = useLoad<any>(() => (sel ? get(`/api/decisions/${sel}`) : Promise.resolve(null)), [sel]);
  const cmds = useLoad<any[]>(() => get('/api/commands'));
  return (
    <Box sx={{ display: 'grid', gap: 2.5, gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: '320px minmax(0, 1fr)' } }}>
      <Section title="Decisions">
        <FormControlLabel control={<Switch checked={allRuns} onChange={e => { setAllRuns(e.target.checked); setSel(null); }} />} label="Include background candidates" />
        {list.error ? <ErrorState message={list.error} onRetry={list.reload} /> : !list.data ? <LoadingBlock /> : !list.data.length ? <Empty>Decisions appear here when the planner runs.</Empty> : (
          <List dense disablePadding sx={{ maxHeight: 520, overflow: 'auto' }}>
            {list.data.map((d) => (
              <ListItemButton key={d.id} selected={sel === d.id} onClick={() => setSel(d.id)}>
                <ListItemText primary={`${f.dateTime(d.created_at)} · ${action(d.summary?.action).label}`} secondary={`#${d.id} · ${d.mode}`} />
              </ListItemButton>
            ))}
          </List>
        )}
      </Section>
      <Stack spacing={2.5}>
        <Section title={sel ? `Decision #${sel}` : 'Pick a decision'}>
          {!sel ? <Empty>Choose a decision to see every interval and its reason.</Empty> : detail.error ? <ErrorState message={detail.error} onRetry={detail.reload} /> : detail.data?.id !== sel ? <LoadingBlock /> : (
            <>
              <Typography variant="body2" sx={{ mb: 1.5 }}>{detail.data.summary?.reason}</Typography>
              <Box role="region" aria-label="Decision intervals" tabIndex={0} sx={{ overflow: 'auto', maxHeight: 420 }}>
                <Table size="small" stickyHeader>
                  <TableHead><TableRow><TableCell>Time</TableCell><TableCell>Action</TableCell><TableCell>Battery</TableCell><TableCell>Why</TableCell></TableRow></TableHead>
                  <TableBody>{detail.data.intervals.map((r: any) => (
                    <TableRow key={r.ts}><TableCell sx={{ whiteSpace: 'nowrap' }}>{f.dateTime(r.ts)}</TableCell><TableCell>{action(r.action).label}</TableCell>
                      <TableCell>{f.pct(r.soc_pct)}</TableCell><TableCell sx={{ minWidth: 240 }}>{r.reason}</TableCell></TableRow>
                  ))}</TableBody>
                </Table>
              </Box>
            </>
          )}
        </Section>
        <Section title="Commands sent">
          {cmds.error ? <ErrorState message={cmds.error} /> : !cmds.data ? <LoadingBlock /> : !cmds.data.length ? <Empty>No commands yet. kWattson only sends commands in automatic mode.</Empty> : (
            <List dense>{cmds.data.slice(0, 30).map((c) => <ListItem key={c.id}><ListItemText primary={`${f.dateTime(c.created_at ?? c.ts)} · ${c.status ?? ''}`} secondary={[c.payload?.system, c.action?.replaceAll('_', ' '), c.payload?.reason || c.payload?.why].filter(Boolean).join(' · ')} /></ListItem>)}</List>
          )}
        </Section>
      </Stack>
    </Box>
  );
}

const NOTE_KINDS = [['note', 'Note'], ['guests', 'Guests'], ['appliance', 'New appliance'], ['outage', 'Outage'], ['maintenance', 'Maintenance']];

function Notes() {
  const { can } = useApp();
  const toast = useToast();
  const notes = useLoad<any[]>(() => get('/api/annotations'));
  const excl = useLoad<any[]>(() => get('/api/exclusions'));
  const [dialog, setDialog] = useState<'note' | 'exclude' | null>(null);
  const [confirm, setConfirm] = useState<{ path: string; label: string; reload: () => void } | null>(null);
  const [busy, setBusy] = useState(false);
  const items = [
    ...(notes.data ?? []).map((n) => ({ key: `n${n.id}`, start: n.start, end: n.end, text: n.text, tag: NOTE_KINDS.find((k) => k[0] === n.kind)?.[1] ?? n.kind, path: `/api/annotations/${n.id}`, reload: notes.reload, exclusion: false })),
    ...(excl.data ?? []).map((x) => ({ key: `x${x.id}`, start: x.start, end: x.end, text: x.reason, tag: 'Excluded data', path: `/api/exclusions/${x.id}`, reload: excl.reload, exclusion: true })),
  ].sort((a, b) => (a.start < b.start ? 1 : -1));
  async function remove() {
    if (!confirm) return;
    setBusy(true);
    try {
      await del(confirm.path);
      toast('Deleted.');
      confirm.reload();
      setConfirm(null);
    } catch (e) {
      toast(errorText(e), 'error');
    } finally {
      setBusy(false);
    }
  }
  return (
    <Section title="Notes & exclusions" action={can('member') && (
      <Stack direction="row" spacing={1}>
        <Button size="small" variant="outlined" startIcon={<AddRounded />} onClick={() => setDialog('note')}>Add note</Button>
        <Button size="small" variant="outlined" startIcon={<BlockRounded />} onClick={() => setDialog('exclude')}>Exclude bad data</Button>
      </Stack>)}>
      {notes.error || excl.error ? <ErrorState message={(notes.error || excl.error)!} /> : !notes.data || !excl.data ? <LoadingBlock /> : !items.length ? (
        <Empty>No notes yet. Note guests, outages or new appliances so the forecasts can make sense of odd days.</Empty>
      ) : (
        <List disablePadding>
          {items.map((it) => (
            <ListItem key={it.key} divider disableGutters secondaryAction={can('member') && <Button size="small" color="error" onClick={() => setConfirm({ path: it.path, label: it.text, reload: it.reload })}>Delete</Button>} sx={{ pr: 10 }}>
              <ListItemText primary={<>{it.text} <Chip size="small" label={it.tag} color={it.exclusion ? 'warning' : 'default'} variant="outlined" sx={{ ml: 0.5, height: 20 }} /></>}
                secondary={it.end ? `${f.dateTime(it.start)} to ${f.dateTime(it.end)}` : f.dateTime(it.start)} />
            </ListItem>
          ))}
        </List>
      )}
      {dialog && <NoteDialog kind={dialog} onClose={() => setDialog(null)} onDone={() => { setDialog(null); notes.reload(); excl.reload(); }} />}
      <ConfirmDialog open={!!confirm} title="Delete this?" confirm="Delete" danger busy={busy} onConfirm={remove} onClose={() => setConfirm(null)}>
        <Typography variant="body2">{confirm?.label}</Typography>
      </ConfirmDialog>
    </Section>
  );
}

function NoteDialog({ kind, onClose, onDone }: { kind: 'note' | 'exclude'; onClose: () => void; onDone: () => void }) {
  const now = Date.now();
  const [start, setStart] = useState(f.localInput(now - 3600e3));
  const [end, setEnd] = useState(kind === 'exclude' ? f.localInput(now) : '');
  const [text, setText] = useState('');
  const [noteKind, setNoteKind] = useState('note');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (kind === 'exclude') await post('/api/exclusions', { start, end, reason: text });
      else await post('/api/annotations', { start, end: end || null, kind: noteKind, text });
      toast(kind === 'exclude' ? 'Data excluded from learning.' : 'Note added.');
      onDone();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }
  return (
    <Dialog open onClose={onClose} maxWidth="xs" fullWidth aria-labelledby="note-title" slotProps={{ paper: { component: 'form', onSubmit: submit } as object }}>
      <DialogTitle id="note-title">{kind === 'exclude' ? 'Exclude bad data' : 'Add note'}</DialogTitle>
      <DialogContent sx={{ display: 'grid', gap: 2, pt: '4px !important' }}>
        {kind === 'note' && (
          <TextField select label="Type" value={noteKind} onChange={(e) => setNoteKind(e.target.value)}>
            {NOTE_KINDS.map(([k, l]) => <MenuItem key={k} value={k}>{l}</MenuItem>)}
          </TextField>
        )}
        <TextField label="From" type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} required slotProps={{ inputLabel: { shrink: true } }} />
        <TextField label={kind === 'exclude' ? 'To' : 'To (optional)'} type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} required={kind === 'exclude'} slotProps={{ inputLabel: { shrink: true } }} />
        <TextField label={kind === 'exclude' ? 'Reason' : 'Note'} value={text} onChange={(e) => setText(e.target.value)} required multiline minRows={2}
          slotProps={{ htmlInput: { minLength: kind === 'exclude' ? 3 : 1, maxLength: kind === 'exclude' ? 300 : 1000 } }} />
        {error && <Alert severity="error" role="alert">{error}</Alert>}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button type="submit" variant="contained" disabled={busy}>{kind === 'exclude' ? 'Exclude' : 'Add note'}</Button>
      </DialogActions>
    </Dialog>
  );
}
