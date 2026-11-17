import { useState, type FormEvent } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import TextField from '@mui/material/TextField';
import Alert from '@mui/material/Alert';
import Chip from '@mui/material/Chip';
import Typography from '@mui/material/Typography';
import Stack from '@mui/material/Stack';
import Dialog from '@mui/material/Dialog';
import DialogTitle from '@mui/material/DialogTitle';
import DialogContent from '@mui/material/DialogContent';
import DialogActions from '@mui/material/DialogActions';
import Stepper from '@mui/material/Stepper';
import Step from '@mui/material/Step';
import StepLabel from '@mui/material/StepLabel';
import { post, errorText } from '../../lib/api';
import { useApp } from '../../lib/app';
import { CopyField, Facts, HelpTip, PageHeader, Section, useToast } from '../../components/common';

const ROLE: Record<string, string> = { admin: 'Admin', member: 'Member', viewer: 'Viewer' };

export default function Account() {
  const { session, signOut } = useApp();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [otp, setOtp] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [mfa, setMfa] = useState<'enable' | 'disable' | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post('/api/auth/password', { password: current, otp, new_password: next });
      signOut();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }
  return (
    <Box>
      <PageHeader title="My account" />
      <Stack spacing={2.5}>
        <Section title="You">
          <Facts rows={[['Username', session.username], ['Role', ROLE[session.role] ?? session.role], ['kWattson version', session.version]]} />
        </Section>
        <Section title="Two-step sign-in" help={<HelpTip label="two-step sign-in">After your password, kWattson asks for a 6-digit code from an authenticator app (such as Aegis, 2FAS, Google Authenticator or 1Password). A stolen password alone then isn’t enough.</HelpTip>}>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{ alignItems: { sm: 'center' }, justifyContent: 'space-between' }}>
            <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
              {session.mfa_enabled ? <Chip color="battery" label="On" /> : <Chip variant="outlined" label="Off" />}
              <Typography variant="body2" color="text.secondary">{session.mfa_enabled ? 'You’ll be asked for a code each time you sign in.' : 'Recommended, especially if kWattson is reachable from the internet.'}</Typography>
            </Stack>
            {session.mfa_enabled
              ? <Button color="error" variant="outlined" onClick={() => setMfa('disable')}>Turn off</Button>
              : <Button variant="contained" color="battery" onClick={() => setMfa('enable')}>Turn on</Button>}
          </Stack>
        </Section>
        <Section title="Change password">
          <Box component="form" onSubmit={submit} sx={{ display: 'grid', gap: 2, maxWidth: 420 }}>
            <TextField label="Current password" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
            <TextField label="New password" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} required
              helperText="At least 14 characters. You’ll be signed out everywhere." slotProps={{ htmlInput: { minLength: 14 } }} />
            {session.mfa_enabled && <TextField label="Two-step code" value={otp} onChange={(e) => setOtp(e.target.value)} autoComplete="one-time-code" slotProps={{ htmlInput: { inputMode: 'numeric' } }} />}
            {error && <Alert severity="error" role="alert">{error}</Alert>}
            <Box><Button type="submit" variant="contained" disabled={busy}>Change password</Button></Box>
          </Box>
        </Section>
      </Stack>
      {mfa === 'enable' && <EnableMfa onClose={() => setMfa(null)} />}
      {mfa === 'disable' && <DisableMfa onClose={() => setMfa(null)} />}
    </Box>
  );
}

function EnableMfa({ onClose }: { onClose: () => void }) {
  const { refreshSession } = useApp();
  const toast = useToast();
  const [password, setPassword] = useState('');
  const [setup, setSetup] = useState<{ secret: string; uri: string } | null>(null);
  const [code, setCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const step = setup ? 1 : 0;
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (!setup) {
        setSetup(await post('/api/auth/mfa/start', { password, otp: '' }));
      } else {
        await post('/api/auth/mfa/confirm', { password, otp: code.replace(/\s/g, '') });
        await refreshSession();
        toast('Two-step sign-in is on.');
        onClose();
      }
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth aria-labelledby="mfa-title" slotProps={{ paper: { component: 'form', onSubmit: submit } as object }}>
      <DialogTitle id="mfa-title">Turn on two-step sign-in</DialogTitle>
      <DialogContent sx={{ display: 'grid', gap: 2, pt: '4px !important' }}>
        <Stepper activeStep={step} sx={{ mb: 1, '& .MuiStepIcon-root.Mui-active, & .MuiStepIcon-root.Mui-completed': { color: 'secondary.main' } }}>
          <Step><StepLabel>Confirm it’s you</StepLabel></Step>
          <Step><StepLabel>Add to your app</StepLabel></Step>
        </Stepper>
        {!setup ? (
          <TextField label="Your password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required autoFocus />
        ) : (
          <>
            <Typography variant="body2">In your authenticator app, add an account and enter the setup key (or paste the link if your app accepts one). The key expires in 10 minutes.</Typography>
            <CopyField label="Setup key" value={setup.secret.replace(/(.{4})/g, '$1 ').trim()} />
            <CopyField label="Setup link (otpauth)" value={setup.uri} />
            <TextField label="6-digit code from the app" value={code} onChange={(e) => setCode(e.target.value)} required autoFocus autoComplete="one-time-code"
              slotProps={{ htmlInput: { inputMode: 'numeric', maxLength: 10 } }} />
          </>
        )}
        {error && <Alert severity="error" role="alert">{error}</Alert>}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button type="submit" variant="contained" disabled={busy}>{setup ? 'Turn on' : 'Next'}</Button>
      </DialogActions>
    </Dialog>
  );
}

function DisableMfa({ onClose }: { onClose: () => void }) {
  const { refreshSession } = useApp();
  const toast = useToast();
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post('/api/auth/mfa/disable', { password, otp: code.replace(/\s/g, '') });
      await refreshSession();
      toast('Two-step sign-in is off.', 'warning');
      onClose();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }
  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="xs" fullWidth aria-labelledby="mfa-off-title" slotProps={{ paper: { component: 'form', onSubmit: submit } as object }}>
      <DialogTitle id="mfa-off-title">Turn off two-step sign-in?</DialogTitle>
      <DialogContent sx={{ display: 'grid', gap: 2, pt: '4px !important' }}>
        <Typography variant="body2" color="text.secondary">Your password alone will be enough to sign in.</Typography>
        <TextField label="Your password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required autoFocus />
        <TextField label="Two-step code" value={code} onChange={(e) => setCode(e.target.value)} required autoComplete="one-time-code" slotProps={{ htmlInput: { inputMode: 'numeric', maxLength: 10 } }} />
        {error && <Alert severity="error" role="alert">{error}</Alert>}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={onClose} disabled={busy} autoFocus>Keep it on</Button>
        <Button type="submit" variant="contained" color="error" disabled={busy}>Turn off</Button>
      </DialogActions>
    </Dialog>
  );
}
