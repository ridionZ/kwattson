import { useState, type ReactNode } from 'react';
import Box from '@mui/material/Box';
import Grid from '@mui/material/Grid';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import Chip from '@mui/material/Chip';
import Collapse from '@mui/material/Collapse';
import CircularProgress from '@mui/material/CircularProgress';
import LinearProgress from '@mui/material/LinearProgress';
import Link from '@mui/material/Link';
import { useColorScheme } from '@mui/material/styles';
import WbSunnyRounded from '@mui/icons-material/WbSunnyRounded';
import HomeRounded from '@mui/icons-material/HomeRounded';
import BatteryChargingFullRounded from '@mui/icons-material/BatteryChargingFullRounded';
import ElectricalServicesRounded from '@mui/icons-material/ElectricalServicesRounded';
import ElectricCarRounded from '@mui/icons-material/ElectricCarRounded';
import ExpandMoreRounded from '@mui/icons-material/ExpandMoreRounded';
import AcUnitRounded from '@mui/icons-material/AcUnitRounded';
import ContentCutRounded from '@mui/icons-material/ContentCutRounded';
import EvStationRounded from '@mui/icons-material/EvStationRounded';
import ArrowDownwardRounded from '@mui/icons-material/ArrowDownwardRounded';
import ArrowUpwardRounded from '@mui/icons-material/ArrowUpwardRounded';
import CheckCircleRounded from '@mui/icons-material/CheckCircleRounded';
import RadioButtonUncheckedRounded from '@mui/icons-material/RadioButtonUncheckedRounded';
import { get, post, errorText } from '../lib/api';
import { useLoad, useNow } from '../lib/hooks';
import { useApp } from '../lib/app';
import { action } from '../lib/actions';
import { tokens, ACTION_COLOR } from '../theme';
import * as f from '../lib/fmt';
import { ErrorState, Facts, HelpTip, LoadingBlock, Section, useToast } from '../components/common';
import ControlRetry from '../components/ControlRetry';
import QuickActions from '../components/QuickActions';
import Scheduled, { describe, type Holiday, type Override } from '../components/Scheduled';
import AlertsList, { type NotificationsDoc } from '../components/AlertsList';

export function useTokens() {
  const { mode, systemMode } = useColorScheme();
  const dark = (mode === 'system' ? systemMode : mode) === 'dark';
  return { t: dark ? tokens.dark : tokens.light, actions: dark ? ACTION_COLOR.dark : ACTION_COLOR.light, dark };
}

