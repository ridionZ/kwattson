import { useState } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import MenuItem from '@mui/material/MenuItem';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';
import { errorText, post } from '../lib/api';
import { useApp } from '../lib/app';
import * as f from '../lib/fmt';
import { ConfirmDialog, useToast } from './common';

const MODES = [
  ['self_use', 'Self-use'], ['hold', 'Pause discharge'], ['force_charge', 'Charge battery'],
  ['force_discharge', 'Export battery'], ['export_priority', 'Solar export first'],
] as const;
type Mode = typeof MODES[number][0];

export default function BatteryModeControls({ system: s }: { system: any }) {
  const { status, can, reloadStatus } = useApp();
  const toast = useToast();
  const [mode, setMode] = useState<Mode | null>(null);
  const [minutes, setMinutes] = useState(15);
  const [power, setPower] = useState('1');
  const [target, setTarget] = useState('100');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!s.enabled || !s.controllable || !s.battery) return null;
  const guards = status?.manual_control_guards ?? [];
  const allowed = can('member') && !!status?.control_commissioned && !guards.length;
  const driven = mode === 'force_charge' || mode === 'force_discharge';
  const minimum = mode === 'force_discharge' ? s.export_floor_pct ?? s.battery.reserve_pct : s.battery.reserve_pct;
  const maximumPower = Math.min(s.ac_kw, mode === 'force_charge' ? s.battery.charge_kw : s.battery.discharge_kw,
    mode === 'force_charge' ? s.manual_charge_limit_kw ?? 0 : s.manual_export_limit_kw ?? 0);
  const valid = !driven || Number(power) > 0 && Number(power) <= maximumPower && Number(target) >= minimum && Number(target) <= s.battery.max_soc_pct;

  function choose(next: Mode) {
    setError(null);
    setMode(next);
    const maximum = Math.min(s.ac_kw, next === 'force_charge' ? s.battery.charge_kw : s.battery.discharge_kw,
      next === 'force_charge' ? s.manual_charge_limit_kw ?? 0 : s.manual_export_limit_kw ?? 0);
    setPower(String(maximum));
    setTarget(String(next === 'force_discharge' ? Math.ceil((s.export_floor_pct ?? s.battery.reserve_pct) * 10) / 10 : s.battery.max_soc_pct));
  }

  async function send(selected: Mode | 'automatic') {
    setBusy(true);
    setError(null);
    try {
      const result = await post(`/api/systems/${encodeURIComponent(s.id)}/mode`, {
        mode: selected, minutes, power_kw: driven && selected !== 'automatic' ? Number(power) : null,
        target_pct: driven && selected !== 'automatic' ? Number(target) : null,
      });
      setMode(null);
      if (selected === 'automatic') toast('Temporary mode ended. Control status has been refreshed.');
      else if (result.control?.executed) toast('Battery mode confirmed.');
      else toast('The mode is waiting for confirmation. Check control status.', 'warning');
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
      await reloadStatus();
    }
  }

  return <Box sx={{ mt: 2, pt: 1.5, borderTop: 1, borderColor: 'divider' }} role="group" aria-label={`${s.name} battery modes`}>
    <Typography variant="subtitle2">{s.name} modes</Typography>
    <Typography variant="caption" component="p" sx={{ mb: 1 }}>
      {s.manual_mode ? `${s.manual_mode.confirmed ? '' : 'Waiting for confirmation: '}${MODES.find(([value]) => value === s.manual_mode.mode)?.[1] ?? s.manual_mode.mode} until ${f.when(s.manual_mode.end)}`
        : status?.mode === 'automatic' ? 'Following the automatic plan' : 'Using the inverter default'}
    </Typography>
    {can('member') && <Stack direction="row" spacing={.75} useFlexGap sx={{ flexWrap: 'wrap' }}>
      <Button size="small" variant="outlined" disabled={busy || !s.manual_mode} onClick={() => void send('automatic')}>
        {status?.mode === 'automatic' ? 'Resume plan' : 'Inverter default'}
      </Button>
      {MODES.filter(([value]) => s.supported_modes?.includes(value)).map(([value, label]) =>
        <Button key={value} size="small" variant={s.manual_mode?.mode === value ? 'contained' : 'outlined'}
          disabled={busy || !allowed || value === 'force_charge' && !(s.manual_charge_limit_kw > 0)
            || value === 'force_discharge' && (!(s.manual_export_limit_kw > 0) || s.export_floor_pct == null || s.reading?.soc <= s.export_floor_pct)}
          onClick={() => choose(value)}>{label}</Button>)}
    </Stack>}
    {!status?.control_commissioned && <Typography variant="caption" component="p">Commission battery control in Solar &amp; batteries before selecting a mode.</Typography>}
    {!!guards.length && status?.control_commissioned && <Typography variant="caption" color="warning.main" component="p">{guards.join(' · ')}</Typography>}
    {s.manual_mode && <Typography variant="caption" component="p" sx={{ mt: .75 }}>The automatic forecast may change as new readings arrive. Keep kWattson online to end this override.</Typography>}
    {error && <Typography variant="caption" color="error" role="alert" component="p">{error}</Typography>}
    <ConfirmDialog open={mode !== null} title={`${MODES.find(([value]) => value === mode)?.[1] ?? 'Battery mode'}: ${s.name}`}
      confirm="Apply temporary mode" cancelLabel="Cancel" busy={busy} disabled={!valid} error={error} onConfirm={() => { if (mode && valid) void send(mode); }} onClose={() => setMode(null)}>
      <Stack spacing={2}>
        <Typography>This temporarily replaces the automatic mode for this inverter. After the timer, it returns to {status?.mode === 'automatic' ? 'the automatic plan' : 'the inverter default'}. Keep kWattson online.</Typography>
        {mode === 'force_charge' && <Typography>Grid power may be used. The current import rate is {f.cents(status?.tariff?.buy)}/kWh.</Typography>}
        {mode === 'force_discharge' && <Typography>The home and car reserve is protected: the cutoff cannot be below {f.pct(minimum)}.</Typography>}
        <TextField label="Duration" select value={minutes} onChange={e => setMinutes(Number(e.target.value))}>
          {[5, 10, 15, 20, 30].map(value => <MenuItem key={value} value={value}>{value} minutes</MenuItem>)}
        </TextField>
        {driven && <>
          <TextField label="Power (kW)" type="number" value={power} onChange={e => setPower(e.target.value)}
            error={!(Number(power) > 0 && Number(power) <= maximumPower)} helperText={`Maximum ${f.kw(maximumPower)}`}
            slotProps={{ htmlInput: { min: .1, max: maximumPower, step: .1 } }} />
          <TextField label={mode === 'force_charge' ? 'Charge target (%)' : 'Stop exporting at (%)'} type="number" value={target}
            onChange={e => setTarget(e.target.value)} error={!(Number(target) >= minimum && Number(target) <= s.battery.max_soc_pct)}
            helperText={`Allowed ${f.n(minimum, 1)}–${s.battery.max_soc_pct}%`} slotProps={{ htmlInput: { min: minimum, max: s.battery.max_soc_pct, step: .1 } }} />
        </>}
      </Stack>
    </ConfirmDialog>
  </Box>;
}
