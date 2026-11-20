import { useId, useState, type ReactNode } from 'react';
import Box from '@mui/material/Box';
import Card from '@mui/material/Card';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import Dialog from '@mui/material/Dialog';
import DialogTitle from '@mui/material/DialogTitle';
import DialogContent from '@mui/material/DialogContent';
import DialogActions from '@mui/material/DialogActions';
import Stepper from '@mui/material/Stepper';
import Step from '@mui/material/Step';
import StepLabel from '@mui/material/StepLabel';
import Tabs from '@mui/material/Tabs';
import Tab from '@mui/material/Tab';
import TextField from '@mui/material/TextField';
import MenuItem from '@mui/material/MenuItem';
import Switch from '@mui/material/Switch';
import FormControlLabel from '@mui/material/FormControlLabel';
import Checkbox from '@mui/material/Checkbox';
import InputAdornment from '@mui/material/InputAdornment';
import Alert from '@mui/material/Alert';
import IconButton from '@mui/material/IconButton';
import useMediaQuery from '@mui/material/useMediaQuery';
import type { Theme } from '@mui/material/styles';
import AddRounded from '@mui/icons-material/AddRounded';
import CloseRounded from '@mui/icons-material/CloseRounded';
import CheckCircleRounded from '@mui/icons-material/CheckCircleRounded';
import SolarPowerRounded from '@mui/icons-material/SolarPowerRounded';
import CloudRounded from '@mui/icons-material/CloudRounded';
import HubRounded from '@mui/icons-material/HubRounded';
import ScienceRounded from '@mui/icons-material/ScienceRounded';
import CallReceivedRounded from '@mui/icons-material/CallReceivedRounded';
import NetworkCheckRounded from '@mui/icons-material/NetworkCheckRounded';
import EditRounded from '@mui/icons-material/EditRounded';
import BoltRounded from '@mui/icons-material/BoltRounded';
import ControlRetry from '../../components/ControlRetry';
import { post, put, errorText } from '../../lib/api';
import { useApp } from '../../lib/app';
import * as f from '../../lib/fmt';
import { ConfirmDialog, Empty, Facts, HelpTip, PageHeader, Section, useToast } from '../../components/common';
import { clone, useSettings } from './ctx';
import { Num, SchemaField, slug } from './fields';
import BatteryModeControls from '../../components/BatteryModeControls';

const PLUGIN_ICON: Record<string, typeof CloudRounded> = { foxess: CloudRounded, homeassistant: HubRounded, push: CallReceivedRounded, simulator: ScienceRounded };

function StatusDot({ status, enabled }: { status: any; enabled: boolean }) {
  const [color, label] = !enabled ? ['text.disabled', 'Turned off'] : !status?.poll ? ['text.disabled', 'Not read yet'] : status.poll.ok ? ['success.main', 'Reading OK'] : ['error.main', 'Can’t read'];
  return (
    <Stack direction="row" spacing={0.75} sx={{ alignItems: 'center' }}>
      <Box component="span" aria-hidden sx={{ width: 10, height: 10, borderRadius: '50%', bgcolor: color }} />
      <Typography variant="caption" sx={{ fontWeight: 600 }}>{label}</Typography>
    </Stack>
  );
}

