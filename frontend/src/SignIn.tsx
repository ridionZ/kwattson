import { useState, type FormEvent } from 'react';
import Box from '@mui/material/Box';
import Card from '@mui/material/Card';
import TextField from '@mui/material/TextField';
import Button from '@mui/material/Button';
import Alert from '@mui/material/Alert';
import Typography from '@mui/material/Typography';
import Collapse from '@mui/material/Collapse';
import Checkbox from '@mui/material/Checkbox';
import FormControlLabel from '@mui/material/FormControlLabel';
import Link from '@mui/material/Link';
import { post, errorText } from './lib/api';
import { Wordmark } from './components/Brand';

export default function SignIn({ onSignedIn, demo }: { onSignedIn: () => void; demo?: boolean }) {
  const [username, setUsername] = useState(demo ? 'demo' : '');
  const [password, setPassword] = useState('');
  const [otp, setOtp] = useState('');
  const [remember, setRemember] = useState(false);
  const [showOtp, setShowOtp] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post('/api/auth/login', { username, password, otp, remember });
      onSignedIn();
    } catch (err) {
      const msg = errorText(err);
      if (/code/i.test(msg)) setShowOtp(true);
      setError(msg);
      setBusy(false);
    }
  }

  return (
    <Box component="main" sx={{ minHeight: '100dvh', display: 'grid', placeItems: 'center', p: 2, bgcolor: 'background.default' }}>
      <Card sx={{ width: '100%', maxWidth: 400, p: { xs: 3, sm: 4 }, borderRadius: 6 }}>
        <Box component="p" sx={{ m: 0, mb: 3 }}><Wordmark size={26} /><Box component="span" sx={{ position: 'absolute', width: '1px', height: '1px', overflow: 'hidden', clipPath: 'inset(50%)' }}>kWattson</Box></Box>
        <Typography variant="h1" sx={{ fontSize: '1.5rem', mb: 0.5 }}>Sign in</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>{demo ? 'Explore the sample house with the demo password you set at startup.' : 'Use the account for your home energy dashboard.'}</Typography>
        <Box component="form" onSubmit={submit} noValidate={false} sx={{ display: 'grid', gap: 2 }}>
          <TextField label="Username" name="username" autoComplete="username" required value={username}
            onChange={(e) => setUsername(e.target.value)} size="medium" autoFocus={!demo} />
          <TextField label="Password" name="password" type="password" autoComplete="current-password" required value={password}
            onChange={(e) => setPassword(e.target.value)} size="medium" autoFocus={demo} />
          <Collapse in={showOtp} unmountOnExit>
            <TextField label="Two-step code" name="otp" autoComplete="one-time-code" value={otp}
              slotProps={{ htmlInput: { inputMode: 'numeric', pattern: '[0-9]*', maxLength: 10 } }}
              onChange={(e) => setOtp(e.target.value)} size="medium" />
          </Collapse>
          <FormControlLabel control={<Checkbox checked={remember} onChange={(_, checked) => setRemember(checked)} />}
            label="Remember me for 30 days" sx={{ my: -0.5 }} />
          {error && <Alert severity="error" role="alert">{error}</Alert>}
          <Button type="submit" variant="contained" size="large" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</Button>
          {!showOtp && (
            <Link component="button" type="button" variant="body2" onClick={() => setShowOtp(true)} sx={{ justifySelf: 'start' }}>
              I use a two-step code
            </Link>
          )}
        </Box>
        {demo && <Alert severity="info" variant="outlined" sx={{ mt: 3 }}>Demo mode: simulated devices, nothing real is controlled.</Alert>}
      </Card>
    </Box>
  );
}

/** First run: create the first (admin) account using the one-time token printed by the server. */
export function Setup({ onDone }: { onDone: () => void }) {
  const [token, setToken] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [repeat, setRepeat] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mismatch = repeat.length > 0 && repeat !== password;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (password !== repeat) {
      setError('The two passwords are different.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await post('/api/auth/setup', { token: token.trim(), username: username.trim(), password });
      try {
        await post('/api/auth/login', { username: username.trim(), password, otp: '' });
      } finally {
        // Account creation succeeded. A failed login should return to sign-in.
        onDone();
      }
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <Box component="main" sx={{ minHeight: '100dvh', display: 'grid', placeItems: 'center', p: 2, bgcolor: 'background.default' }}>
      <Card sx={{ width: '100%', maxWidth: 440, p: { xs: 3, sm: 4 }, borderRadius: 6 }}>
        <Box component="p" sx={{ m: 0, mb: 3 }}><Wordmark size={26} /><Box component="span" sx={{ position: 'absolute', width: '1px', height: '1px', overflow: 'hidden', clipPath: 'inset(50%)' }}>kWattson</Box></Box>
        <Typography variant="h1" sx={{ fontSize: '1.5rem', mb: 0.5 }}>Welcome to kWattson</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
          Create the first admin account. On Unraid you can also set First admin password in the container settings, then sign in as admin.
        </Typography>
        <Box component="form" onSubmit={submit} sx={{ display: 'grid', gap: 2 }}>
          <TextField label="Setup token" name="token" required value={token} autoFocus autoComplete="off"
            onChange={(e) => setToken(e.target.value)} slotProps={{ htmlInput: { spellCheck: false, autoCapitalize: 'none' } }}
            helperText={<>On the server, run <code>kwattson setup-token</code> (in Docker: <code>docker exec kwattson kwattson setup-token</code>) and paste what it prints.</>} />
          <TextField label="Username" name="username" required value={username} autoComplete="username"
            helperText="Use letters, numbers, a dot, dash, underscore or @." onChange={(e) => setUsername(e.target.value)} slotProps={{ htmlInput: { minLength: 3, maxLength: 80, spellCheck: false, autoCapitalize: 'none' } }} />
          <TextField label="Password" name="new-password" type="password" required value={password} autoComplete="new-password"
            onChange={(e) => setPassword(e.target.value)} slotProps={{ htmlInput: { minLength: 14, maxLength: 256 } }}
            helperText="At least 14 characters. A few random words works well." />
          <TextField label="Type the password again" name="repeat-password" type="password" required value={repeat} autoComplete="new-password"
            onChange={(e) => setRepeat(e.target.value)} error={mismatch} helperText={mismatch ? 'Doesn’t match yet.' : ' '} />
          {error && <Alert severity="error" role="alert">{error}</Alert>}
          <Button type="submit" variant="contained" size="large" disabled={busy}>{busy ? 'Creating account…' : 'Create account'}</Button>
        </Box>
        <Typography variant="body2" color="text.secondary" sx={{ mt: 3 }}>
          Next, the setup checklist will guide you through your location, electricity prices and inverter.
        </Typography>
      </Card>
    </Box>
  );
}
