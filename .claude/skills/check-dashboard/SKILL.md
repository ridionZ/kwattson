---
name: check-dashboard
description: Run the kWattson demo and verify the web dashboard in a real browser — screenshots at phone and desktop sizes in light and dark mode, console errors, and Content-Security-Policy / Trusted Types violations. Use after any change to kwattson/web (static files, variants, API used by the UI) and before saying a UI change works.
---

# Check the kWattson dashboard

1. **Start a throwaway demo server** (simulated hardware, synthetic history; never real devices):
   ```sh
   DEMO_MODE=true DEMO_PASSWORD='demo-password-123456' DATA_DIR="$(mktemp -d)" \
     APP_URL=http://localhost:8790 DEV_HTTP=true PORT=8790 HOST=127.0.0.1 \
     .venv/bin/python -m kwattson.cli serve
   ```
   Run it in the background, wait for `GET /healthz`, sign in as `demo` / `demo-password-123456`.
   The `Host` header must match `APP_URL` (use `http://localhost:8790`, not 127.0.0.1).
2. **Drive it with Playwright** (see the `webapp-testing` skill for patterns). For every page you
   touched, at widths **390 and 1280**, with `colorScheme: 'light'` and `'dark'`:
   - listen for `console` errors and `pageerror`;
   - add `document.addEventListener('securitypolicyviolation', …)` via an init script and fail on any;
   - exercise the real flow (open the dialog, submit the form, see the result), not just page loads;
   - save screenshots and **look at them** — check layout, overflow, contrast, focus visibility.
3. **Confirm the server saw no violations:** `GET /api/audit?limit=500` must contain no
   `browser_policy_violation` events.
4. **Rules that must still hold:** no `innerHTML`/`insertAdjacentHTML`/inline handlers or `style=""` in
   HTML strings; no remote scripts, fonts or images; CSRF header on unsafe requests; modelled numbers
   labelled as modelled. Follow the brand rules in `docs/brand/BRAND.md`.
5. Stop the server and delete the temp data dir. Report what you checked, with screenshot paths,
   and anything you could not verify.

For accessibility and web-platform questions use the `modern-web-guidance` skill
(`tools/install-skills.sh` installs it) and `frontend-ui-engineering`'s accessibility checklist.
