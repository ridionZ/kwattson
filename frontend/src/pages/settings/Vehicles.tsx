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
import InputAdornment from '@mui/material/InputAdornment';
import Alert from '@mui/material/Alert';
import IconButton from '@mui/material/IconButton';
import ToggleButton from '@mui/material/ToggleButton';
import ToggleButtonGroup from '@mui/material/ToggleButtonGroup';
import LinearProgress from '@mui/material/LinearProgress';
import useMediaQuery from '@mui/material/useMediaQuery';
import type { Theme } from '@mui/material/styles';
import AddRounded from '@mui/icons-material/AddRounded';
import CloseRounded from '@mui/icons-material/CloseRounded';
import CheckCircleRounded from '@mui/icons-material/CheckCircleRounded';
import ElectricCarRounded from '@mui/icons-material/ElectricCarRounded';
import EditNoteRounded from '@mui/icons-material/EditNoteRounded';
import HubRounded from '@mui/icons-material/HubRounded';
import CallReceivedRounded from '@mui/icons-material/CallReceivedRounded';
import ScienceRounded from '@mui/icons-material/ScienceRounded';
import CloudRounded from '@mui/icons-material/CloudRounded';
import NetworkCheckRounded from '@mui/icons-material/NetworkCheckRounded';
import EditRounded from '@mui/icons-material/EditRounded';
import { post, put, errorText } from '../../lib/api';
import { useApp } from '../../lib/app';
import * as f from '../../lib/fmt';
import { ConfirmDialog, Empty, Facts, HelpTip, PageHeader, useToast } from '../../components/common';
import { clone, useSettings } from './ctx';
import { Num, SchemaField, slug } from './fields';

const ICON: Record<string, typeof CloudRounded> = { manual: EditNoteRounded, homeassistant_vehicle: HubRounded, push_vehicle: CallReceivedRounded, simulator_vehicle: ScienceRounded, mg_ismart: CloudRounded };
const HOURS = Array.from({ length: 24 }, (_, h) => [h, `${h % 12 === 0 ? 12 : h % 12}:00 ${h < 12 ? 'am' : 'pm'}`] as const);

