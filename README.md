# SolarWatch

Multi-site solar inverter monitoring platform. Collects real-time data from
Deye (direct Modbus) and Sunsynk (cloud API) inverters, stores it in
PostgreSQL, and serves a live power flow dashboard with charts, weather,
and battery state — built on the FastAPI SaaS template.

---

## What it does

- **Live power flow dashboard** — solar generation, battery charge/discharge,
  grid import/export, load consumption. Updates every 10 seconds.
- **Advanced charts** — PV output, load, battery SOC, grid voltage, daily
  energy totals, temperature trends, and peak values. All today's data.
- **Weather overlay** — current conditions, UV index, sunrise/sunset from
  Open-Meteo (free, no API key required).
- **Monthly totals** — cumulative PV kWh and grid import for the current month.
- **Multi-site** — switch between sites with the site selector. Each site runs
  independent inverters and has its own data stream.
- **Public share links** — generate a read-only URL for any site. No login
  required. Installable as a PWA on phones and kitchen screens.
- **Offline/stale detection** — red banner when you're offline, amber banner
  when inverter data is more than 5 minutes old.

---

## Architecture

```
Inverters (Deye / Sunsynk)
        │
        ▼
  collector.py  ──── systemd service, runs continuously
        │             polls every 60s, writes to PostgreSQL
        ▼
  PostgreSQL (HA cluster with HAProxy VIP)
        │
        ▼
  FastAPI web app  ──── serves dashboard + admin UI + share links
  (gunicorn + uvicorn workers, multiple pods)
        │
        ▼
  Browser / PWA
```

The collector and web app are **independent processes** sharing one database.
The collector has no knowledge of the web app and vice versa. Restarting or
deploying either one does not affect the other.

The web app is stateless — all session state is in signed cookies. Run as many
pods as needed across as many nodes as you have. The only shared state is the
database and `SECRET_KEY`.

---

## Fresh deployment — step by step

This section covers a complete fresh installation from scratch.

### 1. Prerequisites

- PostgreSQL server (superuser access required for initial setup)
- Python 3.11+ (for local dev) or Docker / Kubernetes (for production)
- Network access to your inverters (Deye) or Sunsynk cloud credentials

### 2. Database provisioning

Run `setup.sql` once as the postgres superuser. This creates everything in one shot.

```bash
psql -h your-postgres-host -U postgres -f setup.sql
```

`setup.sql` creates:
- `solarwatch_user` (password `CHANGEME` — change this immediately)
- The `solarwatch` database
- All tables: `sites` (with `share_token`), `solar_readings`, `weather_readings`
- All performance indexes including the unique indexes for the collector
- Example Selati and Lanner sites (edit or remove to match your setup)

**That's it for a fresh install. The migration files are for upgrades only.**

**Change the default DB password before starting the collector:**
```sql
-- Run as postgres superuser
ALTER USER solarwatch_user WITH PASSWORD 'your-secure-password';
```

### About the migration SQL files

| File | When to run |
|---|---|
| `setup.sql` | **Fresh install only** — creates everything from scratch |
| `migrate_share.sql` | **Upgrade only** — adds `share_token` to an existing DB created before v1.0 |
| `migrate_indexes.sql` | **Upgrade only** — adds performance indexes to an existing DB created before v1.0 |
| `migrate_weather.sql` | **Upgrade only** — adds weather table to an existing DB created before weather support |

If you are doing a fresh install using the current `setup.sql`, none of the
migration files are needed — everything is already included.

### 4. Configure the application

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```bash
SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
DATABASE_URL=postgresql+psycopg2://solarwatch_user:your-password@your-host:5432/solarwatch
APP_ADMIN_USERNAME=admin
APP_ADMIN_PASSWORD=your-initial-admin-password
APP_ENV=production
APP_PROXY_FIX=true   # if running behind a reverse proxy
```

### 5. Install dependencies

```bash
pip install -r requirements.txt
```

### 6. Start the web app

**Development (hot reload):**
```bash
python run.py
# Visit http://localhost:8000
```

**Production (gunicorn + uvicorn):**
```bash
gunicorn main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers 2 \
  --bind 0.0.0.0:8000
```

### 7. First login

Visit the app URL and sign in with `APP_ADMIN_USERNAME` / `APP_ADMIN_PASSWORD`.
You will be prompted to change the password immediately.

