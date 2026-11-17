import { createTheme, alpha } from '@mui/material/styles';
import type { PaletteColor, PaletteColorOptions } from '@mui/material/styles';

// kWattson brand tokens, used with meaning (UX spec principle 6):
// Amber = solar, Teal = battery/charging/positive, Navy = structure/primary, Slate = grid.
export const tokens = {
  light: {
    navy: '#0E2A47',
    solar: '#F5A524', // fills, lines
    solarText: '#B45309',
    solarContainer: '#FEF3C7',
    battery: '#14B8A6',
    batteryText: '#0F766E',
    batteryContainer: '#CCFBF1',
    home: '#0E2A47',
    gridIn: '#64748B',
    gridOut: '#2563EB',
    baseline: '#94A3B8',
    buy: '#0E2A47',
    sell: '#2563EB',
    ev: '#7C8DB5',
    bg: '#F8FAFC',
    surface: '#FFFFFF',
    surface2: '#F1F5F9',
    line: '#E2E8F0',
  },
  dark: {
    navy: '#0E2A47',
    solar: '#F5A524',
    solarText: '#FBBF24',
    solarContainer: '#3A2A0A',
    battery: '#2DD4BF',
    batteryText: '#5EEAD4',
    batteryContainer: '#0B3B37',
    home: '#C7D7EC',
    gridIn: '#94A3B8',
    gridOut: '#60A5FA',
    baseline: '#64748B',
    buy: '#C7D7EC',
    sell: '#60A5FA',
    ev: '#A5B4FC',
    bg: '#0B1220',
    surface: '#111A2B',
    surface2: '#17233A',
    line: '#24324A',
  },
};
export type Tokens = typeof tokens.light;

// Planner actions: icon + short label + colour. Never colour alone.
export const ACTION_COLOR = {
  light: { self_use: '#94A3B8', force_charge: '#0F766E', force_discharge: '#2563EB', export_priority: '#F5A524', hold: '#0E2A47' },
  dark: { self_use: '#64748B', force_charge: '#2DD4BF', force_discharge: '#60A5FA', export_priority: '#F5A524', hold: '#C7D7EC' },
} as const;

declare module '@mui/material/styles' {
  interface Palette {
    solar: PaletteColor;
    battery: PaletteColor;
  }
  interface PaletteOptions {
    solar?: PaletteColorOptions;
    battery?: PaletteColorOptions;
  }
}
declare module '@mui/material/Chip' {
  interface ChipPropsColorOverrides {
    solar: true;
    battery: true;
  }
}
declare module '@mui/material/Button' {
  interface ButtonPropsColorOverrides {
    battery: true;
  }
}

const heading = '"Montserrat", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
const body = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';

