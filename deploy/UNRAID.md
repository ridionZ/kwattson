# Install kWattson on Unraid

The Docker template fills in the data folder, proxy port and container security settings.
You need a reverse proxy that serves HTTPS and a hostname for the dashboard.

## Install with the prefilled form

Once the public listing is approved, open **Apps**, search for **kWattson** and select
**Install**. To use the same form on your own server before approval, open the Unraid terminal and download the template.
This preserves an existing saved template.

```sh
template_file=$(mktemp) &&
  curl -fsSL https://raw.githubusercontent.com/ridionZ/kwattson/main/templates/kwattson.xml -o "$template_file" &&
  mkdir -p /boot/config/plugins/dockerMan/templates-user &&
  cp -n "$template_file" /boot/config/plugins/dockerMan/templates-user/my-kwattson.xml
```

Select **Docker → Add Container → Template → kwattson**. If you already installed kWattson
through DockerMan, use **Docker → kwattson → Edit** to manage that container.

| Installer field | What to enter |
|---|---|
| Dashboard HTTPS address | Your full browser address, for example `https://energy.example.com`. Include a nonstandard HTTPS port if you use one. |
| First admin password | On a new installation, enter a passphrase of 14–256 characters, then sign in as `admin`. Clear the variable in DockerMan after signing in. |
| Application data | Defaults to `/mnt/user/appdata/kwattson`, mounted at `/data`. If reusing an existing folder, ensure UID 99 and GID 100 can write to it. |
| Trusted reverse proxy IP | The address the container sees as its immediate proxy. For nginx on the host with Docker bridge networking, this is commonly the bridge gateway. Check with `docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'`. For a proxy on a shared Docker network, use that proxy's IP. |
| Allowed local services (advanced) | Leave blank unless you connect a local service such as Home Assistant. Enter the hostname used in its URL. |

Unraid supplies the server time zone. The template binds the backend to `127.0.0.1:8085` on the host.
For nginx on the host, forward requests to `http://127.0.0.1:8085` and pass through the original
Host header including any port. For a containerised proxy, put both containers on the same
user-defined bridge and forward to `http://kwattson:8080`. [Example nginx configuration](nginx.conf.example)

Click **Apply**, then open the HTTPS address you entered. To have Unraid's **WebUI** shortcut
open the dashboard, set **WebUI** in Advanced View to the same address.

## First sign-in

1. If you set **First admin password**, sign in as **admin**. Otherwise, in the Unraid terminal run `docker exec kwattson kwattson setup-token`. Paste the token
   into the dashboard, create your admin account and enable two-step sign-in in **Settings → My account**.
2. Follow the Overview checklist for location, electricity prices and your first system. Enter device
   credentials in the device wizard. For supported systems you can import historical readings with
   `docker exec kwattson kwattson backfill --days 60`.
3. Back up the appdata folder, including the database and `master.key`. The key is needed to
   decrypt credentials when restoring a backup.

## Check a connection

- **Weather.** In **Settings → Home & tariff**, search for an address, suburb or postcode, select a result and save. **Test weather** fetches the current hourly forecast. The weather switch is in **Settings → Planning**. Overview shows the forecast time and the solar estimates.
- **MG iSMART.** In **Settings → Vehicles**, choose the region used by the iSMART app. Email sign-in uses the account email and password. For phone sign-in, use the national number and its calling code in the separate fields. **Test** identifies whether sign-in, vehicle discovery or the battery read failed. If the vendor returns a service code, include the stage and code when reporting the issue. The plugin pauses polling for 15 minutes after a failed request.
- **Power updates.** The overview checks the local readings every 30 seconds. Set **Power polling interval** in **Home & tariff** to choose how often to ask the inverter. Existing installations keep their saved interval. The device timestamp shows when the vendor actually measured the power.
- **Export limits.** Enter the approved limit at the grid meter. The planner accounts for forecast house use, EV charging and battery charging before calculating how much power can reach the grid. The separate inverter rating caps the AC power the equipment can supply.

## Build locally or use Compose

To build the image on Unraid, clone the public repository first. If you already have the checkout,
use `git pull --ff-only` inside it to update the source.

```sh
git clone https://github.com/ridionZ/kwattson.git /mnt/user/appdata/kwattson-src
cd /mnt/user/appdata/kwattson-src && docker build -t kwattson:latest .
```

Set **Repository** in the prefilled form to `kwattson:latest` to use that build.
As an alternative to the form, edit `deploy/docker-compose.yml` with your HTTPS address,
time zone, proxy IP and volume path, then run `docker compose -f deploy/docker-compose.yml up -d`
from the project directory. Use one installation method per data directory.

## Updates

For the registry image use **Docker → Check for Updates** and apply the kWattson update. For a
local build, pull the source and rebuild the image, then recreate the container using the saved
template or Compose. Keep the same host directory mounted at `/data`. Before updating,
stop the container, copy the entire appdata directory (including `master.key`), and start
the update. Settings, logins, device credentials and measurements live in that directory.
When a schema upgrade is needed, kWattson saves a consistent pre-migration SQLite copy
beside the database and upgrades it in a transaction. To roll back, stop the container,
restore your full pre-update appdata copy and select the previous image tag.

## Public catalog submission (maintainer)

1. Run **Actions → Publish Unraid image → Run workflow** on `main`. It builds a Linux x86-64
   image, tests first start and pushes the commit SHA and `latest` tags to GitHub Container Registry.
2. For the first publication, set the kWattson package visibility to **Public** in GitHub Package
   settings. Verify `docker pull ghcr.io/ridionz/kwattson:latest` succeeds on a signed-out client.
   [GitHub package visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)
3. Sign in to Unraid Community Apps and submit the repository URL `https://github.com/ridionZ/kwattson`.
   Complete the portal's ownership check if prompted, then **Validate**, **Scan**, inspect the card
   and **Submit for review**. The profile is `ca_profile.xml` and the app is
   `templates/kwattson.xml`. [Submission portal](https://ca.unraid.net/submit/new) · [Builder guide](https://ca.unraid.net/submit/help/builders)

### Recovery
- Forgot password / lost authenticator: `docker exec -it kwattson kwattson reset-user <username> --disable-mfa`.
- Battery doing something unexpected: press **Stop automatic control** in the dashboard, and if the
  app is unreachable disable *Mode Scheduler* in the FoxESS app (see docs/COMMISSIONING.md).