### 8. Configure your sites

Go to **Sites** (admin) and verify your sites are configured correctly:
- Deye sites: check inverter IPs, dongle serials, inverter serial numbers
- Sunsynk sites: enter cloud API credentials

### 9. Start the collector

```bash
# Collector uses same DATABASE_URL or DB_* vars as the web app
python collector.py
```

Or as a systemd service:
```bash
sudo cp solarwatch.service /etc/systemd/system/
sudo systemctl enable --now solarwatch
```

The collector polls every 60 seconds by default (`POLL_INTERVAL=60`).
Config changes in the web UI are picked up on the next config reload (every 5 minutes).

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | **Yes** | — | Signs session cookies. Must be identical across all replicas. |
| `APP_ADMIN_USERNAME` | **Yes** | — | Bootstrap admin username |
| `APP_ADMIN_PASSWORD` | **Yes** | — | Bootstrap admin password — must change on first login |
| `APP_ENV` | No | `development` | Set `production` for strict CSP, HSTS, HTTPS-only cookies |
| `DATABASE_URL` | Yes* | — | `postgresql+psycopg2://user:pass@host:5432/solarwatch` |
| `DB_HOST` | Yes* | `127.0.0.1` | Alternative to `DATABASE_URL` |
| `DB_PORT` | Yes* | `5432` | |
| `DB_USER` | Yes* | `solarwatch_user` | |
| `DB_PASSWORD` | Yes* | — | |
| `DB_NAME` | Yes* | `solarwatch` | |
| `DB_POOL_SIZE` | No | `5` | Connections per worker |
| `DB_MAX_OVERFLOW` | No | `10` | Extra connections beyond pool |
| `DB_POOL_TIMEOUT` | No | `30` | Seconds to wait for connection |
| `APP_PROXY_FIX` | No | `false` | Set `true` behind Traefik / Nginx / Cloudflare |
| `APP_ADMIN_RESET_PASSWORD` | No | `false` | Temporarily set `true` to reset admin password |
| `RATE_LIMIT_LOGIN` | No | `10/minute` | Login attempts per IP |
| `RATE_LIMIT_DEFAULT` | No | `60/minute` | Default API rate limit |
| `POLL_INTERVAL` | — | `60` | Collector poll interval in seconds |
| `MAX_RETRIES` | — | `3` | Retries per inverter per cycle |
| `RETRY_DELAY` | — | `5` | Seconds between retries |
| `CONFIG_RELOAD` | — | `300` | Seconds between site config reloads from DB |
| `WEATHER_INTERVAL` | — | `900` | Weather poll interval in seconds (15 min) |
| `VICTRON_MQTT_HOST` | — | — | Cerbo GX IP address for Victron sites (fallback if not set in inverter config) |
| `VICTRON_MQTT_PORT` | — | `1883` | Victron MQTT broker port |

> **Note:** The collector uses the same `DATABASE_URL` or `DB_*` variables as
> the web app. There are no separate `PG_*` variables — one `.env` file covers both.
> The simplest setup is to set `DATABASE_URL` once and both processes use it.


---

## Database schema

| Table | Owner | Created by | Notes |
|---|---|---|---|
| `sites` | postgres | `setup.sql` | Inverter site config (deye/sunsynk/victron/sungrow) |
| `solar_readings` | postgres | `setup.sql` | Time-series inverter data |
| `weather_readings` | postgres | `weather_readings` | Open-Meteo weather data |
| `roles` | solarwatch_user | App startup | Permission roles |
| `web_users` | solarwatch_user | App startup | User accounts |
| `user_sites` | solarwatch_user | App startup | User ↔ site assignments |

Schema changes to postgres-owned tables require running the SQL migration files
as the postgres superuser. App-owned tables are managed automatically by startup.

**For upgrades from pre-v1.0 installations only:**
```bash
# If upgrading an existing DB — run whichever apply to your situation
psql -h host -U postgres -d solarwatch -f migrate_share.sql    # adds share_token
psql -h host -U postgres -d solarwatch -f migrate_indexes.sql  # adds performance indexes
psql -h host -U postgres -d solarwatch -f migrate_weather.sql  # adds weather table
```