export default function Systems({ sub }: { sub?: string }) {
  const { doc, reload } = useSettings();
  const { status } = useApp();
  const [wizard, setWizard] = useState<{ edit?: string } | null>(sub === 'add' ? {} : null);
  const [enableOpen, setEnableOpen] = useState(false);
  const toast = useToast();
  const [testing, setTesting] = useState<string | null>(null);
  const [remove, setRemove] = useState<any>(null);
  const [removing, setRemoving] = useState(false);
  const { save } = useSettings();
  const systems = doc.settings.systems as any[];

  async function doRemove() {
    setRemoving(true);
    try {
      const s = clone(doc.settings);
      s.systems = s.systems.filter((x: any) => x.id !== remove.id);
      await save(s);
      toast(`${remove.name} removed.`);
      setRemove(null);
    } catch (e) {
      toast(errorText(e), 'error');
    } finally {
      setRemoving(false);
    }
  }

  async function test(id: string) {
    setTesting(id);
    try {
      await post(`/api/test/system/${id}`);
      await reload();
      toast('Connection works. kWattson read the system.');
    } catch (e) {
      toast(errorText(e), 'error');
    } finally {
      setTesting(null);
    }
  }

  return (
    <Box>
      <PageHeader title="Solar & batteries" sub="Each inverter and whatever is connected to it."
        actions={systems.length > 0 && <Button variant="contained" startIcon={<AddRounded />} onClick={() => setWizard({})}>Add a system</Button>} />
      <Stack spacing={2}>
        {!systems.length && <Empty action={<Button variant="contained" onClick={() => setWizard({})}>Add a system</Button>}>No systems yet. Add your inverter to start reading solar and battery data.</Empty>}
        {systems.map((s) => {
          const live = status?.systems?.find((x: any) => x.id === s.id);
          const plugin = doc.plugins.find((p) => p.name === s.plugin);
          const Icon = PLUGIN_ICON[s.plugin] ?? SolarPowerRounded;
          const lastTest = doc.last_tests?.[`system:${s.id}`];
          return (
            <Card key={s.id} sx={{ p: 2.5 }}>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{ alignItems: { sm: 'flex-start' } }}>
                <Box aria-hidden sx={{ width: 48, height: 48, borderRadius: 3, display: 'grid', placeItems: 'center', bgcolor: 'solar.light', color: 'solar.main', flexShrink: 0 }}><Icon /></Box>
                <Box sx={{ flex: 1, minWidth: 0 }}>
                  <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap', rowGap: 0.5 }}>
                    <Typography variant="h2" component="h2">{s.name}</Typography>
                    <StatusDot status={live} enabled={s.enabled} />
                    {s.controllable && <Chip size="small" color="battery" icon={<BoltRounded />} label="Control allowed" sx={{ height: 22 }} />}
                    {s.site_meter && <Chip size="small" variant="outlined" label="Whole-house meter" sx={{ height: 22 }} />}
                  </Stack>
                  <Typography variant="body2" color="text.secondary">{plugin?.title ?? s.plugin}</Typography>
                  <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr 1fr', sm: 'repeat(4, auto)' }, justifyContent: 'start', columnGap: 4, rowGap: 1, mt: 1.5 }}>
                    <Spec label="Panels" value={f.kw(s.pv_kwp)} unit="p" />
                    <Spec label="Inverter" value={f.kw(s.ac_kw)} />
                    <Spec label="Battery" value={s.battery ? f.kwh(s.battery.capacity_kwh) : 'None'} />
                    <Spec label="Last reading" value={live?.reading ? f.relative(live.reading.ts) : '–'} />
                  </Box>
                  {live?.poll && !live.poll.ok && <Alert severity="error" sx={{ mt: 1.5 }}>{live.poll.error ?? 'Reading failed.'}</Alert>}
                  {lastTest && <Typography variant="caption" color="text.secondary" component="p" sx={{ mt: 1 }}>Connection last tested {f.relative(lastTest)}.</Typography>}
                </Box>
                <Stack direction="row" spacing={1} sx={{ flexShrink: 0 }}>
                  <Button size="small" variant="outlined" startIcon={<NetworkCheckRounded />} onClick={() => test(s.id)} disabled={testing === s.id}>
                    {testing === s.id ? 'Testing…' : 'Test'}
                  </Button>
                  <Button size="small" variant="outlined" startIcon={<EditRounded />} onClick={() => setWizard({ edit: s.id })}>Edit</Button>
                  {(status?.mode !== 'automatic' || !s.controllable) && <Button size="small" color="error" onClick={() => setRemove(s)}>Remove</Button>}
                </Stack>
              </Stack>
              {live && <BatteryModeControls system={live} />}
            </Card>
          );
        })}
        <ControlCard onEnable={() => setEnableOpen(true)} />
      </Stack>
      {wizard && <SystemWizard editId={wizard.edit} onClose={() => { setWizard(null); if (sub === 'add') window.location.hash = '#/settings/systems'; }} />}
      {enableOpen && <EnableControlDialog onClose={() => setEnableOpen(false)} />}
      <ConfirmDialog open={!!remove} title={`Remove ${remove?.name}?`} confirm="Remove system" danger busy={removing} onConfirm={doRemove} onClose={() => setRemove(null)}>
        <Typography variant="body2">kWattson stops reading it and plans without it. Its past readings and stored credentials are kept.</Typography>
      </ConfirmDialog>
    </Box>
  );
}

function Spec({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <Box>
      <Typography variant="caption" color="text.secondary" component="div">{label}</Typography>
      <Typography variant="body2" sx={{ fontWeight: 700 }}>{value}{unit && value !== '–' ? unit : ''}</Typography>
    </Box>
  );
}

/* --------------------------------------------------------- automatic control */
function ControlCard({ onEnable }: { onEnable: () => void }) {
  const { status, reloadStatus, can } = useApp();
  const { doc } = useSettings();
  const auto = status?.mode === 'automatic';
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const recovery = status?.control_retry;
  const guards = [...new Set<string>([...(status?.guards ?? []).filter((g: string) => !/monitor mode/i.test(g)),
    ...(recovery ? [recovery.reason] : [])])];
  const controllable = (doc.settings.systems as any[]).filter((s) => s.enabled && s.battery && s.controllable);
  async function toggle() {
    if (!auto && !status?.control_commissioned) {
      onEnable();
      return;
    }
    setBusy(true);
    try {
      const result = await post(auto ? '/api/control/stop' : '/api/control/enable');
      await reloadStatus();
      if (auto && result.problems?.length) toast('Automatic control is off. Release still needs confirmation.', 'warning');
    } catch (error) {
      toast(errorText(error), 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Automatic control" help={<HelpTip label="automatic control">
      In automatic mode kWattson confirms the adopted schedule and keeps checking it. FoxESS daily schedules are only rewritten for material plan changes.
      When a check fails, kWattson pauses commands, returns to self-use and resumes when checks pass.
    </HelpTip>}>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{ alignItems: { sm: 'center' }, justifyContent: 'space-between' }}>
        <Box>
          <Typography variant="body1" sx={{ fontWeight: 700 }}>{auto ? (guards.length ? 'On, waiting to resume' : 'On: kWattson is controlling the battery') : 'Off: monitoring and advice only'}</Typography>
          <Typography variant="body2" color="text.secondary">
            {controllable.length ? `Control allowed on ${controllable.map((s) => s.name).join(', ')}.` : 'No system allows control yet. Edit a system to allow it.'}
          </Typography>
        </Box>
        {can('admin') && <Button variant="contained" color={auto ? 'error' : 'battery'} startIcon={<BoltRounded />}
          onClick={() => void toggle()} disabled={busy || !status || (!auto && !controllable.length)}>
          {auto ? 'Turn off' : status?.control_commissioned ? 'Turn on' : 'Turn on…'}
        </Button>}
      </Stack>
      {controllable.some(s => s.plugin === 'foxess' && s.plugin_config.daily_schedule !== false) && <Alert severity="info" sx={{ mt: 2 }}>
        FoxESS uses a confirmed daily schedule. If kWattson goes offline, that schedule can repeat each day until replaced or disabled in the FoxESS app.
      </Alert>}
      {guards.length > 0 && (
        <Alert severity={auto ? 'warning' : 'info'} sx={{ mt: 2 }}>
          <strong>{auto ? 'Paused because:' : 'Before it can run:'}</strong>
          <Box component="ul" sx={{ m: 0, pl: 2.5 }}>{guards.map((g) => <li key={g}>{g.includes('commissioned') ? 'Complete the checklist under Turn on to commission these system settings.' : g}{/weather/i.test(g) && <Button size="small" href="#/settings/home/location">Check weather</Button>}</li>)}</Box>
          {recovery && <ControlRetry recovery={recovery} />}
        </Alert>
      )}
    </Section>
  );
}

