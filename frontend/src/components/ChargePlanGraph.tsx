import Box from '@mui/material/Box';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';
import { useTheme } from '@mui/material/styles';
import * as f from '../lib/fmt';
import type { ChargeEstimate } from './ChargeDialog';

/** Small responsive timeline: every window, including the overnight pause and morning solar. */
export default function ChargePlanGraph({ estimate: e, target = 100, deadline }: { estimate: ChargeEstimate; target?: number; deadline?: string }) {
  const theme = useTheme();
  const points = e.projection ?? [];
  if (points.length < 2) return null;
  const first = Date.parse(points[0].ts);
  const last = Date.parse(deadline ?? e.deadline ?? points[points.length - 1].ts);
  if (last <= first) return null;
  const x = (ts: string) => Math.max(0, Math.min(1000, 1000 * (Date.parse(ts) - first) / (last - first)));
  const y = (soc: number) => 140 - Math.max(0, Math.min(100, soc)) * 1.4;
  const expected = points.map(p => `${x(p.ts)},${y(p.soc)}`).join(' ');
  const conservative = points.map(p => `${x(p.ts)},${y(p.conservative_soc)}`).join(' ');
  const showMargin = points.some(p => p.soc - p.conservative_soc > 1);
  const sources = e.source_windows?.length ? e.source_windows : (e.windows ?? []).map(w => ({ ...w, source: 'home' }));
  const color = (source: string) => source === 'solar' ? theme.palette.warning.main : source === 'battery' ? theme.palette.secondary.main : theme.palette.info.main;
  const names: Record<string, string> = { solar: 'Solar', battery: 'Home battery', grid: 'Grid', mixed: 'Solar + battery', home: 'Home energy' };
  return <Box component="figure" sx={{ m: 0, mt: 1.5 }} aria-label="Projected car battery level and all planned charging windows">
    <Typography component="figcaption" variant="caption" sx={{ fontWeight: 700, display: 'block', mb: .75 }}>Projected battery level · modelled</Typography>
    <Box sx={{ display: 'flex', gap: .75 }}>
      <Stack sx={{ width: 30, height: 140, flexShrink: 0, justifyContent: 'space-between' }}>
        {[100, 50, 0].map(pct => <Typography variant="caption" key={pct} sx={{ lineHeight: 1 }}>{pct}%</Typography>)}
      </Stack>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Box component="svg" viewBox="0 0 1000 140" preserveAspectRatio="none" role="img" aria-label={`Target ${target}% by ${f.when(deadline ?? points[points.length - 1].ts)}`}
          sx={{ display: 'block', width: '100%', height: 140, overflow: 'visible', bgcolor: 'action.hover', borderBottom: 1, borderColor: 'divider' }}>
          {sources.map((w, i) => <rect key={i} x={x(w.start)} y={0} width={Math.max(0, x(w.end) - x(w.start))} height={140} fill={color(w.source)} fillOpacity={.13}>
            <title>{names[w.source] ?? 'Home energy'}: {f.when(w.start)} to {f.when(w.end)}</title>
          </rect>)}
          <line x1={0} x2={1000} y1={y(target)} y2={y(target)} stroke={theme.palette.text.secondary} strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />
          {showMargin && <polyline points={conservative} fill="none" stroke={theme.palette.text.secondary} strokeWidth={1.5} strokeDasharray="3 4" vectorEffect="non-scaling-stroke" />}
          <polyline points={expected} fill="none" stroke={theme.palette.secondary.main} strokeWidth={2.5} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
        </Box>
        <Stack direction="row" sx={{ justifyContent: 'space-between', mt: .5 }}>
          <Typography variant="caption">Now</Typography>
          <Typography variant="caption">{f.time(new Date((first + last) / 2).toISOString())}</Typography>
          <Typography variant="caption">{f.time(new Date(last).toISOString())}</Typography>
        </Stack>
      </Box>
    </Box>
    <Typography variant="caption" component="p" sx={{ mt: .5 }}>
      Expected {f.pct(e.expected_end_soc ?? e.end_soc)}{e.expected_finish ? ` by ${f.when(e.expected_finish)}` : ''}
      {showMargin && ` · conservative ${f.pct(e.end_soc)}`} · target {f.pct(target)}
    </Typography>
    <Stack component="ul" spacing={.25} sx={{ pl: 2, my: .5 }}>
      {(e.windows ?? []).map(w => {
        const source = [...new Set(sources.filter(s => s.start < w.end && s.end > w.start).map(s => names[s.source] ?? 'Home energy'))].join(' + ');
        return <Typography component="li" variant="caption" key={w.start}>{f.when(w.start)}–{f.when(w.end)}{source ? ` · ${source}` : ''}</Typography>;
      })}
    </Stack>
    <Typography variant="caption" color="text.secondary">Shading marks charging windows. Flat sections are pauses. Actual charge rate and available home energy can change the result.</Typography>
  </Box>;
}
