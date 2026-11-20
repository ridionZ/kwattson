import { useEffect, useId, useState, type FormEvent } from 'react';
import Accordion from '@mui/material/Accordion';
import AccordionDetails from '@mui/material/AccordionDetails';
import AccordionSummary from '@mui/material/AccordionSummary';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Checkbox from '@mui/material/Checkbox';
import Chip from '@mui/material/Chip';
import Dialog from '@mui/material/Dialog';
import DialogActions from '@mui/material/DialogActions';
import DialogContent from '@mui/material/DialogContent';
import DialogTitle from '@mui/material/DialogTitle';
import FormControlLabel from '@mui/material/FormControlLabel';
import Stack from '@mui/material/Stack';
import Tab from '@mui/material/Tab';
import Tabs from '@mui/material/Tabs';
import TextField from '@mui/material/TextField';
import ToggleButton from '@mui/material/ToggleButton';
import ToggleButtonGroup from '@mui/material/ToggleButtonGroup';
import Typography from '@mui/material/Typography';
import useMediaQuery from '@mui/material/useMediaQuery';
import type { Theme } from '@mui/material/styles';
import ExpandMoreRounded from '@mui/icons-material/ExpandMoreRounded';
import { errorText, get, post } from '../lib/api';
import * as f from '../lib/fmt';
import { useToast } from './common';

type Mode = 'time' | 'percentage' | 'ready_by';

export interface ChargingVehicle {
  id: string;
  name: string;
  can_stop_charging?: boolean;
  can_schedule_charging?: boolean;
  target_pct?: number;
  manual_soc?: number | null;
  reading?: { soc?: number | null; ts?: string };
}

interface Window { start: string; end: string }
interface TariffWindow extends Window { rate: number; overflow_rate: number | null }
export interface ChargeEstimate {
  soc?: number | null;
  soc_at?: string | null;
  charging?: boolean;
  automatic?: boolean;
  charging_outside_window?: boolean;
  continuous_end_soc?: number | null;
  continuous_target_reachable?: boolean | null;
  finish: string;
  deadline?: string;
  energy_kwh: number;
  end_soc: number | null;
  target_reachable: boolean | null;
  kw: number;
  rate_basis: 'measured' | 'learned' | 'configured';
  paid_grid_kwh: number | null;
  paid_grid_aud: number | null;
  solar_kwh?: number;
  battery_kwh?: number;
  grid_kwh?: number;
  windows?: Window[];
  tariffs?: TariffWindow[];
  rate_options?: number[];
}

const HOURS = [1, 3, 6, 12, 24];
const TARGETS = ['50', '80', '100', 'Custom'];

function rateLabel(rate: number) {
  return rate === 0 ? 'Free' : `${Number((100 * rate).toFixed(3))}¢/kWh`;
}

function ChargeTimeline({ tariffs, windows }: { tariffs: TariffWindow[]; windows: Window[] }) {
  if (!tariffs.length) return null;
  const start = Date.parse(tariffs[0].start);
  const span = Date.parse(tariffs[tariffs.length - 1].end) - start;
  const rates = tariffs.map(t => t.rate);
  const low = Math.min(...rates);
  const high = Math.max(...rates);
  const position = (at: string) => 100 * (Date.parse(at) - start) / span;
  const bounds = (w: Window) => ({ left: `${position(w.start)}%`, width: `${position(w.end) - position(w.start)}%` });
  return (
    <Box component="figure" aria-label="Grid rates and charging windows" sx={{ m: 0, mb: 1 }}>
      <Box sx={{ position: 'relative', height: 29, bgcolor: 'action.hover', borderRadius: 1, overflow: 'hidden' }}>
        {tariffs.map(w => {
          const price = w.overflow_rate != null ? `${rateLabel(w.rate)} · then ${rateLabel(w.overflow_rate)} after allowance` : rateLabel(w.rate);
          return <Box key={w.start} title={`${f.when(w.start)} to ${f.when(w.end)} · ${price}`}
            sx={{ ...bounds(w), position: 'absolute', height: '100%', display: 'grid', placeItems: 'center', overflow: 'hidden',
              bgcolor: w.rate === low ? 'battery.light' : w.rate === high ? 'solar.light' : 'action.selected', borderRight: '1px solid', borderColor: 'divider' }}>
            {position(w.end) - position(w.start) >= 13 && <Typography variant="caption" noWrap>{rateLabel(w.rate)}</Typography>}
          </Box>;
        })}
      </Box>
      <Typography variant="caption" component="div" sx={{ mt: .5 }}>Charging</Typography>
      <Box sx={{ position: 'relative', height: 12, bgcolor: 'action.hover', borderRadius: 1, overflow: 'hidden', mt: .5 }}>
        {windows.map(w => <Box key={w.start} title={`Charging ${f.when(w.start)} to ${f.when(w.end)}`}
          sx={{ ...bounds(w), position: 'absolute', height: '100%', bgcolor: 'battery.main' }} />)}
      </Box>
      <Stack direction="row" sx={{ justifyContent: "space-between" }}>
        <Typography variant="caption">{f.when(tariffs[0].start)}</Typography>
        <Typography variant="caption">{f.when(tariffs[tariffs.length - 1].end)}</Typography>
      </Stack>
      <Stack component="ul" sx={{ m: 0, mt: 1, pl: 2.5 }} spacing={.25}>
        {tariffs.map(w => <Typography component="li" variant="caption" key={w.start}>
          {f.when(w.start)}–{f.when(w.end)} · {rateLabel(w.rate)}
          {w.overflow_rate != null && ` · then ${rateLabel(w.overflow_rate)} after allowance`}
        </Typography>)}
      </Stack>
    </Box>
  );
}