const PHRASE = 'ENABLE BATTERY CONTROL';
const CHECKLIST = [
  'The inverter’s own app shows the same battery level and power as kWattson.',
  'I know how to put the inverter back into self-use from its own app.',
  'I checked the minimum battery level against the inverter and the grid limits against my connection agreement. The planner subtracts house demand before calculating export.',
  'I’ve watched at least a day of plans in monitoring mode and they make sense.',
];

function EnableControlDialog({ onClose }: { onClose: () => void }) {
  const { reloadStatus, session } = useApp();
  const toast = useToast();
  const [ticks, setTicks] = useState<boolean[]>(CHECKLIST.map(() => false));
  const [phrase, setPhrase] = useState('');
  const [password, setPassword] = useState('');
  const [otp, setOtp] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ready = ticks.every(Boolean) && phrase.trim() === PHRASE && password.length > 0;
  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const result = await post('/api/control/enable', { password, otp, confirmation: phrase.trim(), acknowledge: true });
      toast(result.initial_control?.reasons?.length ? 'Automatic control is on and waiting to resume.' : 'Automatic control is on.');
      await reloadStatus();
      onClose();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }
  return (
    <Dialog open onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth aria-labelledby="enable-title">
      <DialogTitle id="enable-title">Turn on automatic control</DialogTitle>
      <DialogContent sx={{ display: 'grid', gap: 2 }}>
        <Typography variant="body2" color="text.secondary">Commissioning checklist. Tick each one only if it’s true.</Typography>
        <Box>
          {CHECKLIST.map((c, i) => (
            <FormControlLabel key={c} sx={{ display: 'flex', alignItems: 'flex-start', mb: 1, '& .MuiCheckbox-root': { pt: 0.25 } }}
              control={<Checkbox checked={ticks[i]} onChange={(e) => setTicks(ticks.map((t, j) => (j === i ? e.target.checked : t)))} />}
              label={<Typography variant="body2">{c}</Typography>} />
          ))}
        </Box>
        <TextField label={`Type ${PHRASE}`} value={phrase} onChange={(e) => setPhrase(e.target.value)} autoComplete="off" />
        <TextField label="Your password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        {session.mfa_enabled && <TextField label="Two-step code" value={otp} onChange={(e) => setOtp(e.target.value)} slotProps={{ htmlInput: { inputMode: 'numeric' } }} />}
        {error && <Alert severity="error" role="alert">{error}</Alert>}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={onClose} disabled={busy} autoFocus>Cancel</Button>
        <Button variant="contained" color="battery" onClick={submit} disabled={busy || !ready}>{busy ? 'Turning on…' : 'Turn on automatic control'}</Button>
      </DialogActions>
    </Dialog>
  );
}

/* ------------------------------------------------------------------- wizard */

interface Draft {
  id: string; name: string; plugin: string; plugin_config: Record<string, any>; enabled: boolean; coupling: 'dc' | 'ac';
  ac_kw: string; pv_kwp: string; pv_dc_max_kw: string; hasBattery: boolean;
  capacity_kwh: string; charge_kw: string; discharge_kw: string; reserve_pct: string; site_meter: boolean; controllable: boolean;
  battery_extra: Record<string, any>;
}

