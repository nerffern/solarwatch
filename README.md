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
  Open-Meteo (free, no API key).
- **Monthly totals** — cumulative PV kWh and grid import for the current month.
- **Multi-site** — switch between sites with the site selector. Each site runs
  independent inverters and has its own data stream.
- **Public share links** — generate a read-only URL for any site. No login
  required. Installable as a PWA on phones and screens.

---

## Architecture

```
Inverters (Deye / Sunsynk)
        │
        ▼
  collector.py  ──── systemd service, runs continuously
        │             polls every 60s, writes to PostgreSQL
        ▼
  PostgreSQL (HA cluster)
        │
        ▼
  FastAPI web app  ──── serves dashboard + admin UI + share links
        │
        ▼
  Browser / PWA
```

The collector and web app are **independent processes** sharing one database.
The collector has no knowledge of the web app and vice versa. Restarting or
deploying either one does not affect the other.

---

## Quick start

### Prerequisites
- Python 3.11+
- PostgreSQL database with the SolarWatch schema (run `setup.sql` as postgres)
- Inverters reachable over the network (Deye) or Sunsynk cloud credentials

### Local development

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — see Environment variables section below

# 3. Start the web app
python run.py
# Visit http://localhost:8000

# 4. Start the collector (separate terminal)
python collector.py
```

### Production (systemd)

```bash
# Web app
sudo cp solarwatch.service /etc/systemd/system/
sudo systemctl enable --now solarwatch

# Collector
sudo cp solarwatch-powerflow.service /etc/systemd/system/
sudo systemctl enable --now solarwatch-powerflow
```

---

## Database setup

Run `setup.sql` once as the postgres superuser to create the database, user,
tables, indexes, and seed your sites:

```bash
psql -h your-postgres-host -U postgres -f setup.sql
```

For subsequent schema additions, run the matching migration file:

```bash
# Add share_token column (required for public share links)
psql -h your-postgres-host -U postgres -d solarwatch -f migrate_share.sql

# Add performance indexes (recommended)
psql -h your-postgres-host -U postgres -d solarwatch -f migrate_indexes.sql

# Add weather table (if upgrading from an older version)
psql -h your-postgres-host -U postgres -d solarwatch -f migrate_weather.sql
```

The web app creates `roles`, `web_users`, and `user_sites` automatically on
first start — those tables are owned by the app user. All other tables
(`sites`, `solar_readings`, `weather_readings`) are owned by the postgres
superuser and require the migration SQL files for schema changes.

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | **Yes** | — | Signs session cookies. Must be identical across all replicas. Generate: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `APP_ADMIN_USERNAME` | **Yes** | — | Bootstrap admin username |
| `APP_ADMIN_PASSWORD` | **Yes** | — | Bootstrap admin password — forced to change on first login |
| `APP_ENV` | No | `development` | Set `production` for strict CSP, HSTS, HTTPS-only cookies |
| `DATABASE_URL` | Yes* | — | `postgresql+psycopg2://user:pass@host:5432/solarwatch` |
| `DB_HOST` | Yes* | `127.0.0.1` | Alternative to `DATABASE_URL` |
| `DB_PORT` | Yes* | `5432` | |
| `DB_USER` | Yes* | `solarwatch_user` | |
| `DB_PASSWORD` | Yes* | — | |
| `DB_NAME` | Yes* | `solarwatch` | |
| `APP_PROXY_FIX` | No | `false` | Set `true` behind Traefik / ingress-nginx |
| `APP_ADMIN_RESET_PASSWORD` | No | `false` | Set `true` temporarily to reset the admin password |
| `RATE_LIMIT_LOGIN` | No | `10/minute` | Login attempt limit per IP |
| `PG_HOST` | — | — | Collector DB host (separate from web app config) |
| `PG_PORT` | — | `5432` | |
| `PG_DB` | — | `solarwatch` | |
| `PG_USER` | — | `solarwatch_user` | |
| `PG_PASS` | — | — | |
| `POLL_INTERVAL` | — | `60` | Collector poll interval in seconds |
| `WEATHER_INTERVAL` | — | `900` | Weather poll interval in seconds (15 min) |

---

## Managing the app via the web UI

### First login

Sign in at `/auth/login` with the credentials set in `APP_ADMIN_USERNAME` and
`APP_ADMIN_PASSWORD`. You will be prompted to change the password immediately.

### Sites — `/sites`

The Sites page is the main configuration hub. Each site represents one
physical installation with one or more inverters.

**Viewing sites:** The list shows all sites, their type (Deye or Sunsynk),
location, inverter count, and enabled status.

**Adding a site:**
1. Click **Add site**
2. Enter the site name (internal identifier, cannot change later), display
   name, source type, location, and GPS coordinates
3. For Deye sites: add inverters after creation (see below)
4. For Sunsynk sites: enter username, password, and plant ID

**Editing a site:**
Click **Edit** on any site to open the full configuration page, which has:

