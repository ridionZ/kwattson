import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import Box from '@mui/material/Box';
import AppBar from '@mui/material/AppBar';
import Toolbar from '@mui/material/Toolbar';
import Container from '@mui/material/Container';
import Chip from '@mui/material/Chip';
import IconButton from '@mui/material/IconButton';
import Badge from '@mui/material/Badge';
import Avatar from '@mui/material/Avatar';
import Menu from '@mui/material/Menu';
import MenuItem from '@mui/material/MenuItem';
import ListItemIcon from '@mui/material/ListItemIcon';
import ListItemText from '@mui/material/ListItemText';
import Divider from '@mui/material/Divider';
import Button from '@mui/material/Button';
import Tooltip from '@mui/material/Tooltip';
import Drawer from '@mui/material/Drawer';
import Typography from '@mui/material/Typography';
import Stack from '@mui/material/Stack';
import Paper from '@mui/material/Paper';
import BottomNavigation from '@mui/material/BottomNavigation';
import BottomNavigationAction from '@mui/material/BottomNavigationAction';
import CircularProgress from '@mui/material/CircularProgress';
import ToggleButton from '@mui/material/ToggleButton';
import ToggleButtonGroup from '@mui/material/ToggleButtonGroup';
import { useColorScheme } from '@mui/material/styles';
import DashboardRounded from '@mui/icons-material/DashboardRounded';
import TimelineRounded from '@mui/icons-material/TimelineRounded';
import HistoryRounded from '@mui/icons-material/HistoryRounded';
import SettingsRounded from '@mui/icons-material/SettingsRounded';
import NotificationsRounded from '@mui/icons-material/NotificationsRounded';
import VisibilityRounded from '@mui/icons-material/VisibilityRounded';
import BoltRounded from '@mui/icons-material/BoltRounded';
import StopCircleRounded from '@mui/icons-material/StopCircleRounded';
import PersonRounded from '@mui/icons-material/PersonRounded';
import LogoutRounded from '@mui/icons-material/LogoutRounded';
import CloseRounded from '@mui/icons-material/CloseRounded';
import LightModeRounded from '@mui/icons-material/LightModeRounded';
import DarkModeRounded from '@mui/icons-material/DarkModeRounded';
import SettingsBrightnessRounded from '@mui/icons-material/SettingsBrightnessRounded';
import { get, post, setCsrf, errorText } from './lib/api';
import { setZone } from './lib/fmt';
import { useLoad, useRoute, go } from './lib/hooks';
import { AppCtx, RANK, useApp, type AppState, type Role, type Session } from './lib/app';
import { ConfirmDialog, ErrorState, LoadingBlock, ToastProvider, useToast } from './components/common';
import AlertsList, { type NotificationsDoc } from './components/AlertsList';
import SignIn, { Setup } from './SignIn';
import Overview from './pages/Overview';
import { Wordmark } from './components/Brand';

const Plan = lazy(() => import('./pages/Plan'));
const History = lazy(() => import('./pages/History'));
const Settings = lazy(() => import('./pages/settings/Settings'));

const NAV = [
  { key: 'overview', label: 'Overview', Icon: DashboardRounded },
  { key: 'plan', label: 'Plan', Icon: TimelineRounded },
  { key: 'history', label: 'History', Icon: HistoryRounded },
  { key: 'settings', label: 'Settings', Icon: SettingsRounded },
];

export default function App() {
  const [phase, setPhase] = useState<'loading' | 'setup' | 'signin' | 'ready' | 'error'>('loading');
  const [session, setSession] = useState<Session | null>(null);
  const [demo, setDemo] = useState(false);
  const [bootError, setBootError] = useState('');

  const boot = useCallback(async () => {
    try {
      const st = await get('/api/auth/status');
      setDemo(!!st.demo);
      if (st.setup_required) {
        setPhase('setup');
        return;
      }
      if (!st.signed_in) {
        setPhase('signin');
        return;
      }
      const s = await get<Session>('/api/auth/session');
      setCsrf(s.csrf);
      setSession({ ...s, role: (s.role ?? 'admin') as Role });
      setPhase('ready');
    } catch (e) {
      setBootError(errorText(e));
      setPhase('error');
    }
  }, []);

  useEffect(() => {
    void boot();
    const onAuth = () => {
      setCsrf(null);
      setPhase('signin');
    };
    window.addEventListener('kwattson:auth-required', onAuth);
    return () => window.removeEventListener('kwattson:auth-required', onAuth);
  }, [boot]);

  if (phase === 'loading') return <Box sx={{ minHeight: '100dvh', display: 'grid', placeItems: 'center' }}><CircularProgress aria-label="Loading kWattson" /></Box>;
  if (phase === 'error') return <Box sx={{ p: 3 }}><ErrorState message={bootError} onRetry={boot} /></Box>;
  if (phase === 'setup') return <Setup onDone={boot} />;
  if (phase === 'signin' || !session) return <SignIn demo={demo} onSignedIn={boot} />;
  return (
    <ToastProvider>
      <Shell session={session} onSignedOut={() => { setCsrf(null); setSession(null); setPhase('signin'); }}
        onRefresh={async () => { const s = await get<Session>('/api/auth/session'); setCsrf(s.csrf); setSession({ ...s, role: (s.role ?? 'admin') as Role }); }} />
    </ToastProvider>
  );
}