export default function Overview() {
  const { status: st, statusError, reloadStatus, can } = useApp();
  const now = useNow();
  // Everyone can see what's scheduled; only members and admins can cancel it.
  const sched = useLoad(() => Promise.all([get<Holiday[]>('/api/holidays'), get<Override[]>('/api/overrides')]), [Math.floor(now / 60000)]);
  const alerts = useLoad<NotificationsDoc>(() => get('/api/notifications?limit=10&unread=true'), [st?.unread]);
  const toast = useToast();

  if (!st) return statusError ? <ErrorState message={statusError} onRetry={reloadStatus} /> : <OverviewSkeleton />;

  const vehicles = (st.vehicles ?? []).filter((v: any) => v.enabled);
  const hasSystems = (st.systems ?? []).some((x: any) => x.enabled);
  const setupNeeded = !st.demo && (!hasSystems || (st.weather_enabled && !st.site_located) || st.using_example_tariff);
  const items = sched.data ? describe(sched.data[0], sched.data[1], vehicles, now) : [];
  const refresh = () => { void sched.reload(); void reloadStatus(); };

  async function dismissAlert(id?: number) {
    try {
      await post('/api/notifications/seen' + (id == null ? '' : `?notification_id=${id}`));
      await Promise.all([alerts.reload(), reloadStatus()]);
      if (id == null) toast('Overview alerts cleared.');
    } catch (e) {
      toast(errorText(e), 'error');
    }
  }

  return (
    <Box>
      <Typography variant="h1" sx={{ position: 'absolute', width: '1px', height: '1px', overflow: 'hidden', clipPath: 'inset(50%)', whiteSpace: 'nowrap' }}>Overview</Typography>
      <Banners st={st} now={now} />
      <Grid container spacing={2.5}>
        <Grid size={{ xs: 12, md: 8 }} sx={{ display: 'grid', gap: 2.5, alignContent: 'start' }}>
          {setupNeeded && (can('admin') || !hasSystems) && <GetStarted st={st} hasSystems={hasSystems} />}
          {hasSystems && <RightNow st={st} now={now} />}
          {hasSystems && <LivePower st={st} now={now} />}
          {hasSystems && <Section title="Weather & solar outlook">
            <Facts rows={[
              ['This hour’s temperature', st.weather?.temperature != null ? f.n(st.weather.temperature, 1) + ' °C' : 'Unavailable'],
              ['Cloud cover', f.pct(st.weather?.cloud_cover)],
              ['Estimated solar · next hour', f.kwh(st.solar_estimate?.next_hour)],
              ['Estimated solar · until midnight', f.kwh(st.solar_estimate?.rest_today)],
            ]} />
            <Typography component="p" variant="caption" color="text.secondary" sx={{ mt: 1 }}>
              {st.weather && (st.demo ? 'Simulated weather' : 'Weather by Open-Meteo') + ' · updated ' + f.relative(st.weather.fetched_at) + '. '}
              {st.solar_estimate?.created_at && 'Solar forecast made ' + f.relative(st.solar_estimate.created_at) + '.'}
            </Typography>
            {can('admin') && !st.weather && <Button href="#/settings/home/location">Configure weather</Button>}
          </Section>}
          {hasSystems && <ElectricityCost periods={st.money ?? {}} today={st.today} projected={st.money?.projected_today} />}
          {hasSystems && <Advice st={st} />}
        </Grid>
        <Grid size={{ xs: 12, md: 4 }} sx={{ display: 'grid', gap: 2.5, alignContent: 'start' }}>
          {can('member') && hasSystems && (
            <Section title="Quick actions">
              <QuickActions vehicles={vehicles} onDone={refresh} />
            </Section>
          )}
          {hasSystems && <Section title="Scheduled & active">
            {sched.error ? <ErrorState message={sched.error} onRetry={sched.reload} /> : !sched.data ? <LoadingBlock /> :
              <Scheduled items={items} canEdit={can('member')} onChanged={refresh} />}
          </Section>}
          <Section title="Alerts" titleId="overview-alerts"
            action={can('member') && alerts.data?.notifications.some((n) => !n.seen) && <Button size="small" onClick={() => void dismissAlert()}>Clear all</Button>}>
            {alerts.error ? <ErrorState message={alerts.error} onRetry={alerts.reload} /> : !alerts.data ? <LoadingBlock /> : <AlertsList doc={alerts.data} onDismiss={can('member') ? dismissAlert : undefined} />}
            {can('admin') && <Button size="small" href="#/settings/alerts" sx={{ mt: 1.5 }}>Alert settings</Button>}
          </Section>
        </Grid>
      </Grid>
      <Typography variant="caption" color="text.secondary" component="p" sx={{ mt: 3 }}>
        Updated {f.time(st.server_time)} ({f.getZone().replace('_', ' ')} time). Checks for new readings every 30 seconds.
      </Typography>
    </Box>
  );
}

function OverviewSkeleton() {
  return (
    <Grid container spacing={2.5}>
      <Grid size={{ xs: 12, md: 8 }} sx={{ display: 'grid', gap: 2.5 }}>
        <LoadingBlock height={150} /><LoadingBlock height={220} /><LoadingBlock height={120} />
      </Grid>
      <Grid size={{ xs: 12, md: 4 }} sx={{ display: 'grid', gap: 2.5 }}>
        <LoadingBlock height={200} /><LoadingBlock height={160} />
      </Grid>
    </Grid>
  );
}

