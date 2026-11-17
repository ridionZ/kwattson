import Box from '@mui/material/Box';
import type { SxProps, Theme } from '@mui/material/styles';
import mark from '../mark.svg';

/** The kWattson mark followed by the wordmark as real text (Montserrat 700).
 *  `onDark`: Sunlight Amber + white (navy app bar); otherwise Deep Amber + Grid Navy,
 *  switching to the on-dark colours in dark mode. */
export function Wordmark({ size = 22, onDark = false, sx }: { size?: number; onDark?: boolean; sx?: SxProps<Theme> }) {
  const light = onDark ? { kw: '#F5A524', rest: '#FFFFFF' } : { kw: '#D97706', rest: '#0E2A47' };
  return (
    <Box component="span" sx={[{ display: 'inline-flex', alignItems: 'center', gap: `${Math.round(size * 0.4)}px` }, ...(Array.isArray(sx) ? sx : [sx])]}>
      <Box component="img" src={mark} alt="" sx={{ width: size * 1.45, height: size * 1.45, display: 'block', flexShrink: 0 }} />
      <Box component="span" aria-hidden sx={(t) => ({
        fontFamily: '"Montserrat", system-ui, sans-serif', fontWeight: 700, fontSize: size, lineHeight: 1, letterSpacing: '-0.01em', whiteSpace: 'nowrap',
        color: light.rest, '& b': { color: light.kw, fontWeight: 700 },
        ...(!onDark && t.applyStyles('dark', { color: '#FFFFFF', '& b': { color: '#F5A524' } })),
      })}>
        <b>kW</b>attson
      </Box>
    </Box>
  );
}
