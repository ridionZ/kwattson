import { useId, useState, type FormEvent } from 'react';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import Dialog from '@mui/material/Dialog';
import DialogActions from '@mui/material/DialogActions';
import DialogContent from '@mui/material/DialogContent';
import DialogTitle from '@mui/material/DialogTitle';
import MenuItem from '@mui/material/MenuItem';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';
import AddRounded from '@mui/icons-material/AddRounded';
import * as f from '../lib/fmt';
import { errorText, post } from '../lib/api';
import { HelpTip, Section, useToast } from './common';
import Scheduled, { describe, type Override } from './Scheduled';

interface Vehicle { id: string; name: string }
interface Recommendation { start: string; end: string; reason?: string; cost_aud: number }
interface Projection {
  day: string; soc_pct: number | null; soc_upper_pct: number | null;
  source: 'estimate' | 'observed' | null; planned_charge_pct?: number;
}
export interface VehicleOutlook {
  vehicle: string; name: string; soc: number | null; projection: Projection[];
  recommendation: Recommendation | null; suggested_day?: string | null;
}
interface Draft { vehicle: string; start?: string; hours?: string }

const HOURS = [1, 3, 6, 12];
const calendar = new Intl.DateTimeFormat('en-AU', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
const dayLabel = (day: string, now: number) => day === f.dayKey(now) ? 'Today' : calendar.format(new Date(`${day}T00:00:00Z`));

export default function EvPlan({ vehicles, outlook, charges, canEdit, onChanged, now, horizonEnd }: {
  vehicles: Vehicle[]; outlook: VehicleOutlook[]; charges: Override[]; canEdit: boolean;
  onChanged: () => void; now: number; horizonEnd?: number;
}) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const items = describe([], charges, vehicles, now);
  const outsideHorizon = charges.some(x => horizonEnd === undefined || Date.parse(x.starts) >= horizonEnd);
  return (
    <Section title="EV charging" action={canEdit &&
      <Button size="small" variant="outlined" startIcon={<AddRounded />} onClick={() => setDraft({ vehicle: vehicles[0].id })}>
        Add future charge
      </Button>}>
      <Stack spacing={2}>
        {outlook.map(v => (
          <Box key={v.vehicle} sx={{ pb: 2, borderBottom: 1, borderColor: 'divider' }}>
            <Typography variant="h3">{v.name}{v.soc != null && ` · ${f.pct(v.soc)} reported or entered`}</Typography>
            {v.recommendation ? (
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: .5, alignItems: { sm: 'center' } }}>
                <Box>
                  <Typography variant="body2"><strong>Suggested window</strong> {f.dateTime(v.recommendation.start)}–{f.when(v.recommendation.end, now)}</Typography>
                  <Typography variant="caption" color="text.secondary">{f.money(v.recommendation.cost_aud)} estimated energy cost. {v.recommendation.reason}</Typography>
                </Box>
                {canEdit && Date.parse(v.recommendation.start) > now + 60000 &&
                  <Button size="small" onClick={() => {
                    const start = Date.parse(v.recommendation!.start);
                    return setDraft({ vehicle: v.vehicle, start: f.localInput(Math.ceil(start / 60000) * 60000),
                      hours: String(Math.round((Date.parse(v.recommendation!.end) - start) / 60000) / 60) });
                  }}>Use window</Button>}
              </Stack>
            ) : (
              <Typography variant="body2" color="text.secondary" sx={{ mt: .5 }}>
                {v.suggested_day ? `Consider charging by ${v.suggested_day === f.dayKey(now) ? 'today' : dayLabel(v.suggested_day, now)}.`
                  : v.soc == null ? 'A current battery percentage is needed for charging suggestions.'
                  : 'No additional charging window suggested.'}
              </Typography>
            )}
            {v.soc != null && v.projection.length > 0 && (
              <>
                <Typography variant="caption" component="p" color="text.secondary" sx={{ mt: 1.25, mb: .5 }}>
                  Estimated battery at the end of each day
                  {v.projection.some(p => p.soc_upper_pct != null) && <HelpTip label="EV forecast limits">
                    A percentage marked ≤ assumes no driving on days without enough data. The car could have less charge.
                  </HelpTip>}
                </Typography>
                <Box component="ul" sx={{ m: 0, p: 0, listStyle: 'none', display: 'grid', gap: .75,
                  gridTemplateColumns: 'repeat(auto-fit, minmax(76px, 1fr))' }}>
                  {v.projection.map(p => (
                    <Box component="li" key={p.day} sx={{ px: .75, py: .75, borderRadius: 1, bgcolor: 'action.hover' }}>
                      <Typography variant="caption" component="div">{dayLabel(p.day, now)}</Typography>
                      <Typography variant="body2" sx={{ fontWeight: 600 }}>
                        {p.soc_pct != null ? f.pct(p.soc_pct) : p.soc_upper_pct != null ? `≤ ${f.pct(p.soc_upper_pct)}` : 'Unknown'}
                      </Typography>
                      {!!p.planned_charge_pct && <Typography variant="caption" color="battery.main">Charge planned</Typography>}
                    </Box>
                  ))}
                </Box>
              </>
            )}
          </Box>
        ))}
        <Box>
          <Typography variant="h3">Planned charges</Typography>
          {items.length ? <Scheduled items={items} canEdit={canEdit} onChanged={onChanged} />
            : <Typography variant="body2" color="text.secondary" sx={{ mt: .5 }}>No future charges saved.</Typography>}
          {outsideHorizon && <Typography variant="caption" color="text.secondary">
            Dates beyond the current forecast will enter the home energy plan as they get closer.
          </Typography>}
        </Box>
      </Stack>
      {draft && <FutureChargeDialog vehicles={vehicles} draft={draft} onClose={() => setDraft(null)}
        onDone={() => { setDraft(null); onChanged(); }} />}
    </Section>
  );
}