/* ----------------------------------------------------------------- banners */
function Banners({ st, now }: { st: any; now: number }) {
  const { can } = useApp();
  const out: ReactNode[] = [];
  for (const p of st.pending_tariffs ?? []) {
    out.push(
      <Alert key={`t${p.id}`} severity="warning" action={can('admin') && <Button color="inherit" size="small" href="#/settings/home">Activate when confirmed</Button>}>
        {p.name}{p.retailer ? ` (${p.retailer})` : ''} isn’t active yet. Planning still uses your current tariff.
      </Alert>);
  }
  const site = st.site;
  const hasSystems = (st.systems ?? []).some((x: any) => x.enabled);
  if (!site) {
    if (hasSystems) out.push(<Alert key="nosite" severity="error">No readings yet. Check Settings › Solar &amp; batteries and run Test connection.</Alert>);
  } else if (f.ageMinutes(site.ts, now) > 15) out.push(<Alert key="stale" severity="error">Latest readings are from {f.relative(site.ts, now)}. Figures below may be out of date.</Alert>);
  if (site?.fault) out.push(<Alert key="fault" severity="error">A device reports a fault. Check the inverter’s own app.</Alert>);
  // No heartbeat yet just means the worker hasn't finished its first cycle (e.g. right after setup).
  if (st.heartbeat && f.ageMinutes(st.heartbeat, now) > 10) out.push(<Alert key="beat" severity="error">The background worker has been silent since {f.relative(st.heartbeat, now)}. Plans and readings aren’t updating.</Alert>);
  for (const s of st.systems ?? []) {
    if (s.enabled && s.poll && !s.poll.ok)
      out.push(<Alert key={`p${s.id}`} severity="warning">Can’t read {s.name} since {f.relative(s.poll.since, now)}{s.poll.error ? `: ${s.poll.error}` : ''}.</Alert>);
  }
  const recovery = st.control_retry;
  if ((st.mode === 'automatic' && ((st.guards ?? []).length || recovery)) || recovery?.phase === 'release') {
    const reasons = [...new Set<string>([...(st.mode === 'automatic' ? st.guards ?? [] : []), ...(recovery ? [recovery.reason] : [])])];
    out.push(<Alert key="guards" severity={recovery?.phase === 'release' ? 'error' : 'warning'}>
      <strong>{recovery?.phase === 'release' ? 'Inverter release unconfirmed.' : 'Automatic control is paused.'}</strong>
      {' '}{reasons.join(' · ')}
      {recovery && <ControlRetry recovery={recovery} />}
    </Alert>);
  }
  if (st.demo) out.push(<Alert key="demo" severity="info" variant="outlined"><strong>Demo house.</strong> The readings and three weeks of history are simulated. <Link href="#/plan">Explore the battery plan</Link> or <Link href="#/history">see what each day cost</Link>.</Alert>);
  if (!out.length) return null;
  return <Stack spacing={1.25} sx={{ mb: 2.5 }}>{out}</Stack>;
}

