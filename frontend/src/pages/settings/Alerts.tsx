import { useEffect, useState, type FormEvent } from 'react';
import Box from '@mui/material/Box';
import Card from '@mui/material/Card';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import Switch from '@mui/material/Switch';
import FormControlLabel from '@mui/material/FormControlLabel';
import TextField from '@mui/material/TextField';
import MenuItem from '@mui/material/MenuItem';
import List from '@mui/material/List';
import ListItem from '@mui/material/ListItem';
import ListItemText from '@mui/material/ListItemText';
import Divider from '@mui/material/Divider';
import Dialog from '@mui/material/Dialog';
import DialogTitle from '@mui/material/DialogTitle';
import DialogContent from '@mui/material/DialogContent';
import DialogActions from '@mui/material/DialogActions';
import Alert from '@mui/material/Alert';
import InputAdornment from '@mui/material/InputAdornment';
import AddRounded from '@mui/icons-material/AddRounded';
import SendRounded from '@mui/icons-material/SendRounded';
import CheckRounded from '@mui/icons-material/CheckRounded';
import { get, post, put, del, errorText } from '../../lib/api';
import { useLoad } from '../../lib/hooks';
import * as f from '../../lib/fmt';
import { ConfirmDialog, Empty, ErrorState, HelpTip, LoadingBlock, PageHeader, Section, useToast } from '../../components/common';
import { clone, useSettings } from './ctx';

interface Channel { id: number; name: string; kind: string; enabled: number; config: Record<string, string>; configured: string[] }
interface Group { id: string; label: string; description: string; events: { event: string; label: string; severity: string; enabled: boolean }[]; channels: number[]; cooldown_minutes: number; respect_quiet: boolean }

const COOLDOWNS = [[0, 'Every time'], [15, '15 minutes'], [30, '30 minutes'], [60, '1 hour'], [120, '2 hours'], [240, '4 hours'], [720, '12 hours'], [1440, '1 day']] as const;

export default function Alerts() {
  const ch = useLoad<{ channels: Channel[]; kinds: any[] }>(() => get('/api/channels'));
  const groups = useLoad<Group[]>(() => get('/api/notification-groups'));
  const [adding, setAdding] = useState(false);
  return (
    <Box>
      <PageHeader title="Alerts" sub="Where alerts go, which ones you get, and when." />
      <Stack spacing={2.5}>
        <Section title="Where to send" action={<Button size="small" variant="outlined" startIcon={<AddRounded />} onClick={() => setAdding(true)}>Add channel</Button>}>
          {ch.error ? <ErrorState message={ch.error} onRetry={ch.reload} /> : !ch.data ? <LoadingBlock /> : <Channels data={ch.data} reload={ch.reload} />}
        </Section>
        <Box>
          <Typography variant="h2" sx={{ mb: 0.5 }}>What to send</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>Alerts always appear in kWattson. Pick channels to also get them on your phone.</Typography>
          {groups.error ? <ErrorState message={groups.error} onRetry={groups.reload} /> : !groups.data || !ch.data ? <LoadingBlock height={240} /> : (
            <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: '1fr', lg: '1fr 1fr' } }}>
              {groups.data.map((g) => <GroupCard key={g.id} group={g} channels={ch.data!.channels} onSaved={groups.reload} />)}
            </Box>
          )}
        </Box>
        <Preferences />
      </Stack>
      {adding && ch.data && <ChannelDialog kinds={ch.data.kinds} onClose={() => setAdding(false)} onDone={() => { setAdding(false); ch.reload(); }} />}
    </Box>
  );
}

