// Settings sub-pages: Home & tariff, Planning, Connections & API, Data & privacy.
import { useState, type FormEvent, type ReactNode } from 'react';
import Box from '@mui/material/Box';
import Card from '@mui/material/Card';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import TextField from '@mui/material/TextField';
import MenuItem from '@mui/material/MenuItem';
import Switch from '@mui/material/Switch';
import FormControlLabel from '@mui/material/FormControlLabel';
import InputAdornment from '@mui/material/InputAdornment';
import Alert from '@mui/material/Alert';
import Collapse from '@mui/material/Collapse';
import Dialog from '@mui/material/Dialog';
import DialogTitle from '@mui/material/DialogTitle';
import DialogContent from '@mui/material/DialogContent';
import DialogActions from '@mui/material/DialogActions';
import Table from '@mui/material/Table';
import TableHead from '@mui/material/TableHead';
import TableBody from '@mui/material/TableBody';
import TableRow from '@mui/material/TableRow';
import TableCell from '@mui/material/TableCell';
import List from '@mui/material/List';
import ListItem from '@mui/material/ListItem';
import ListItemText from '@mui/material/ListItemText';
import DownloadRounded from '@mui/icons-material/DownloadRounded';
import AddRounded from '@mui/icons-material/AddRounded';
import UploadFileRounded from '@mui/icons-material/UploadFileRounded';
import { get, post, del, errorText } from '../../lib/api';
import { useLoad } from '../../lib/hooks';
import { useApp } from '../../lib/app';
import * as f from '../../lib/fmt';
import { ConfirmDialog, CopyField, Empty, ErrorState, Facts, HelpTip, LoadingBlock, PageHeader, Section, useToast } from '../../components/common';
import { slug } from './fields';
import { clone, useSettings } from './ctx';

export default function Misc({ section, label, sub: subsection }: { section: string; label: string; sub?: string }) {
  const sub: Record<string, string> = {
    home: 'Where you are, your grid connection and what you pay.',
    planning: 'How cautious the plan is and what it assumes about your home.',
    connections: 'Tokens for Home Assistant and devices that push readings.',
    data: 'Your data stays on this server. Export it or see who changed what.',
  };
  return (
    <Box>
      <PageHeader title={label} sub={sub[section]} />
      {section === 'home' && (subsection === 'tariffs' ? <Tariffs /> : <Home locationOnly={subsection === 'location'} />)}
      {section === 'planning' && <Planning />}
      {section === 'connections' && <Connections />}
      {section === 'data' && <DataPrivacy />}
    </Box>
  );
}

function useSaveForm<T>(pick: (s: any) => T, apply: (s: any, v: T) => void, message: string) {
  const { doc, save } = useSettings();
  const toast = useToast();
  const [value, setValue] = useState<T>(() => clone(pick(doc.settings)));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dirty = JSON.stringify(value) !== JSON.stringify(pick(doc.settings));
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const s = clone(doc.settings);
      apply(s, value);
      await save(s);
      toast(message);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }
  return { value, setValue, busy, error, dirty, submit };
}

function FormFoot({ busy, error, dirty, label = 'Save' }: { busy: boolean; error: string | null; dirty: boolean; label?: string }) {
  return (
    <>
      {error && <Alert severity="error" role="alert" sx={{ mt: 2 }}>{error}</Alert>}
      <Stack direction="row" sx={{ mt: 2, justifyContent: 'flex-end', alignItems: 'center', gap: 1.5 }}>
        {dirty && <Typography variant="caption" color="text.secondary">Unsaved changes</Typography>}
        <Button type="submit" variant="contained" disabled={!dirty || busy}>{busy ? 'Saving…' : label}</Button>
      </Stack>
    </>
  );
}

function NumField({ label, unit, value, onChange, help, min, max }: { label: string; unit?: string; value: any; onChange: (v: any) => void; help?: ReactNode; min?: number; max?: number }) {
  return (
    <TextField label={label} type="number" value={value ?? ''} onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
      slotProps={{ htmlInput: { step: 'any', min, max }, input: { endAdornment: (unit || help) ? <InputAdornment position="end">{unit}{help && <HelpTip label={label}>{help}</HelpTip>}</InputAdornment> : undefined } }} />
  );
}

function PasswordConfirm({ password, setPassword, otp, setOtp }: { password: string; setPassword: (v: string) => void; otp: string; setOtp: (v: string) => void }) {
  const { session } = useApp();
  return (
    <Stack spacing={2} sx={{ pt: 1.5, borderTop: 1, borderColor: 'divider' }}>
      <Typography variant="caption" color="text.secondary">Confirm it’s you.</Typography>
      <TextField label="Your password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
      {session.mfa_enabled && <TextField label="Two-step code" value={otp} onChange={(e) => setOtp(e.target.value)} autoComplete="one-time-code" slotProps={{ htmlInput: { inputMode: 'numeric' } }} />}
    </Stack>
  );
}

