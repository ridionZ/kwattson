import { useState } from 'react';
import Button from '@mui/material/Button';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';
import { useApp } from '../lib/app';
import { post, errorText } from '../lib/api';
import { useNow } from '../lib/hooks';
import { getZone } from '../lib/fmt';

export type ControlRecovery = { phase: 'command' | 'release'; retry_at: string; reason: string };

export default function ControlRetry({ recovery }: { recovery: ControlRecovery }) {
  const { can, reloadStatus } = useApp();
  const now = useNow(1000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const release = recovery.phase === 'release';
  const time = new Date(recovery.retry_at);
  const label = time.getTime() > now
    ? `Next ${release ? 'release attempt' : 'retry'} at ${time.toLocaleTimeString('en-AU', { timeZone: getZone(), hour: 'numeric', minute: '2-digit', second: '2-digit' })}`
    : `${release ? 'Release attempt' : 'Retry'} due now`;

  async function retry() {
    setBusy(true);
    setError(null);
    try {
      await post('/api/control/retry');
    } catch (e) {
      setError(errorText(e));
    } finally {
      await reloadStatus();
      setBusy(false);
    }
  }

  return <Stack spacing={0.5} sx={{ mt: 0.5 }}>
    <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', alignItems: 'center' }}>
      <Typography variant="body2">{label}</Typography>
      {can('admin') && <Button size="small" color="inherit" variant="outlined" disabled={busy} onClick={() => void retry()}>
        {busy ? 'Retrying…' : 'Retry now'}
      </Button>}
    </Stack>
    {error && <Typography variant="caption" role="alert">{error}</Typography>}
  </Stack>;
}