Fresh installs using the current `setup.sql` do not need any of these.

---

## DB connection pool sizing

SQLAlchemy pools connections per Gunicorn worker process. Plan capacity:

```
max_db_connections ≈ pods × workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW)
```

Default profile (2 pods, 2 workers, pool 3+2):
```
2 × 2 × (3 + 2) = 20 connections
```

Any increase to pods or workers must be coordinated with your PostgreSQL
server's `max_connections` setting.

---

## Managing the app via the web UI

### First login

Sign in at `/auth/login`. You will be prompted to change the bootstrap password
before accessing anything else.

### Sites — `/sites` (admin only)

The Sites page is the main configuration hub.

**Adding a site:**
1. Click **Add site**
2. Set site name (internal ID, cannot change), display name, source type
3. For Deye: add inverters after creation
4. For Sunsynk: enter username, password, plant ID

**Editing a site (admin):** Click **Edit** to access:
- Site details (name, location, coordinates, enabled toggle)
- Inverters (Deye) — add/remove with name, IP, dongle serial, inverter SN
- Sunsynk credentials
- User access — assign site_admin/site_viewer users
- Share link management
- Danger zone — delete site

**My Sites — `/my-sites` (site_admin only):**
Site admins see only their assigned sites. They can edit display details,
manage inverters, and control share links — but cannot see other sites or
manage users.

### Users — `/auth/admin/users` (admin only)

**Roles:**
- `admin` — full access: all sites, all admin pages
- `site_admin` — manages assigned sites (inverters, share links, details)
- `site_viewer` — read-only access to assigned sites
- `user` — identical to site_viewer (legacy name)

**Creating a user:** Enter username, password, role → Create user.
User is forced to change their password on first login.

**Assigning users to sites:** Done from the **Sites → Edit** page, not the
users page. Go to Sites → Edit site → User access section.

### Roles — `/auth/admin/roles` (admin only)

System roles (`admin`, `user`, `site_admin`, `site_viewer`) are recreated on
every startup and cannot be deleted. Custom roles can be created but have no
built-in permissions — they behave like `site_viewer`.

---

## Public share links (PWA-ready)

Share links give **read-only, unauthenticated** access to a single site's
live dashboard.

### Generating a share link

1. **Admin:** Sites → Edit site → Public share link → Generate share link
2. **Site admin:** My Sites → Edit site → Public share link → Generate share link

### Installing as a PWA

**Android (Chrome):** Open share link → three-dot menu → Add to Home screen

**iOS (Safari):** Open share link → Share button → Add to Home Screen

**Kitchen monitor / TV (kiosk mode):**
```bash
chromium-browser --kiosk --app=https://your-domain/share/your-token
```

The PWA installs with the site's name (e.g. "Selati — SolarWatch") and opens
directly to the live dashboard with no browser chrome.

### Revoking a share link

Click **Revoke link** on the site edit page. The URL stops working immediately.

### Offline and stale data behaviour

- **Red banner** — shown when you're offline. The dashboard shows the last
  cached data. Dismisses automatically when connectivity restores.
- **Amber banner** — shown when inverter data is more than 5 minutes old.
  Indicates the collector or inverter may be offline. Includes the data age.
- **Red dot** in the header — pulses when data is stale.

---

## API documentation

SolarWatch exposes a JSON API used by the dashboard. All endpoints require
authentication (session cookie) except `/api/version` and the public share
endpoints (`/api/share/{token}/*`).

### Accessing the API docs (admin only)

The interactive API documentation is available to admin users in both
development and production environments:

| URL | Description |
|---|---|
| `/api/docs` | Swagger UI — interactive, try requests in the browser |
| `/api/redoc` | ReDoc — clean reference format |
| `/api/openapi.json` | Raw OpenAPI schema |

Both pages require admin login. They are linked from the **Admin console**
quick links and from the admin navbar.

### Key endpoints

