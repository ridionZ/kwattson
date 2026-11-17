# kWattson dashboard

React 19 + MUI 9 with a Material Design 3-flavoured kWattson theme, `@mui/x-charts` (MIT tier) for charts,
TypeScript, built with Vite 8. The build is written to `kwattson/web/static/`, which the Python app serves
(`index.html` at `/`, assets under `/static/`). The built files are committed, so running kWattson never needs Node.

## Build

The Docker build uses Node 24.

```sh
cd frontend
npm ci --ignore-scripts   # exact versions from package-lock.json; no install scripts needed
npm run build             # tsc --noEmit, then vite build → ../kwattson/web/static/ (replaces its contents)
```

Restart `kwattson serve` after a build: the server reads `static/index.html` at start-up.

Development with hot reload: start a demo server on port 8771 (see `AGENTS.md`), then `npm run dev` and open
`http://localhost:5173/static/`. Vite proxies `/api` to the demo server. Sign-in cookies need `APP_URL` to
match, so for anything involving writes use the built app on the demo server instead.

`npm run typecheck` runs the TypeScript checks only.

## Layout

| Path | What |
|---|---|
| `src/main.tsx` | Emotion cache with the CSP nonce, theme, root |
| `src/theme.ts` | Brand tokens (Navy structure, Amber solar, Teal battery/positive, Slate grid), light + dark schemes |
| `src/App.tsx` | Shell: app bar with wordmark, nav links (`aria-current`), mode chip, Stop button, alerts drawer, user menu, bottom nav, sign-in |
| `src/lib/` | API client (CSRF in memory only), en-AU formatting in the site time zone, hooks, roles, planner actions |
| `src/components/` | Toggletip (`HelpTip`), quick-action dialogs, scheduled list, stacked time charts, copy fields, wordmark |
| `src/pages/` | Overview, Plan, History (lazy), `settings/` (lazy): systems + vehicle wizards, alerts, users, tariffs, tokens, account |

Routing is hash-based (`#/plan`, `#/settings/alerts`), so deep links need no server rewrites. The UI follows
`docs/brand/BRAND.md`. Members and viewers only see what their role allows
(see `kwattson/web/permissions.py`).

## Content Security Policy

The server's CSP is strict (`script-src 'nonce-…' 'strict-dynamic'`, `style-src 'self'`, Trusted Types required,
no remote hosts). How the build fits it:

- `index.html` has one entry `<script type="module" nonce="__CSP_NONCE__">`; Vite's `html.cspNonce` puts the
  placeholder on the entry script, modulepreloads and stylesheet links, and the server replaces it per request.
  Lazy chunks load through `'strict-dynamic'`.
- Emotion (MUI's style engine) injects `<style>` elements at runtime. `public/csp.json` declares
  `{"style_nonce": true}` so the server adds the nonce to `style-src`, and `main.tsx` creates the Emotion cache with
  the nonce from `<meta name="csp-nonce">`.
- Inline styles from React, MUI and Popper are set through CSSOM (`element.style`), which `style-src` allows.
- No Trusted Types policy is needed: nothing here uses `dangerouslySetInnerHTML` or other HTML sinks.
- Fonts are self-hosted: Montserrat 600/700 (latin) from `@fontsource/montserrat`; its SIL Open Font License ships
  as `static/licenses/montserrat-OFL.txt`. Body text uses the system font stack.

## Bundle size (gzip)

| Chunk | gzip |
|---|---|
| Entry (React, MUI core, shell, Overview) | ~104 kB |
| Shared MUI form/menu chunk (preloaded) | ~87 kB |
| Charts (`@mui/x-charts`, Plan and History only) | ~98 kB |
| Settings pages (all, lazy) | ~40 kB |
| **Everything** | **~340 kB** |

First load of the Overview is about 190 kB of gzipped JavaScript; the chart library loads only on Plan or History.

## Checking a change

Use the `check-dashboard` skill (`.claude/skills/check-dashboard/SKILL.md`): demo server, Playwright at 390 and 1280 px
in light and dark, no console errors, no CSP or Trusted Types violations, and no `browser_policy_violation` rows in
`GET /api/audit`.