function Channels({ data, reload }: { data: { channels: Channel[]; kinds: any[] }; reload: () => void }) {
  const toast = useToast();
  const [testing, setTesting] = useState<number | null>(null);
  const [remove, setRemove] = useState<Channel | null>(null);
  if (!data.channels.length) return <Empty>No channels yet. Add ntfy, Telegram, Pushover, email or a webhook to get alerts away from the dashboard.</Empty>;
  async function test(c: Channel) {
    setTesting(c.id);
    try {
      await post(`/api/channels/${c.id}/test`);
      toast(`Test sent to ${c.name}.`);
    } catch (e) {
      toast(errorText(e), 'error');
    } finally {
      setTesting(null);
    }
  }
  return (
    <>
      <List disablePadding>
        {data.channels.map((c) => (
          <ListItem key={c.id} divider disableGutters sx={{ gap: 1, flexWrap: 'wrap' }}>
            <ListItemText primary={c.name} secondary={`${data.kinds.find((k) => k.kind === c.kind)?.title ?? c.kind}${c.enabled ? '' : ' · off'}`}
              slotProps={{ primary: { sx: { fontWeight: 600 } } }} />
            <Button size="small" startIcon={<SendRounded />} onClick={() => test(c)} disabled={testing === c.id}>{testing === c.id ? 'Sending…' : 'Send test'}</Button>
            <Button size="small" color="error" onClick={() => setRemove(c)}>Delete</Button>
          </ListItem>
        ))}
      </List>
      <ConfirmDialog open={!!remove} title={`Delete ${remove?.name}?`} confirm="Delete channel" danger onClose={() => setRemove(null)}
        onConfirm={async () => { try { await del(`/api/channels/${remove!.id}`); toast('Channel deleted.'); reload(); } catch (e) { toast(errorText(e), 'error'); } setRemove(null); }}>
        <Typography variant="body2">Alerts stop going to this channel straight away.</Typography>
      </ConfirmDialog>
    </>
  );
}

function ChannelDialog({ kinds, onClose, onDone }: { kinds: any[]; onClose: () => void; onDone: () => void }) {
  const [kind, setKind] = useState(kinds[0]?.kind ?? 'ntfy');
  const [name, setName] = useState('');
  const [config, setConfig] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const spec = kinds.find((k) => k.kind === kind);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post('/api/channels', { name: name.trim() || spec.title, kind, enabled: true, config });
      toast('Channel added. Use “Send test” to check it.');
      onDone();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }
  return (
    <Dialog open onClose={onClose} maxWidth="xs" fullWidth aria-labelledby="ch-title" slotProps={{ paper: { component: 'form', onSubmit: submit } as object }}>
      <DialogTitle id="ch-title">Add channel</DialogTitle>
      <DialogContent sx={{ display: 'grid', gap: 2, pt: '4px !important' }}>
        <TextField select label="Type" value={kind} onChange={(e) => { setKind(e.target.value); setConfig({}); }}>
          {kinds.map((k) => <MenuItem key={k.kind} value={k.kind}>{k.title}</MenuItem>)}
        </TextField>
        <TextField label="Name" value={name} onChange={(e) => setName(e.target.value)} placeholder={spec?.title} slotProps={{ htmlInput: { maxLength: 60 } }} />
        {spec?.fields.map((fl: any) => (
          <TextField key={fl.name} label={fl.label} required={fl.required} type={fl.secret ? 'password' : 'text'} autoComplete={fl.secret ? 'new-password' : 'off'}
            value={config[fl.name] ?? ''} onChange={(e) => setConfig({ ...config, [fl.name]: e.target.value })}
            helperText={fl.secret ? 'Stored encrypted. Never shown again.' : undefined}
            slotProps={{ input: { endAdornment: fl.help ? <InputAdornment position="end"><HelpTip label={fl.label}>{fl.help}</HelpTip></InputAdornment> : undefined } }} />
        ))}
        {error && <Alert severity="error" role="alert">{error}</Alert>}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button type="submit" variant="contained" disabled={busy}>Add channel</Button>
      </DialogActions>
    </Dialog>
  );
}

const SEV_COLOR: Record<string, 'error' | 'warning' | 'default'> = { critical: 'error', warning: 'warning', info: 'default' };

