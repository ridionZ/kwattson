import { useState } from 'react';
import List from '@mui/material/List';
import ListItem from '@mui/material/ListItem';
import ListItemIcon from '@mui/material/ListItemIcon';
import ListItemText from '@mui/material/ListItemText';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import Typography from '@mui/material/Typography';
import LuggageRounded from '@mui/icons-material/LuggageRounded';
import ThunderstormRounded from '@mui/icons-material/ThunderstormRounded';
import PauseCircleRounded from '@mui/icons-material/PauseCircleRounded';
import BlockRounded from '@mui/icons-material/BlockRounded';
import EvStationRounded from '@mui/icons-material/EvStationRounded';
import DirectionsCarRounded from '@mui/icons-material/DirectionsCarRounded';
import { del, errorText } from '../lib/api';
import * as f from '../lib/fmt';
import { ConfirmDialog, Empty, useToast } from './common';

export interface Holiday { id: number; name: string; start: string; end: string; ev_away: number }
export interface Override { id: number; kind: string; starts: string; expires: string; data: { pct?: number; vehicle_id?: string } }

interface Item { key: string; path: string; Icon: typeof LuggageRounded; title: string; detail: string; active: boolean; noun: string; battery: boolean }

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
      case 'ev_charge_now':
        items.push({ ...base, Icon: EvStationRounded, title: `Charging ${car(o.data?.vehicle_id)} now`, detail: until, noun: 'charging now', battery: true });
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
            secondaryAction={canEdit && <Button size="small" color="error" onClick={() => { setError(null); setConfirm(it); }} aria-label={`Cancel ${it.title}`}>Cancel</Button>}
            sx={{ pr: canEdit ? 10 : 0 }}>
            <ListItemIcon sx={{ minWidth: 40, color: it.battery ? 'battery.main' : 'primary.main' }}><it.Icon /></ListItemIcon>
            <ListItemText
              primary={<>{it.title} {it.active && <Chip size="small" label="Active" color="battery" sx={{ height: 20, ml: 0.5, fontSize: '0.6875rem' }} />}</>}
              secondary={it.detail} slotProps={{ primary: { variant: 'body2', sx: { fontWeight: 600 } } }} />
          </ListItem>
        ))}
      </List>
      <ConfirmDialog open={!!confirm} title={`Cancel ${confirm?.noun ?? 'this'}?`} confirm="Cancel it" danger busy={busy} error={error}
        onConfirm={cancel} onClose={() => setConfirm(null)}>
        <Typography variant="body2">{confirm?.title}, {confirm?.detail}. The plan goes back to normal straight away.</Typography>
      </ConfirmDialog>
    </>
  );
}