function FutureChargeDialog({ vehicles, draft, onClose, onDone }: {
  vehicles: Vehicle[]; draft: Draft; onClose: () => void; onDone: () => void;
}) {
  const id = useId();
  const toast = useToast();
  const [vehicle, setVehicle] = useState(draft.vehicle);
  const [start, setStart] = useState(() => draft.start ?? `${f.dayKey(Date.now() + 86400e3)}T20:00`);
  const [hours, setHours] = useState(draft.hours ?? '3');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const minutes = Math.round(Number(hours) * 60);
  const minDate = f.localInput(Date.now());
  const valid = start > minDate && minutes >= 6 && minutes <= 14 * 24 * 60;

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    setError(null);
    try {
      await post('/api/overrides', { kind: 'ev_charge_planned', vehicle_id: vehicle, start_at: start, minutes });
      toast('Charge added. The home energy plan is updating.');
      onDone();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  return (
    <Dialog open fullWidth maxWidth="xs" onClose={busy ? undefined : onClose} aria-labelledby={id}
      slotProps={{ paper: { component: 'form', onSubmit: save } as object }}>
      <DialogTitle id={id}>Add future charge</DialogTitle>
      <DialogContent sx={{ pt: '4px !important' }}>
        <Stack spacing={2}>
          <Typography variant="body2" color="text.secondary">
            Tell kWattson when to expect charging at home. Start the charge from your car or charger.
          </Typography>
          {vehicles.length > 1 && <TextField select label="Vehicle" value={vehicle} onChange={e => setVehicle(e.target.value)}>
            {vehicles.map(v => <MenuItem key={v.id} value={v.id}>{v.name}</MenuItem>)}
          </TextField>}
          <TextField label="Start charging at" type="datetime-local" value={start} onChange={e => setStart(e.target.value)} required
            helperText={`Site time · ${f.getZone().replace('_', ' ')}`}
            slotProps={{ inputLabel: { shrink: true }, htmlInput: { min: minDate } }} />
          <TextField label="Charging duration (hours)" type="number" value={hours} onChange={e => setHours(e.target.value)}
            required slotProps={{ htmlInput: { min: .1, max: 336, step: 'any' } }} />
          <Stack direction="row" useFlexGap spacing={.75} sx={{ flexWrap: 'wrap' }} role="group" aria-label="Quick durations">
            {HOURS.map(h => <Chip key={h} label={`${h} h`} onClick={() => setHours(String(h))} aria-pressed={Number(hours) === h}
              color={Number(hours) === h ? 'secondary' : 'default'} variant={Number(hours) === h ? 'filled' : 'outlined'} />)}
          </Stack>
          {error && <Alert severity="error">{error}</Alert>}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button type="submit" variant="contained" disabled={busy || !valid}>{busy ? 'Saving…' : 'Save charge'}</Button>
      </DialogActions>
    </Dialog>
  );
}
