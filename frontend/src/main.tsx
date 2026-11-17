import '@fontsource/montserrat/latin-600.css';
import '@fontsource/montserrat/latin-700.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import createCache from '@emotion/cache';
import { CacheProvider } from '@emotion/react';
import { ThemeProvider } from '@mui/material/styles';
import CssBaseline from '@mui/material/CssBaseline';
import { theme } from './theme';
import App from './App';

// CSP: Emotion injects <style> elements at runtime. The server puts a fresh
// nonce in <meta name="csp-nonce"> and allows it in style-src (csp.json).
const nonce = document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')?.content;
const cache = createCache({ key: 'kw', nonce: nonce && nonce !== '__CSP_NONCE__' ? nonce : undefined, prepend: true });

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <CacheProvider value={cache}>
      <ThemeProvider theme={theme} defaultMode="system" noSsr>
        <CssBaseline enableColorScheme />
        <App />
      </ThemeProvider>
    </CacheProvider>
  </StrictMode>,
);