function Shell({ session, onSignedOut, onRefresh }: { session: Session; onSignedOut: () => void; onRefresh: () => Promise<void> }) {
  const route = useRoute();
  const toast = useToast();
  const [status, setStatus] = useState<any>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [alertsOpen, setAlertsOpen] = useState(false);
  const [stopOpen, setStopOpen] = useState(false);
  const [stopBusy, setStopBusy] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);

  const reloadStatus = useCallback(async () => {
    try {
      const st = await get('/api/status');
      setZone(st.timezone);
      setStatus(st);
      setStatusError(null);
      return st;
    } catch (e) {
      setStatusError(errorText(e));
      return null;
    }
  }, []);
  useEffect(() => {
    void reloadStatus();
    const t = setInterval(() => document.visibilityState === 'visible' && reloadStatus(), 60000);
    return () => clearInterval(t);
  }, [reloadStatus]);

  const signOut = useCallback(async () => {
    try { await post('/api/auth/logout'); } catch { /* signed out anyway */ }
    onSignedOut();
  }, [onSignedOut]);

  const ctx: AppState = useMemo(() => ({
    session, status, statusError, reloadStatus,
    can: (role: Role) => RANK[session.role] >= RANK[role],
    openAlerts: () => setAlertsOpen(true),
    signOut,
    refreshSession: onRefresh,
  }), [session, status, statusError, reloadStatus, signOut, onRefresh]);

  const page = route[0];
  const automatic = status?.mode === 'automatic';

  useEffect(() => {
    const label = NAV.find((n) => n.key === page)?.label ?? 'kWattson';
    document.title = `${label} · kWattson`;
  }, [page]);

  async function stop() {
    setStopBusy(true);
    setStopError(null);
    try {
      const r = await post('/api/control/stop');
      setStopOpen(false);
      toast(r.problems?.length ? `Stopped, with problems: ${r.problems.join('; ')}` : 'Automatic control stopped. kWattson is monitoring only.', r.problems?.length ? 'warning' : 'success');
      await reloadStatus();
    } catch (e) {
      setStopError(errorText(e));
    } finally {
      setStopBusy(false);
    }
  }

  return (
    <AppCtx.Provider value={ctx}>
      <Box sx={{ minHeight: '100dvh', bgcolor: 'background.default', pb: { xs: 10, md: 4 } }}>
        <Box component="a" href="#main" sx={{
          position: 'absolute', left: 8, top: -48, zIndex: 2000, bgcolor: 'background.paper', p: 1, borderRadius: 1,
          '&:focus': { top: 8 },
        }}>Skip to content</Box>
        <AppBar position="sticky" sx={{ bgcolor: '#0E2A47', color: '#fff', borderBottom: 1, borderColor: 'rgba(255,255,255,0.08)' }}>
          <Toolbar sx={{ gap: { xs: 1, sm: 1.5 }, minHeight: { xs: 56, md: 64 } }}>
            <Box component="a" href="#/overview" aria-label="kWattson — Overview" sx={{
              display: 'flex', alignItems: 'center', mr: { md: 2 }, textDecoration: 'none', borderRadius: 1, minWidth: 0,
              '&:focus-visible': { outline: '2px solid #2DD4BF', outlineOffset: 4 },
            }}>
              <Wordmark onDark size={20} sx={{ display: { xs: 'none', sm: 'inline-flex' } }} />
              <Wordmark onDark size={17} sx={{ display: { xs: 'inline-flex', sm: 'none' } }} />
            </Box>
            <Box component="nav" aria-label="Main" sx={{ display: { xs: 'none', md: 'flex' }, alignSelf: 'stretch' }}>
              <Box component="ul" sx={{ display: 'flex', m: 0, p: 0, listStyle: 'none', gap: 0.5 }}>
                {NAV.map(({ key, label }) => {
                  const current = page === key;
                  return (
                    <li key={key} style={{ display: 'flex' }}>
                      <Box component="a" href={`#/${key}`} aria-current={current ? 'page' : undefined} sx={{
                        display: 'flex', alignItems: 'center', px: 2, color: current ? '#fff' : 'rgba(255,255,255,0.72)',
                        textDecoration: 'none', fontWeight: 600, fontSize: '0.9375rem', position: 'relative', borderRadius: '8px 8px 0 0',
                        transition: 'color .15s, background-color .15s',
                        '&:hover': { color: '#fff', bgcolor: 'rgba(255,255,255,0.06)' },
                        '&:focus-visible': { outline: '2px solid #2DD4BF', outlineOffset: -2 },
                        '&::after': {
                          content: '""', position: 'absolute', left: 12, right: 12, bottom: 0, height: 3, borderRadius: '3px 3px 0 0',
                          bgcolor: '#2DD4BF', transform: current ? 'scaleX(1)' : 'scaleX(0)', transition: 'transform .2s',
                        },
                      }}>{label}</Box>
                    </li>
                  );
                })}
              </Box>
            </Box>
            <Box sx={{ flex: 1 }} />
            {status && <ModeChip automatic={automatic} />}
            {automatic && ctx.can('member') && (
              <Button variant="contained" color="error" size="small" startIcon={<StopCircleRounded />} onClick={() => setStopOpen(true)}
                sx={{ whiteSpace: 'nowrap' }}>
                <Box component="span" sx={{ display: { xs: 'none', sm: 'inline' } }}>Stop automatic control</Box>
                <Box component="span" sx={{ display: { xs: 'inline', sm: 'none' } }}>Stop</Box>
              </Button>
            )}
            <Tooltip title="Alerts">
              <IconButton color="inherit" onClick={() => setAlertsOpen(true)}
                aria-label={status?.unread ? `Alerts, ${status.unread} unread` : 'Alerts'}>
                <Badge badgeContent={status?.unread || 0} color="error" max={99}>
                  <NotificationsRounded />
                </Badge>
              </IconButton>
            </Tooltip>
            <UserMenu session={session} onSignOut={signOut} />
          </Toolbar>
        </AppBar>

        <Container component="main" id="main" tabIndex={-1} maxWidth="lg" sx={{ pt: { xs: 2.5, md: 4 }, outline: 'none' }}>
          <Suspense fallback={<LoadingBlock height={420} />}>
            {page === 'overview' && <Overview />}
            {page === 'plan' && <Plan />}
            {page === 'history' && <History tab={route[1]} />}
            {page === 'settings' && <Settings section={route[1]} sub={route[2]} />}
            {!NAV.some((n) => n.key === page) && <ErrorState message="That page doesn’t exist." onRetry={() => go('overview')} />}
          </Suspense>
        </Container>

        <Paper component="nav" aria-label="Main" elevation={0} sx={{
          display: { xs: 'block', md: 'none' }, position: 'fixed', bottom: 0, left: 0, right: 0, zIndex: 1100,
          borderTop: 1, borderColor: 'divider', pb: 'env(safe-area-inset-bottom)',
        }}>
          <BottomNavigation showLabels value={page} sx={{ height: 64, bgcolor: 'background.paper' }}>
            {NAV.map(({ key, label, Icon }) => (
              <BottomNavigationAction key={key} value={key} label={label} href={`#/${key}`} component="a"
                aria-current={page === key ? 'page' : undefined}
                icon={<Box sx={{ px: 2.5, py: 0.25, borderRadius: 4, display: 'flex', transition: 'background-color .2s', bgcolor: page === key ? 'secondary.light' : 'transparent' }}><Icon /></Box>}
                sx={{ minWidth: 0, '&.Mui-selected': { color: 'text.primary' }, '& .MuiBottomNavigationAction-label': { fontWeight: 600, mt: 0.25 } }} />
            ))}
          </BottomNavigation>
        </Paper>

        <Drawer anchor="right" open={alertsOpen} onClose={() => setAlertsOpen(false)}
          slotProps={{ paper: { sx: { width: { xs: '100%', sm: 420 } }, role: 'dialog', 'aria-labelledby': 'alerts-drawer-title' } as object }}>
          <AlertsDrawer onClose={() => setAlertsOpen(false)} />
        </Drawer>

        <ConfirmDialog open={stopOpen} title="Stop automatic control?" confirm="Stop automatic control" danger busy={stopBusy} error={stopError}
          onConfirm={stop} onClose={() => setStopOpen(false)}>
          <Typography variant="body2">
            kWattson hands the inverter back to its normal self-use mode and keeps monitoring and planning. You can turn automatic control on again in Settings › Solar &amp; batteries.
          </Typography>
        </ConfirmDialog>
      </Box>
    </AppCtx.Provider>
  );
}

