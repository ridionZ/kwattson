# Running on Unraid behind nginx

1. **Build the image** (Unraid terminal, or any Docker host then push to your registry):
   ```sh
   git clone <your repo> /mnt/user/appdata/kwattson-src
   cd /mnt/user/appdata/kwattson-src && docker build -t kwattson:latest .
   ```
2. **Create the container** — either `docker compose -f deploy/docker-compose.yml up -d`
   (Compose Manager plugin) or *Docker → Add Container* with:
   - Repository `kwattson:latest`, network `bridge`
   - Path `/data` → `/mnt/user/appdata/kwattson` (holds the database, `master.key`, backups)
   - Port container `8080` → host `8085`
   - Variables `APP_URL=https://solar.your-domain`, `TZ=Australia/Brisbane`,
     `FORWARDED_ALLOW_IPS=<IP of your nginx container/host>`, optional `OUTBOUND_ALLOW_HOSTS=homeassistant.local`
   - Extra parameters: `--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true`
   - The appdata folder must be writable by uid 99 / gid 100 (`chown -R 99:100 /mnt/user/appdata/kwattson`).
3. **Proxy**: point your nginx (SWAG, NPM or plain nginx) at `http://<unraid-ip>:8085` using
   `deploy/nginx.conf.example`. Pass the `Host` header through unchanged. Use HTTPS only;
   cookies are `Secure` and the app refuses other hosts.
4. **Create the owner**: `docker exec -it kwattson kwattson setup-token`, open
   `APP_URL`, paste the token, choose a username and a 14+ character passphrase. Then turn
   on two-step sign-in under *Settings → My account* — the dashboard is on the internet.
5. **Connect the inverter**: *Settings → Solar & batteries → Add a system*. Choose your source, enter the hardware limits,
   give it a name and enter its credentials. Select *Test connection*, then review and save. Optionally backfill history:
   `docker exec -it kwattson kwattson backfill --days 60`.
6. **Back up** `/mnt/user/appdata/kwattson` (the app also writes daily copies to
   `backups/`). Keep `master.key` with the database — without it stored credentials are lost.

### Recovery
- Forgot password / lost authenticator: `docker exec -it kwattson kwattson reset-user <username> --disable-mfa`.
- Battery doing something unexpected: press **Stop automatic control** in the dashboard, and if the
  app is unreachable disable *Mode Scheduler* in the FoxESS app (see docs/COMMISSIONING.md).

### Updating
`docker build -t kwattson:latest . && docker compose -f deploy/docker-compose.yml up -d` (database migrations run automatically;
the app refuses to start on a database from a newer version).