- **Site details** — display name, location, coordinates, enabled toggle
- **Inverters** (Deye sites only) — add or remove inverters. Each inverter
  needs a name, IP address, dongle serial, and inverter serial number. The
  collector picks up changes within 5 minutes (next config reload).
- **Sunsynk credentials** — update cloud API credentials. Leave password
  blank to keep the existing one.
- **User access** — assign site_admin or site_viewer users to this site
- **Share link** — generate a public read-only URL (see below)
- **Danger zone** — delete the site (admin password required)

**Enabling / disabling a site:** Disabled sites stop being polled by the
collector (on next config reload) and are hidden from the dashboard.

### Users — `/auth/admin/users`

**Creating a user:**
1. Enter username, password, and assign a role
2. Click **Create user** — the user is created enabled and must change their
   password on first login

**Roles:**
- `admin` — full access to all sites, all admin pages
- `site_admin` — can manage their assigned sites and view all data for those sites
- `site_viewer` — read-only access to assigned sites
- `user` — read-only access to assigned sites (same as site_viewer)

**Assigning users to sites:** Done from the site edit page, not the user page.
Go to Sites → Edit site → User access section.

**Managing users:**
- **Update role** — change role from the dropdown in the users table
- **Reset password** — enter a new password and click Reset
- **Disable/Enable** — prevents login without deleting the account
- **Delete** — requires admin password confirmation

### Roles — `/auth/admin/roles`

System roles (`admin`, `user`, `site_admin`, `site_viewer`) cannot be deleted
or disabled — they are recreated on every startup. Custom roles can be created
for future use.

---

## Public share links (PWA-ready)

Share links give **read-only access** to a single site's live dashboard
without requiring login. They are designed for kitchen screens, client
dashboards, and mobile home screen installs.

### Generating a share link

1. Go to **Sites** → click **Edit** on the site you want to share
2. Scroll to **Public share link**
3. Click **Generate share link**
4. Copy the URL or click **Preview ↗** to test it

### What the share link shows

The full power flow dashboard — same live data, same charts, same weather —
but locked to that one site. The site selector is disabled. The admin button
is hidden. No account or login needed.

### Installing as a PWA

The share link is a fully installable Progressive Web App:

**On Android (Chrome):**
1. Open the share link in Chrome
2. Tap the three-dot menu → **Add to Home screen**
3. Confirm — it installs with the site name (e.g. "Selati — SolarWatch")
4. Opens full-screen, no browser chrome, auto-refreshes every 10 seconds

**On iOS (Safari):**
1. Open the share link in Safari
2. Tap the Share button → **Add to Home Screen**
3. Confirm the name and tap **Add**
4. Opens as a standalone app

**On a kitchen monitor or TV screen:**
Open the share link in Chrome in kiosk mode for a completely hands-off display:
```bash
chromium-browser --kiosk --app=https://your-domain/share/your-token
```

### Revoking a share link

Click **Revoke link** on the site edit page. The URL stops working immediately.
Generating a new link creates a fresh token — the old URL is permanently dead.

### Security properties

- 256 bits of entropy per token — brute force is not feasible
- Read-only — no mutations are possible through the share API
- Rate limited — 30 requests per minute per IP
- Single-site scope — a token only ever exposes one site's data
- Instant revocation — clearing the token in the database kills all access

---

## Kubernetes / production deployment

```yaml
env:
  - name: SECRET_KEY
    valueFrom:
      secretKeyRef: { name: solarwatch-secrets, key: secret-key }
  - name: DATABASE_URL
    valueFrom:
      secretKeyRef: { name: solarwatch-secrets, key: database-url }
  - name: APP_ENV
    value: "production"
  - name: APP_PROXY_FIX
    value: "true"
```

`SECRET_KEY` must be **identical across all pod replicas and all datacentres**.
Session cookies are validated against it on every request — a mismatched key
logs users out.

Health probe: `GET /health` always returns `200 {"status": "ok"}`.

---

## Collector

`collector.py` runs as a separate process and is the only component that
talks to inverters. It:

- Loads site and inverter config from the `sites` table every 5 minutes
- Polls Deye inverters directly via Solarman V5 Modbus over WAN
- Polls Sunsynk inverters via the Sunsynk cloud API
- Polls weather via Open-Meteo in a background thread (no API key needed)
- Writes all readings to `solar_readings` and `weather_readings`
- Reconnects automatically after HA PostgreSQL failover

Config changes made in the web UI (adding/removing inverters, updating
credentials, disabling sites) are picked up by the collector on its next
config reload — no restart needed.

---

## Database schema summary

| Table | Owner | Created by |
|---|---|---|
| `sites` | postgres | `setup.sql` |
| `solar_readings` | postgres | `setup.sql` |
| `weather_readings` | postgres | `setup.sql` |
| `roles` | solarwatch_user | App startup |
| `web_users` | solarwatch_user | App startup |
| `user_sites` | solarwatch_user | App startup |

Schema changes to postgres-owned tables require running the migration SQL
files as the postgres superuser. Schema changes to app-owned tables are
handled automatically by startup.
