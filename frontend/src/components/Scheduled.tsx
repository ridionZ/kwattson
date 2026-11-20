import { useState } from 'react';
import List from '@mui/material/List';
import ListItem from '@mui/material/ListItem';
import ListItemIcon from '@mui/material/ListItemIcon';
import ListItemText from '@mui/material/ListItemText';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import Typography from '@mui/material/Typography';
import Stack from '@mui/material/Stack';
import LuggageRounded from '@mui/icons-material/LuggageRounded';
import ThunderstormRounded from '@mui/icons-material/ThunderstormRounded';
import PauseCircleRounded from '@mui/icons-material/PauseCircleRounded';
import BlockRounded from '@mui/icons-material/BlockRounded';
import EvStationRounded from '@mui/icons-material/EvStationRounded';
import DirectionsCarRounded from '@mui/icons-material/DirectionsCarRounded';
import { del, errorText } from '../lib/api';
import * as f from '../lib/fmt';
import { ConfirmDialog, Empty, useToast } from './common';
import type { ChargeEstimate } from './ChargeDialog';
import ChargePlanGraph from './ChargePlanGraph';

export interface Holiday { id: number; name: string; start: string; end: string; ev_away: number }
export interface Override {
  id: number;
  kind: string;
  starts: string;
  expires: string;
  data: { pct?: number; vehicle_id?: string; target_pct?: number; state?: string; stop_at_limit?: boolean; auto_schedule?: boolean; cancel_requested?: boolean; charge_mode?: string };
  estimate?: ChargeEstimate;
}

interface Item { key: string; path: string; Icon: typeof LuggageRounded; title: string; detail: string; active: boolean; noun: string; battery: boolean; charging?: boolean; automatic?: boolean; cancelDetail?: string; estimate?: ChargeEstimate; target?: number; deadline?: string }

export function describe(holidays: Holiday[], overrides: Override[], vehicles: { id: string; name: string }[], now = Date.now()): Item[] {
  const car = (id?: string) => vehicles.find((v) => v.id === id)?.name ?? 'Car';
  const items: Item[] = [];
  for (const o of overrides) {
    const until = `until ${f.when(o.expires, now)}`;
    const base = { key: `o${o.id}`, path: `/api/overrides/${o.id}`, active: true };
    switch (o.kind) {
      case 'reserve':
        items.push({ ...base, Icon: ThunderstormRounded, title: `Storm mode: keep ≥ ${f.pct(o.data?.pct)}`, detail: until, noun: 'storm mode', battery: true });
        break;
      case 'hold':
        items.push({ ...base, Icon: PauseCircleRounded, title: 'Battery paused (no discharge)', detail: until, noun: 'the battery pause', battery: true });
        break;
      case 'no_battery_export':
        items.push({ ...base, Icon: BlockRounded, title: 'No export from battery', detail: until, noun: 'this', battery: false });
        break;
      case 'ev_charge_now': {
        const estimate = o.estimate;
        const expectedSoc = estimate?.expected_end_soc ?? estimate?.end_soc;
        const reachable = estimate?.expected_target_reachable ?? estimate?.target_reachable;
        const goal = f.pct(o.data?.target_pct);
        const limit = o.data?.charge_mode === 'ready_by' ? `${goal} by ${f.when(o.expires, now)}`
          : o.data?.charge_mode === 'percentage' ? `to ${goal}`
          : o.data?.target_pct == null ? until : `up to ${goal}, ${until} at the latest`;
        const next = estimate?.windows?.find(w => Date.parse(w.end) > now);
        const state = o.data?.state;
        const details = o.data?.cancel_requested ? ['Ending automatic schedule']
          : state === 'stopping' ? ['Waiting for the car to confirm charging stopped']
          : state === 'confirmed' ? [o.data?.auto_schedule || o.data?.stop_at_limit ? 'Car confirmed charging stopped' : 'Target reached; automatic control was off']
          : state === 'finished' ? ['Charge plan ended']
          : state === 'unconfirmed' ? ['Check the car or charger to confirm charging has stopped']
          : o.data?.charge_mode === 'ready_by' ? [limit, o.data.auto_schedule ? 'Automatic schedule' : 'Estimate only — car is not controlled']
          : [limit, o.data?.stop_at_limit ? 'Automatic stop enabled' : 'Set the stop on your car or charger'];
        if (!state && !o.data?.cancel_requested && estimate && (estimate.energy_kwh > 0 || estimate.target_reachable === false)) {
          if (estimate.soc != null) details.push(`current ${f.pct(estimate.soc)}`);
          if (reachable === false) details.push(`estimated ${f.pct(expectedSoc)}`);
          else details.push(`expected finish ${f.when(estimate.expected_finish ?? estimate.finish, now)}`);
          if (next) details.push(`next window ${f.when(next.start, now)}–${f.when(next.end, now)}`);
          if (estimate.paid_grid_kwh) details.push(`about ${f.money(estimate.paid_grid_aud)} in paid grid power`);
          if (estimate.charging_outside_window && !o.data.auto_schedule) details.push('Car is charging outside this plan; imports may be billed');
          if (estimate.continuous_end_soc != null) details.push(`if charging continues, estimated ${f.pct(estimate.continuous_end_soc)} by the deadline`);
        }
        items.push({ ...base, active: !state || state === 'stopping', Icon: EvStationRounded,
          title: `${car(o.data?.vehicle_id)} charge plan`, detail: details.join(' · '), noun: 'the charge plan', battery: true, charging: true, automatic: o.data?.auto_schedule,
          estimate: !state ? estimate : undefined, target: o.data.target_pct, deadline: o.expires });
        break;
      }
      case 'ev_charge_planned':
        items.push({ ...base, Icon: EvStationRounded, title: `${car(o.data?.vehicle_id)} planned charge`,
          detail: `${f.dateTime(o.starts)} to ${f.dateTime(o.expires)}`, active: false,
          noun: 'the planned charge', battery: true, cancelDetail: 'This charge will be removed from the home energy forecast.' });
        break;
      case 'ev_away':
        items.push({ ...base, Icon: DirectionsCarRounded, title: `${car(o.data?.vehicle_id)} away`, detail: until, noun: 'car away', battery: false });
        break;
      default:
        items.push({ ...base, Icon: BlockRounded, title: o.kind, detail: until, noun: 'this', battery: false });
    }
  }
  for (const h of holidays) {
    if (new Date(h.end).getTime() < now) continue;
    const active = new Date(h.start).getTime() <= now;
    items.push({
      key: `h${h.id}`, path: `/api/holidays/${h.id}`, Icon: LuggageRounded, active, battery: false, noun: 'this trip',
      title: `Away: ${h.name}`,
      detail: (active ? `Now until ${f.when(h.end, now)}` : `${f.dateTime(h.start)} to ${f.dateTime(h.end)}`) + (h.ev_away ? ' · car away too' : ''),
    });
  }
  return items;
}