export default function Vehicles({ sub }: { sub?: string }) {
  const { doc, save } = useSettings();
  const { status } = useApp();
  const toast = useToast();
  const [wizard, setWizard] = useState<{ edit?: string } | null>(sub === 'add' ? {} : null);
  const [remove, setRemove] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const vehicles = doc.settings.vehicles as any[];
  async function doRemove() {
    setBusy(true);
    try {
      const s = clone(doc.settings);
      s.vehicles = s.vehicles.filter((v: any) => v.id !== remove.id);
      await save(s);
      toast(`${remove.name} removed.`);
      setRemove(null);
    } catch (e) {
      toast(errorText(e), 'error');
    } finally {
      setBusy(false);
    }
  }
  return (
    <Box>
      <PageHeader title="Vehicles" sub="Cars the plan should leave room for."
        actions={vehicles.length > 0 && <Button variant="contained" startIcon={<AddRounded />} onClick={() => setWizard({})}>Add a vehicle</Button>} />
      <Stack spacing={2}>
        {!vehicles.length && <Empty action={<Button variant="contained" onClick={() => setWizard({})}>Add a vehicle</Button>}>No vehicles yet. Add your EV so the plan knows when it charges and kWattson can suggest the cheapest time.</Empty>}
        {vehicles.map((v) => {
          const live = status?.vehicles?.find((x: any) => x.id === v.id);
          const soc = live?.reading?.soc ?? v.manual_soc;
          const plugin = doc.plugins.find((p) => p.name === v.plugin);
          const Icon = ICON[v.plugin] ?? ElectricCarRounded;
          return (
            <Card key={v.id} sx={{ p: 2.5 }}>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{ alignItems: { sm: 'flex-start' } }}>
                <Box aria-hidden sx={{ width: 48, height: 48, borderRadius: 3, display: 'grid', placeItems: 'center', bgcolor: 'battery.light', color: 'battery.main', flexShrink: 0 }}><Icon /></Box>
                <Box sx={{ flex: 1, minWidth: 0 }}>
                  <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
                    <Typography variant="h2">{v.name}</Typography>
                    {!v.enabled && <Chip size="small" label="Off" variant="outlined" sx={{ height: 22 }} />}
                  </Stack>
                  <Typography variant="body2" color="text.secondary">{plugin?.title ?? v.plugin}</Typography>
                  {soc != null && (
                    <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center', mt: 1.5, maxWidth: 360 }}>
                      <Typography variant="body2" sx={{ fontWeight: 700, minWidth: 40 }}>{f.pct(soc)}</Typography>
                      <LinearProgress variant="determinate" value={soc} color="secondary" sx={{ flex: 1 }} aria-label={`${v.name} battery level`} />
                      <Typography variant="caption" color="text.secondary">target {f.pct(v.target_pct)}</Typography>
                    </Stack>
                  )}
                  <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr 1fr', sm: 'repeat(4, auto)' }, justifyContent: 'start', columnGap: 4, rowGap: 1, mt: 1.5 }}>
                    <Spec label="Battery" value={f.kwh(v.capacity_kwh, 0)} />
                    <Spec label="Charger" value={f.kw(v.charger_kw)} />
                    <Spec label="Driving" value={`${f.n(v.km_per_week, 0)} km/week`} />
                    <Spec label="Usually charges" value={schedText(v.schedule)} />
                  </Box>
                </Box>
                <Stack direction="row" spacing={1} sx={{ flexShrink: 0 }}>
                  <Button size="small" variant="outlined" startIcon={<EditRounded />} onClick={() => setWizard({ edit: v.id })}>Edit</Button>
                  <Button size="small" color="error" onClick={() => setRemove(v)}>Remove</Button>
                </Stack>
              </Stack>
            </Card>
          );
        })}
      </Stack>
      {wizard && <VehicleWizard editId={wizard.edit} onClose={() => { setWizard(null); if (sub === 'add') window.location.hash = '#/settings/vehicles'; }} />}
      <ConfirmDialog open={!!remove} title={`Remove ${remove?.name}?`} confirm="Remove vehicle" danger busy={busy} onConfirm={doRemove} onClose={() => setRemove(null)}>
        <Typography variant="body2">The plan stops allowing for this car. Its past readings are kept.</Typography>
      </ConfirmDialog>
    </Box>
  );
}

function schedText(s: any) {
  const days = (s?.days ?? []) as number[];
  if (!days.length) return 'Any day';
  return `${days.length === 7 ? 'Every day' : days.map((d) => f.WEEKDAYS[d]).join(', ')}, from ${HOURS[s.start_hour ?? 18][1]}`;
}

function Spec({ label, value }: { label: string; value: string }) {
  return (
    <Box>
      <Typography variant="caption" color="text.secondary" component="div">{label}</Typography>
      <Typography variant="body2" sx={{ fontWeight: 700 }}>{value}</Typography>
    </Box>
  );
}

const STEPS = ['Choose a source', 'Car details', 'Connection', 'Review & save'];
const str = (v: any) => (v == null ? '' : String(v));