/* --------------------------------------------------------------- right now */
function RightNow({ st, now }: { st: any; now: number }) {
  const [why, setWhy] = useState(false);
  const { actions, dark } = useTokens();
  const d = st.decision;
  const s = d?.summary;
  const a = action(s?.action);
  const color = (actions as Record<string, string>)[s?.action] ?? actions.self_use;
  const t = st.tariff;
  return (
    <Card component="section" aria-labelledby="now-title" sx={{
      p: { xs: 2, sm: 2.5 }, border: 0,
      bgcolor: dark ? '#14263F' : '#E8EFF8',
    }}>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={{ xs: 2, sm: 3 }} sx={{ alignItems: { sm: 'center' } }}>
        <Stack direction="row" spacing={2} sx={{ flex: 1, minWidth: 0, alignItems: 'flex-start' }}>
          <Box aria-hidden sx={{ width: 56, height: 56, borderRadius: 4, display: 'grid', placeItems: 'center', flexShrink: 0, bgcolor: color, color: s?.action === 'export_priority' || (dark && s?.action !== 'self_use') ? '#0E2A47' : '#fff' }}>
            <a.Icon sx={{ fontSize: 30 }} />
          </Box>
          <Box sx={{ minWidth: 0 }}>
            <Typography id="now-title" variant="overline" component="h2" color="text.secondary" sx={{ display: 'block', lineHeight: 1.6 }}>Right now</Typography>
            <Typography variant="h5" component="p" sx={{ fontSize: { xs: '1.25rem', sm: '1.5rem' } }}>{d ? a.label : 'No plan yet'}</Typography>
            <Typography variant="body2" sx={{ mt: 0.5, color: 'text.secondary' }}>
              {d ? s.reason : 'The planner runs every few minutes once readings arrive.'}
            </Typography>
            {d && (
              <Button size="small" onClick={() => setWhy(!why)} aria-expanded={why} aria-controls="why-panel"
                endIcon={<ExpandMoreRounded sx={{ transform: why ? 'rotate(180deg)' : 'none', transition: 'transform .2s' }} />}
                sx={{ mt: 0.5, ml: -1 }}>
                Why?
              </Button>
            )}
          </Box>
        </Stack>
        {t && (
          <Box sx={{ display: 'grid', gridTemplateColumns: 'auto auto', columnGap: 3, rowGap: 0.25, flexShrink: 0, pl: { sm: 3 }, borderLeft: { sm: 1 }, borderColor: { sm: 'divider' }, pt: { xs: 1.5, sm: 0 }, borderTop: { xs: 1, sm: 0 } }}>
            <Typography variant="caption" color="text.secondary" sx={{ gridColumn: '1 / -1' }}>
              <Chip size="small" label={t.label} sx={{ height: 22, mr: 0.75 }} />
              price now
            </Typography>
            <Box>
              <Typography variant="h5" component="p">{f.n(t.buy * 100)}<Box component="span" sx={{ fontSize: '0.8rem', fontWeight: 600, ml: 0.25 }}>c</Box></Typography>
              <Typography variant="caption" color="text.secondary">Buy, per kWh</Typography>
            </Box>
            <Box>
              <Typography variant="h5" component="p">{f.n(t.sell * 100)}<Box component="span" sx={{ fontSize: '0.8rem', fontWeight: 600, ml: 0.25 }}>c</Box></Typography>
              <Typography variant="caption" color="text.secondary">Feed-in, per kWh</Typography>
            </Box>
          </Box>
        )}
      </Stack>
      {(st.tariff_today ?? []).length > 0 && <PriceDay periods={st.tariff_today} now={now} />}
      {d && (
        <Collapse in={why} id="why-panel">
          <Box sx={{ mt: 2, p: 2, borderRadius: 3, bgcolor: 'background.paper' }}>
            <Typography variant="body2" sx={{ mb: 1.5 }}>{a.help}</Typography>
            <Facts rows={[
              ['Plan made', `${f.time(d.created_at)} (${f.relative(d.created_at, now)})`],
              ['Saving vs self-use', <>{f.money(s.modelled_saving_24h_aud)} over 24 h <Typography component="span" variant="caption" color="text.secondary">(modelled)</Typography></>],
              ['Forecast confidence', s.confidence === 'established' ? 'Established' : s.confidence ?? '–'],
              t?.allowances?.length ? ['Allowances', t.allowances.map((x: any) => `${x.kwh_per_day} kWh/day ${x.kind}, then ${f.cents(x.rate_after)}`).join('; ')] : null,
              (s.overrides ?? []).length ? ['Your overrides', s.overrides.map((o: any) => (typeof o === 'string' ? o : o.kind)).join(', ')] : null,
            ]} />
            <Link href="#/plan" variant="body2" sx={{ display: 'inline-block', mt: 1.5, fontWeight: 600 }}>See the 48-hour plan</Link>
          </Box>
        </Collapse>
      )}
    </Card>
  );
}

/* -------------------------------------------------------------- live power */
function Tile({ icon, tone, label, value, sub, children }: { icon: ReactNode; tone: { fg: string; bg: string }; label: string; value: ReactNode; sub?: ReactNode; children?: ReactNode }) {
  return (
    <Box sx={{ p: 1.75, borderRadius: 3, border: 1, borderColor: 'divider', bgcolor: 'background.paper', display: 'flex', flexDirection: 'column', gap: 0.5, minWidth: 0 }}>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
        <Box aria-hidden sx={{ width: 30, height: 30, borderRadius: '50%', display: 'grid', placeItems: 'center', bgcolor: tone.bg, color: tone.fg, '& svg': { fontSize: 18 } }}>{icon}</Box>
        <Typography variant="subtitle2" component="h3" color="text.secondary">{label}</Typography>
      </Stack>
      <Typography variant="h4" component="p" sx={{ fontSize: { xs: '1.5rem', sm: '1.75rem' }, mt: 0.5 }}>{value}</Typography>
      {children}
      {sub && <Typography variant="caption" color="text.secondary" component="div">{sub}</Typography>}
    </Box>
  );
}