function toDraft(s: any): Draft {
  const b = s.battery;
  const str = (v: any) => (v == null ? '' : String(v));
  return {
    id: s.id, name: s.name, plugin: s.plugin, plugin_config: { ...(s.plugin_config ?? {}) }, enabled: s.enabled, coupling: s.coupling,
    ac_kw: str(s.ac_kw), pv_kwp: str(s.pv_kwp), pv_dc_max_kw: str(s.pv_dc_max_kw), hasBattery: !!b,
    capacity_kwh: str(b?.capacity_kwh ?? 10), charge_kw: str(b?.charge_kw ?? 5), discharge_kw: str(b?.discharge_kw ?? 5), reserve_pct: str(b?.reserve_pct ?? 15),
    site_meter: s.site_meter, controllable: s.controllable, battery_extra: b ? { ...b } : {},
  };
}
function fromDraft(d: Draft, enabled: boolean) {
  const num = (v: string) => (v === '' ? null : Number(v));
  return {
    id: d.id, name: d.name.trim(), plugin: d.plugin, plugin_config: d.plugin_config, enabled, coupling: d.coupling,
    ac_kw: num(d.ac_kw), pv_kwp: num(d.pv_kwp) ?? 0, pv_dc_max_kw: num(d.pv_dc_max_kw),
    battery: d.hasBattery ? { ...d.battery_extra, capacity_kwh: num(d.capacity_kwh), charge_kw: num(d.charge_kw), discharge_kw: num(d.discharge_kw), reserve_pct: num(d.reserve_pct) ?? 15 } : null,
    controllable: d.controllable, site_meter: d.site_meter,
  };
}

const STEPS = ['Choose a source', 'Connection', 'Hardware limits', 'Review & save'];