/* ------------------------------------------------------------ home & tariff */
function LocationSearch({ choose }: { choose: (patch: any) => void }) {
  const [query, setQuery] = useState('');
  const [matches, setMatches] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  async function search(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setMessage(''); setMatches([]);
    try {
      const rows = await post('/api/location/search', { query: query.trim() });
      setMatches(rows);
      if (!rows.length) setMessage('No matches. Try a suburb or postcode, or enter coordinates below.');
    } catch (e) { setMessage(errorText(e)); } finally { setBusy(false); }
  }
  async function select(match: any) {
    setBusy(true); setMatches([]); setQuery(''); setMessage('');
    const coords = { latitude: Number(match.latitude.toFixed(3)), longitude: Number(match.longitude.toFixed(3)) };
    try {
      const result = await post('/api/location/timezone', coords);
      choose({ ...coords, timezone: result.timezone });
      setMessage('Location and time zone filled in. Review them and save below.');
    } catch (e) { choose(coords); setMessage(errorText(e)); } finally { setBusy(false); }
  }
  return <Box sx={{ mb: 3 }}>
    <Box component="form" onSubmit={search} sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
      <TextField fullWidth label="Address, suburb or postcode" value={query} disabled={busy} onChange={e => { setQuery(e.target.value); setMatches([]); }}
        helperText={<>Search sends your text to Photon. Only the selected coordinates and time zone are saved. Map data © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap contributors</a>.</>} />
      <Button type="submit" disabled={busy || query.trim().length < 3}>{busy ? 'Looking up…' : 'Search'}</Button>
    </Box>
    {matches.map((m, i) => <Button key={i} fullWidth sx={{ justifyContent: 'flex-start', textAlign: 'left' }} onClick={() => select(m)}>{m.label}</Button>)}
    {message && <Typography role="status" variant="body2">{message}</Typography>}
  </Box>;
}

function WeatherCheck({ dirty }: { dirty: boolean }) {
  const { status, reloadStatus } = useApp();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  async function check() {
    setBusy(true); setMessage('');
    try {
      const r = await post('/api/weather/refresh');
      setMessage((r.simulated ? 'Demo forecast available' : 'Open-Meteo forecast received') + ' for ' + f.dateTime(r.weather.ts) + '.');
      await reloadStatus();
    } catch (e) { setMessage(errorText(e)); } finally { setBusy(false); }
  }
  return <Section title="Weather forecast">
    <Typography variant="body2">{status?.weather ? 'Forecast last updated ' + f.dateTime(status.weather.fetched_at) + '.' : 'No forecast available for the current hour.'} {dirty ? 'Save the home settings before checking.' : 'Checks the forecast for your saved location.'}</Typography>
    <Button disabled={busy || dirty} onClick={check}>{busy ? 'Checking…' : 'Test weather'}</Button>
    <Button href="#/settings/planning">Planning settings</Button>
    {message && <Typography variant="body2" role="status">{message}</Typography>}
  </Section>;
}