function BatteryRing({ soc, color, track, reserve }: { soc: number | null; color: string; track: string; reserve?: number | null }) {
  return (
    <Box sx={{ position: 'relative', width: 96, height: 96, flexShrink: 0 }}>
      {reserve != null && (
        <Box aria-hidden sx={{ position: 'absolute', inset: 0, transform: `rotate(${reserve * 3.6}deg)`, zIndex: 1, pointerEvents: 'none' }}>
          <Box sx={{ position: 'absolute', left: 'calc(50% - 1.5px)', top: -3, width: 3, height: 13, borderRadius: 1, bgcolor: 'text.primary' }} />
        </Box>
      )}
      <CircularProgress variant="determinate" value={100} size={96} thickness={5} sx={{ color: track, position: 'absolute' }} aria-hidden />
      <CircularProgress variant="determinate" value={soc ?? 0} size={96} thickness={5} sx={{ color, position: 'absolute', '& circle': { strokeLinecap: 'round' } }}
        aria-label="Battery level" />
      <Box sx={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center' }}>
        <Typography variant="h5" component="span">{soc == null ? '–' : f.pct(soc)}</Typography>
      </Box>
    </Box>
  );
}

/** Shown until a system is added: what to set up, in order, with a link to each place. */
function GetStarted({ st, hasSystems }: { st: any; hasSystems: boolean }) {
  const { can } = useApp();
  if (!can('admin')) {
    return (
      <Section title="Not set up yet">
        <Typography>kWattson hasn’t been connected to a solar system yet. Ask an admin to finish setting it up.</Typography>
      </Section>
    );
  }
  const steps = [
    { done: !!st.site_located || !st.weather_enabled, title: 'Set your location and time zone', text: 'Use approximate coordinates for the local weather forecast.', href: '#/settings/home/location', button: 'Set location' },
    { done: !st.using_example_tariff, title: 'Set your electricity prices', text: 'Use the rates from your bill. The starting plan uses example prices.', href: '#/settings/home/tariffs', button: 'Set prices' },
    { done: hasSystems, title: 'Connect your inverter', text: 'Have the inverter rating and battery limits handy. You can test the connection before saving.', href: '#/settings/systems/add', button: 'Add a system' },
  ];
  const completed = steps.filter((step) => step.done).length;
  return (
    <Section title="Let’s get you set up">
      <Typography color="text.secondary" sx={{ mb: 2 }}>Monitoring setup · {completed} of {steps.length} complete. You can leave and come back at any time.</Typography>
      <Stack component="ol" spacing={1.5} sx={{ m: 0, p: 0, listStyle: 'none' }}>
        {steps.map((step) => (
          <Box component="li" key={step.title} sx={{ display: 'grid', gridTemplateColumns: { xs: 'auto 1fr', sm: 'auto 1fr auto' }, columnGap: 1.5, rowGap: 0.75, alignItems: 'start' }}>
            {step.done
              ? <CheckCircleRounded sx={{ color: 'success.main', mt: 0.25 }} aria-label="Done" />
              : <RadioButtonUncheckedRounded sx={{ color: 'text.disabled', mt: 0.25 }} aria-label="Not done yet" />}
            <Box sx={{ flex: 1, minWidth: 0 }}>
              <Typography sx={{ fontWeight: 600 }}>{step.title}</Typography>
              <Typography variant="body2" color="text.secondary">{step.text}</Typography>
            </Box>
            {!step.done && <Button variant="outlined" size="small" href={step.href} sx={{ gridColumn: { xs: 2, sm: 3 }, justifySelf: 'start' }}>{step.button}</Button>}
          </Box>
        ))}
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>Optional · <Link href="#/settings/vehicles/add">Add an EV</Link> or <Link href="#/settings/alerts">choose alerts</Link>.</Typography>
    </Section>
  );
}

function LivePower({ st, now }: { st: any; now: number }) {
  const { t } = useTokens();
  const s = st.site ?? {};
  const charging = (s.charge_kw ?? 0) > 0.05;
  const discharging = (s.discharge_kw ?? 0) > 0.05;
  const importing = (s.import_kw ?? 0) > 0.05;
  const exporting = (s.export_kw ?? 0) > 0.05;
  const solarTone = { fg: t.solarText, bg: t.solarContainer };
  const batTone = { fg: t.batteryText, bg: t.batteryContainer };
  const neutral = { fg: 'text.secondary', bg: 'action.hover' };
  const vehicles = (st.vehicles ?? []).filter((v: any) => v.enabled);
  const nextBlock = (st.advice?.schedule ?? [])[1];
  return (
    <Section title="Live power" action={s.ts && <Typography variant="caption" color="text.secondary">{f.relative(s.ts, now)}</Typography>}>
      <Box sx={{ display: 'grid', gap: 1.5, gridTemplateColumns: { xs: '1fr 1fr', sm: 'repeat(4, 1fr)' } }}>
        <Box sx={{ gridColumn: { xs: '1 / -1', sm: 'span 2' }, gridRow: { sm: 'span 2' }, p: 2, borderRadius: 3, bgcolor: t.batteryContainer, display: 'flex', gap: 2, alignItems: 'center' }}>
          <BatteryRing soc={s.soc ?? null} color={t.battery} track={t.battery + '33'} reserve={st.battery_reserve_pct} />
          <Box sx={{ minWidth: 0 }}>
            <Stack direction="row" spacing={0.75} sx={{ alignItems: 'center', color: t.batteryText }}>
              <BatteryChargingFullRounded fontSize="small" />
              <Typography variant="subtitle2" component="h3">Battery</Typography>
            </Stack>
            <Typography variant="h5" component="p" sx={{ mt: 0.5, color: 'text.primary', fontSize: '1.25rem' }}>
              {charging ? `Charging ${f.kw(s.charge_kw)}` : discharging ? `Discharging ${f.kw(s.discharge_kw)}` : 'Idle'}
            </Typography>
            <Typography variant="body2" color="text.secondary">
              {s.stored_kwh != null ? `${f.kwh(s.stored_kwh)} stored of ${f.kwh(s.capacity_kwh, 0)}` : 'Level unknown (treated as at reserve)'}
            </Typography>
            {st.battery_reserve_pct != null && (
              <Typography variant="caption" color="text.secondary" component="p" sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                <Box component="span" aria-hidden sx={{ width: 3, height: 11, borderRadius: 1, bgcolor: 'text.primary', display: 'inline-block' }} />
                Reserve {f.pct(st.battery_reserve_pct)}: the plan never goes below this
              </Typography>
            )}
            {nextBlock && (
              <Typography variant="body2" sx={{ mt: 1.5, pt: 1.5, borderTop: 1, borderColor: t.battery + '55' }}>
                Next: <strong>{action(nextBlock.action).label}</strong> at {f.when(nextBlock.start, now)}, ending near {f.pct(nextBlock.soc_end)}
              </Typography>
            )}
          </Box>
        </Box>
        <Tile icon={<WbSunnyRounded />} tone={solarTone} label="Solar" value={f.kw(s.solar_kw)}
          sub={s.output_limited ? 'Limited by the inverter' : (s.solar_kw ?? 0) < 0.05 ? 'No sun right now' : 'Generating'} />
        <Tile icon={<HomeRounded />} tone={neutral} label="Home" value={f.kw(s.load_kw)}
          sub={(s.ev_kw ?? 0) > 0.05 ? `incl. car ${f.kw(s.ev_kw)}` : 'Using now'} />
        <Tile icon={<ElectricalServicesRounded />} tone={neutral} label="Grid"
          value={importing ? f.kw(s.import_kw) : exporting ? f.kw(s.export_kw) : '0 kW'}
          sub={<Stack direction="row" spacing={0.5} sx={{ alignItems: 'center' }}>
            {importing ? <ArrowDownwardRounded sx={{ fontSize: 14 }} /> : exporting ? <ArrowUpwardRounded sx={{ fontSize: 14, color: t.gridOut }} /> : null}
            <span>{importing ? `Importing at ${f.cents(st.tariff?.buy)}/kWh` : exporting ? `Exporting at ${f.cents(st.tariff?.sell)}/kWh` : 'Not importing or exporting'}</span>
          </Stack>} />
        {vehicles.map((v: any) => {
          const r = v.reading;
          const soc = r?.soc ?? v.manual_soc;
          const state = !r ? 'No live data' : r.at_home === 0 ? 'Away' : r.charging ? `Charging${r.charge_kw ? ` ${f.kw(r.charge_kw)}` : ''}` : r.plugged_in ? 'Plugged in' : 'Not plugged in';
          return (
            <Tile key={v.id} icon={<ElectricCarRounded />} tone={batTone} label={v.name} value={soc != null ? f.pct(soc) : '–'}
              sub={`${state} · target ${f.pct(v.target_pct)}`}>
              {soc != null && <LinearProgress variant="determinate" value={soc} aria-label={`${v.name} battery level`}
                sx={{ bgcolor: t.battery + '33', '& .MuiLinearProgress-bar': { bgcolor: t.battery } }} />}
            </Tile>
          );
        })}
      </Box>
    </Section>
  );
}

/* ------------------------------------------------------------------- today */
function Figure({ label, value, sub, positive }: { label: ReactNode; value: string; sub?: ReactNode; positive?: boolean }) {
  const { t } = useTokens();
  return (
    <Box sx={{ minWidth: 0 }}>
      <Typography variant="caption" color="text.secondary" component="h3" sx={{ fontWeight: 600, display: 'flex', alignItems: 'center' }}>{label}</Typography>
      <Typography variant="h5" component="p" sx={{ mt: 0.25, color: positive ? t.batteryText : 'text.primary' }}>{value}</Typography>
      {sub && <Typography variant="caption" color="text.secondary" component="p">{sub}</Typography>}
    </Box>
  );
}

function ElectricityCost({ periods, today, projected }: { periods: Record<string, { net_aud: number; coverage: number } | null>; today?: any; projected?: number | null }) {
  return (
    <Section title="Electricity cost" help={<HelpTip label="Electricity cost">
      Estimates use recorded imports and exports and include daily supply charges for days with readings. Weeks start on Monday in your site time zone.
    </HelpTip>}>
      <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: 'repeat(2, minmax(0, 1fr))', lg: 'repeat(4, minmax(0, 1fr))' } }}>
        {[['today', 'Today so far'], ['yesterday', 'Yesterday'], ['this_week', 'This week so far'], ['last_week', 'Last week']].map(([key, label]) => {
          const period = periods[key];
          return <Figure key={key} label={label} value={f.netCost(period?.net_aud)} positive={(period?.net_aud ?? 0) < -0.005}
            sub={period ? (period.coverage < 0.99 ? `${f.pct(period.coverage * 100)} of period measured` : undefined) : 'No readings'} />;
        })}
      </Box>
      <Box sx={{ mt: 2, pt: 1.5, borderTop: 1, borderColor: 'divider' }}>
        <Figure label="Today’s projected total" value={f.netCost(projected)} positive={(projected ?? 0) < -0.005}
          sub={projected == null ? 'Waiting for readings and a plan through midnight' : 'Readings so far plus the plan through midnight, including supply'} />
      </Box>
      {today && <Stack spacing={0.5} sx={{ mt: 2, pt: 1.5, borderTop: 1, borderColor: 'divider' }}>
        <Facts rows={[
          ['Today vs plain self-use', f.better(today.saving_vs_self_use_aud)],
          ['Today vs no solar', f.better(today.no_solar_net_aud - today.net_aud)],
        ]} />
        <Typography variant="caption" color="text.secondary">
          Solar {f.kwh(today.solar_kwh)} · used {f.kwh(today.load_kwh)} · imported {f.kwh(today.import_kwh)} · exported {f.kwh(today.export_kwh)}
        </Typography>
      </Stack>}
    </Section>
  );
}