function SystemWizard({ editId, onClose }: { editId?: string; onClose: () => void }) {
  const { doc, reload } = useSettings();
  const toast = useToast();
  const fullScreen = useMediaQuery((t: Theme) => t.breakpoints.down('sm'));
  const titleId = useId();
  const existing = editId ? (doc.settings.systems as any[]).find((s) => s.id === editId) : null;
  const plugins = doc.plugins.filter((p) => p.kind === 'system');
  const taken = new Set((doc.settings.systems as any[]).map((s) => s.id));
  const [step, setStep] = useState(editId ? 1 : 0);
  const [draft, setDraft] = useState<Draft>(() => existing ? toDraft(existing) : {
    id: '', name: '', plugin: '', plugin_config: {}, enabled: true, coupling: 'dc', ac_kw: '', pv_kwp: '', pv_dc_max_kw: '', hasBattery: true,
    capacity_kwh: '', charge_kw: '', discharge_kw: '', reserve_pct: '', site_meter: !(doc.settings.systems as any[]).some((s) => s.site_meter), controllable: false, battery_extra: {},
  });
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [tested, setTested] = useState<'ok' | 'skipped' | null>(editId ? 'skipped' : null);
  const [reported, setReported] = useState<Partial<Pick<Draft, 'ac_kw' | 'reserve_pct' | 'capacity_kwh'>>>({});
  const [hardware, setHardware] = useState<Record<string, any>>({});
  const [connectionWarnings, setConnectionWarnings] = useState<string[]>([]);
  const [testMsg, setTestMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const plugin = plugins.find((p) => p.name === draft.plugin);
  const configured = !editId ? [] : doc.configured_secrets[`system:${draft.id}`] ?? [];
  function resetTest() {
    setReported({});
    setHardware({});
    setConnectionWarnings([]);
    setTested(null);
    setTestMsg(null);
    setError(null);
  }
  function set(patch: Partial<Draft>) {
    if (step < 2) resetTest();
    // Once an owner edits a discovered rating, the field stays fully editable, including blank.
    if ('ac_kw' in patch || 'reserve_pct' in patch || 'capacity_kwh' in patch) {
      setReported(previous => ({ ...previous, ...('ac_kw' in patch ? { ac_kw: undefined } : {}), ...('reserve_pct' in patch ? { reserve_pct: undefined } : {}), ...('capacity_kwh' in patch ? { capacity_kwh: undefined } : {}) }));
    }
    setDraft((previous) => ({ ...previous, ...patch }));
  }

  const discoverySummary = [reported.ac_kw && ('an inverter rating of ' + reported.ac_kw + ' kW'),
    reported.capacity_kwh && ('a battery capacity of ' + reported.capacity_kwh + ' kWh'),
    reported.reserve_pct && ('a grid-connected reserve of ' + reported.reserve_pct + '%')].filter(Boolean).join(' and ');
  const hardwareDraft = { ...draft, ac_kw: draft.ac_kw || reported.ac_kw || '', reserve_pct: draft.reserve_pct || reported.reserve_pct || '', capacity_kwh: draft.capacity_kwh || reported.capacity_kwh || '' };
  const hardwareWarnings = [...connectionWarnings];
  if (hardware.work_modes?.length) {
    const missing = ['SelfUse', 'Feedin', 'ForceCharge', 'ForceDischarge'].filter(mode => !hardware.work_modes.includes(mode));
    if (missing.length) hardwareWarnings.push(`The inverter does not report support for: ${missing.join(', ')}. A plan requiring those modes cannot be sent.`);
  }
  if (draft.hasBattery && hardware.scheduler_limit_kw && Number(draft.charge_kw) > hardware.scheduler_limit_kw) {
    hardwareWarnings.push(`The battery DC charge limit is above the ${f.kw(hardware.scheduler_limit_kw)} AC scheduler limit. Solar can use the DC limit; grid charging commands will be capped automatically.`);
  }
  if (draft.hasBattery && hardware.capacity_kwh && hardwareDraft.capacity_kwh && Math.abs(Number(hardwareDraft.capacity_kwh) - hardware.capacity_kwh) > 0.1) {
    hardwareWarnings.push(`FoxESS reports ${f.kwh(hardware.capacity_kwh)} battery capacity. Check the capacity you entered.`);
  }

  function stepError(i: number, requireTest = true): string | null {
    if (i === 0) return draft.plugin ? null : 'Choose what you’re connecting.';
    if (i === 1) {
      if (!draft.name.trim()) return 'Give the system a name.';
      if (!editId && taken.has(draft.id)) return 'A system with this name already exists. Choose another name.';
      const missingField = plugin?.config_schema?.required?.find((key: string) => draft.plugin_config[key] == null || draft.plugin_config[key] === '');
      if (missingField) return 'Enter ' + (plugin.config_schema.properties[missingField]?.title || missingField) + '.';
      const missing = (plugin?.secrets ?? []).filter((s: any) => s.required && !secrets[s.name]?.trim() && !configured.includes(s.name));
      if (missing.length) return `Enter ${missing.map((m: any) => m.label).join(', ')}.`;
      if (requireTest && draft.plugin !== 'push' && !tested) return 'Test the connection, or choose “Skip test” to carry on without it.';
    }
    if (i === 2) {
      const n = (v: string) => Number(v);
      if (!(n(hardwareDraft.ac_kw) > 0 && n(hardwareDraft.ac_kw) <= 100)) return 'Inverter limit must be between 0 and 100 kW.';
      if (hardware.ac_kw && n(hardwareDraft.ac_kw) > hardware.ac_kw + 0.01) return `Inverter AC limit cannot exceed the reported ${hardware.ac_kw} kW rating.`;
      if (draft.pv_kwp === '' || n(draft.pv_kwp) < 0 || n(draft.pv_kwp) > 200) return 'Panels must be between 0 and 200 kWp.';
      if (draft.pv_dc_max_kw && !(n(draft.pv_dc_max_kw) > 0 && n(draft.pv_dc_max_kw) <= 200)) return 'Max PV DC must be above 0 and at most 200 kW.';
      if (n(hardwareDraft.capacity_kwh) > 500 && draft.hasBattery) return 'Battery capacity must be at most 500 kWh.';
      if (draft.hasBattery && (n(draft.charge_kw) > 100 || n(draft.discharge_kw) > 100)) return 'Charge and discharge limits must be at most 100 kW.';
      if (draft.site_meter && draft.enabled && doc.settings.systems.some((s: any) => s.id !== editId && s.enabled && s.site_meter)) return 'Another system already measures the whole house. Turn this switch off.';
      if (draft.hasBattery && !(n(hardwareDraft.capacity_kwh) > 0.5 && n(draft.charge_kw) > 0 && n(draft.discharge_kw) > 0)) return 'Enter the battery size and its charge and discharge limits.';
      if (draft.hasBattery && hardwareDraft.reserve_pct === '') return 'Enter the battery reserve.';
      if (draft.hasBattery && !(n(hardwareDraft.reserve_pct) >= 0 && n(hardwareDraft.reserve_pct) <= 90)) return 'Reserve must be between 0 and 90%.';
      if (draft.hasBattery && !(Number(draft.battery_extra.max_soc_pct ?? 100) >= 30 && Number(draft.battery_extra.max_soc_pct ?? 100) <= 100)) return 'Maximum battery level must be between 30 and 100%.';
      if (draft.hasBattery && n(hardwareDraft.reserve_pct) >= Number(draft.battery_extra.max_soc_pct ?? 100)) return 'Reserve must be below the maximum battery level.';
      if (draft.hasBattery && hardware.min_soc_pct != null && n(hardwareDraft.reserve_pct) < hardware.min_soc_pct) return `Reserve must be at least the inverter minimum of ${hardware.min_soc_pct}%.`;
      if (draft.plugin === 'foxess' && draft.hasBattery && draft.controllable && hardware.daily_schedule_supported === false && draft.plugin_config.daily_schedule !== false) return 'Disable Daily schedule in Connection, or retest to confirm the inverter supports it.';
    }
    return null;
  }

  async function runTest() {
    const connectionError = stepError(1, false);
    if (connectionError) { setError(connectionError); return; }
    setBusy(true);
    resetTest();
    try {
      // Tests the unsaved config; nothing is written. Blank secrets fall back to stored ones.
      const values = Object.fromEntries(Object.entries(secrets).filter(([, v]) => v.trim() !== ''));
      const spec = { id: draft.id, name: draft.name.trim(), plugin: draft.plugin, plugin_config: draft.plugin_config };
      const r = await post('/api/test-draft/system', { spec, secrets: values });
      const hw = r.hardware ?? {};
      setReported({
        ...(typeof hw.ac_kw === 'number' ? { ac_kw: String(hw.ac_kw) } : {}),
        ...(typeof hw.reserve_pct === 'number' ? { reserve_pct: String(hw.reserve_pct) } : {}),
        ...(typeof hw.capacity_kwh === 'number' ? { capacity_kwh: String(hw.capacity_kwh) } : {}),
      });
      setHardware(hw);
      setConnectionWarnings(r.warnings ?? []);
      if (r.connection?.serial && !draft.plugin_config.serial) {
        setDraft(previous => ({ ...previous, plugin_config: { ...previous.plugin_config, serial: r.connection.serial } }));
      }
      const reading = r?.reading ?? r;
      const soc = reading?.soc;
      setTested('ok');
      setTestMsg({ ok: true, text: `Connected.${soc != null ? ` Battery at ${f.pct(soc)}.` : ''}${reading?.pv_kw != null ? ` Solar ${f.kw(reading.pv_kw)}.` : ''}` });
    } catch (e) {
      setTestMsg({ ok: false, text: errorText(e) });
    } finally {
      setBusy(false);
    }
  }

  async function finish() {
    setBusy(true);
    setError(null);
    try {
      await put('/api/devices/system', { spec: fromDraft(hardwareDraft, draft.enabled), secrets, original: existing ?? null });
      await reload();
      toast(editId ? `${draft.name} saved.` : `${draft.name} added. kWattson will start reading it on its next poll.`);
      onClose();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  const cancel = onClose;

  function next() {
    setTouched(true);
    const e = stepError(step);
    if (e) { setError(e); return; }
    setError(null);
    setTouched(false);
    setStep(step + 1);
  }

  const body: Record<number, ReactNode> = {
    0: (
      <Box role="radiogroup" aria-label="System type" sx={{ display: 'grid', gap: 1.5, gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' } }}>
        {plugins.map((p) => {
          const Icon = PLUGIN_ICON[p.name] ?? SolarPowerRounded;
          const sel = draft.plugin === p.name;
          return (
            <Card key={p.name} component="button" type="button" role="radio" aria-checked={sel}
              onClick={() => { if (p.name === draft.plugin) return; setSecrets({}); set({ plugin: p.name, plugin_config: {} }); }}
              sx={{
                p: 2, textAlign: 'left', cursor: 'pointer', font: 'inherit', color: 'inherit', bgcolor: sel ? 'secondary.light' : 'background.paper',
                borderColor: sel ? 'secondary.main' : 'divider', borderWidth: sel ? 2 : 1, m: sel ? 0 : '1px', transition: 'background-color .15s, border-color .15s',
                '&:hover': { borderColor: 'secondary.main' }, '&:focus-visible': { outline: '3px solid', outlineColor: 'secondary.main', outlineOffset: 2 },
              }}>
              <Stack direction="row" spacing={1.5} sx={{ alignItems: 'flex-start' }}>
                <Box aria-hidden sx={{ width: 40, height: 40, borderRadius: 2.5, display: 'grid', placeItems: 'center', bgcolor: 'solar.light', color: 'solar.main', flexShrink: 0 }}><Icon /></Box>
                <Box sx={{ flex: 1 }}>
                  <Typography variant="subtitle2" component="span" sx={{ display: 'block', fontSize: '0.9375rem' }}>{p.title}</Typography>
                  <Typography variant="caption" color="text.secondary" component="span" sx={{ display: 'block' }}>{p.description}</Typography>
                  {p.can_control && <Chip size="small" label="Can control battery" color="battery" variant="outlined" sx={{ mt: 0.75, height: 20 }} />}
                </Box>
                {sel && <CheckCircleRounded sx={{ color: 'secondary.main' }} />}
              </Stack>
            </Card>
          );
        })}
      </Box>
    ),
    1: plugin && (
      <Box sx={{ display: 'grid', gap: 2 }}>
        <TextField label="Name" value={draft.name} required autoFocus={!editId}
          onChange={(e) => set({ name: e.target.value, ...(editId ? {} : { id: slug(e.target.value) }) })}
          error={touched && !draft.name.trim()} helperText={!editId && draft.id ? `ID: ${draft.id}` : ' '} slotProps={{ htmlInput: { maxLength: 60 } }} />
        {Object.entries<any>(plugin.config_schema?.properties ?? {}).map(([key, sch]) => (
          <SchemaField key={key} required={plugin.config_schema?.required?.includes(key)} name={key} schema={sch} value={draft.plugin_config[key]} onChange={(v) => set({ plugin_config: { ...draft.plugin_config, [key]: v } })} />
        ))}
        {(plugin.secrets ?? []).map((s: any) => (
          <TextField key={s.name} label={s.label} type="password" autoComplete="new-password" required={s.required && !configured.includes(s.name)}
            value={secrets[s.name] ?? ''} onChange={(e) => { setSecrets({ ...secrets, [s.name]: e.target.value }); resetTest(); }}
            placeholder={configured.includes(s.name) ? 'Saved. Leave blank to keep it.' : undefined}
            slotProps={{ inputLabel: configured.includes(s.name) ? { shrink: true } : undefined, input: { endAdornment: s.help ? <InputAdornment position="end"><HelpTip label={s.label}>{s.help}</HelpTip></InputAdornment> : undefined } }}
            helperText="Stored encrypted. Never shown again." />
        ))}
        {draft.plugin === 'push' && !editId ? <Alert severity="info">After saving, create an ingest token in Settings › Connections &amp; API. Send readings to <code>/api/ingest/system/{draft.id || 'your-id'}</code>.</Alert> : <Box sx={{ p: 2, borderRadius: 3, bgcolor: 'action.hover' }}>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5} sx={{ alignItems: { sm: 'center' } }}>
            <Button variant="contained" startIcon={<NetworkCheckRounded />} onClick={runTest} disabled={busy || !draft.name.trim()}>
              {busy ? 'Testing…' : 'Test connection'}
            </Button>
            {tested !== 'ok' && <Button onClick={() => { setTested('skipped'); setError(null); setTestMsg({ ok: true, text: 'Test skipped. You can test from the system card later.' }); }}>Skip test</Button>}
          </Stack>
          {testMsg && <Alert severity={testMsg.ok ? (tested === 'ok' ? 'success' : 'info') : 'error'} sx={{ mt: 1.5 }} role="status">{testMsg.text}</Alert>}
          <Typography variant="caption" color="text.secondary" component="p" sx={{ mt: 1 }}>The test only reads. Nothing is saved until you finish.</Typography>
        </Box>}
      </Box>
    ),
    2: (
      <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' } }}>
        <Typography variant="body2" color="text.secondary" sx={{ gridColumn: '1 / -1' }}>{discoverySummary ? 'The connection reported ' + discoverySummary + '. Check the values below.' : 'Enter the ratings from your inverter and battery.'} For a solar-only system, turn off “Has a battery”.</Typography>
        <Num label="Inverter AC limit" unit="kW" value={hardwareDraft.ac_kw} onChange={(v) => set({ ac_kw: v })} required help="The most AC power the inverter can output. Solar above this is clipped unless a DC-coupled battery soaks it up." />
        <Num label="Panels" unit="kWp" value={draft.pv_kwp} required onChange={(v) => set({ pv_kwp: v })} help="Nameplate size of the solar array on this inverter. Use 0 for a battery-only inverter." />
        <Num label="Max PV DC" unit="kW" value={draft.pv_dc_max_kw} onChange={(v) => set({ pv_dc_max_kw: v })} help="The most DC solar power the inverter accepts. Leave blank if you don’t know." />
        <TextField select label="Coupling" value={draft.coupling} onChange={(e) => set({ coupling: e.target.value as 'dc' | 'ac' })}
          slotProps={{ input: { endAdornment: <InputAdornment position="end" sx={{ mr: 2 }}><HelpTip title="DC or AC coupled?" label="coupling">
            <strong>DC</strong>: panels and battery share one hybrid inverter, so the battery can catch solar above the AC limit.<br />
            <strong>AC</strong>: a separate battery inverter (or a solar-only inverter with no battery).
          </HelpTip></InputAdornment> } }}>
          <MenuItem value="dc">DC (hybrid inverter)</MenuItem>
          <MenuItem value="ac">AC (separate battery inverter)</MenuItem>
        </TextField>
        <Box sx={{ gridColumn: '1 / -1' }}>
          <FormControlLabel control={<Switch checked={draft.hasBattery} onChange={(e) => set({ hasBattery: e.target.checked })} />} label="Has a battery" />
        </Box>
        {draft.hasBattery && <>
          <Num label="Battery capacity" unit="kWh" value={hardwareDraft.capacity_kwh} onChange={(v) => set({ capacity_kwh: v })} required help="Battery design capacity, when reported by the inverter. Unknown values are left for you to confirm." />
          <Num label="Reserve" unit="%" value={hardwareDraft.reserve_pct} onChange={(v) => set({ reserve_pct: v })} help="The plan never takes the battery below this. Keep some for blackouts." />
          <Num label="Maximum battery level" unit="%" value={String(draft.battery_extra.max_soc_pct ?? 100)} onChange={v => set({ battery_extra: { ...draft.battery_extra, max_soc_pct: Number(v) } })} help="Charging targets cannot exceed this level. It must stay above the reserve." />
          <Num label="Battery DC charge limit" unit="kW" value={draft.charge_kw} onChange={(v) => set({ charge_kw: v })} required help="Maximum combined battery charging from solar and grid. This can be higher than the AC scheduler limit." />
          <Num label="Discharge limit" unit="kW" value={draft.discharge_kw} onChange={(v) => set({ discharge_kw: v })} required />
        </>}
        {!!hardware.scheduler_limit_kw && <Alert severity="info" sx={{ gridColumn: '1 / -1' }}>FoxESS scheduler limit: {f.kw(hardware.scheduler_limit_kw)} AC. This is read from the inverter before commands are sent. Battery DC charge power must be confirmed separately.</Alert>}
        {hardware.min_soc_pct != null && <Typography variant="body2" sx={{ gridColumn: '1 / -1' }}>Inverter minimum battery level: {f.pct(hardware.min_soc_pct)}. Choose a reserve at or above this.</Typography>}
        {draft.plugin === 'foxess' && draft.hasBattery && draft.plugin_config.daily_schedule !== false && <Alert severity="info" sx={{ gridColumn: '1 / -1' }}>
          Daily control confirms the remaining day's schedule and only replaces it for material plan changes. If kWattson goes offline, the schedule repeats each day until disabled in the FoxESS app.
        </Alert>}
        {hardwareWarnings.map((warning, index) => <Alert key={index} severity="warning" sx={{ gridColumn: '1 / -1' }}>{warning}</Alert>)}
        <Box sx={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center' }}>
          <FormControlLabel control={<Switch checked={draft.site_meter} onChange={(e) => set({ site_meter: e.target.checked })} />} label="This system measures the whole house" />
          <HelpTip label="whole-house meter">Turn on if this inverter’s meter sees all grid import and export for the house. Only one system should have this on.</HelpTip>
        </Box>
        {editId && plugin?.can_control && (
          <Box sx={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center' }}>
            <FormControlLabel control={<Switch checked={draft.controllable} onChange={(e) => set({ controllable: e.target.checked })} />} label="Allow automatic control" />
            <HelpTip label="allow automatic control">Lets kWattson send commands to this system once automatic control is turned on. Nothing is sent in monitoring mode.</HelpTip>
          </Box>
        )}
      </Box>
    ),
    3: (
      <Box>
        <Facts rows={[
          ['Type', plugin?.title ?? draft.plugin],
          ['Name', draft.name],
          ['Connection', tested === 'ok' ? 'Tested and working' : draft.plugin === 'push' && !editId ? 'Waiting for the first push' : 'Not tested'],
          ['Inverter', `${f.kw(Number(hardwareDraft.ac_kw))} AC · ${draft.coupling.toUpperCase()} coupled`],
          ['Panels', `${f.n(Number(draft.pv_kwp || 0))} kWp${draft.pv_dc_max_kw ? ` · max DC ${f.kw(Number(draft.pv_dc_max_kw))}` : ''}`],
          ['Battery', draft.hasBattery ? `${f.kwh(Number(hardwareDraft.capacity_kwh))}, ${f.kw(Number(draft.charge_kw))} DC in / ${f.kw(Number(draft.discharge_kw))} out, reserve ${f.pct(Number(hardwareDraft.reserve_pct))}` : 'None'],
          ['Whole-house meter', draft.site_meter ? 'Yes' : 'No'],
        ]} />
        {hardwareWarnings.map((warning, index) => <Alert key={index} severity="warning" sx={{ mt: 1 }}>{warning}</Alert>)}
        <Alert severity="info" variant="outlined" sx={{ mt: 2 }}>{draft.controllable ? 'This system allows battery commands when automatic control is on.' : 'This system will be monitored. Battery commands are disabled for it.'}</Alert>
      </Box>
    ),
  };

  return (
    <Dialog open onClose={busy ? undefined : cancel} fullScreen={fullScreen} maxWidth="md" fullWidth aria-labelledby={titleId}
      slotProps={{ paper: { sx: { minHeight: { sm: 560 } } } }}>
      <DialogTitle id={titleId} sx={{ display: 'flex', alignItems: 'center', pr: 1 }}>
        <Box sx={{ flex: 1 }}>{editId ? `Edit ${existing?.name}` : 'Add a system'}</Box>
        <IconButton onClick={cancel} aria-label="Close" disabled={busy}><CloseRounded /></IconButton>
      </DialogTitle>
      <Box sx={{ px: 3, pb: 2 }}>
        {editId ? (
          <Tabs value={step} onChange={(_, v) => { if (busy) return; setError(null); setStep(v); }} aria-label="Edit sections">
            <Tab value={1} label="Connection" id="etab-1" aria-controls="epanel" />
            <Tab value={2} label="Hardware limits" id="etab-2" aria-controls="epanel" />
            <Tab value={3} label="Review" id="etab-3" aria-controls="epanel" />
          </Tabs>
        ) : (
          <Stepper activeStep={step} alternativeLabel={!fullScreen} orientation="horizontal" aria-label="Steps"
            sx={{ '& .MuiStepLabel-label': { fontWeight: 600 }, '& .MuiStepIcon-root.Mui-active, & .MuiStepIcon-root.Mui-completed': { color: 'secondary.main' } }}>
            {STEPS.map((l, i) => (
              <Step key={l}><StepLabel aria-current={i === step ? 'step' : undefined}>{fullScreen && i !== step ? '' : l}</StepLabel></Step>
            ))}
          </Stepper>
        )}
      </Box>
      <DialogContent key={step} dividers id="epanel" role={editId ? 'tabpanel' : 'group'} aria-label={editId ? undefined : `Step ${step + 1} of 4: ${STEPS[step]}`}
        aria-labelledby={editId ? `etab-${step}` : undefined}>
        {!editId && <Typography variant="h3" component="h3" sx={{ mb: 2 }}>{STEPS[step]}</Typography>}
        <Box component="fieldset" disabled={busy} sx={{ border: 0, m: 0, p: 0, minWidth: 0, overflowWrap: 'anywhere' }}>{body[step]}</Box>
        {error && <Alert severity="error" role="alert" sx={{ mt: 2 }}>{error}</Alert>}
      </DialogContent>
      <DialogActions sx={{ px: 3, py: 2 }}>
        {editId ? (
          <>
            <Button onClick={cancel} disabled={busy}>Cancel</Button>
            <Button variant="contained" onClick={() => { for (const i of [1, 2]) { const e = stepError(i); if (e) { setStep(i); setError(e); return; } } void finish(); }} disabled={busy}>{busy ? 'Saving…' : 'Save'}</Button>
          </>
        ) : (
          <>
            <Button onClick={cancel} disabled={busy} sx={{ mr: 'auto' }}>Cancel</Button>
            {step > 0 && <Button onClick={() => { if (busy) return; setError(null); setStep(step - 1); }} disabled={busy}>Back</Button>}
            {step < 3 ? <Button variant="contained" onClick={next} disabled={busy}>Next</Button>
              : <Button variant="contained" color="battery" onClick={finish} disabled={busy}>{busy ? 'Saving…' : 'Save system'}</Button>}
          </>
        )}
      </DialogActions>
    </Dialog>
  );
}

