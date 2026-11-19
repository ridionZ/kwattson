import { useEffect, useId, useState, type FormEvent, type ReactNode } from 'react';
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
import Checkbox from '@mui/material/Checkbox';
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
import { get, post, errorText } from '../lib/api';
import * as f from '../lib/fmt';
import { useToast } from './common';

type Kind = 'away' | 'reserve' | 'hold' | 'no_battery_export' | 'ev_charge_now' | 'ev_away';
interface Vehicle {
  id: string;
  name: string;
  can_stop_charging?: boolean;
  manual_soc?: number | null;
  reading?: { soc?: number | null; ts?: string };
}

export interface ChargeEstimate {
  finish: string;
  energy_kwh: number;
  end_soc: number | null;
  target_reachable: boolean | null;
  kw: number;
  rate_basis: 'measured' | 'learned' | 'configured';
  paid_grid_kwh: number | null;
  paid_grid_aud: number | null;
}
type Open = { kind: Kind; vehicle?: Vehicle } | null;

export default function QuickActions({ vehicles, onDone }: { vehicles: Vehicle[]; onDone: () => void }) {
  const [open, setOpen] = useState<Open>(null);
  const items: { kind: Kind; label: string; Icon: typeof LuggageRounded; vehicle?: Vehicle; tone: string }[] = [
    { kind: 'away', label: 'Going away', Icon: LuggageRounded, tone: 'primary' },
    { kind: 'reserve', label: 'Storm coming', Icon: ThunderstormRounded, tone: 'battery' },
    { kind: 'hold', label: 'Pause battery', Icon: PauseCircleRounded, tone: 'battery' },
    { kind: 'no_battery_export', label: 'Don’t export from battery', Icon: BlockRounded, tone: 'primary' },
    ...vehicles.flatMap((v) => [
      { kind: 'ev_charge_now' as Kind, label: `Plan ${v.name} charging`, Icon: EvStationRounded, vehicle: v, tone: 'battery' },
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

function HoursField({ value, onChange, label = 'For how long', max = 336 }: { value: string; onChange: (v: string) => void; label?: string; max?: number }) {
  const id = useId();
  const num = Number(value);
  const invalid = value !== '' && (!(num >= 0.1) || num > max);
  return (
    <Box>
      <TextField label={label} type="number" value={value} onChange={(e) => onChange(e.target.value)} required error={invalid}
        helperText={invalid ? `Enter between 0.1 and ${max} hours.` : ' '}
        slotProps={{ htmlInput: { min: 0.1, max, step: 'any', 'aria-describedby': id }, input: { endAdornment: <InputAdornment position="end">hours</InputAdornment> } }} />
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
  const vehicleId = open.kind === 'ev_charge_now' ? v?.id : undefined;
  const [target, setTarget] = useState('Time only');
  const [customTarget, setCustomTarget] = useState('75');
  const [startingSoc, setStartingSoc] = useState(v?.manual_soc == null ? '' : String(v.manual_soc));
  const [stopAtLimit, setStopAtLimit] = useState(false);
  const [preview, setPreview] = useState<ChargeEstimate | null>(null);
  const [previewFailed, setPreviewFailed] = useState(false);
  const hasLiveSoc = v?.reading?.soc != null && f.ageMinutes(v.reading.ts, now) <= 60;
  const minutes = Math.round(Number(hours) * 60);
  const maxHours = stopAtLimit ? 24 : 336;
  const targetPct = target === 'Time only' ? null : Number(target === 'Custom' ? customTarget : target);
  const initialPct = hasLiveSoc || startingSoc === '' ? null : Number(startingSoc);
  const valid = minutes >= 6 && minutes <= 60 * maxHours
    && (targetPct === null || targetPct > 0 && targetPct <= 100)
    && (initialPct === null || initialPct >= 0 && initialPct <= 100);
  useEffect(() => {
    setPreview(null);
    setPreviewFailed(false);
    if (!vehicleId || !valid) return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      const query = new URLSearchParams({ vehicle_id: vehicleId, minutes: String(minutes) });
      if (targetPct !== null) query.set('target_pct', String(targetPct));
      if (initialPct !== null) query.set('starting_soc', String(initialPct));
      try {
        const result = await get<ChargeEstimate>(`/api/overrides/charge-preview?${query}`);
        if (!cancelled) setPreview(result);
      } catch {
        if (!cancelled) setPreviewFailed(true);
      }
    }, 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [vehicleId, minutes, targetPct, initialPct, valid]);

  const meta: Record<Kind, { title: string; intro: ReactNode; confirm: string }> = {
    away: { title: 'Going away', intro: 'The plan expects a quiet house while you’re gone and won’t buy power you won’t use.', confirm: 'Save trip' },
    reserve: { title: 'Storm coming', intro: 'Keep the battery high so the house has backup power if the grid goes down.', confirm: 'Keep battery high' },
    hold: { title: 'Pause battery', intro: 'The battery won’t discharge. Solar can still charge it.', confirm: 'Pause battery' },
    no_battery_export: { title: 'Don’t export from battery', intro: 'The battery only powers the home; it won’t sell to the grid.', confirm: 'Stop battery export' },
    ev_charge_now: { title: `Plan ${v?.name ?? 'car'} charging`, intro: 'Plug in at home and start charging before saving. Choose a time limit and an optional battery target.', confirm: 'Save charge plan' },
    ev_away: { title: `${v?.name ?? 'Car'} is away`, intro: 'The plan leaves the car out of home use until it’s back.', confirm: 'Mark as away' },
  };
  const m = meta[open.kind];

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (open.kind !== 'away' && !valid) {
      setError('Check the duration and battery percentages.');
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
        if (vehicleId) {
          body.target_pct = targetPct;
          body.starting_soc = initialPct;
          body.stop_at_limit = stopAtLimit;
        }
        await post('/api/overrides', body);
        toast(vehicleId ? 'Charging plan saved.' : `${m.title}: on for ${f.duration(minutes)}. The plan is updating.`);
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
        {open.kind !== 'away' && <HoursField value={hours} onChange={setHours} max={maxHours} label={vehicleId ? 'Maximum charging time' : undefined} />}
        {vehicleId && (
          <>
            <Box role="group" aria-label="Charge target">
              <Typography variant="body2" sx={{ mb: 0.75 }}>Charge target</Typography>
              <Stack direction="row" useFlexGap sx={{ gap: 0.75, flexWrap: 'wrap' }}>
                {['Time only', '50', '80', '100', 'Custom'].map((option) => (
                  <Chip key={option} label={Number(option) ? `${option}%` : option}
                    color={target === option ? 'primary' : 'default'} variant={target === option ? 'filled' : 'outlined'}
                    onClick={() => setTarget(option)} aria-pressed={target === option} />
                ))}
              </Stack>
            </Box>
            {target === 'Custom' && <TextField label="Target battery %" type="number" value={customTarget}
              onChange={(e) => setCustomTarget(e.target.value)} required slotProps={{ htmlInput: { min: 1, max: 100, step: 'any' } }} />}
            {!hasLiveSoc && <TextField label="Current battery % (optional)" type="number" value={startingSoc}
              onChange={(e) => setStartingSoc(e.target.value)} slotProps={{ htmlInput: { min: 0, max: 100, step: 'any' } }} />}
            {v?.can_stop_charging && <FormControlLabel label="Stop charging at the first limit"
              control={<Checkbox checked={stopAtLimit} onChange={(e) => setStopAtLimit(e.target.checked)} />} />}
            <Typography variant="body2" color="text.secondary">
              {stopAtLimit
                ? 'kWattson will ask the car to stop at the first limit. iSMART updates can take 10 minutes or longer. Use the car or charger timer for a firm limit.'
                : 'Use the car or charger timer to stop charging. This plan updates your home energy forecast.'}
            </Typography>
            {preview ? (
              <Box aria-live="polite">
                <Typography variant="body2" sx={{ fontWeight: 600 }}>
                  {preview.energy_kwh === 0 ? 'Battery limit already reached' : `Estimated finish ${f.when(preview.finish)}`}
                </Typography>
                {preview.energy_kwh > 0 && <Typography variant="body2" color="text.secondary">
                  {f.n(preview.kw)} kW ({preview.rate_basis}) · {f.kwh(preview.energy_kwh)}
                  {preview.end_soc !== null && ` · about ${f.pct(preview.end_soc)} at finish`}
                </Typography>}
                {targetPct !== null && preview.target_reachable === null && <Typography variant="body2" color="text.secondary">Enter the current battery percentage for a target estimate.</Typography>}
                {preview.target_reachable === false && <Typography variant="body2" color="text.secondary">The time limit is expected to come first.</Typography>}
                {preview.paid_grid_kwh ? <Alert severity="warning" sx={{ mt: 1 }}>
                  Estimated paid grid power {f.kwh(preview.paid_grid_kwh)} ({f.money(preview.paid_grid_aud)}).
                </Alert> : <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                  {preview.paid_grid_aud === null ? 'A current energy plan covering the session is needed to estimate grid cost.' : 'No paid grid power expected.'}
                </Typography>}
              </Box>
            ) : valid && <Typography variant="body2" color="text.secondary" role="status">{previewFailed ? 'Charging estimate unavailable.' : 'Estimating charging…'}</Typography>}
          </>
        )}
        {error && <Alert severity="error" role="alert">{error}</Alert>}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button type="submit" variant="contained" disabled={busy || (!!vehicleId && !valid)}>{busy ? 'Saving…' : m.confirm}</Button>
      </DialogActions>
    </Dialog>
  );
}
