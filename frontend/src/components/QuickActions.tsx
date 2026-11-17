import { useId, useState, type FormEvent, type ReactNode } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Dialog from '@mui/material/Dialog';
import DialogTitle from '@mui/material/DialogTitle';
import DialogContent from '@mui/material/DialogContent';
import DialogActions from '@mui/material/DialogActions';
import TextField from '@mui/material/TextField';
import InputAdornment from '@mui/material/InputAdornment';
import FormControlLabel from '@mui/material/FormControlLabel';
import Switch from '@mui/material/Switch';
import Alert from '@mui/material/Alert';
import Typography from '@mui/material/Typography';
import Chip from '@mui/material/Chip';
import Stack from '@mui/material/Stack';
import useMediaQuery from '@mui/material/useMediaQuery';
import type { Theme } from '@mui/material/styles';
import LuggageRounded from '@mui/icons-material/LuggageRounded';
import ThunderstormRounded from '@mui/icons-material/ThunderstormRounded';
import PauseCircleRounded from '@mui/icons-material/PauseCircleRounded';
import BlockRounded from '@mui/icons-material/BlockRounded';
import EvStationRounded from '@mui/icons-material/EvStationRounded';
import DirectionsCarRounded from '@mui/icons-material/DirectionsCarRounded';
import { post, errorText } from '../lib/api';
import * as f from '../lib/fmt';
import { useToast } from './common';

type Kind = 'away' | 'reserve' | 'hold' | 'no_battery_export' | 'ev_charge_now' | 'ev_away';
interface Vehicle { id: string; name: string }
type Open = { kind: Kind; vehicle?: Vehicle } | null;

export default function QuickActions({ vehicles, onDone }: { vehicles: Vehicle[]; onDone: () => void }) {
  const [open, setOpen] = useState<Open>(null);
  const items: { kind: Kind; label: string; Icon: typeof LuggageRounded; vehicle?: Vehicle; tone: string }[] = [
    { kind: 'away', label: 'Going away', Icon: LuggageRounded, tone: 'primary' },
    { kind: 'reserve', label: 'Storm coming', Icon: ThunderstormRounded, tone: 'battery' },
    { kind: 'hold', label: 'Pause battery', Icon: PauseCircleRounded, tone: 'battery' },
    { kind: 'no_battery_export', label: 'Don’t export from battery', Icon: BlockRounded, tone: 'primary' },
    ...vehicles.flatMap((v) => [
      { kind: 'ev_charge_now' as Kind, label: `Charge ${v.name} now`, Icon: EvStationRounded, vehicle: v, tone: 'battery' },
      { kind: 'ev_away' as Kind, label: `${v.name} is away`, Icon: DirectionsCarRounded, vehicle: v, tone: 'primary' },
    ]),
  ];
  return (
    <>
      <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr 1fr', sm: 'repeat(3, 1fr)', md: '1fr 1fr' }, gap: 1 }}>
        {items.map(({ kind, label, Icon, vehicle, tone }) => (
          <Button key={kind + (vehicle?.id ?? '')} variant="outlined" color="inherit" onClick={() => setOpen({ kind, vehicle })}
            startIcon={<Icon sx={{ color: tone === 'battery' ? 'battery.main' : 'primary.main' }} />}
            sx={{
              justifyContent: 'flex-start', textAlign: 'left', borderRadius: 3, py: 1.25, px: 1.5, lineHeight: 1.25,
              borderColor: 'divider', fontWeight: 600, minHeight: 52,
              '&:hover': { borderColor: tone === 'battery' ? 'battery.main' : 'primary.main', bgcolor: 'action.hover' },
            }}>
            {label}
          </Button>
        ))}
      </Box>
      {open && <ActionDialog open={open} onClose={() => setOpen(null)} onDone={() => { setOpen(null); onDone(); }} vehicles={vehicles} />}
    </>
  );
}

const PRESETS = [1, 3, 6, 12, 24];

function HoursField({ value, onChange, label = 'For how long' }: { value: string; onChange: (v: string) => void; label?: string }) {
  const id = useId();
  const num = Number(value);
  const invalid = value !== '' && (!(num >= 0.1) || num > 336);
  return (
    <Box>
      <TextField label={label} type="number" value={value} onChange={(e) => onChange(e.target.value)} required error={invalid}
        helperText={invalid ? 'Enter between 0.1 and 336 hours (14 days).' : ' '}
        slotProps={{ htmlInput: { min: 0.1, max: 336, step: 'any', 'aria-describedby': id }, input: { endAdornment: <InputAdornment position="end">hours</InputAdornment> } }} />
      <Stack direction="row" spacing={0.75} id={id} sx={{ flexWrap: 'wrap', gap: 0.75, mt: -1 }} role="group" aria-label="Quick durations">
        {PRESETS.map((h) => (
          <Chip key={h} label={`${h} h`} size="small" variant={num === h ? 'filled' : 'outlined'} color={num === h ? 'primary' : 'default'}
            onClick={() => onChange(String(h))} aria-pressed={num === h} sx={{ m: 0 }} />
        ))}
      </Stack>
    </Box>
  );
}

