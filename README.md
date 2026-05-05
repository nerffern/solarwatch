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

Run `setup.sql` as the postgres superuser. This creates the database, user,
schema, tables, indexes, and seeds your initial sites.

```bash
psql -h your-postgres-host -U postgres -f setup.sql
```

`setup.sql` is idempotent — it uses `IF NOT EXISTS` and `ON CONFLICT DO NOTHING`
throughout so it is safe to re-run. It will:

- Create `solarwatch_user` with password `CHANGEME` (change this immediately)
- Create the `solarwatch` database
- Create all tables: `sites`, `solar_readings`, `weather_readings`
- Create all performance indexes
- Seed example Selati and Lanner sites (edit or remove as needed)

**Change the default password before running the collector:**
```sql
-- Connect as postgres
ALTER USER solarwatch_user WITH PASSWORD 'your-secure-password';
```

### 3. Share token column (required for share links)

The `share_token` column on the `sites` table must be added by the postgres
superuser (the app user does not own the `sites` table and cannot run DDL):

```bash
psql -h your-postgres-host -U postgres -d solarwatch -f migrate_share.sql
```

This is safe to run even if the column already exists.

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
# Edit .env (or environment) to set PG_HOST, PG_USER, PG_PASS, PG_DB
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
| `PG_HOST` | — | — | Collector DB host |
| `PG_PORT` | — | `5432` | |
| `PG_DB` | — | `solarwatch` | |
| `PG_USER` | — | `solarwatch_user` | |
| `PG_PASS` | — | — | |
| `POLL_INTERVAL` | — | `60` | Collector poll interval (seconds) |
| `WEATHER_INTERVAL` | — | `900` | Weather poll interval (seconds) |
| `CONFIG_RELOAD` | — | `300` | How often collector reloads site config (seconds) |

---

## Database schema

| Table | Owner | Created by | Notes |
|---|---|---|---|
| `sites` | postgres | `setup.sql` | Inverter site configuration |
| `solar_readings` | postgres | `setup.sql` | Time-series inverter data |
| `weather_readings` | postgres | `weather_readings` | Open-Meteo weather data |
| `roles` | solarwatch_user | App startup | Permission roles |
| `web_users` | solarwatch_user | App startup | User accounts |
| `user_sites` | solarwatch_user | App startup | User ↔ site assignments |

Schema changes to postgres-owned tables require running the SQL migration files
as the postgres superuser. App-owned tables are managed automatically by startup.

**Migration files:**
```bash
# Required: share token column (run once after initial setup)
psql -h host -U postgres -d solarwatch -f migrate_share.sql

# Optional: additional performance indexes (if not already in setup.sql)
psql -h host -U postgres -d solarwatch -f migrate_indexes.sql

# Optional: if upgrading from an older version without weather support
psql -h host -U postgres -d solarwatch -f migrate_weather.sql
```

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

### Create the image pull secret (private registry)

```bash
kubectl create secret docker-registry hfisystems-registry \
  --docker-server=registry.hfisystems.com \
  --docker-username=<your-gitlab-username> \
  --docker-password=<your-gitlab-token> \
  --docker-email=<your-email> \
  --namespace solarwatch
```

Replace:
- `<your-gitlab-username>` — your GitLab username
- `<your-gitlab-token>` — Personal Access Token with `read_registry` scope
- `<your-email>` — your email address

### Configure a tenant

```bash
# Create tenant values file
mkdir -p deploy/tenants/mysite
cp deploy/tenants/example/values.yaml deploy/tenants/mysite/values.yaml
# Edit values.yaml — set secrets, database URL, ingress host, image tag
```

Key values to set (must align with `.env.example` for consistent behaviour across dev and prod):

```yaml
image:
  tag: "1.0.0"          # pin to specific version — must match VERSION file

imagePullSecrets:
  - name: hfisystems-registry

secrets:
  secretKey: "<python -c 'import secrets; print(secrets.token_hex(32))'>"
  databaseUrl: "postgresql+psycopg2://solarwatch_user:pass@postgres-ha:5432/solarwatch"
  adminUsername: admin
  adminPassword: "<strong-initial-password>"

ingress:
  enabled: true
  host: solarwatch.your-domain.com
```

### Deploy

```bash
# Create namespace
kubectl create namespace solarwatch

# Install / upgrade
helm upgrade --install solarwatch deploy/helm/solarwatch \
  -f deploy/tenants/mysite/values.yaml \
  --namespace solarwatch \
  --create-namespace

# Check rollout
kubectl rollout status deployment/solarwatch -n solarwatch

# View pods
kubectl get pods -n solarwatch

# View logs
kubectl logs -n solarwatch -l app=solarwatch --tail=50 -f
```

### Rolling updates (zero downtime)

```bash
# Update image tag in values.yaml, then:
helm upgrade solarwatch deploy/helm/solarwatch \
  -f deploy/tenants/mysite/values.yaml \
  --namespace solarwatch
```

The chart uses `RollingUpdate` with `maxUnavailable: 0` — at least one pod
is always serving traffic during deploys.

### Multi-DC topology

The chart includes `topologySpreadConstraints` that spread pods across nodes.
For multi-DC deployments (e.g. Lanner + Xneelo):

```yaml
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels:
        app: solarwatch
```

**Critical:** `SECRET_KEY` must be **identical across all pods in all
datacentres**. Sessions are signed cookies — a pod with a different key
rejects sessions created by other pods and logs users out.

### Health probes

The `/health` endpoint always returns `200 {"status": "ok"}`. Used for both
liveness and readiness probes. If startup checks fail (DB unreachable, missing
tables), `/health` still returns 200 but all other routes return 503 until the
issue is resolved.

---

## Collector deployment

The collector runs as a long-lived process. It is recommended to run it as a
systemd service on a worker node close to your inverters.

```bash
# Copy and edit the service file
sudo cp solarwatch.service /etc/systemd/system/solarwatch-collector.service
sudo nano /etc/systemd/system/solarwatch-collector.service
# Set WorkingDirectory, EnvironmentFile, and ExecStart paths

sudo systemctl daemon-reload
sudo systemctl enable --now solarwatch-collector

# Check status
sudo systemctl status solarwatch-collector
sudo journalctl -u solarwatch-collector -f
```

The collector reloads site configuration from the database every 5 minutes
(`CONFIG_RELOAD=300`). Changes made via the web UI (adding inverters, updating
credentials, enabling/disabling sites) are picked up automatically — no restart
required.

---

## Upgrading

1. Pull the new code
2. Run any new migration SQL files as postgres if present
3. Restart the web app (or roll out new K8s deployment)
4. The collector does not need restarting for most upgrades

For K8s upgrades, update the image tag in your tenant values file and run
`helm upgrade`. The rolling update ensures zero downtime.