function Home({ locationOnly }: { locationOnly: boolean }) {
  const site = useSaveForm((s) => s.site, (s, v) => { s.site = v; }, 'Home settings saved. The plan is updating.');
  const v = site.value;
  const set = (patch: any) => site.setValue({ ...v, ...patch });
  return (
    <Stack spacing={2.5}>
      <Section title="Home">
        <LocationSearch choose={set} />
        <Box component="form" onSubmit={site.submit}>
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' } }}>
            <TextField label="Site name" value={v.name} onChange={(e) => set({ name: e.target.value })} required />
            <TextField label="Time zone" value={v.timezone} onChange={(e) => set({ timezone: e.target.value })} required
              slotProps={{ input: { endAdornment: <InputAdornment position="end"><HelpTip label="time zone">An IANA name such as Australia/Brisbane or Australia/Sydney. Every time in kWattson is shown in this zone.</HelpTip></InputAdornment> } }} />
            <NumField label="Latitude" value={v.latitude} onChange={(x) => set({ latitude: x })} min={-90} max={90} help="Used for the weather and solar forecast. Two decimals (about 1 km) is plenty." />
            <NumField label="Longitude" value={v.longitude} onChange={(x) => set({ longitude: x })} min={-180} max={180} />
            <NumField label="Export limit" unit="kW" value={v.export_kw} onChange={(x) => set({ export_kw: x })} help="The most your network lets you send to the grid. Check your connection agreement." />
            <NumField label="Power polling interval" unit="seconds" value={v.poll_seconds} onChange={(x) => set({ poll_seconds: x })} min={60} max={3600}
              help="How often kWattson asks for inverter readings. Some clouds publish more slowly or limit daily requests across devices." />
            <NumField label="Import limit" unit="kW" value={v.grid_import_kw} onChange={(x) => set({ grid_import_kw: x })} help="The most the house can draw from the grid at once." />
            <Box sx={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center' }}>
              <FormControlLabel control={<Switch checked={!!v.export_limit_confirmed} onChange={(e) => set({ export_limit_confirmed: e.target.checked })} />}
                label="Export limit confirmed" />
              <HelpTip label="export limit confirmed">Turn on once you’ve checked the limit with your network or installer. Automatic control won’t export from the battery until it’s confirmed.</HelpTip>
            </Box>
          </Box>
          <FormFoot {...site} />
        </Box>
      </Section>
      <WeatherCheck dirty={site.dirty} />
      {!locationOnly && <Tariffs />}
    </Stack>
  );
}

const STATUS_COLOR: Record<string, 'success' | 'warning' | 'default'> = { active: 'success', pending: 'warning', retired: 'default' };
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const dayText = (d: number[]) => (d.length === 7 ? 'Every day' : d.length === 5 && d.every((x) => x < 5) ? 'Weekdays' : d.length === 2 && d.every((x) => x >= 5) ? 'Weekends' : d.map((x) => DAYS[x]).join(', '));

function Tariffs() {
  const { doc, reload } = useSettings();
  const toast = useToast();
  const [rates, setRates] = useState<string | null>(null);
  const [flatOpen, setFlatOpen] = useState(false);
  const [editing, setEditing] = useState<any>(null);
  const { status } = useApp();
  const [showEarlier, setShowEarlier] = useState(false);
  const [importing, setImporting] = useState(false);
  const [activate, setActivate] = useState<any>(null);
  const [retire, setRetire] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const tariffs = [...(doc.settings.tariffs as any[])].sort((a, b) => ['active', 'pending', 'retired'].indexOf(a.status) - ['active', 'pending', 'retired'].indexOf(b.status));
  const earlier = (t: any) => t.status === 'active' && t.id !== status?.tariff?.plan_id && status?.tariff?.plan_id && (!t.active_from || Date.parse(t.active_from) <= Date.now());
  async function doRetire() {
    setBusy(true);
    try { await post(`/api/tariffs/${retire.id}/retire`); toast(`${retire.name} retired.`); setRetire(null); await reload(); }
    catch (e) { toast(errorText(e), 'error'); } finally { setBusy(false); }
  }
  return (
    <Section title="Tariffs" help={<HelpTip label="tariffs">The active tariff prices every plan. Import a new one as pending, then activate it from the date your retailer confirms. Retired tariffs are kept for history.</HelpTip>}
      action={<Button size="small" variant="outlined" startIcon={<AddRounded />} onClick={() => setImporting(true)}>Import tariff</Button>}>
      <Button variant="contained" sx={{ mb: 2 }} onClick={() => setFlatOpen(true)}>Enter flat rates</Button>
      <Stack spacing={1.5}>
        {tariffs.some(earlier) && <Button onClick={() => setShowEarlier(!showEarlier)}>{showEarlier ? 'Hide earlier tariffs' : 'Show earlier tariffs'}</Button>}
        {tariffs.filter(t => showEarlier || !earlier(t)).map((t) => (
          <Card key={t.id} sx={{ p: 2, borderColor: t.status === 'pending' ? 'warning.main' : 'divider', opacity: t.status === 'retired' ? 0.75 : 1 }}>
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ alignItems: { sm: 'center' } }}>
              <Box sx={{ flex: 1, minWidth: 0 }}>
                <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
                  <Typography variant="subtitle2" component="h3" sx={{ fontSize: '0.9375rem' }}>{t.name}</Typography>
                  <Chip size="small" label={earlier(t) ? 'Earlier rates' : t.status[0].toUpperCase() + t.status.slice(1)} color={STATUS_COLOR[t.status]} sx={{ height: 20 }} />
                </Stack>
                <Typography variant="caption" color="text.secondary">
                  {t.retailer || 'Retailer not set'}{t.active_from ? ` · from ${f.dateTime(t.active_from)}` : ''} · supply {f.money(t.supply_daily)}/day
                </Typography>
              </Box>
              <Stack direction="row" spacing={1}>
                <Button size="small" onClick={() => setRates(rates === t.id ? null : t.id)} aria-expanded={rates === t.id} aria-controls={`rates-${t.id}`}>
                  {rates === t.id ? 'Hide rates' : 'View rates'}
                </Button>
                {t.periods.length === 1 && t.status !== 'retired' && <Button size="small" onClick={() => setEditing(t)}>Edit rates</Button>}
                {t.status === 'pending' && <Button size="small" variant="contained" onClick={() => setActivate(t)}>Activate</Button>}
                {t.status !== 'retired' && (t.active_from || t.status === 'pending') && <Button size="small" color="error" onClick={() => setRetire(t)}>Retire</Button>}
              </Stack>
            </Stack>
            <Collapse in={rates === t.id} unmountOnExit id={`rates-${t.id}`}>
              <Box sx={{ overflowX: 'auto', mt: 1.5 }}>
                <Table size="small" aria-label={`${t.name} rates`}>
                  <TableHead><TableRow><TableCell>Period</TableCell><TableCell>Time</TableCell><TableCell>Days</TableCell><TableCell align="right">Buy</TableCell><TableCell align="right">Feed-in</TableCell></TableRow></TableHead>
                  <TableBody>{t.periods.map((p: any) => (
                    <TableRow key={p.name}><TableCell component="th" scope="row">{p.label || p.name}</TableCell><TableCell sx={{ whiteSpace: 'nowrap' }}>{p.start}–{p.end}</TableCell>
                      <TableCell>{dayText(p.days ?? [])}</TableCell><TableCell align="right">{f.cents(p.import_rate)}/kWh</TableCell><TableCell align="right">{f.cents(p.export_rate)}/kWh</TableCell></TableRow>
                  ))}</TableBody>
                </Table>
                {(t.allowances ?? []).length > 0 && (
                  <Typography variant="caption" color="text.secondary" component="p" sx={{ mt: 1 }}>
                    Allowances: {t.allowances.map((a: any) => `${a.kwh_per_day} kWh/day ${a.kind} in ${a.periods.join(', ')}, then ${f.cents(a.rate_after)}/kWh`).join('; ')}.
                  </Typography>
                )}
                {t.notes && <Typography variant="caption" color="text.secondary" component="p">{t.notes}</Typography>}
              </Box>
            </Collapse>
          </Card>
        ))}
      </Stack>
      {editing && <FlatTariffDialog existing={editing} onClose={() => setEditing(null)} onDone={async () => { setEditing(null); await reload(); toast('Rates updated.'); }} />}
      {flatOpen && <FlatTariffDialog onClose={() => setFlatOpen(false)} onDone={async (plan) => { setFlatOpen(false); await reload(); setActivate(plan); }} />}
      {importing && <ImportDialog onClose={() => setImporting(false)} onDone={async () => { setImporting(false); await reload(); }} />}
      {activate && <ActivateDialog tariff={activate} onClose={() => setActivate(null)} onDone={async () => { setActivate(null); await reload(); }} />}
      <ConfirmDialog open={!!retire} title={`Retire ${retire?.name}?`} confirm="Retire tariff" danger busy={busy} onConfirm={doRetire} onClose={() => setRetire(null)}>
        <Typography variant="body2">{retire?.status === 'active' ? 'Planning switches to your other active tariff, if there is one. ' : ''}It stays in the list for history.</Typography>
      </ConfirmDialog>
    </Section>
  );
}