function GroupCard({ group, channels, onSaved }: { group: Group; channels: Channel[]; onSaved: () => void }) {
  const [g, setG] = useState(group);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  useEffect(() => setG(group), [group]);
  const dirty = JSON.stringify(g) !== JSON.stringify(group);
  const hid = `grp-${group.id}`;
  async function save() {
    setBusy(true);
    try {
      await put(`/api/notification-groups/${g.id}`, {
        events: Object.fromEntries(g.events.map((e) => [e.event, e.enabled])), channels: g.channels, cooldown_minutes: g.cooldown_minutes, respect_quiet: g.respect_quiet,
      });
      toast(`${g.label} saved.`);
      onSaved();
    } catch (e) {
      toast(errorText(e), 'error');
    } finally {
      setBusy(false);
    }
  }
  return (
    <Card component="section" aria-labelledby={hid} sx={{ p: 2.5, display: 'flex', flexDirection: 'column' }}>
      <Typography variant="h3" id={hid}>{g.label}</Typography>
      <Typography variant="body2" color="text.secondary">{g.description}</Typography>
      <Box role="group" aria-label={`${g.label}: send to`} sx={{ mt: 2 }}>
        <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>Send to</Typography>
        <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 0.75, mt: 0.5 }}>
          <Chip label="Dashboard" size="small" icon={<CheckRounded />} variant="filled" disabled sx={{ opacity: '1 !important' }} />
          {channels.map((c) => {
            const on = g.channels.includes(c.id);
            return <Chip key={c.id} label={c.name} size="small" clickable aria-pressed={on} color={on ? 'secondary' : 'default'} variant={on ? 'filled' : 'outlined'}
              icon={on ? <CheckRounded /> : undefined}
              onClick={() => setG({ ...g, channels: on ? g.channels.filter((x) => x !== c.id) : [...g.channels, c.id] })} />;
          })}
          {!channels.length && <Typography variant="caption" color="text.secondary">Add a channel above to send these elsewhere.</Typography>}
        </Stack>
      </Box>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{ mt: 2, alignItems: { sm: 'center' } }}>
        <TextField select label="At most every" value={g.cooldown_minutes} onChange={(e) => setG({ ...g, cooldown_minutes: Number(e.target.value) })} sx={{ maxWidth: { sm: 180 } }}>
          {[...COOLDOWNS.map(([m, l]) => [m, l] as [number, string]), ...(COOLDOWNS.some(([m]) => m === g.cooldown_minutes) ? [] : [[g.cooldown_minutes, f.duration(g.cooldown_minutes)] as [number, string]])]
            .sort((x, y) => x[0] - y[0]).map(([m, l]) => <MenuItem key={m} value={m}>{l}</MenuItem>)}
        </TextField>
        <FormControlLabel control={<Switch checked={g.respect_quiet} onChange={(e) => setG({ ...g, respect_quiet: e.target.checked })} />} label="Respect quiet hours" />
      </Stack>
      <Divider sx={{ my: 1.5 }} />
      <List dense disablePadding sx={{ flex: 1 }}>
        {g.events.map((e) => (
          <ListItem key={e.event} disableGutters secondaryAction={
            <Switch edge="end" checked={e.enabled} slotProps={{ input: { 'aria-label': e.label } }}
              onChange={(ev) => setG({ ...g, events: g.events.map((x) => (x.event === e.event ? { ...x, enabled: ev.target.checked } : x)) })} />}>
            <ListItemText primary={<>{e.label} {e.severity !== 'info' && <Chip size="small" variant="outlined" color={SEV_COLOR[e.severity]} label={e.severity === 'critical' ? 'Critical' : 'Warning'} sx={{ height: 18, fontSize: '0.6875rem', ml: 0.5 }} />}</>}
              slotProps={{ primary: { variant: 'body2' } }} />
          </ListItem>
        ))}
      </List>
      <Stack direction="row" spacing={1} sx={{ mt: 1.5, justifyContent: 'flex-end', alignItems: 'center' }}>
        {dirty && <Typography variant="caption" color="text.secondary" sx={{ mr: 'auto' }}>Unsaved changes</Typography>}
        {dirty && <Button size="small" onClick={() => setG(group)} disabled={busy}>Undo</Button>}
        <Button size="small" variant="contained" onClick={save} disabled={!dirty || busy}>{busy ? 'Saving…' : 'Save'}</Button>
      </Stack>
    </Card>
  );
}

