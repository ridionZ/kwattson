import { useState } from 'react';
import List from '@mui/material/List';
import ListItem from '@mui/material/ListItem';
import Alert from '@mui/material/Alert';
import Typography from '@mui/material/Typography';
import Box from '@mui/material/Box';
import Collapse from '@mui/material/Collapse';
import Button from '@mui/material/Button';
import { Empty } from './common';
import * as f from '../lib/fmt';

const SEV = {
  critical: 'error', warning: 'warning', info: 'info',
} as const;

export interface NotificationsDoc {
  notifications: { id: number; created_at: string; event: string; severity: keyof typeof SEV; title: string; body: string; seen: number }[];
  deliveries: { id: number; notification_id: number; channel: string; status: string; attempts: number; result: any }[];
}

export default function AlertsList({ doc, limit = 10 }: { doc: NotificationsDoc; limit?: number }) {
  const [open, setOpen] = useState<number | null>(null);
  const items = doc.notifications.slice(0, limit);
  if (!items.length) return <Empty>No alerts yet. You’ll see faults, price spikes and daily summaries here.</Empty>;
  return (
    <List disablePadding>
      {items.map((n) => {
        const sev = SEV[n.severity] ?? SEV.info;
        const deliveries = doc.deliveries.filter((d) => d.notification_id === n.id);
        const unread = !n.seen;
        return (
          <ListItem key={n.id} disablePadding sx={{ mb: 1 }}>
            <Alert severity={sev} variant="standard" role="group" aria-label={n.severity} sx={{ width: '100%', '& .MuiAlert-message': { minWidth: 0 } }}>
              <Box sx={{ flex: 1, minWidth: 0 }}>
                <Typography variant="body2" sx={{ fontWeight: unread ? 700 : 500 }}>
                  {n.title}
                  {unread && <Box component="span" sx={{ position: 'absolute', width: '1px', height: '1px', overflow: 'hidden', clipPath: 'inset(50%)', whiteSpace: 'nowrap' }}> (unread)</Box>}
                </Typography>
                {n.body && <Typography variant="body2" color="text.secondary">{n.body}</Typography>}
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 0.25 }}>
                  <Typography variant="caption" color="text.secondary">{f.dateTime(n.created_at)}</Typography>
                  {deliveries.length > 0 && (
                    <Button size="small" variant="text" sx={{ minHeight: 0, py: 0, px: 0.5, fontSize: '0.75rem' }}
                      aria-expanded={open === n.id} onClick={() => setOpen(open === n.id ? null : n.id)}>
                      Delivery ({deliveries.length})
                    </Button>
                  )}
                </Box>
                <Collapse in={open === n.id} unmountOnExit>
                  <Box component="ul" sx={{ m: 0, mt: 0.5, pl: 2.5, typography: 'caption', color: 'text.secondary' }}>
                    {deliveries.map((d) => (
                      <li key={d.id}>{d.channel}: {d.status}{d.attempts > 1 ? ` after ${d.attempts} tries` : ''}{d.result?.error ? ` (${d.result.error})` : ''}</li>
                    ))}
                  </Box>
                </Collapse>
              </Box>
            </Alert>
          </ListItem>
        );
      })}
    </List>
  );
}