function FlatTariffDialog({ existing, onClose, onDone }: { existing?: any; onClose: () => void; onDone: (plan: any) => void }) {
  const cents = (value?: number) => value == null ? '' : String(Number((100 * value).toFixed(6)));
  const [name, setName] = useState<string>(existing?.name ?? '');
  const [buy, setBuy] = useState(cents(existing?.periods[0].import_rate));
  const [sell, setSell] = useState(cents(existing?.periods[0].export_rate));
  const [daily, setDaily] = useState(cents(existing?.supply_daily));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      if (existing) {
        await post('/api/tariffs/' + existing.id + '/rates', { original: existing, name: name.trim(), buy: Number(buy) / 100, sell: Number(sell) / 100, daily: Number(daily) / 100 });
        await onDone(existing);
        return;
      }
      const r = await post('/api/tariffs/import', {
        id: slug(name, 'my-plan'), name: name.trim(), supply_daily: Number(daily) / 100,
        periods: [{ name: 'anytime', label: 'Any time', start: '00:00', end: '24:00', import_rate: Number(buy) / 100, export_rate: Number(sell) / 100 }],
      });
      onDone(r.imported);
    } catch (err) { setError(errorText(err)); setBusy(false); }
  }
  return <Dialog open maxWidth="xs" fullWidth aria-labelledby="flat-title" onClose={busy ? undefined : onClose} slotProps={{ paper: { component: 'form', onSubmit: submit } as object }}>
    <DialogTitle id="flat-title">{existing ? 'Edit flat rates' : 'Enter flat rates'}</DialogTitle>
    <DialogContent sx={{ display: 'grid', gap: 2, pt: '4px !important' }}>
      <Typography variant="body2">Use the cents shown on your electricity bill. {existing ? 'Edits apply to the dates this plan covers. Add a new tariff for a price change on a specific date.' : 'You’ll choose the start date after saving.'}</Typography>
      <TextField required label="Plan name" value={name} onChange={e => setName(e.target.value)} slotProps={{ htmlInput: { maxLength: 80 } }} />
      <TextField required type="number" label="Electricity you buy (¢/kWh)" value={buy} onChange={e => setBuy(e.target.value)} slotProps={{ htmlInput: { step: 'any', min: -200, max: 500 } }} />
      <TextField required type="number" label="Solar you sell (¢/kWh)" value={sell} onChange={e => setSell(e.target.value)} slotProps={{ htmlInput: { step: 'any', min: -200, max: 500 } }} />
      <TextField required type="number" label="Daily supply charge (¢/day)" value={daily} onChange={e => setDaily(e.target.value)} slotProps={{ htmlInput: { step: 'any', min: 0, max: 2000 } }} />
      {error && <Alert severity="error">{error}</Alert>}
    </DialogContent>
    <DialogActions><Button onClick={onClose} disabled={busy}>Cancel</Button><Button type="submit" variant="contained" disabled={busy}>{busy ? 'Saving…' : 'Save rates'}</Button></DialogActions>
  </Dialog>;
}

function ImportDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    let body: unknown;
    try { body = JSON.parse(text); } catch { setError('That isn’t valid JSON. Paste or upload the whole file.'); return; }
    setBusy(true);
    try {
      const r = await post('/api/tariffs/import', body);
      toast(`${r.imported?.name ?? 'Tariff'} imported as pending. Activate it once your retailer confirms the date.`);
      onDone();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }
  async function pick(file?: File) {
    if (!file) return;
    if (file.size > 1_500_000) { setError('That file is too large (1.5 MB limit).'); return; }
    setText(await file.text());
  }
  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth aria-labelledby="imp-title" slotProps={{ paper: { component: 'form', onSubmit: submit } as object }}>
      <DialogTitle id="imp-title">Import tariff</DialogTitle>
      <DialogContent sx={{ display: 'grid', gap: 2, pt: '4px !important' }}>
        <Typography variant="body2" color="text.secondary">
          Paste kWattson tariff JSON (see <code>tariffs/</code> in the repository) or the plan detail JSON from Energy Made Easy (the AER’s CDR API). It’s added as pending.
        </Typography>
        <Button component="label" variant="outlined" startIcon={<UploadFileRounded />} sx={{ justifySelf: 'start' }}>
          Choose a file
          <input type="file" accept="application/json,.json" hidden onChange={(e) => void pick(e.target.files?.[0])} />
        </Button>
        <TextField label="Tariff JSON" multiline minRows={6} maxRows={14} value={text} onChange={(e) => setText(e.target.value)} required
          slotProps={{ htmlInput: { spellCheck: false, style: { fontFamily: 'ui-monospace, Menlo, Consolas, monospace', fontSize: 12 } } }} />
        {error && <Alert severity="error" role="alert">{error}</Alert>}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button type="submit" variant="contained" disabled={busy || !text.trim()}>{busy ? 'Importing…' : 'Import'}</Button>
      </DialogActions>
    </Dialog>
  );
}

function ActivateDialog({ tariff, onClose, onDone }: { tariff: any; onClose: () => void; onDone: () => void }) {
  const [when, setWhen] = useState(f.localInput(Date.now()));
  const [password, setPassword] = useState('');
  const [otp, setOtp] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await post(`/api/tariffs/${tariff.id}/activate`, { active_from: when, password, otp });
      toast(`${tariff.name} active from ${f.dateTime(result.active_from)}. The plan is updating.`);
      onDone();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }
  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="xs" fullWidth aria-labelledby="act-title" slotProps={{ paper: { component: 'form', onSubmit: submit } as object }}>
      <DialogTitle id="act-title">Activate {tariff.name}</DialogTitle>
      <DialogContent sx={{ display: 'grid', gap: 2, pt: '4px !important' }}>
        <Typography variant="body2" color="text.secondary">Use the start date your retailer confirmed. Plans use this tariff from then on.</Typography>
        <TextField label="Active from" type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} required slotProps={{ inputLabel: { shrink: true } }}
          helperText={`${f.getZone().replace('_', ' ')} time`} />
        <PasswordConfirm password={password} setPassword={setPassword} otp={otp} setOtp={setOtp} />
        {error && <Alert severity="error" role="alert">{error}</Alert>}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button type="submit" variant="contained" disabled={busy}>{busy ? 'Activating…' : 'Activate tariff'}</Button>
      </DialogActions>
    </Dialog>
  );
}