export default function Scheduled({ items, canEdit, onChanged }: { items: Item[]; canEdit: boolean; onChanged: () => void }) {
  const [confirm, setConfirm] = useState<Item | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();
  if (!items.length) return <Empty>{canEdit ? 'Nothing scheduled. Use a quick action when you’re going away or a storm is coming.' : 'Nothing scheduled.'}</Empty>;
  async function cancel() {
    if (!confirm) return;
    setBusy(true);
    setError(null);
    try {
      await del(confirm.path);
      toast(`Cancelled: ${confirm.title}. The plan is updating.`);
      setConfirm(null);
      onChanged();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <List disablePadding>
        {items.map((it) => (
          <ListItem key={it.key} disableGutters divider
            alignItems={it.estimate ? 'flex-start' : 'center'}
            secondaryAction={canEdit && !it.estimate && <Button size="small" color="error" onClick={() => { setError(null); setConfirm(it); }} aria-label={`Cancel ${it.title}`}>Cancel</Button>}
            sx={{ pr: canEdit && !it.estimate ? 10 : 0 }}>
            <ListItemIcon sx={{ minWidth: 40, color: it.battery ? 'battery.main' : 'primary.main' }}><it.Icon /></ListItemIcon>
            <ListItemText
              primary={<Stack component="span" direction="row" spacing={.5} sx={{ alignItems: 'flex-start', justifyContent: 'space-between' }}>
                <span>{it.title} {it.active && <Chip size="small" label="Active" color="battery" sx={{ height: 20, ml: 0.5, fontSize: '0.6875rem' }} />}</span>
                {canEdit && it.estimate && <Button size="small" color="error" onClick={() => { setError(null); setConfirm(it); }} aria-label={`Cancel ${it.title}`}>Cancel</Button>}
              </Stack>}
              secondary={<>{it.detail}{it.estimate && <ChargePlanGraph estimate={it.estimate} target={it.target} deadline={it.deadline} />}</>}
              slotProps={{ primary: { variant: 'body2', sx: { fontWeight: 600 } }, secondary: { component: 'div' } }} />
          </ListItem>
        ))}
      </List>
      <ConfirmDialog open={!!confirm} title={`Cancel ${confirm?.noun ?? 'this'}?`} confirm="Cancel it" danger busy={busy} error={error}
        onConfirm={cancel} onClose={() => setConfirm(null)}>
        <Typography variant="body2">{confirm?.cancelDetail ?? (confirm?.automatic ? 'Cancelling ends the schedule and requests a stop if the car is charging.' : confirm?.charging ? 'Cancelling ends this plan and any automatic stop monitoring. Use the car or charger to stop charging.' : `${confirm?.title}, ${confirm?.detail}. The plan goes back to normal straight away.`)}</Typography>
      </ConfirmDialog>
    </>
  );
}