/* ------------------------------------------------------------------ advice */
function Advice({ st }: { st: any }) {
  const { t } = useTokens();
  const a = st.advice ?? {};
  const cards: ReactNode[] = [];
  for (const v of a.vehicles ?? []) {
    if (!v.recommendation) continue;
    const r = v.recommendation;
    cards.push(
      <AdviceCard key={`ev${v.vehicle}`} icon={<EvStationRounded />} tone={{ fg: t.batteryText, bg: t.batteryContainer }} title={`Best time to charge ${v.name}`}>
        {r.text} About {f.money(r.cost_aud)} for {f.kwh(r.energy_kwh, 0)}.
      </AdviceCard>);
  }
  const c = a.climate;
  if (c?.available) {
    cards.push(
      <AdviceCard key="ac" icon={<AcUnitRounded />} tone={{ fg: t.solarText, bg: t.solarContainer }} title={`Good time to ${c.mode === 'heat' ? 'heat' : 'cool'} the house`}>
        {c.reason}{c.estimated_cost_aud != null ? ` About ${f.money(c.estimated_cost_aud)} for ${c.duration_minutes} min.` : ''}
      </AdviceCard>);
  }
  const cl = a.clipping;
  if (cl && (cl.planned_kwh > 0.05 || cl.self_use_kwh > 0.05)) {
    cards.push(
      <AdviceCard key="clip" icon={<ContentCutRounded />} tone={{ fg: t.solarText, bg: t.solarContainer }} title="Solar clipping today">
        About {f.kwh(cl.planned_kwh)} lost with the plan versus {f.kwh(cl.self_use_kwh)} on plain self-use (peak {f.kw(cl.peak_solar_kw)}). The plan keeps battery room before peak sun.
      </AdviceCard>);
  }
  if (!cards.length) return null;
  return (
    <Section title="Advice">
      <Stack spacing={1.25}>{cards}</Stack>
    </Section>
  );
}

