import { useState, type FormEvent } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Table from '@mui/material/Table';
import TableHead from '@mui/material/TableHead';
import TableBody from '@mui/material/TableBody';
import TableRow from '@mui/material/TableRow';
import TableCell from '@mui/material/TableCell';
import Chip from '@mui/material/Chip';
import TextField from '@mui/material/TextField';
import MenuItem from '@mui/material/MenuItem';
import Dialog from '@mui/material/Dialog';
import DialogTitle from '@mui/material/DialogTitle';
import DialogContent from '@mui/material/DialogContent';
import DialogActions from '@mui/material/DialogActions';
import Alert from '@mui/material/Alert';
import Typography from '@mui/material/Typography';
import Stack from '@mui/material/Stack';
import useMediaQuery from '@mui/material/useMediaQuery';
import type { Theme } from '@mui/material/styles';
import PersonAddRounded from '@mui/icons-material/PersonAddRounded';
import { get, post, put, errorText } from '../../lib/api';
import { useLoad } from '../../lib/hooks';
import { useApp, type Role } from '../../lib/app';
import * as f from '../../lib/fmt';
import { ErrorState, HelpTip, LoadingBlock, PageHeader, Section, useToast } from '../../components/common';

interface User { id: number; username: string; role: Role; mfa_enabled: boolean; created_at: string; last_login: string | null; you?: boolean }
const ROLES: [Role, string, string][] = [
  ['admin', 'Admin', 'Everything, including settings, users and automatic control.'],
  ['member', 'Member', 'View everything, use quick actions and mark alerts read.'],
  ['viewer', 'Viewer', 'Read-only.'],
];
const roleLabel = (r: Role) => ROLES.find((x) => x[0] === r)?.[1] ?? r;

type Pending = { kind: 'add' } | { kind: 'role'; user: User; role: Role } | { kind: 'remove'; user: User };