/* ----------------------------------------------------------------- planning */
function Planning() {
  const fc = useSaveForm((s) => ({ forecast: s.forecast, ac_kw: s.site.ac_kw, weather: s.site.weather_enabled }),
    (s, v) => { s.forecast = v.forecast; s.site.ac_kw = v.ac_kw; s.site.weather_enabled = v.weather; }, 'Planning settings saved. The plan is updating.');
  const v = fc.value.forecast;
  const set = (patch: any) => fc.setValue({ ...fc.value, forecast: { ...v, ...patch } });
  return (
    <Box component="form" onSubmit={fc.submit}>
      <Stack spacing={2.5}>
        <Section title="Forecast">
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' } }}>
            <TextField select label="Forecast risk" value={v.risk} onChange={(e) => set({ risk: e.target.value })}
              slotProps={{ input: { endAdornment: <InputAdornment position="end" sx={{ mr: 2 }}><HelpTip label="forecast risk">Cautious plans for less solar and more use than expected, so the battery is rarely caught short. Optimistic plans for the opposite. Balanced uses the middle forecast.</HelpTip></InputAdornment> } }}>
              {['cautious', 'balanced', 'optimistic'].map((r) => <MenuItem key={r} value={r}>{r[0].toUpperCase() + r.slice(1)}</MenuItem>)}
            </TextField>
            <NumField label="History used" unit="days" value={v.history_days} onChange={(x) => set({ history_days: x })} help="How many past days the home-use forecast learns from." />
            <NumField label="Recent days count more" unit="days" value={v.half_life_days} onChange={(x) => set({ half_life_days: x })} help="Half-life of the weighting: a day this old counts half as much as yesterday." />
            <NumField label="Plan ahead" unit="hours" value={v.horizon_hours} onChange={(x) => set({ horizon_hours: x })} help="How far ahead each plan looks. 48 hours lets it see tomorrow’s sun." />
            <Box sx={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center' }}>
              <FormControlLabel control={<Switch checked={!!fc.value.weather} onChange={(e) => fc.setValue({ ...fc.value, weather: e.target.checked })} />} label="Use weather forecast" />
              <HelpTip label="weather forecast">Fetches the Open-Meteo forecast for your location. Without it, solar is forecast from recent days only.</HelpTip>
            </Box>
          </Box>
        </Section>
        <Section title="Comfort & heating">
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' } }}>
            <NumField label="Cooling from" unit="°C" value={v.cooling_base_c} onChange={(x) => set({ cooling_base_c: x })} help="Above this outdoor temperature your home use starts rising for cooling." />
            <NumField label="Heating below" unit="°C" value={v.heating_base_c} onChange={(x) => set({ heating_base_c: x })} help="Below this outdoor temperature your home use starts rising for heating." />
            <NumField label="AC power" unit="kW" value={fc.value.ac_kw} onChange={(x) => fc.setValue({ ...fc.value, ac_kw: x })} help="Roughly what your air conditioner draws while running. Used for the pre-cool / pre-heat advice." />
            <NumField label="Away base load" unit="kW" value={v.holiday_base_load_kw} onChange={(x) => set({ holiday_base_load_kw: x })} help="What the house uses while you’re away (fridge, standby)." />
          </Box>
        </Section>
        <Section title="Battery value">
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' } }}>
            <NumField label="End-of-plan value" value={v.terminal_value_factor} onChange={(x) => set({ terminal_value_factor: x })} min={0} max={1.5}
              help="How much energy left in the battery at the end of the plan is worth, as a share of future prices. Lower lets the plan use the battery more freely; higher keeps more in reserve." />
          </Box>
          <FormFoot {...fc} />
        </Section>
      </Stack>
    </Box>
  );
}

/* --------------------------------------------------------------- connections */
function Connections() {
  const tokens = useLoad<any[]>(() => get('/api/tokens'));
  const { doc } = useSettings();
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const [revoke, setRevoke] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const origin = window.location.origin;
  const push = (doc.settings.systems as any[]).find((s) => s.plugin === 'push');
  const pushCar = (doc.settings.vehicles as any[]).find((v) => v.plugin === 'push_vehicle');
  async function doRevoke() {
    setBusy(true);
    try { await del(`/api/tokens/${revoke.id}`); toast(`${revoke.name} revoked. Anything using it stops working now.`); setRevoke(null); tokens.reload(); }
    catch (e) { toast(errorText(e), 'error'); } finally { setBusy(false); }
  }
  return (
    <Stack spacing={2.5}>
      <Section title="API tokens" action={<Button size="small" variant="outlined" startIcon={<AddRounded />} onClick={() => setCreating(true)}>Create token</Button>}
        help={<HelpTip label="API tokens"><strong>Read</strong> tokens can only read the Home Assistant state. <strong>Ingest</strong> tokens can also push readings. Neither can change settings or control anything.</HelpTip>}>
        {tokens.error ? <ErrorState message={tokens.error} onRetry={tokens.reload} /> : !tokens.data ? <LoadingBlock /> : !tokens.data.length ? (
          <Empty>No tokens yet. Create a read token for Home Assistant, or an ingest token for a device that pushes readings.</Empty>
        ) : (
          <List disablePadding>{tokens.data.map((t) => (
            <ListItem key={t.id} divider disableGutters secondaryAction={<Button size="small" color="error" onClick={() => setRevoke(t)}>Revoke</Button>} sx={{ pr: 10 }}>
              <ListItemText primary={<>{t.name} <Chip size="small" label={t.scope === 'read' ? 'Read' : 'Ingest'} variant="outlined" sx={{ height: 20, ml: 0.5 }} /></>}
                secondary={`Created ${f.date(t.created_at)} · last used ${t.last_used ? f.relative(t.last_used) : 'never'}`}
                slotProps={{ primary: { sx: { fontWeight: 600 } } }} />
            </ListItem>
          ))}</List>
        )}
      </Section>
      <Section title="Home Assistant">
        <Stack spacing={2}>
          <Typography variant="body2">
            Home Assistant polls one URL for battery, solar, grid, price, the planned action and the AC signal. The ready-made package and the pre-cool / pre-heat blueprint are in
            the <code>homeassistant/</code> folder of the kWattson repository; its README walks through setup.
          </Typography>
          <CopyField label="State URL" value={`${origin}/api/ha/state`} />
          <CopyField label="secrets.yaml" multiline value={`kwattson_state_url: ${origin}/api/ha/state\nkwattson_token: "Bearer kw_your_read_token"`}
            helper="Replace kw_your_read_token with a read token from above." />
        </Stack>
      </Section>
      <Section title="Pushing readings">
        <Stack spacing={2}>
          <Typography variant="body2">
            Devices or scripts can send readings to a system that uses the <strong>Pushed readings</strong> connection (or a vehicle using <strong>Pushed vehicle readings</strong>),
            with an ingest token. Powers are in kW, levels in %; send at least every few minutes.
          </Typography>
          <CopyField label="System example" multiline value={`curl -X POST ${origin}/api/ingest/system/${push?.id ?? '<system id>'} \\\n  -H "Authorization: Bearer kw_your_ingest_token" \\\n  -H "Content-Type: application/json" \\\n  -d '{"soc": 64, "pv_kw": 4.2, "load_kw": 1.1, "charge_kw": 2.8, "discharge_kw": 0, "import_kw": 0, "export_kw": 0.3}'`} />
          <CopyField label="Vehicle example" multiline value={`curl -X POST ${origin}/api/ingest/vehicle/${pushCar?.id ?? '<vehicle id>'} \\\n  -H "Authorization: Bearer kw_your_ingest_token" \\\n  -H "Content-Type: application/json" \\\n  -d '{"soc": 58, "plugged_in": true, "charging": false, "at_home": true}'`} />
          <Typography variant="caption" color="text.secondary">
            System fields: soc, pv_kw, charge_kw, discharge_kw, ac_output_kw, import_kw, export_kw, load_kw, battery_temp, plus optional ts, running_state and fault.
            Vehicle fields: soc, charging, plugged_in, at_home, charge_kw, range_km, ts.
          </Typography>
        </Stack>
      </Section>
      {creating && <TokenDialog onClose={() => { setCreating(false); tokens.reload(); }} />}
      <ConfirmDialog open={!!revoke} title={`Revoke ${revoke?.name}?`} confirm="Revoke token" danger busy={busy} onConfirm={doRevoke} onClose={() => setRevoke(null)}>
        <Typography variant="body2">Anything using this token stops working straight away. This can’t be undone.</Typography>
      </ConfirmDialog>
    </Stack>
  );
}

function TokenDialog({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState('');
  const [scope, setScope] = useState('read');
  const [password, setPassword] = useState('');
  const [otp, setOtp] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [token, setToken] = useState<string | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await post('/api/tokens', { name: name.trim(), scope, password, otp });
      setToken(r.token);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth aria-labelledby="tok-title" slotProps={{ paper: { component: 'form', onSubmit: submit } as object }}>
      <DialogTitle id="tok-title">{token ? 'Copy your token now' : 'Create token'}</DialogTitle>
      <DialogContent sx={{ display: 'grid', gap: 2, pt: '4px !important' }}>
        {token ? (
          <>
            <Alert severity="warning">This is the only time it’s shown. Store it in Home Assistant’s secrets.yaml or your device’s config.</Alert>
            <CopyField label="Token" value={token} />
          </>
        ) : (
          <>
            <TextField label="Name" value={name} onChange={(e) => setName(e.target.value)} required autoFocus placeholder="e.g. Home Assistant" slotProps={{ htmlInput: { maxLength: 80 } }} />
            <TextField select label="Access" value={scope} onChange={(e) => setScope(e.target.value)}
              helperText={scope === 'read' ? 'Can read the Home Assistant state only.' : 'Can push readings and read the Home Assistant state.'}>
              <MenuItem value="read">Read</MenuItem>
              <MenuItem value="ingest">Ingest (push readings)</MenuItem>
            </TextField>
            <PasswordConfirm password={password} setPassword={setPassword} otp={otp} setOtp={setOtp} />
            {error && <Alert severity="error" role="alert">{error}</Alert>}
          </>
        )}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        {token ? <Button variant="contained" onClick={onClose}>Done</Button> : (
          <>
            <Button onClick={onClose} disabled={busy}>Cancel</Button>
            <Button type="submit" variant="contained" disabled={busy}>{busy ? 'Creating…' : 'Create token'}</Button>
          </>
        )}
      </DialogActions>
    </Dialog>
  );
}

/* ------------------------------------------------------------ data & privacy */
function DataPrivacy() {
  const audit = useLoad<any[]>(() => get('/api/audit?limit=100'));
  const { session } = useApp();
  const [days, setDays] = useState(90);
  const ret = useSaveForm((s) => s.raw_retention_days, (s, v) => { s.raw_retention_days = v; }, 'Retention saved.');
  return (
    <Stack spacing={2.5}>
      <Section title="Export">
        <Typography variant="body2" sx={{ mb: 2 }}>
          Readings, forecasts, plans, decisions and daily outcomes as JSON lines, ready for analysis or for an AI agent to study. Credentials and password hashes are never included.
        </Typography>
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5} sx={{ alignItems: { sm: 'center' } }}>
          <TextField select label="Period" value={days} onChange={(e) => setDays(Number(e.target.value))} sx={{ maxWidth: { sm: 200 } }}>
            {[30, 90, 180, 365, 3650].map((d) => <MenuItem key={d} value={d}>{d === 3650 ? 'Everything' : `Last ${d} days`}</MenuItem>)}
          </TextField>
          <Button variant="contained" startIcon={<DownloadRounded />} href={`/api/export?days=${days}`} download>Download export</Button>
        </Stack>
      </Section>
      <Section title="Retention">
        <Box component="form" onSubmit={ret.submit}>
          <Box sx={{ maxWidth: 320 }}>
            <NumField label="Keep raw readings for" unit="days" value={ret.value} onChange={ret.setValue} min={7}
              help="Minute-by-minute readings older than this are deleted. Half-hour summaries, plans and daily outcomes are kept so the models can keep learning." />
          </Box>
          <FormFoot {...ret} />
        </Box>
      </Section>
      <Section title="Audit log">
        {audit.error ? <ErrorState message={audit.error} onRetry={audit.reload} /> : !audit.data ? <LoadingBlock /> : !audit.data.length ? <Empty>Nothing yet.</Empty> : (
          <Box role="region" aria-label="Audit log" tabIndex={0} sx={{ maxHeight: 420, overflow: 'auto', border: 1, borderColor: 'divider', borderRadius: 2 }}>
            <Table size="small" stickyHeader>
              <TableHead><TableRow><TableCell>When</TableCell><TableCell>What</TableCell><TableCell>Who</TableCell></TableRow></TableHead>
              <TableBody>{audit.data.map((r) => (
                <TableRow key={r.id}>
                  <TableCell sx={{ whiteSpace: 'nowrap' }}>{f.dateTime(r.at)}</TableCell>
                  <TableCell>{String(r.event).replaceAll('_', ' ').replace(/^\w/, (c) => c.toUpperCase())}</TableCell>
                  <TableCell>{r.actor}</TableCell>
                </TableRow>
              ))}</TableBody>
            </Table>
          </Box>
        )}
      </Section>
      <Section title="About">
        <Facts rows={[['kWattson version', session.version], ['Time zone', f.getZone()], ['Licence', 'MIT (open source)'], ['Fonts', 'Montserrat, SIL Open Font License (/static/licenses/)']]} />
      </Section>
    </Stack>
  );
}