function AdviceCard({ icon, tone, title, children }: { icon: ReactNode; tone: { fg: string; bg: string }; title: string; children: ReactNode }) {
  return (
    <Stack direction="row" spacing={1.5} sx={{ p: 1.5, borderRadius: 3, border: 1, borderColor: 'divider' }}>
      <Box aria-hidden sx={{ width: 36, height: 36, borderRadius: 2, flexShrink: 0, display: 'grid', placeItems: 'center', bgcolor: tone.bg, color: tone.fg }}>{icon}</Box>
      <Box>
        <Typography variant="subtitle2" component="h3">{title}</Typography>
        <Typography variant="body2" color="text.secondary">{children}</Typography>
      </Box>
    </Stack>
  );
}

/* ------------------------------------------------------------ today's prices */
const mins = (hhmm: string) => { const [h, m] = hhmm.split(':').map(Number); return h * 60 + m; };

function PriceDay({ periods, now }: { periods: { label: string; start: string; end: string; buy: number; sell: number }[]; now: number }) {
  const { t, dark } = useTokens();
  const p = f.parts(now);
  const nowMin = Number(p.hour) * 60 + Number(p.minute);
  const max = Math.max(...periods.map((x) => x.buy));
  const tone = (buy: number) => buy <= 0.05 ? { bg: t.battery, fg: '#042F2E' } : buy >= max - 0.001 && max > 0.3 ? { bg: t.solar, fg: '#1F1300' } : { bg: dark ? '#334155' : '#CBD5E1', fg: dark ? '#E2E8F0' : '#0F172A' };
  const label = (m: number) => (m === 0 || m === 1440 ? '12 am' : m === 720 ? '12 pm' : `${(m / 60) % 12} ${m < 720 ? 'am' : 'pm'}`);
  return (
    <Box sx={{ mt: 2.5 }}>
      <Typography variant="caption" color="text.secondary" component="h3" sx={{ fontWeight: 600, mb: 0.75 }}>Today’s buy prices</Typography>
      <Box sx={{ position: 'relative' }}>
        <Box sx={{ display: 'flex', height: 28, borderRadius: 1.5, overflow: 'hidden', gap: '2px' }} aria-hidden>
          {periods.map((x) => {
            const w = mins(x.end) - mins(x.start);
            const c = tone(x.buy);
            return (
              <Box key={x.start} title={`${x.label} ${x.start}–${x.end}: ${f.cents(x.buy)}/kWh buy, ${f.cents(x.sell)}/kWh feed-in`}
                sx={{ flexGrow: w, flexBasis: 0, minWidth: 0, bgcolor: c.bg, color: c.fg, display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: '0.6875rem', fontWeight: 700, whiteSpace: 'nowrap', overflow: 'hidden', opacity: mins(x.end) <= nowMin ? 0.55 : 1 }}>
                {w >= 150 ? `${f.n(x.buy * 100, 0)}c` : ''}
              </Box>
            );
          })}
        </Box>
        <Box aria-hidden sx={{ position: 'absolute', top: -4, bottom: -4, width: 2, borderRadius: 1, bgcolor: 'text.primary', left: `${(nowMin / 1440) * 100}%` }} />
      </Box>
      <Box aria-hidden sx={{ display: 'flex', justifyContent: 'space-between', mt: 0.5 }}>
        {[0, 360, 720, 1080, 1440].map((m) => <Typography key={m} variant="caption" color="text.secondary" sx={{ fontSize: '0.6875rem' }}>{label(m)}</Typography>)}
      </Box>
      <Box component="ul" sx={{ position: 'absolute', width: '1px', height: '1px', overflow: 'hidden', clipPath: 'inset(50%)', whiteSpace: 'nowrap' }}>
        {periods.map((x) => <li key={x.start}>{x.label}, {x.start} to {x.end}: buy {f.cents(x.buy)} per kWh, feed-in {f.cents(x.sell)} per kWh</li>)}
      </Box>
    </Box>
  );
}
