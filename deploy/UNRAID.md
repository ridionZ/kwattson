# Install kWattson on Unraid

The Docker template fills in the data folder, proxy port and container security settings.
You need a reverse proxy that serves HTTPS and a hostname for the dashboard.

## Install with the prefilled form

Once the public listing is approved, open **Apps**, search for **kWattson** and select
**Install**. To use the same form on your own server before approval, open the Unraid terminal.
For a new checkout run:

```sh
git clone https://github.com/ridionZ/kwattson.git /mnt/user/appdata/kwattson-src
```

Then run the following. This also works for an existing checkout and preserves any saved
template with the same name.

```sh
cd /mnt/user/appdata/kwattson-src && git pull --ff-only &&
  mkdir -p /boot/config/plugins/dockerMan/templates-user &&
  cp -n templates/kwattson.xml /boot/config/plugins/dockerMan/templates-user/my-kwattson.xml
```

Select **Docker → Add Container → Template → kwattson**. If you already installed kWattson
through DockerMan, use **Docker → kwattson → Edit** to manage that container.

| Installer field | What to enter |
|---|---|
| Dashboard HTTPS address | Your full browser address, for example `https://energy.example.com`. Include a nonstandard HTTPS port if you use one. |
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

1. In the Unraid terminal run `docker exec kwattson kwattson setup-token`. Paste the token
   into the dashboard, create your admin account and enable two-step sign-in in **Settings → My account**.
2. Follow the Overview checklist for location, electricity prices and your first system. Enter device
   credentials in the device wizard. For supported systems you can import historical readings with
   `docker exec kwattson kwattson backfill --days 60`.
3. Back up the appdata folder, including the database and `master.key`. The key is needed to
   decrypt credentials when restoring a backup.

## Build locally or use Compose

If the registry image is unavailable, build it on Unraid from the checkout above:

```sh
cd /mnt/user/appdata/kwattson-src && docker build -t kwattson:latest .
```

Set **Repository** in the prefilled form to `kwattson:latest` to use that build.
As an alternative to the form, edit `deploy/docker-compose.yml` with your HTTPS address,
time zone, proxy IP and volume path, then run `docker compose -f deploy/docker-compose.yml up -d`
from the project directory. Use one installation method per data directory.

## Updates

For the registry image use **Docker → Check for Updates** and apply the kWattson update. For a
local build, pull the source and rebuild the image, then recreate the container using the saved
template or Compose. The app migrates its database on startup.

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
