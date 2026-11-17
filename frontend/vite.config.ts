import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Built into kwattson/web/static/ and served by the Python app: index.html at /,
// assets at /static/. The server replaces __CSP_NONCE__ in index.html on every request.
export default defineConfig({
  base: '/static/',
  plugins: [react()],
  // Vite puts nonce="__CSP_NONCE__" on the entry script, preloads and stylesheet links;
  // the server substitutes the real per-request nonce.
  html: { cspNonce: '__CSP_NONCE__' },
  build: {
    outDir: '../kwattson/web/static',
    emptyOutDir: true,
    assetsInlineLimit: 0, // fonts/images as files (font-src/img-src 'self')
    chunkSizeWarningLimit: 700,
  },
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://localhost:8771', changeOrigin: false } },
  },
});