| Endpoint | Auth | Description |
|---|---|---|
| `GET /api/version` | None | Running app version and name |
| `GET /api/me` | Login required | Current user's profile |
| `GET /api/solar/sites` | Login required | Sites accessible to the user |
| `GET /api/solar/flow?site=X` | Login required | Live power data (10s cache) |
| `GET /api/solar/weather?site=X` | Login required | Latest weather (90s cache) |
| `GET /api/solar/monthly?site=X` | Login required | Month's PV and grid kWh (120s cache) |
| `GET /api/solar/chart/{type}?site=X` | Login required | Chart data (120s cache) |
| `GET /api/share/{token}/flow` | Token | Public share: live power data |
| `GET /api/share/{token}/weather` | Token | Public share: weather |
| `GET /api/share/{token}/monthly` | Token | Public share: monthly totals |
| `GET /api/share/{token}/chart/{type}` | Token | Public share: chart data |

Chart types: `pv`, `load`, `battery`, `grid`, `daily`, `temps`, `peaks`

---

## Versioning

The app version is stored in the `VERSION` file in the repo root. This version
is used in three places — they must all match:

```
VERSION          ← single source of truth (e.g. 1.0.0)
Chart.yaml       ← appVersion field
Docker image tag ← registry.hfisystems.com/hfisystems/solarwatch:1.0.0
```

The version is displayed in the UI:
- Dashboard header (very small, in the logo area)
- Footer on all admin pages
- `GET /api/version` endpoint

**Releasing a new version:**
1. Update `VERSION` to the new version
2. Update `Chart.yaml` `appVersion` to match
3. Build and push Docker image with that tag
4. Update `image.tag` in your tenant `values.yaml`
5. Run `helm upgrade`


---

## Docker

### Build and publish

```bash
# Authenticate to your registry
docker login registry.hfisystems.com

# Build
docker build -t registry.hfisystems.com/hfisystems/solarwatch:latest .

# Tag with version
docker tag registry.hfisystems.com/hfisystems/solarwatch:latest \
           registry.hfisystems.com/hfisystems/solarwatch:v1.0.0

# Push
docker push registry.hfisystems.com/hfisystems/solarwatch:latest
docker push registry.hfisystems.com/hfisystems/solarwatch:v1.0.0
```

### Local Docker Compose (development)

```bash
docker compose up
# App at http://localhost:8000, local Postgres included
```

---

## Kubernetes / Helm deployment

The `deploy/helm/solarwatch` chart deploys the web app. The collector is
deployed separately (systemd on a worker node or as its own Deployment).

The chart deploys **both** the web app and the collector from a single Helm release.
The chart structure mirrors the WTS chart so deployment procedures are identical.

### Prerequisites

Your K8s nodes must be labelled with `topology.kubernetes.io/zone`:
```bash
# Label Lanner nodes
kubectl label node <lanner-node> topology.kubernetes.io/zone=lnr

# Label Xneelo nodes
kubectl label node <xneelo-node> topology.kubernetes.io/zone=xne
```

The web app uses `topologySpreadConstraints` to ensure one pod lands in each
zone — matching your WTS multi-DC pattern exactly.

### Create the namespace

```bash
kubectl create namespace solarwatch
```

The namespace is not managed by Helm (matching WTS convention) — create it
manually so `helm uninstall` doesn't accidentally destroy everything.

### Create the image pull secret

```bash
kubectl create secret docker-registry hfisystems-registry \
  --docker-server=registry.hfisystems.com \
  --docker-username=<your-gitlab-username> \
  --docker-password=<your-gitlab-token> \
  --docker-email=<your-email> \
  --namespace solarwatch
```

### Configure a tenant values file

```bash
mkdir -p deploy/tenants/solarwatch
cp deploy/tenants/example/values.yaml deploy/tenants/solarwatch/values.yaml
```

Key values to set (all others have safe defaults):

```yaml
image:
  tag: "1.0.0"              # must match VERSION file

database:
  host: postgres-ha.hfisystems.com
  name: solarwatch
  user: solarwatch_user
  password: "your-db-password"

secrets:
  secretKey: "generate-with-python-secrets-token-hex-32"
  adminUsername: admin
  adminPassword: "strong-initial-password"

collector:
  enabled: true
  zone: lnr                  # pin collector to Lanner (has inverter access)
```

### Deploy