function ModeChip({ automatic }: { automatic: boolean }) {
  return (
    <Tooltip title={automatic ? 'kWattson is controlling the battery' : 'kWattson plans and advises; it does not control the battery'}>
      <Chip component="a" href="#/settings/systems" clickable size="small"
        icon={automatic ? <BoltRounded /> : <VisibilityRounded />}
        label={automatic ? 'Automatic' : 'Monitoring'}
        sx={automatic
          ? { bgcolor: '#2DD4BF', color: '#042F2E', '& .MuiChip-icon': { color: '#042F2E' }, '&:hover': { bgcolor: '#5EEAD4' } }
          : { bgcolor: 'rgba(255,255,255,0.10)', color: '#fff', border: '1px solid rgba(255,255,255,0.24)', '& .MuiChip-icon': { color: '#fff' }, '&:hover': { bgcolor: 'rgba(255,255,255,0.18)' } }} />
    </Tooltip>
  );
}

const ROLE_LABEL: Record<Role, string> = { admin: 'Admin', member: 'Member', viewer: 'Viewer' };

function UserMenu({ session, onSignOut }: { session: Session; onSignOut: () => void }) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const { mode, setMode } = useColorScheme();
  return (
    <>
      <IconButton onClick={(e) => setAnchor(e.currentTarget)} aria-label={`Account: ${session.username}`} aria-haspopup="menu"
        aria-expanded={!!anchor} sx={{ p: 0.5 }}>
        <Avatar sx={{ width: 32, height: 32, bgcolor: '#F5A524', color: '#0E2A47', fontWeight: 700, fontSize: '0.9rem' }}>
          {session.username.slice(0, 1).toUpperCase()}
        </Avatar>
      </IconButton>
      <Menu anchorEl={anchor} open={!!anchor} onClose={() => setAnchor(null)} anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }} slotProps={{ paper: { sx: { minWidth: 240, mt: 1 } } }}>
        <Box sx={{ px: 2, py: 1 }}>
          <Typography variant="subtitle2">{session.username}</Typography>
          <Typography variant="caption" color="text.secondary">{ROLE_LABEL[session.role]}</Typography>
        </Box>
        <Divider />
        <MenuItem component="a" href="#/settings/account" onClick={() => setAnchor(null)}>
          <ListItemIcon><PersonRounded fontSize="small" /></ListItemIcon>
          <ListItemText>My account</ListItemText>
        </MenuItem>
        <Box sx={{ px: 2, py: 1 }}>
          <Typography variant="caption" color="text.secondary" id="theme-label">Appearance</Typography>
          <ToggleButtonGroup size="small" exclusive fullWidth value={mode ?? 'system'} aria-labelledby="theme-label"
            onChange={(_, v) => v && setMode(v)} sx={{ mt: 0.5 }}>
            <ToggleButton value="light" aria-label="Light"><LightModeRounded fontSize="small" /></ToggleButton>
            <ToggleButton value="system" aria-label="Match system"><SettingsBrightnessRounded fontSize="small" /></ToggleButton>
            <ToggleButton value="dark" aria-label="Dark"><DarkModeRounded fontSize="small" /></ToggleButton>
          </ToggleButtonGroup>
        </Box>
        <Divider />
        <MenuItem onClick={() => { setAnchor(null); onSignOut(); }}>
          <ListItemIcon><LogoutRounded fontSize="small" /></ListItemIcon>
          <ListItemText>Sign out</ListItemText>
        </MenuItem>
      </Menu>
    </>
  );
}