const HOURS = Array.from({ length: 24 }, (_, h) => [h, `${h % 12 === 0 ? 12 : h % 12}:00 ${h < 12 ? 'am' : 'pm'}`] as const);

const THRESHOLDS: { key: string; label: string; unit: string; help: string }[] = [
  { key: 'high_price_cents', label: 'High price', unit: 'c/kWh', help: 'Alert when you’re importing at or above this price.' },
  { key: 'low_battery_pct', label: 'Low battery', unit: '%', help: 'Alert when the battery falls to this level.' },
  { key: 'good_day_aud', label: 'Good day under', unit: '$', help: 'The daily summary calls a day good when net cost is at or below this.' },
  { key: 'bad_day_aud', label: 'Bad day over', unit: '$', help: 'The daily summary flags days that cost more than this.' },
  { key: 'ac_hot_c', label: 'Hot day', unit: '°C', help: 'Suggest pre-cooling with cheap power when the forecast reaches this.' },
  { key: 'ac_cold_c', label: 'Cold day', unit: '°C', help: 'Suggest pre-heating with cheap power when the forecast falls to this.' },
  { key: 'ac_max_cents', label: 'AC worth it under', unit: 'c/kWh', help: 'Only suggest running heating or cooling when power costs less than this.' },
  { key: 'clipping_alert_kwh', label: 'Clipping over', unit: 'kWh', help: 'Alert when the plan expects more solar than this to be clipped today.' },
];

function Preferences() {
  const { doc, save } = useSettings();
  const toast = useToast();
  const [a, setA] = useState(() => clone(doc.settings.alerts));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dirty = JSON.stringify(a) !== JSON.stringify(doc.settings.alerts);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const s = clone(doc.settings);
      s.alerts = a;
      await save(s);
      toast('Alert preferences saved.');
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }
  const hour = (key: string, label: string) => (
    <TextField select label={label} value={a[key]} onChange={(e) => setA({ ...a, [key]: Number(e.target.value) })}>
      {HOURS.map(([h, l]) => <MenuItem key={h} value={h}>{l}</MenuItem>)}
    </TextField>
  );
  return (
    <Section title="Preferences">
      <Box component="form" onSubmit={submit}>
        <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr', md: 'repeat(3, 1fr)' } }}>
          {hour('quiet_start_hour', 'Quiet from')}
          {hour('quiet_end_hour', 'Quiet until')}
          {hour('daily_summary_hour', 'Daily summary at')}
          {THRESHOLDS.map((t) => (
            <TextField key={t.key} label={t.label} type="number" value={a[t.key] ?? ''} onChange={(e) => setA({ ...a, [t.key]: e.target.value === '' ? '' : Number(e.target.value) })}
              slotProps={{ htmlInput: { step: 'any' }, input: { endAdornment: <InputAdornment position="end">{t.unit}<HelpTip label={t.label}>{t.help}</HelpTip></InputAdornment> } }} />
          ))}
        </Box>
        {error && <Alert severity="error" role="alert" sx={{ mt: 2 }}>{error}</Alert>}
        <Stack direction="row" sx={{ mt: 2, justifyContent: 'flex-end' }}>
          <Button type="submit" variant="contained" disabled={!dirty || busy}>{busy ? 'Saving…' : 'Save preferences'}</Button>
        </Stack>
      </Box>
    </Section>
  );
}