function ActionDialog({ open, onClose, onDone, vehicles }: { open: NonNullable<Open>; onClose: () => void; onDone: () => void; vehicles: Vehicle[] }) {
  const titleId = useId();
  const toast = useToast();
  const fullScreen = useMediaQuery((t: Theme) => t.breakpoints.down('sm'));
  const now = Date.now();
  const [hours, setHours] = useState(open.kind === 'reserve' ? '12' : open.kind === 'ev_away' ? '24' : open.kind === 'ev_charge_now' ? '4' : '3');
  const [pct, setPct] = useState('80');
  const [name, setName] = useState('');
  const [from, setFrom] = useState(f.localInput(now));
  const [to, setTo] = useState(f.localInput(now + 3 * 86400e3));
  const [carAway, setCarAway] = useState(vehicles.length > 0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const v = open.vehicle;

  const meta: Record<Kind, { title: string; intro: ReactNode; confirm: string }> = {
    away: { title: 'Going away', intro: 'The plan expects a quiet house while you’re gone and won’t buy power you won’t use.', confirm: 'Save trip' },
    reserve: { title: 'Storm coming', intro: 'Keep the battery high so the house has backup power if the grid goes down.', confirm: 'Keep battery high' },
    hold: { title: 'Pause battery', intro: 'The battery won’t discharge. Solar can still charge it.', confirm: 'Pause battery' },
    no_battery_export: { title: 'Don’t export from battery', intro: 'The battery only powers the home; it won’t sell to the grid.', confirm: 'Stop battery export' },
    ev_charge_now: { title: `Charge ${v?.name ?? 'car'} now`, intro: 'The plan assumes the car is charging and keeps the battery for the house.', confirm: 'Charge now' },
    ev_away: { title: `${v?.name ?? 'Car'} is away`, intro: 'The plan leaves the car out of home use until it’s back.', confirm: 'Mark as away' },
  };
  const m = meta[open.kind];

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const minutes = Math.round(Number(hours) * 60);
    if (open.kind !== 'away' && !(minutes >= 5 && minutes <= 60 * 24 * 14)) {
      setError('Enter between 0.1 and 336 hours.');
      return;
    }
    if (open.kind === 'away' && (!from || !to || to <= from)) {
      setError('The return date must be after the start.');
      return;
    }
    setBusy(true);
    try {
      if (open.kind === 'away') {
        await post('/api/holidays', { name: name.trim() || 'Away', start: from, end: to, ev_away: carAway });
        toast(`Trip saved. Away from ${f.dateTime(from)} to ${f.dateTime(to)}.`);
      } else {
        const body: Record<string, unknown> = { kind: open.kind, minutes };
        if (open.kind === 'reserve') body.pct = Number(pct);
        if (v) body.vehicle_id = v.id;
        await post('/api/overrides', body);
        toast(`${m.title}: on for ${f.duration(minutes)}. The plan is updating.`);
      }
      onDone();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <Dialog open onClose={busy ? undefined : onClose} aria-labelledby={titleId} fullScreen={fullScreen} maxWidth="xs" fullWidth
      slotProps={{ paper: { component: 'form', onSubmit: submit } as object }}>
      <DialogTitle id={titleId}>{m.title}</DialogTitle>
      <DialogContent sx={{ display: 'grid', gap: 2, pt: '4px !important' }}>
        <Typography variant="body2" color="text.secondary">{m.intro}</Typography>
        {open.kind === 'away' && (
          <>
            <TextField label="Name (optional)" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Christmas at the coast" autoFocus
              slotProps={{ htmlInput: { maxLength: 100 } }} />
            <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' }, gap: 2 }}>
              <TextField label="From" type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} required slotProps={{ inputLabel: { shrink: true } }} />
              <TextField label="To" type="datetime-local" value={to} onChange={(e) => setTo(e.target.value)} required slotProps={{ inputLabel: { shrink: true }, htmlInput: { min: from } }} />
            </Box>
            {vehicles.length > 0 && (
              <FormControlLabel control={<Switch checked={carAway} onChange={(e) => setCarAway(e.target.checked)} />} label="Car away too" />
            )}
            <Typography variant="caption" color="text.secondary">Times are in {f.getZone().replace('_', ' ')} time.</Typography>
          </>
        )}
        {open.kind === 'reserve' && (
          <TextField label="Keep at least" type="number" value={pct} onChange={(e) => setPct(e.target.value)} required autoFocus
            slotProps={{ htmlInput: { min: 10, max: 100, step: 5 }, input: { endAdornment: <InputAdornment position="end">%</InputAdornment> } }} />
        )}
        {open.kind !== 'away' && <HoursField value={hours} onChange={setHours} />}
        {error && <Alert severity="error" role="alert">{error}</Alert>}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button type="submit" variant="contained" disabled={busy}>{busy ? 'Saving…' : m.confirm}</Button>
      </DialogActions>
    </Dialog>
  );
}