function AlertsDrawer({ onClose }: { onClose: () => void }) {
  const { reloadStatus, can } = useApp();
  const { data, error, reload } = useLoad<NotificationsDoc>(() => get('/api/notifications?limit=30'));
  const toast = useToast();
  async function markAll() {
    try {
      await post('/api/notifications/seen');
      await Promise.all([reload(), reloadStatus()]);
      toast('All alerts marked as read.');
    } catch (e) {
      toast(errorText(e), 'error');
    }
  }
  return (
    <Box sx={{ p: 2.5 }}>
      <Stack direction="row" sx={{ alignItems: 'center', mb: 2 }}>
        <Typography variant="h2" id="alerts-drawer-title" sx={{ flex: 1 }}>Alerts</Typography>
        <IconButton onClick={onClose} aria-label="Close alerts"><CloseRounded /></IconButton>
      </Stack>
      <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
        {can('member') && <Button size="small" variant="outlined" onClick={markAll} disabled={!data?.notifications.some((n) => !n.seen)}>Mark all read</Button>}
        {can('admin') && <Button size="small" href="#/settings/alerts" onClick={onClose}>Alert settings</Button>}
      </Stack>
      {error ? <ErrorState message={error} onRetry={reload} /> : !data ? <LoadingBlock lines={5} /> : <AlertsList doc={data} limit={30} />}
    </Box>
  );
}