export default function ChargeDialog({ vehicle, onClose, onDone }: {
  vehicle: ChargingVehicle; onClose: () => void; onDone: () => void;
}) {
  const id = useId();
  const toast = useToast();
  const fullScreen = useMediaQuery((t: Theme) => t.breakpoints.down('sm'));
  const [openedAt] = useState(Date.now);
  const [mode, setMode] = useState<Mode>('time');
  const [timeKind, setTimeKind] = useState<'in' | 'at'>('in');
  const [hours, setHours] = useState('4');
  const [clock, setClock] = useState(() => f.localInput(openedAt + 3 * 3600e3));
  const [deadline, setDeadline] = useState(() => `${f.dayKey(openedAt + 86400e3)}T10:00`);
  const initialTarget = String(vehicle.target_pct ?? 80);
  const [target, setTarget] = useState(TARGETS.includes(initialTarget) ? initialTarget : 'Custom');
  const [customTarget, setCustomTarget] = useState(initialTarget);
  const [startingSoc, setStartingSoc] = useState(vehicle.manual_soc == null ? '' : String(vehicle.manual_soc));
  const [stop, setStop] = useState(!!vehicle.can_stop_charging);
  const [automatic, setAutomatic] = useState(!!vehicle.can_schedule_charging);
  const [allowedRates, setAllowedRates] = useState<number[] | null>(null);
  const [rateOptions, setRateOptions] = useState<number[]>([]);
  const [tariffsOpen, setTariffsOpen] = useState(false);
  const [preview, setPreview] = useState<ChargeEstimate | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const hasLiveSoc = vehicle.reading?.soc != null && f.ageMinutes(vehicle.reading.ts) <= 60;
  const readyBy = mode === 'ready_by';
  const duration = mode === 'time' && timeKind === 'in';
  const endAt = readyBy ? deadline : mode === 'time' && timeKind === 'at' ? clock : null;
  const minutes = duration ? Math.round(Number(hours) * 60) : 120;
  const maxHours = stop && !readyBy ? 24 : 48;
  const targetPct = mode === 'time' ? null : Number(target === 'Custom' ? customTarget : target);
  const initialPct = hasLiveSoc || startingSoc === '' ? null : Number(startingSoc);
  const valid = (!duration || (minutes >= 6 && minutes <= 60 * maxHours))
    && (endAt === null || endAt > f.localInput(Date.now()))
    && (targetPct === null || targetPct > 0 && targetPct <= 100)
    && (initialPct === null || initialPct >= 0 && initialPct <= 100);
  const fields = {
    kind: 'ev_charge_now', vehicle_id: vehicle.id, charge_mode: mode, minutes, end_at: endAt,
    target_pct: targetPct, starting_soc: initialPct, stop_at_limit: !readyBy && stop,
    auto_schedule: readyBy && automatic, grid_rates: readyBy ? allowedRates : null,
  };
  const params = new URLSearchParams({
    vehicle_id: vehicle.id, charge_mode: mode, minutes: String(minutes), auto_schedule: String(fields.auto_schedule),
    stop_at_limit: String(fields.stop_at_limit),
  });
  if (endAt !== null) params.set('end_at', endAt);
  if (targetPct !== null) params.set('target_pct', String(targetPct));
  if (initialPct !== null) params.set('starting_soc', String(initialPct));
  if (fields.grid_rates !== null) {
    params.set('restrict_grid', 'true');
    fields.grid_rates.forEach(rate => params.append('grid_rates', String(rate)));
  }
  const query = params.toString();
  const extraRate = allowedRates !== null && preview?.tariffs
    ? [...new Set(preview.tariffs.flatMap(t => t.overflow_rate === null ? [t.rate] : [t.rate, t.overflow_rate]))]
      .sort((a, b) => a - b).find(rate => !allowedRates.includes(rate))
    : undefined;
  useEffect(() => {
    setPreview(null);
    setPreviewError(null);
    if (!valid) return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        const result = await get<ChargeEstimate>(`/api/overrides/charge-preview?${query}`);
        if (!cancelled) {
          setPreview(result);
          if (result.rate_options) setRateOptions(result.rate_options);
        }
      } catch (e) {
        if (!cancelled) setPreviewError(errorText(e));
      }
    }, 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [query, valid, vehicle.reading?.ts]);

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!valid || (readyBy && preview?.target_reachable == null)) return;
    setError(null);
    setBusy(true);
    try {
      await post('/api/overrides', fields);
      toast(fields.auto_schedule ? 'Automatic charging schedule saved.' : fields.stop_at_limit
        ? 'Charge limit saved. kWattson will request and verify a stop.' : 'Estimate saved. The car will not be controlled.');
      onDone();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  return (
    <Dialog open onClose={busy ? undefined : onClose} aria-labelledby={`${id}-title`} fullScreen={fullScreen} maxWidth="sm" fullWidth
      slotProps={{ paper: { component: 'form', onSubmit: save } as object }}>
      <DialogTitle id={`${id}-title`}>Plan {vehicle.name} charging</DialogTitle>
      <Tabs value={mode} onChange={(_, next: Mode) => { setMode(next); setError(null); }} variant="fullWidth"
        aria-label="Charging goal" textColor="secondary" indicatorColor="secondary" sx={{ mx: { xs: 2, sm: 3 }, mb: 2, borderBottom: 1, borderColor: 'divider' }}>
        {([['time', 'Time'], ['percentage', 'Percentage'], ['ready_by', 'Ready by']] as const).map(([key, label]) =>
          <Tab key={key} value={key} label={label} id={`${id}-tab-${key}`} aria-controls={`${id}-panel-${key}`} sx={{ minWidth: 0 }} />)}
      </Tabs>
      <DialogContent sx={{ pt: '4px !important' }}>
        <Stack role="tabpanel" id={`${id}-panel-${mode}`} aria-labelledby={`${id}-tab-${mode}`} spacing={2}>
          {mode === 'time' && (
            <>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5} sx={{ alignItems: { sm: 'center' } }}>
                <ToggleButtonGroup exclusive size="small" value={timeKind} onChange={(_, next) => next && setTimeKind(next)} aria-label="Stop timing">
                  <ToggleButton value="in">In</ToggleButton>
                  <ToggleButton value="at">At</ToggleButton>
                </ToggleButtonGroup>
                {duration
                  ? <TextField label="Stop in (hours)" type="number" value={hours} onChange={e => setHours(e.target.value)}
                    required slotProps={{ htmlInput: { min: .1, max: maxHours, step: 'any' } }} />
                  : <TextField label="Stop at" type="datetime-local" value={clock} onChange={e => setClock(e.target.value)}
                    required slotProps={{ inputLabel: { shrink: true } }} />}
              </Stack>
              <Stack direction="row" useFlexGap spacing={.75} sx={{ flexWrap: "wrap" }} role="group" aria-label="Quick durations">
                {HOURS.map(h => <Chip key={h} label={`${h} h`} onClick={() => { setHours(String(h)); setTimeKind('in'); }}
                  aria-pressed={duration && Number(hours) === h} variant={duration && Number(hours) === h ? 'filled' : 'outlined'}
                  color={duration && Number(hours) === h ? 'secondary' : 'default'} />)}
              </Stack>
            </>
          )}
          {mode !== 'time' && (
            <>
              <Box role="group" aria-label="Charge target">
                <Typography variant="body2" sx={{ mb: .75 }}>Charge target</Typography>
                <Stack direction="row" useFlexGap spacing={.75} sx={{ flexWrap: "wrap" }}>
                  {TARGETS.map(option => <Chip key={option} label={option === 'Custom' ? option : `${option}%`}
                    variant={target === option ? 'filled' : 'outlined'} color={target === option ? 'secondary' : 'default'}
                    onClick={() => setTarget(option)} aria-pressed={target === option} />)}
                </Stack>
              </Box>
              {target === 'Custom' && <TextField label="Target battery %" type="number" value={customTarget}
                onChange={e => setCustomTarget(e.target.value)} required slotProps={{ htmlInput: { min: .1, max: 100, step: 'any' } }} />}
            </>
          )}
          {readyBy && <TextField label="Ready by" type="datetime-local" value={deadline} onChange={e => setDeadline(e.target.value)}
            required slotProps={{ inputLabel: { shrink: true } }} />}
          {endAt !== null && <Typography variant="caption" color="text.secondary">Times are in {f.getZone().replace('_', ' ')} time.</Typography>}
          {!hasLiveSoc && <TextField label={readyBy ? 'Current battery %' : 'Current battery % (optional)'} type="number"
            value={startingSoc} onChange={e => setStartingSoc(e.target.value)} required={readyBy}
            slotProps={{ htmlInput: { min: 0, max: 100, step: 'any' } }} />}
          {preview && (
            <Box aria-live="polite" sx={{ px: 2, py: 1.5, bgcolor: 'battery.light', color: 'battery.main', borderRadius: 2 }}>
              <Typography variant="body2" sx={{ fontWeight: 600 }}>Estimated at {readyBy ? 'departure' : 'stop'}</Typography>
              <Stack direction="row" spacing={1} sx={{ alignItems: "baseline", flexWrap: "wrap" }}>
                <Typography variant="h4" component="p">{preview.end_soc == null ? 'Battery % unknown' : f.pct(preview.end_soc)}</Typography>
                {preview.energy_kwh > 0 && <Typography variant="body2">
                  {readyBy && preview.target_reachable === false ? `by ${f.when(preview.deadline)}` : `at ${f.when(preview.finish)}`}
                </Typography>}
                {preview.target_reachable && preview.energy_kwh === 0 && <Typography variant="body2">Target already reached</Typography>}
              </Stack>
              {preview.energy_kwh > 0 && <Typography variant="body2">
                {f.n(preview.kw)} kW {readyBy ? 'planning rate' : `(${preview.rate_basis})`}
                {!!preview.solar_kwh && ` · ${f.kwh(preview.solar_kwh)} solar`}
                {!!preview.battery_kwh && ` · ${f.kwh(preview.battery_kwh)} home battery`}
              </Typography>}
              {preview.paid_grid_aud != null && !preview.paid_grid_kwh && <Typography variant="body2">
                {preview.paid_grid_aud < 0 ? `${f.money(-preview.paid_grid_aud)} estimated grid credit` : 'No paid grid power expected'}
              </Typography>}
            </Box>
          )}
          {!!preview?.paid_grid_kwh && <Alert severity="warning">
            About {f.kwh(preview.paid_grid_kwh)} of paid grid power ({f.money(preview.paid_grid_aud)} estimated).
          </Alert>}
          {preview?.target_reachable === false && readyBy && <Alert severity="warning">
            {f.pct(targetPct)} is unlikely by {f.when(preview.deadline)}. Expect about {f.pct(preview.end_soc)} with this plan.
            {extraRate !== undefined && allowedRates !== null && <Box sx={{ mt: .5 }}>
              <Button size="small" color="inherit" onClick={() => setAllowedRates([...allowedRates, extraRate].sort((a, b) => a - b))}>
                Also allow {rateLabel(extraRate)}
              </Button>
            </Box>}
          </Alert>}
          {readyBy && allowedRates !== null && <Alert severity="info">
            Only the selected grid rates are permitted. A deadline does not override this restriction; allow another rate if the target cannot be reached.
          </Alert>}
          {preview && preview.energy_kwh > 0 && preview.paid_grid_aud === null &&
            <Typography variant="body2" color="text.secondary">Grid cost estimate unavailable.</Typography>}
          {preview?.target_reachable == null && mode === 'percentage' && !hasLiveSoc && initialPct == null &&
            <Typography variant="body2" color="text.secondary">Enter the current battery percentage for a finish time.</Typography>}
          {preview && readyBy && !!preview.windows?.length && <Typography variant="body2">
            <strong>Charging windows</strong>{' '}
            {preview.windows.map(w => `${f.when(w.start)}–${f.when(w.end)}`).join(', ')}
          </Typography>}
          {readyBy && (
            <Accordion disableGutters elevation={0} expanded={tariffsOpen} onChange={(_, expanded) => setTariffsOpen(expanded)}
              sx={{ bgcolor: 'action.hover', borderRadius: 2, '&::before': { display: 'none' } }}>
              <AccordionSummary expandIcon={<ExpandMoreRounded />} aria-controls={`${id}-rates`} id={`${id}-rates-label`}>
                <Typography variant="body2" sx={{ fontWeight: 600 }}>Grid rates{allowedRates !== null && ` · ${allowedRates.length ? allowedRates.map(rateLabel).join(', ') : 'No grid use'}`}</Typography>
              </AccordionSummary>
              <AccordionDetails>
                {preview?.tariffs && (Number(preview.grid_kwh) > 0 || allowedRates !== null) &&
                  <ChargeTimeline tariffs={preview.tariffs} windows={preview.windows ?? []} />}
                <FormControlLabel label="Allow any grid rate" control={<Checkbox checked={allowedRates === null}
                  onChange={e => setAllowedRates(e.target.checked ? null : rateOptions.filter(r => r === 0))} />} />
                {allowedRates !== null && <Stack direction="row" sx={{ flexWrap: "wrap" }} role="group" aria-label="Allowed grid rates">
                  {rateOptions.map(rate => <FormControlLabel key={rate} label={rateLabel(rate)}
                    control={<Checkbox checked={allowedRates.includes(rate)} onChange={e =>
                      setAllowedRates(e.target.checked ? [...allowedRates, rate].sort((a, b) => a - b) : allowedRates.filter(r => r !== rate))} />} />)}
                </Stack>}
                {allowedRates !== null && <Typography variant="caption" color="text.secondary">
                  Spare solar and home battery energy can also supply the car outside these grid rates.
                  {automatic && ' Charging pauses if home energy runs short or readings become stale. Cloud control delays can allow brief paid imports.'}
                </Typography>}
              </AccordionDetails>
            </Accordion>
          )}
          {readyBy
            ? <Box>
                {vehicle.can_schedule_charging && <FormControlLabel label="Run this schedule automatically"
                  control={<Checkbox checked={automatic} onChange={e => setAutomatic(e.target.checked)} />} />}
                <Typography variant="body2" color="text.secondary">{automatic
                  ? 'Leave the car plugged in at home. Automatic start and stop depend on the car connection staying online.'
                  : 'Set the charging windows on your car or charger.'}</Typography>
                {!automatic && <Alert severity="warning" sx={{ mt: 1 }}>Estimate only. Saving this plan will not start or stop your car.</Alert>}
              </Box>
            : <Box>
                {vehicle.can_stop_charging && <FormControlLabel label="Ask the car to stop at this limit"
                  control={<Checkbox checked={stop} onChange={e => setStop(e.target.checked)} />} />}
                <Typography variant="body2" color="text.secondary">{stop
                  ? 'Start charging at home. The car connection can take several minutes to report or confirm a stop.'
                  : 'Set the stop on your car or charger.'}</Typography>
                {!stop && <Alert severity="warning" sx={{ mt: 1 }}>Automatic stopping is off. The car may keep charging beyond this target or time.</Alert>}
              </Box>}
          {!preview && valid && !previewError && <Typography variant="body2" color="text.secondary" role="status">Estimating charging…</Typography>}
          {(error || previewError) && <Alert severity="error">{error || previewError}</Alert>}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button type="submit" variant="contained" disabled={busy || !valid || !!previewError || (readyBy && preview?.target_reachable == null)}>
          {busy ? 'Saving…' : readyBy && automatic ? 'Save automatic schedule' : !readyBy && stop ? 'Save automatic stop' : 'Save estimate only'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
