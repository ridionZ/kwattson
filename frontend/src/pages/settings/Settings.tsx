import { lazy, Suspense, useCallback, useMemo } from 'react';
import Box from '@mui/material/Box';
import List from '@mui/material/List';
import ListItemButton from '@mui/material/ListItemButton';
import ListItemIcon from '@mui/material/ListItemIcon';
import ListItemText from '@mui/material/ListItemText';
import Card from '@mui/material/Card';
import Button from '@mui/material/Button';
import useMediaQuery from '@mui/material/useMediaQuery';
import type { Theme } from '@mui/material/styles';
import HomeWorkRounded from '@mui/icons-material/HomeWorkRounded';
import SolarPowerRounded from '@mui/icons-material/SolarPowerRounded';
import ElectricCarRounded from '@mui/icons-material/ElectricCarRounded';
import NotificationsActiveRounded from '@mui/icons-material/NotificationsActiveRounded';
import TuneRounded from '@mui/icons-material/TuneRounded';
import GroupRounded from '@mui/icons-material/GroupRounded';
import PersonRounded from '@mui/icons-material/PersonRounded';
import HubRounded from '@mui/icons-material/HubRounded';
import ShieldRounded from '@mui/icons-material/ShieldRounded';
import ChevronRightRounded from '@mui/icons-material/ChevronRightRounded';
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded';
import { get, put } from '../../lib/api';
import { useLoad } from '../../lib/hooks';
import { useApp } from '../../lib/app';
import { ErrorState, LoadingBlock, PageHeader } from '../../components/common';
import { SettingsCtx, type SettingsDoc } from './ctx';
import Account from './Account';

const Systems = lazy(() => import('./Systems'));
const Alerts = lazy(() => import('./Alerts'));
const Users = lazy(() => import('./Users'));
const Vehicles = lazy(() => import('./Vehicles'));
const Misc = lazy(() => import('./Misc'));

const SECTIONS = [
  { key: 'home', label: 'Home & tariff', Icon: HomeWorkRounded, admin: true },
  { key: 'systems', label: 'Solar & batteries', Icon: SolarPowerRounded, admin: true },
  { key: 'vehicles', label: 'Vehicles', Icon: ElectricCarRounded, admin: true },
  { key: 'alerts', label: 'Alerts', Icon: NotificationsActiveRounded, admin: true },
  { key: 'planning', label: 'Planning', Icon: TuneRounded, admin: true },
  { key: 'users', label: 'Users', Icon: GroupRounded, admin: true },
  { key: 'account', label: 'My account', Icon: PersonRounded, admin: false },
  { key: 'connections', label: 'Connections & API', Icon: HubRounded, admin: true },
  { key: 'data', label: 'Data & privacy', Icon: ShieldRounded, admin: true },
];

export default function Settings({ section, sub }: { section?: string; sub?: string }) {
  const { can } = useApp();
  const admin = can('admin');
  const visible = SECTIONS.filter((s) => admin || !s.admin);
  const wide = useMediaQuery((t: Theme) => t.breakpoints.up('md'));
  const current = visible.find((s) => s.key === section);
  const active = current ?? (wide ? visible.find((s) => s.key === (admin ? 'systems' : 'account')) : undefined);

  const nav = (
    <Card component="nav" aria-label="Settings" sx={{ p: 1, alignSelf: 'start', position: { md: 'sticky' }, top: { md: 88 } }}>
      <List disablePadding>
        {visible.map(({ key, label, Icon }) => {
          const sel = active?.key === key;
          return (
            <ListItemButton key={key} component="a" href={`#/settings/${key}`} selected={sel} aria-current={sel ? 'page' : undefined}
              sx={{ borderRadius: 2.5, mb: 0.25, '&.Mui-selected': { bgcolor: 'secondary.light', '&:hover': { bgcolor: 'secondary.light' } }, '&.Mui-selected .MuiListItemText-primary': { fontWeight: 700 } }}>
              <ListItemIcon sx={{ minWidth: 36, color: sel ? 'secondary.dark' : 'text.secondary' }}><Icon fontSize="small" /></ListItemIcon>
              <ListItemText primary={label} slotProps={{ primary: { variant: 'body2', sx: { fontWeight: 600 } } }} />
              {!wide && <ChevronRightRounded sx={{ color: 'text.secondary' }} />}
            </ListItemButton>
          );
        })}
      </List>
    </Card>
  );

  if (!wide && !active) {
    return (
      <Box>
        <PageHeader title="Settings" />
        {nav}
      </Box>
    );
  }
  return (
    <Box>
      {sub && ['add', 'location', 'tariffs'].includes(sub) && <Button href="#/overview" startIcon={<ArrowBackRounded />} sx={{ mb: 1 }}>Back to overview</Button>}
      {!sub && !wide && <Button href="#/settings" startIcon={<ArrowBackRounded />} sx={{ mb: 1, ml: -1 }}>Settings</Button>}
      <Box sx={{ display: 'grid', gap: 3, gridTemplateColumns: { xs: '1fr', md: '240px 1fr' } }}>
        {wide && nav}
        <Box sx={{ minWidth: 0 }}>
          {active?.key === 'account' ? <Account /> : <AdminSettings section={active!.key} label={active!.label} sub={sub} />}
        </Box>
      </Box>
    </Box>
  );
}

function AdminSettings({ section, label, sub }: { section: string; label: string; sub?: string }) {
  const { reloadStatus } = useApp();
  const { data, error, reload: reloadSettings } = useLoad<SettingsDoc>(() => get('/api/settings'));
  const reload = useCallback(async () => {
    await Promise.all([reloadSettings(), reloadStatus()]);
  }, [reloadSettings, reloadStatus]);
  const save = useCallback(async (settings: any) => {
    await put('/api/settings', settings);
    await reload();
  }, [reload]);
  const value = useMemo(() => (data ? { doc: data, reload, save } : null), [data, reload, save]);
  if (error) return <ErrorState message={error} onRetry={reload} />;
  if (!value) return <><PageHeader title={label} /><LoadingBlock height={300} /></>;
  return (
    <SettingsCtx.Provider value={value}>
      <Suspense fallback={<LoadingBlock height={300} />}>
        {section === 'systems' && <Systems sub={sub} />}
        {section === 'alerts' && <Alerts />}
        {section === 'users' && <Users />}
        {section === 'vehicles' && <Vehicles sub={sub} />}
        {!['systems', 'alerts', 'users', 'vehicles'].includes(section) && <Misc section={section} label={label} sub={sub} />}
      </Suspense>
    </SettingsCtx.Provider>
  );
}