function VehicleWizard({ editId, onClose }: { editId?: string; onClose: () => void }) {
  const { doc, reload } = useSettings();
  const toast = useToast();
  const fullScreen = useMediaQuery((t: Theme) => t.breakpoints.down('sm'));
  const titleId = useId();
  const existing = editId ? (doc.settings.vehicles as any[]).find((v) => v.id === editId) : null;
  const plugins = doc.plugins.filter((p) => p.kind === 'vehicle');
  const taken = new Set((doc.settings.vehicles as any[]).map((v) => v.id));
  const [step, setStep] = useState(editId ? 1 : 0);
  const [v, setV] = useState<any>(() => existing ? { ...clone(existing), _cap: str(existing.capacity_kwh), _target: str(existing.target_pct), _charger: str(existing.charger_kw), _km: str(existing.km_per_week), _eff: str(existing.kwh_per_100km), _soc: str(existing.manual_soc) } : {
    id: '', name: '', plugin: '', plugin_config: {}, enabled: true, capacity_confirmed: false, charger_efficiency: 0.88, reminder_soc_pct: 50,
    schedule: { days: [0, 1, 2, 3, 4], start_hour: 18 }, _cap: '', _target: '80', _charger: '', _km: '250', _eff: '15', _soc: '',
  });
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [tested, setTested] = useState<'ok' | 'skipped' | null>(editId ? 'skipped' : null);
  const [testMsg, setTestMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const plugin = plugins.find((p) => p.name === v.plugin);
  const needsTest = plugin && plugin.name !== 'manual' && (plugin.name !== 'push_vehicle' || !!editId);
  const configured = !editId ? [] : doc.configured_secrets[`vehicle:${v.id}`] ?? [];
  function resetTest() {
    setTested(null);
    setTestMsg(null);
    setError(null);
  }
  function set(patch: any) {
    resetTest();
    setV((previous: any) => ({ ...previous, ...patch }));
  }

  function spec() {
    const num = (s: string) => (s === '' ? null : Number(s));
    const { _cap, _target, _charger, _km, _eff, _soc, ...rest } = v;
    return { ...rest, name: v.name.trim(), capacity_kwh: num(_cap), target_pct: num(_target) ?? 80, charger_kw: num(_charger) ?? 2.2,
      km_per_week: num(_km) ?? 0, kwh_per_100km: num(_eff) ?? 14, manual_soc: num(_soc) };
  }
  function stepError(i: number, requireTest = true): string | null {
    if (i === 0) return v.plugin ? null : 'Choose how kWattson gets this car’s data.';
    if (i === 2) {
      if (!v.name.trim()) return 'Give the car a name.';
      if (!editId && taken.has(v.id)) return 'A vehicle with this name already exists. Choose another name.';
      const missingField = plugin?.config_schema?.required?.find((key: string) => v.plugin_config[key] == null || v.plugin_config[key] === '');
      if (missingField) return 'Enter ' + (plugin.config_schema.properties[missingField]?.title || missingField) + '.';
      const missing = (plugin?.secrets ?? []).filter((s: any) => s.required && !secrets[s.name]?.trim() && !configured.includes(s.name));
      if (missing.length) return `Enter ${missing.map((m: any) => m.label).join(', ')}.`;
      if (requireTest && needsTest && !tested) return 'Test the connection, or choose “Skip test” to carry on without it.';
    }
    if (i === 1) {
      const n = Number;
      if (!(n(v._cap) > 5 && n(v._cap) <= 250)) return 'Battery size must be between 5 and 250 kWh.';
      if (!(n(v._target) >= 20 && n(v._target) <= 100)) return 'Target must be between 20 and 100%.';
      if (!(n(v._charger) >= 0.5 && n(v._charger) <= 22)) return 'Charger power must be between 0.5 and 22 kW.';
      if (!(n(v._km) >= 0 && n(v._km) <= 5000)) return 'Driving must be between 0 and 5,000 km per week.';
      if (!(n(v._eff) >= 5 && n(v._eff) <= 60)) return 'Efficiency must be between 5 and 60 kWh per 100 km.';
      if (v._soc !== '' && !(n(v._soc) >= 0 && n(v._soc) <= 100)) return 'Battery level must be between 0 and 100%.';
    }
    return null;
  }
  async function runTest() {
    const connectionError = stepError(2, false);
    if (connectionError) { setError(connectionError); return; }
    setBusy(true);
    resetTest();
    try {
      const values = Object.fromEntries(Object.entries(secrets).filter(([, x]) => x.trim() !== ''));
      const r = await post('/api/test-draft/vehicle', { spec: spec(), secrets: values });
      const reading = r?.reading;
      if (r && 'reading' in r && !reading) throw new Error('No reading yet. Check the source or skip the test and try again later.');
      setTested('ok');
      setTestMsg({ ok: true, text: `Connected.${reading?.soc != null ? ` Car at ${f.pct(reading.soc)}.` : ''}` });
    } catch (e) {
      setTestMsg({ ok: false, text: errorText(e) });
    } finally {
      setBusy(false);
    }
  }
  async function finish() {
    for (const i of [1, 2]) {
      const e = stepError(i);
      if (e) { setError(e); setStep(i); return; }
    }
    setBusy(true);
    setError(null);
    try {
      const out = spec();
      await put('/api/devices/vehicle', { spec: out, secrets, original: existing ?? null });
      await reload();
      toast(editId ? `${out.name} saved.` : `${out.name} added.`);
      onClose();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }
  function next() {
    const e = stepError(step);
    if (e) { setError(e); return; }
    setError(null);
    setStep(step + 1);
  }

  const body: Record<number, ReactNode> = {
    0: (
      <Box role="radiogroup" aria-label="Connection type" sx={{ display: 'grid', gap: 1.5, gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' } }}>
        {plugins.map((p) => {
          const Icon = ICON[p.name] ?? ElectricCarRounded;
          const sel = v.plugin === p.name;
          return (
            <Card key={p.name} component="button" type="button" role="radio" aria-checked={sel}
              onClick={() => { if (p.name === v.plugin) return; setSecrets({}); set({ plugin: p.name, plugin_config: {} }); }}
              sx={{
                p: 2, textAlign: 'left', cursor: 'pointer', font: 'inherit', color: 'inherit', bgcolor: sel ? 'secondary.light' : 'background.paper',
                borderColor: sel ? 'secondary.main' : 'divider', borderWidth: sel ? 2 : 1, m: sel ? 0 : '1px',
                '&:hover': { borderColor: 'secondary.main' }, '&:focus-visible': { outline: '3px solid', outlineColor: 'secondary.main', outlineOffset: 2 },
              }}>
              <Stack direction="row" spacing={1.5} sx={{ alignItems: 'flex-start' }}>
                <Box aria-hidden sx={{ width: 40, height: 40, borderRadius: 2.5, display: 'grid', placeItems: 'center', bgcolor: 'battery.light', color: 'battery.main', flexShrink: 0 }}><Icon /></Box>
                <Box sx={{ flex: 1 }}>
                  <Typography variant="subtitle2" component="span" sx={{ display: 'block', fontSize: '0.9375rem' }}>{p.title}</Typography>
                  <Typography variant="caption" color="text.secondary" component="span" sx={{ display: 'block' }}>{p.description}</Typography>
                </Box>
                {sel && <CheckCircleRounded sx={{ color: 'secondary.main' }} />}
              </Stack>
            </Card>
          );
        })}
      </Box>
    ),
    2: plugin && (
      <Box sx={{ display: 'grid', gap: 2 }}>
        <TextField label="Name" value={v.name} required autoFocus={!editId} placeholder="e.g. MG4"
          onChange={(e) => set({ name: e.target.value, ...(editId ? {} : { id: slug(e.target.value, 'car') }) })}
          helperText={!editId && v.id ? `ID: ${v.id}` : ' '} slotProps={{ htmlInput: { maxLength: 60 } }} />
        {Object.entries<any>(plugin.config_schema?.properties ?? {}).map(([key, sch]) => (
          <SchemaField key={key} required={plugin.config_schema?.required?.includes(key)} name={key} schema={sch} value={v.plugin_config[key]} onChange={(x) => set({ plugin_config: { ...v.plugin_config, [key]: x } })} />
        ))}
        {(plugin.secrets ?? []).map((s: any) => (
          <TextField key={s.name} label={s.label} type="password" autoComplete="new-password" required={s.required && !configured.includes(s.name)}
            value={secrets[s.name] ?? ''} onChange={(e) => { setSecrets({ ...secrets, [s.name]: e.target.value }); resetTest(); }}
            placeholder={configured.includes(s.name) ? 'Saved. Leave blank to keep it.' : undefined}
            slotProps={{ inputLabel: configured.includes(s.name) ? { shrink: true } : undefined, input: { endAdornment: s.help ? <InputAdornment position="end"><HelpTip label={s.label}>{s.help}</HelpTip></InputAdornment> : undefined } }}
            helperText="Stored encrypted. Never shown again." />
        ))}
        {needsTest ? (
          <Box sx={{ p: 2, borderRadius: 3, bgcolor: 'action.hover' }}>
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5} sx={{ alignItems: { sm: 'center' } }}>
              <Button variant="contained" startIcon={<NetworkCheckRounded />} onClick={runTest} disabled={busy || !v.name.trim()}>{busy ? 'Testing…' : 'Test connection'}</Button>
              {tested !== 'ok' && <Button onClick={() => { setTested('skipped'); setError(null); setTestMsg({ ok: true, text: 'Test skipped. Readings will show on the card once they arrive.' }); }}>Skip test</Button>}
            </Stack>
            {testMsg && <Alert severity={testMsg.ok ? (tested === 'ok' ? 'success' : 'info') : 'error'} sx={{ mt: 1.5 }} role="status">{testMsg.text}</Alert>}
            <Typography variant="caption" color="text.secondary" component="p" sx={{ mt: 1 }}>The test only reads. Nothing is saved until you finish.</Typography>
          </Box>
        ) : v.plugin === 'push_vehicle' ? <Alert severity="info">After saving, create an ingest token in Settings › Connections &amp; API. Send readings to <code>/api/ingest/vehicle/{v.id || 'your-id'}</code>.</Alert>
          : <Alert severity="info" variant="outlined">No connection needed. kWattson estimates the battery from your driving, and you can type in a level any time.</Alert>}
      </Box>
    ),
    1: (
      <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' } }}>
        <Num label="Battery size" unit="kWh" value={v._cap} onChange={(x) => set({ _cap: x })} required help="Usable capacity from the car’s specs." />
        <Num label="Charge to" unit="%" value={v._target} onChange={(x) => set({ _target: x })} help="The level you usually charge to. Many cars recommend 80% for daily use." />
        <Num label="Charger power" unit="kW" value={v._charger} onChange={(x) => set({ _charger: x })} help="2.2–2.4 kW for a wall socket, about 7 kW for a single-phase wall charger." />
        <Num label="Driving" unit="km/week" value={v._km} onChange={(x) => set({ _km: x })} />
        <Num label="Efficiency" unit="kWh/100 km" value={v._eff} onChange={(x) => set({ _eff: x })} help="Most small EVs use 13–17 kWh per 100 km." />
        {v.plugin === 'manual' && <Num label="Battery now" unit="%" value={v._soc} onChange={(x) => set({ _soc: x })} help="Optional. Type the level from the car’s app; update it when you remember." />}
        <Box sx={{ gridColumn: '1 / -1' }}>
          <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.75 }} id="days-label">Usual charging days</Typography>
          <ToggleButtonGroup size="small" value={v.schedule.days} aria-labelledby="days-label"
            onChange={(_, days) => set({ schedule: { ...v.schedule, days: [...days].sort() } })} sx={{ flexWrap: 'wrap' }}>
            {f.WEEKDAYS.map((d, i) => <ToggleButton key={d} value={i} sx={{ px: 1.5 }}>{d}</ToggleButton>)}
          </ToggleButtonGroup>
        </Box>
        <TextField select label="Usually plugged in from" value={v.schedule.start_hour} onChange={(e) => set({ schedule: { ...v.schedule, start_hour: Number(e.target.value) } })}>
          {HOURS.map(([h, l]) => <MenuItem key={h} value={h}>{l}</MenuItem>)}
        </TextField>
      </Box>
    ),
    3: (
      <Box>
        <Facts rows={[
          ['Connection', plugin?.title ?? v.plugin],
          ['Name', v.name],
          ['Test', needsTest ? (tested === 'ok' ? 'Tested and working' : 'Not tested') : 'Not needed'],
          ['Battery', `${f.kwh(Number(v._cap), 0)}, charge to ${f.pct(Number(v._target))}`],
          ['Charger', f.kw(Number(v._charger))],
          ['Driving', `${f.n(Number(v._km), 0)} km/week at ${f.n(Number(v._eff))} kWh/100 km`],
          ['Usually charges', schedText(v.schedule)],
        ]} />
      </Box>
    ),
  };

  return (
    <Dialog open onClose={busy ? undefined : onClose} fullScreen={fullScreen} maxWidth="md" fullWidth aria-labelledby={titleId} slotProps={{ paper: { sx: { minHeight: { sm: 540 } } } }}>
      <DialogTitle id={titleId} sx={{ display: 'flex', alignItems: 'center', pr: 1 }}>
        <Box sx={{ flex: 1 }}>{editId ? `Edit ${existing?.name}` : 'Add a vehicle'}</Box>
        <IconButton onClick={onClose} aria-label="Close" disabled={busy}><CloseRounded /></IconButton>
      </DialogTitle>
      <Box sx={{ px: 3, pb: 2 }}>
        {editId ? (
          <Tabs value={step} onChange={(_, x) => { if (busy) return; setError(null); setStep(x); }} aria-label="Edit sections">
            <Tab value={1} label="Car details" id="vtab-1" aria-controls="vpanel" />
            <Tab value={2} label="Connection" id="vtab-2" aria-controls="vpanel" />
            <Tab value={3} label="Review" id="vtab-3" aria-controls="vpanel" />
          </Tabs>
        ) : (
          <Stepper activeStep={step} alternativeLabel={!fullScreen} aria-label="Steps"
            sx={{ '& .MuiStepLabel-label': { fontWeight: 600 }, '& .MuiStepIcon-root.Mui-active, & .MuiStepIcon-root.Mui-completed': { color: 'secondary.main' } }}>
            {STEPS.map((l, i) => <Step key={l}><StepLabel aria-current={i === step ? 'step' : undefined}>{fullScreen && i !== step ? '' : l}</StepLabel></Step>)}
          </Stepper>
        )}
      </Box>
      <DialogContent key={step} dividers id="vpanel" role={editId ? 'tabpanel' : 'group'} aria-label={editId ? undefined : `Step ${step + 1} of 4: ${STEPS[step]}`} aria-labelledby={editId ? `vtab-${step}` : undefined}>
        {!editId && <Typography variant="h3" component="h3" sx={{ mb: 2 }}>{STEPS[step]}</Typography>}
        <Box component="fieldset" disabled={busy} sx={{ border: 0, m: 0, p: 0, minWidth: 0, overflowWrap: 'anywhere' }}>{body[step]}</Box>
        {error && <Alert severity="error" role="alert" sx={{ mt: 2 }}>{error}</Alert>}
      </DialogContent>
      <DialogActions sx={{ px: 3, py: 2 }}>
        {editId ? (
          <>
            <Button onClick={onClose} disabled={busy}>Cancel</Button>
            <Button variant="contained" onClick={finish} disabled={busy}>{busy ? 'Saving…' : 'Save'}</Button>
          </>
        ) : (
          <>
            <Button onClick={onClose} disabled={busy} sx={{ mr: 'auto' }}>Cancel</Button>
            {step > 0 && <Button onClick={() => { if (busy) return; setError(null); setStep(step - 1); }} disabled={busy}>Back</Button>}
            {step < 3 ? <Button variant="contained" onClick={next} disabled={busy}>Next</Button>
              : <Button variant="contained" color="battery" onClick={finish} disabled={busy}>{busy ? 'Saving…' : 'Save vehicle'}</Button>}
          </>
        )}
      </DialogActions>
    </Dialog>
  );
}