export const theme = createTheme({
  cssVariables: { colorSchemeSelector: 'data-mui-color-scheme' },
  colorSchemes: {
    light: {
      palette: {
        primary: { main: '#0E2A47', light: '#D6E3F3', dark: '#081A2D', contrastText: '#FFFFFF' },
        secondary: { main: '#0F766E', light: '#CCFBF1', dark: '#134E4A', contrastText: '#FFFFFF' },
        battery: { main: '#0F766E', light: '#CCFBF1', dark: '#134E4A', contrastText: '#FFFFFF' },
        solar: { main: '#B45309', light: '#FEF3C7', dark: '#92400E', contrastText: '#FFFFFF' },
        error: { main: '#B91C1C' },
        warning: { main: '#B45309' },
        success: { main: '#0F766E' },
        info: { main: '#1D4ED8' },
        background: { default: tokens.light.bg, paper: tokens.light.surface },
        text: { primary: '#0F172A', secondary: '#475569' },
        divider: tokens.light.line,
      },
    },
    dark: {
      palette: {
        primary: { main: '#A9C7EC', light: '#1C3A5E', dark: '#D6E3F3', contrastText: '#0E2A47' },
        secondary: { main: '#2DD4BF', light: '#0B3B37', dark: '#5EEAD4', contrastText: '#042F2E' },
        battery: { main: '#2DD4BF', light: '#0B3B37', dark: '#5EEAD4', contrastText: '#042F2E' },
        solar: { main: '#F5A524', light: '#3A2A0A', dark: '#FBBF24', contrastText: '#1F1300' },
        error: { main: '#F87171' },
        warning: { main: '#FBBF24' },
        success: { main: '#2DD4BF' },
        info: { main: '#93C5FD' },
        background: { default: tokens.dark.bg, paper: tokens.dark.surface },
        text: { primary: '#E2E8F0', secondary: '#CBD5E1' },
        divider: tokens.dark.line,
      },
    },
  },
  shape: { borderRadius: 12 },
  typography: {
    fontFamily: body,
    h1: { fontFamily: heading, fontWeight: 700, fontSize: '1.75rem', lineHeight: 1.25, letterSpacing: '-0.01em' },
    h2: { fontFamily: heading, fontWeight: 600, fontSize: '1.125rem', lineHeight: 1.35 },
    h3: { fontFamily: heading, fontWeight: 600, fontSize: '1rem', lineHeight: 1.4 },
    h4: { fontFamily: heading, fontWeight: 700, fontSize: '1.75rem', lineHeight: 1.15, fontVariantNumeric: 'tabular-nums' },
    h5: { fontFamily: heading, fontWeight: 600, fontSize: '1.375rem', lineHeight: 1.2, fontVariantNumeric: 'tabular-nums' },
    h6: { fontFamily: heading, fontWeight: 600, fontSize: '0.9375rem', lineHeight: 1.4 },
    subtitle2: { fontWeight: 600, fontSize: '0.8125rem' },
    body1: { fontSize: '0.9375rem', lineHeight: 1.5 },
    body2: { fontSize: '0.875rem', lineHeight: 1.45 },
    caption: { fontSize: '0.75rem', lineHeight: 1.4 },
    button: { textTransform: 'none', fontWeight: 600, letterSpacing: 0 },
    overline: { textTransform: 'none', letterSpacing: 0, fontWeight: 600, fontSize: '0.75rem' },
  },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        'html, body': { WebkitFontSmoothing: 'antialiased' },
        body: { fontVariantNumeric: 'tabular-nums' },
        ':focus-visible': { outlineOffset: 2 },
        '@media (prefers-reduced-motion: reduce)': {
          '*, *::before, *::after': { animationDuration: '0.01ms !important', transitionDuration: '0.01ms !important' },
        },
      },
    },
    MuiButtonBase: { defaultProps: { disableRipple: false } },
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: { root: { borderRadius: 20, paddingInline: 16, minHeight: 36 }, sizeSmall: { minHeight: 30, paddingInline: 12 } },
    },
    MuiCard: { defaultProps: { variant: 'outlined' }, styleOverrides: { root: { borderRadius: 16 } } },
    MuiCardHeader: { styleOverrides: { root: { paddingBottom: 4 }, title: { fontFamily: heading, fontWeight: 600, fontSize: '1.0625rem' } } },
    MuiChip: { styleOverrides: { root: { fontWeight: 600, borderRadius: 8 } } },
    MuiDialog: { styleOverrides: { paper: { borderRadius: 24 } } },
    MuiDialogTitle: { styleOverrides: { root: { fontFamily: heading, fontWeight: 600, fontSize: '1.25rem' } } },
    MuiTextField: { defaultProps: { size: 'small', fullWidth: true } },
    MuiAppBar: { defaultProps: { elevation: 0 } },
    MuiTooltip: { defaultProps: { arrow: true } },
    MuiSwitch: { defaultProps: { color: 'secondary' } },
    MuiTab: { styleOverrides: { root: { textTransform: 'none', fontWeight: 600, minHeight: 44 } } },
    MuiTableCell: { styleOverrides: { head: { fontWeight: 600 } } },
    MuiAlert: { styleOverrides: { root: { borderRadius: 12, alignItems: 'center' } } },
    MuiLinearProgress: { styleOverrides: { root: { borderRadius: 4, height: 8 } } },
  },
});

export { alpha };