export default function Users() {
  const users = useLoad<User[]>(() => get('/api/users'));
  const [pending, setPending] = useState<Pending | null>(null);
  const wide = useMediaQuery((t: Theme) => t.breakpoints.up('sm'));
  return (
    <Box>
      <PageHeader title="Users" sub="Who can see and change kWattson."
        actions={<Button variant="contained" startIcon={<PersonAddRounded />} onClick={() => setPending({ kind: 'add' })}>Add user</Button>} />
      <Section title="People" help={<HelpTip title="Roles" label="roles">
        {ROLES.map(([, l, d]) => <Box key={l} sx={{ mb: 0.5 }}><strong>{l}</strong>: {d}</Box>)}
      </HelpTip>}>
        {users.error ? <ErrorState message={users.error} onRetry={users.reload} /> : !users.data ? <LoadingBlock /> : (
          <Box sx={{ overflowX: 'auto' }}>
            <Table size="small" aria-label="Users">
              <TableHead>
                <TableRow>
                  <TableCell>Name</TableCell>
                  <TableCell>Role</TableCell>
                  {wide && <TableCell>Two-step</TableCell>}
                  {wide && <TableCell>Last sign-in</TableCell>}
                  <TableCell align="right"><Box component="span" sx={{ position: 'absolute', width: '1px', height: '1px', overflow: 'hidden', clipPath: 'inset(50%)', whiteSpace: 'nowrap' }}>Actions</Box></TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {users.data.map((u) => (
                  <TableRow key={u.id}>
                    <TableCell component="th" scope="row" sx={{ fontWeight: 600 }}>
                      {u.username} {u.you && <Chip size="small" label="You" sx={{ height: 20, ml: 0.5 }} />}
                      {!wide && <Typography variant="caption" color="text.secondary" component="div">{u.last_login ? `Signed in ${f.relative(u.last_login)}` : 'Never signed in'}</Typography>}
                    </TableCell>
                    <TableCell>
                      {u.you ? roleLabel(u.role) : (
                        <TextField select size="small" value={u.role} onChange={(e) => setPending({ kind: 'role', user: u, role: e.target.value as Role })}
                          slotProps={{ htmlInput: { 'aria-label': `Role for ${u.username}` } }} sx={{ minWidth: 120 }}>
                          {ROLES.map(([r, l]) => <MenuItem key={r} value={r}>{l}</MenuItem>)}
                        </TextField>
                      )}
                    </TableCell>
                    {wide && <TableCell>{u.mfa_enabled ? <Chip size="small" color="battery" label="On" /> : <Chip size="small" variant="outlined" label="Off" />}</TableCell>}
                    {wide && <TableCell>{u.last_login ? f.relative(u.last_login) : 'Never'}</TableCell>}
                    <TableCell align="right">{!u.you && <Button size="small" color="error" onClick={() => setPending({ kind: 'remove', user: u })}>Remove</Button>}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Box>
        )}
      </Section>
      {pending && <UserDialog pending={pending} onClose={() => setPending(null)} onDone={() => { setPending(null); users.reload(); }} />}
    </Box>
  );
}

function UserDialog({ pending, onClose, onDone }: { pending: Pending; onClose: () => void; onDone: () => void }) {
  const { session } = useApp();
  const toast = useToast();
  const [username, setUsername] = useState('');
  const [role, setRole] = useState<Role>('member');
  const [initial, setInitial] = useState('');
  const [password, setPassword] = useState('');
  const [otp, setOtp] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const title = pending.kind === 'add' ? 'Add user' : pending.kind === 'role' ? `Make ${pending.user.username} ${roleLabel(pending.role).toLowerCase()}?` : `Remove ${pending.user.username}?`;
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (pending.kind === 'add') {
        await post('/api/users', { username: username.trim(), role, initial_password: initial, password, otp });
        toast(`${username.trim()} added. Give them their password in person.`);
      } else if (pending.kind === 'role') {
        await put(`/api/users/${pending.user.id}`, { role: pending.role, password, otp });
        toast(`${pending.user.username} is now ${roleLabel(pending.role).toLowerCase()}.`);
      } else {
        await post(`/api/users/${pending.user.id}/delete`, { password, otp });
        toast(`${pending.user.username} removed.`);
      }
      onDone();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }
  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="xs" fullWidth aria-labelledby="user-title" slotProps={{ paper: { component: 'form', onSubmit: submit } as object }}>
      <DialogTitle id="user-title">{title}</DialogTitle>
      <DialogContent sx={{ display: 'grid', gap: 2, pt: '4px !important' }}>
        {pending.kind === 'add' && (
          <>
            <TextField label="Username" value={username} onChange={(e) => setUsername(e.target.value)} required autoFocus autoComplete="off" slotProps={{ htmlInput: { minLength: 3, maxLength: 80 } }} />
            <TextField select label="Role" value={role} onChange={(e) => setRole(e.target.value as Role)} helperText={ROLES.find((r) => r[0] === role)?.[2]}>
              {ROLES.map(([r, l]) => <MenuItem key={r} value={r}>{l}</MenuItem>)}
            </TextField>
            <TextField label="Initial password" type="password" autoComplete="new-password" value={initial} onChange={(e) => setInitial(e.target.value)} required
              helperText="At least 14 characters. They can change it after signing in." slotProps={{ htmlInput: { minLength: 14 } }} />
          </>
        )}
        {pending.kind === 'remove' && <Typography variant="body2">They’re signed out straight away and can’t sign in again.</Typography>}
        {pending.kind === 'role' && <Typography variant="body2">{ROLES.find((r) => r[0] === pending.role)?.[2]}</Typography>}
        <Stack spacing={2} sx={{ pt: 1, borderTop: 1, borderColor: 'divider' }}>
          <Typography variant="caption" color="text.secondary">Confirm it’s you.</Typography>
          <TextField label="Your password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          {session.mfa_enabled && <TextField label="Two-step code" value={otp} onChange={(e) => setOtp(e.target.value)} autoComplete="one-time-code" slotProps={{ htmlInput: { inputMode: 'numeric' } }} />}
        </Stack>
        {error && <Alert severity="error" role="alert">{error}</Alert>}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button type="submit" variant="contained" color={pending.kind === 'remove' ? 'error' : 'primary'} disabled={busy}>
          {pending.kind === 'add' ? 'Add user' : pending.kind === 'role' ? 'Change role' : 'Remove user'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
