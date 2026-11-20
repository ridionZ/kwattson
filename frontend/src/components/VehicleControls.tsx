import { useState } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import MenuItem from '@mui/material/MenuItem';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';
import { errorText, post } from '../lib/api';
import { useApp } from '../lib/app';
import { useNow } from '../lib/hooks';
import * as f from '../lib/fmt';
import { ConfirmDialog, useToast } from './common';

/** The same charging actions and confirmation states on Overview and vehicle settings. */
export default function VehicleControls({ vehicle: v, climate = false }: { vehicle: any; climate?: boolean }) {
  const { can, reloadStatus, status } = useApp();
  const now = useNow();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<'charge' | 'climate' | 'home' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [temperature, setTemperature] = useState(22);
  const [minutes, setMinutes] = useState(10);
  const fresh = v.reading && f.ageMinutes(v.reading.ts, now) <= 25;
  const reading = v.climate?.reading;
  const climateFresh = reading && f.ageMinutes(reading.ts, now) <= 2 && reading.time_verified;
  const climateLabel = ({ on: 'AC on', off: 'AC off', fan: 'Fan only', defrost: 'Defrost on' } as Record<string, string>)[reading?.mode] ?? 'Climate status unknown';

  async function send(action: 'start' | 'stop' | 'refresh' | 'climate-refresh' | 'climate-start' | 'climate-stop' | 'home') {
    setBusy(true);
    setError(null);
    try {
      const path = `/api/vehicles/${encodeURIComponent(v.id)}/`;
      if (action === 'refresh') await post(path + 'refresh');
      else if (action === 'home') await post(path + 'home-verification', { acknowledge_interruption: true });
      else if (action === 'climate-refresh') await post(path + 'climate/refresh');
      else if (action.startsWith('climate-')) await post(path + 'climate', { action: action === 'climate-start' ? 'start' : 'stop', temperature_c: temperature, minutes });
      else await post(path + 'charge', { action });
      setConfirm(null);
      toast(action.includes('refresh') ? 'Car reading refreshed.' : 'Request sent. Waiting for the car to confirm.');
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
      await reloadStatus();
    }
  }

  return <Box sx={{ mt: 1 }}>
    {v.pending_charge && <Typography variant="caption" component="p" role="status">Waiting for confirmation: {v.pending_charge.action}</Typography>}
    {v.native_timer && <Typography variant="caption" component="p" color={v.native_timer.state === 'verified' ? 'text.secondary' : 'warning.main'}>
      {v.native_timer.state === 'verified' ? `Car timer confirmed: ${f.time(v.native_timer.desired.start)}–${f.time(v.native_timer.desired.end)}`
        : v.native_timer.state === 'clearing' ? 'Waiting for the car timer to be disabled' : 'Car timer needs confirmation; check iSMART'}
    </Typography>}
    {v.native_timer?.state === 'verified' && <Typography variant="caption" component="p">This cutoff works offline. Loading later windows needs connectivity. The car timer can repeat daily if kWattson stays offline.</Typography>}
    {v.home_charging && <Typography variant="caption" component="p" color={v.home_charging.source === 'power_mismatch' ? 'warning.main' : 'text.secondary'}>
      {v.home_charging.source === 'plan_assumption' ? 'Home assumed from active charging plan' : v.home_charging.reason}
    </Typography>}
    {error && <Typography variant="caption" color="error" component="p" role="alert">{error}</Typography>}
    {can('member') && v.plugin !== 'manual' && <Stack direction="row" spacing={.5} useFlexGap sx={{ flexWrap: 'wrap' }}>
      <Button size="small" disabled={busy} onClick={() => void send('refresh')}>Refresh</Button>
      {v.can_schedule_charging && v.reading?.charging !== 1 && <Button size="small" disabled={busy || !!v.pending_charge || !fresh || v.reading?.plugged_in !== 1}
        onClick={() => { setError(null); setConfirm('charge'); }}>Start charging</Button>}
      {v.can_stop_charging && <Button size="small" color="error" disabled={busy || (v.pending_charge?.action === 'stop' && f.ageMinutes(v.pending_charge.requested_at, now) < 1.5)}
        onClick={() => void send('stop')}>Stop charging</Button>}
    </Stack>}
    {can('member') && v.can_stop_charging && <Typography variant="caption" component="p">Manual charging commands end this car's automatic charging plans.</Typography>}
    {climate && can('member') && v.can_schedule_charging && <Button size="small"
      disabled={busy || v.reading?.charging !== 1 || v.home_charging?.probing || v.home_charging?.tested_this_session}
      onClick={() => { setError(null); setConfirm('home'); }}>Verify home charging</Button>}
    {climate && v.climate_supported && <Box sx={{ mt: 2, pt: 1.5, borderTop: 1, borderColor: 'divider' }}>
      <Typography variant="subtitle2">Parked climate</Typography>
      <Typography variant="body2">{climateLabel}{reading?.parked === true ? ' · parked' : reading?.parked === false ? ' · car in use' : ''}
        {reading?.interior_c != null && ` · cabin ${f.n(reading.interior_c, 0)} °C`}</Typography>
      {reading?.ts && <Typography variant="caption" component="p">Read {f.relative(reading.ts, now)}</Typography>}
      {v.climate?.until && <Typography variant="caption" component="p">Stop scheduled for {f.when(v.climate.until, now)}</Typography>}
      {v.climate?.request && <Typography variant="caption" component="p" role="status">Waiting for climate {v.climate.request.action} confirmation</Typography>}
      {can('member') && <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap', alignItems: 'center', mt: 1 }}>
        <Button size="small" disabled={busy} onClick={() => void send('climate-refresh')}>Read parked status</Button>
        {v.can_control_climate && <>
          <TextField label="AC temperature" select size="small" value={temperature} onChange={e => setTemperature(Number(e.target.value))} sx={{ minWidth: 125 }}>
            {Array.from({ length: 12 }, (_, i) => i + 17).map(c => <MenuItem key={c} value={c}>{c} °C</MenuItem>)}
          </TextField>
          <TextField label="Run for" select size="small" value={minutes} onChange={e => setMinutes(Number(e.target.value))} sx={{ minWidth: 110 }}>
            {[5, 10, 15].map(m => <MenuItem key={m} value={m}>{m} min</MenuItem>)}
          </TextField>
          <Button size="small" disabled={busy || !!v.climate?.request || !climateFresh || reading.parked !== true}
            onClick={() => { setError(null); setConfirm('climate'); }}>Start AC</Button>
          <Button size="small" color="error" disabled={busy || (v.climate?.request?.action === 'stop' && f.ageMinutes(v.climate.request.requested_at, now) < 1.5)}
            onClick={() => void send('climate-stop')}>Stop AC</Button>
        </>}
      </Stack>}
      <Typography variant="caption" component="p" sx={{ mt: .75 }}>
        {!v.can_control_climate ? 'Enable parked climate control in Edit connection to use these controls.'
          : !climateFresh || reading.parked !== true ? 'Read parked status before starting AC. The car must confirm it is parked.'
          : 'Timed stopping needs kWattson and the car connection online. Climate use can reduce charging progress.'}
      </Typography>
    </Box>}
    <ConfirmDialog open={confirm !== null} title={confirm === 'home' ? `Verify ${v.name} home charging?` : confirm === 'climate' ? `Start AC in ${v.name}?` : `Start charging ${v.name}?`}
      confirm={confirm === 'home' ? 'Run one stop/restart test' : confirm === 'climate' ? 'Start AC' : 'Start charging'} cancelLabel="Cancel" busy={busy} error={error}
      onConfirm={() => void send(confirm === 'home' ? 'home' : confirm === 'climate' ? 'climate-start' : 'start')} onClose={() => setConfirm(null)}>
      <Typography>{confirm === 'home' ? 'This briefly stops and restarts charging while comparing whole-home power. It can interrupt charging away from home. A restart is attempted within three minutes; an unconfirmed restart raises an alert. The test will not repeat during this continuous connection.'
        : confirm === 'climate' ? `Set parked climate to ${temperature} °C for ${minutes} minutes. This uses the car battery and can reduce charging progress.`
        : `Charging starts now${status?.tariff?.buy != null ? `; the current grid rate is ${f.cents(status.tariff.buy)}/kWh` : ''}. This replaces automatic charging control for this car. Set a new charge plan if you want an automatic cutoff.`}</Typography>
    </ConfirmDialog>
  </Box>;
}