```bash
# Install / upgrade (same command for both — Helm is idempotent)
helm upgrade --install solarwatch deploy/helm/solarwatch \
  -f deploy/tenants/solarwatch/values.yaml \
  --namespace solarwatch

# Check all pods
kubectl get pods -n solarwatch
# Expected: 2 web pods (one xne, one lnr) + 1 collector pod (lnr)

# Check web rollout
kubectl rollout status deployment/solarwatch -n solarwatch

# Check collector
kubectl rollout status deployment/solarwatch-collector -n solarwatch

# Web logs
kubectl logs -n solarwatch -l component=web --tail=50 -f

# Collector logs
kubectl logs -n solarwatch -l component=collector --tail=100 -f
```

### Cloudflare Tunnel (no Ingress needed)

The chart has `ingress.enabled: false` by default. With Cloudflare Tunnel
running on each node, point your tunnel config to the Service DNS name:

```yaml
# In your cloudflared config.yaml or tunnel route:
ingress:
  - hostname: solarwatch.your-domain.com
    service: http://solarwatch.solarwatch.svc.cluster.local:80
```

No LoadBalancer, no NodePort, no cert-manager needed. Cloudflare handles TLS.

### What each pod does

| Pod | Replicas | Zone | Purpose |
|---|---|---|---|
| `solarwatch-*` | 2 | xne + lnr | Web app + API (gunicorn + uvicorn) |
| `solarwatch-collector-*` | 1 | lnr (configurable) | Polls inverters, writes to DB |

The PodDisruptionBudget ensures at least 1 web pod stays alive during node
drains and cluster maintenance — matching the WTS `minAvailable: 1` pattern.

### Moving the collector between DCs

If Lanner is down and you need the collector to run from Xneelo:

```yaml
# In your tenant values.yaml
collector:
  zone: xne
```

```bash
helm upgrade solarwatch deploy/helm/solarwatch \
  -f deploy/tenants/solarwatch/values.yaml \
  --namespace solarwatch
```

The old collector pod is terminated, a new one starts in the Xneelo zone.

### Rolling updates (zero downtime)

```bash
# Update VERSION, build and push new Docker image, then:
helm upgrade solarwatch deploy/helm/solarwatch \
  -f deploy/tenants/solarwatch/values.yaml \
  --namespace solarwatch
```

`maxUnavailable: 0` ensures at least one web pod serves traffic at all times.
The collector restarts briefly during updates — one poll cycle is missed.

### Critical: SECRET_KEY must be identical across all pods

Sessions are signed cookies. A pod with a different `SECRET_KEY` rejects
sessions from other pods and logs users out. Store it in `secrets.secretKey`
in your tenant values file and never change it unless you intentionally want
to invalidate all active sessions.

### Health probes

`/health` always returns `200 {"status": "ok"}`. If startup checks fail
(DB unreachable, missing columns) the app serves `503` on all other routes
until resolved — the pod stays in the load balancer so Cloudflare Tunnel
can still reach it for health checking.

---

## Collector deployment

The collector runs as a long-lived process. It is recommended to run it as a
systemd service on a worker node close to your inverters.

### Bare-metal / systemd deployment (Rocky Linux / RHEL / Ubuntu)

This is the recommended approach for a dedicated always-on server.

**1. Create the system user and directory**
```bash
sudo useradd -r -s /sbin/nologin -d /opt/solarwatch solarwatch
sudo mkdir -p /opt/solarwatch
sudo chown solarwatch:solarwatch /opt/solarwatch
```

**2. Clone the repository**
```bash
sudo -u solarwatch git clone https://github.com/nerffern/solarwatch.git /opt/solarwatch
```

**3. Create the Python virtual environment**
```bash
sudo python3.12 -m venv /opt/solarwatch/venv
sudo chown -R solarwatch:solarwatch /opt/solarwatch/venv
sudo -u solarwatch /opt/solarwatch/venv/bin/pip install -r /opt/solarwatch/requirements.txt
```

**4. Create the .env file**
```bash
sudo -u solarwatch cp /opt/solarwatch/.env.example /opt/solarwatch/.env
sudo -u solarwatch nano /opt/solarwatch/.env
```

Minimum required settings for the collector:
```bash
# Database — use your HA PostgreSQL VIP
DATABASE_URL=postgresql+psycopg2://solarwatch_user:yourpassword@postgres-ha.hfisystems.com:5432/solarwatch

# Collector tuning (defaults are fine to start)
POLL_INTERVAL=60
CONFIG_RELOAD=300
WEATHER_INTERVAL=900
```

Note: The collector does not need `SECRET_KEY`, `APP_ADMIN_*`, or any web app
settings — it only reads the DB connection variables.

**5. Install and start the collector service**
```bash
sudo cp /opt/solarwatch/solarwatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now solarwatch
```

**6. Check it's running**
```bash
sudo systemctl status solarwatch
sudo journalctl -u solarwatch -f
```

Expected startup output:
```
SolarWatch Collector starting
DB              : postgres-ha.../solarwatch
DB connection verified
Sites loaded:
  deye:    ['Selati', 'Lanner']
  sunsynk: ['Penguin']
  victron: ['Harmonia', 'Oppikop']
```

**Useful commands**
```bash
# Live logs
sudo journalctl -u solarwatch -f

# Restart after .env or code changes
sudo systemctl restart solarwatch

# Check last 50 log lines
sudo journalctl -u solarwatch -n 50 --no-pager

# Stop collector without disabling
sudo systemctl stop solarwatch
```

**Running collector alongside an existing web server**

The collector is completely independent — it only writes to the database.
You can run the new collector (`solarwatch.service`) alongside any existing
dashboard or web server with no conflicts. They share only the database.

To replace the old collector:
```bash
# Pull the latest code
sudo -u solarwatch git -C /opt/solarwatch pull

# Install any new dependencies
sudo -u solarwatch /opt/solarwatch/venv/bin/pip install -r /opt/solarwatch/requirements.txt

# Stop and disable the old collector service (use whatever the old service is named)
sudo systemctl stop solarwatch-old
sudo systemctl disable solarwatch-old

# Start the new one
sudo systemctl enable --now solarwatch
```

The collector reloads site configuration from the database every 5 minutes
(`CONFIG_RELOAD=300`). Changes made via the web UI (adding inverters, updating
credentials, enabling/disabling sites) are picked up automatically — no restart
required.

**Supported inverter types:**

| Type | Protocol | Collector notes |
|---|---|---|
| `deye` | Solarman V5 Modbus over WAN | Direct IP to dongle, no cloud |
| `sunsynk` | REST API at api.sunsynk.net | Cloud — needs username/password/plant ID |
| `victron` | MQTT via Cerbo GX / CCGX | Direct local network — no VRM cloud needed |
| `sungrow` | Placeholder | Worker pending — site disabled until added |

**All inverter types store data in the same `solar_readings` table and are
displayed identically on the dashboard.** The collector fills the fields that
each inverter type provides; fields not available from that source are stored
as `NULL` (the dashboard handles them gracefully).

**Victron MQTT configuration:**

Victron sites connect to the Cerbo GX MQTT broker directly over the local
network. No VRM cloud account or API key needed. The Cerbo GX must be on the
same network as the collector pod/server.

Configure a Victron site via the web UI:
1. Sites → Add site → source type: **Victron — MQTT (Cerbo GX / CCGX)**
2. Add one inverter entry in the Inverters section:
   - Name: `Victron_1` (or any label)
   - IP address: Cerbo GX local IP (e.g. `10.0.1.80`)
   - Dongle serial: leave blank
   - Inverter SN: leave blank
3. Set latitude/longitude so weather data is collected
4. The collector connects via MQTT, sends a keepalive to flush all topics,
   and collects power, battery, grid, and solar data within ~10 seconds per cycle.

Multiple MPPT chargers are handled automatically — power is summed across all
solar chargers, and daily yield is totalled across all devices.

---

## Upgrading

### Bare-metal / systemd

```bash
# Pull latest code
sudo -u solarwatch git -C /opt/solarwatch pull

# Install any new dependencies
sudo -u solarwatch /opt/solarwatch/venv/bin/pip install -r /opt/solarwatch/requirements.txt

# Run any new migration SQL files if present (check git log / release notes)
# psql -h your-postgres-host -U postgres -d solarwatch -f /opt/solarwatch/migrate_xyz.sql

# Restart services
sudo systemctl restart solarwatch          # collector
sudo systemctl restart solarwatch-powerflow  # web app (if running)
```

### Kubernetes

Update the image tag in your tenant values file and run `helm upgrade`.
The rolling update ensures zero downtime.

For most upgrades the collector does not need restarting — config changes
are picked up automatically every `CONFIG_RELOAD` seconds (default 5 min).
