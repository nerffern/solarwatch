# Adding a site to SolarWatch

This guide walks through adding each supported inverter type via the web UI.
All configuration is stored in the database — the collector picks it up
within 5 minutes, no restart required.

---

## Before you start

You need:
- Admin login to the SolarWatch web UI
- For **Deye**: the local IP address of each inverter's Solarman WiFi dongle,
  plus the dongle serial number and inverter serial number (on the label)
- For **Sunsynk**: your Sunsynk cloud account username, password, and plant ID
- For **Victron**: the local IP address of the Cerbo GX (find it in your
  router's DHCP table or the Cerbo GX screen under Settings → Ethernet)

---

## Deye inverter

Deye inverters communicate via their Solarman V5 WiFi dongle directly over
your local network — no cloud account needed.

**Steps:**

1. Go to **Sites → Add site**
2. Fill in:
   - **Site name** — internal ID, lowercase, no spaces (e.g. `selati`) — cannot be changed later
   - **Display name** — shown on the dashboard (e.g. `Selati`)
   - **Source type** — select `Deye — Solarman V5 (direct)`
   - **Location** — free text (e.g. `60 Selati Street, Alphen Park`)
   - **Latitude / Longitude** — decimal degrees, used for weather data
3. Click **Create site** — you'll land on the edit page
4. Under **Inverters**, click **Add inverter** for each physical inverter:
   - **Name** — label for logs (e.g. `Inverter_1`)
   - **IP address** — the dongle's local IP (e.g. `192.168.1.100`)
   - **Dongle serial** — 10-digit number on the dongle label (e.g. `2705000422`)
   - **Inverter SN** — serial number on the inverter nameplate (e.g. `2208262350`)
5. Set the site to **Enabled** if it isn't already
6. The collector will pick up the new site within 5 minutes

**Checking it works:**
```bash
sudo journalctl -u solarwatch-collector -f
# Look for:  [Selati/Inverter_1] SOC=45.0% PV=3200W ...
```

**Troubleshooting:**
- `Poll failed: No socket available` — the dongle IP is wrong or unreachable.
  Ping the IP from the collector server to verify.
- `Connection timeout` — firewall blocking port 8899. The dongle uses TCP 8899.
- Only one connection at a time is allowed per dongle — if the Solarman app
  is open it may block the collector.

---

## Sunsynk inverter

Sunsynk inverters are polled via the Sunsynk cloud API — no local network
access required.

**Steps:**

1. Go to **Sites → Add site**
2. Fill in:
   - **Site name** — internal ID (e.g. `penguin`)
   - **Display name** — shown on the dashboard (e.g. `Penguin`)
   - **Source type** — select `Sunsynk — Cloud API`
   - **Location** and **Latitude / Longitude**
3. Click **Create site**
4. Under **Sunsynk credentials**, enter:
   - **Username** — your Sunsynk app login email
   - **Password** — your Sunsynk app password
   - **Plant ID** — found in the Sunsynk app under your plant settings,
     or from the URL when viewing your plant (`/plant/XXXXXXXX`)
5. Click **Save credentials**
6. Enable the site

**Finding your Plant ID:**
- Log in to [home.sunsynk.net](https://home.sunsynk.net)
- Click your plant — the URL contains the plant ID:
  `https://home.sunsynk.net/monitor/plant/12345678`
- The plant ID is `12345678`

**Checking it works:**
```bash
sudo journalctl -u solarwatch-collector -f
# Look for:  [Penguin/Inverter_1] SOC=65.0% PV=4200W ...
```

**Troubleshooting:**
- `Sunsynk login failed` — wrong username or password. Verify you can log in
  to the Sunsynk app with these credentials.
- `Plant not found` — wrong plant ID. Double-check from the URL above.
- Sunsynk cloud rate-limits requests. The collector caches the auth token
  and reuses it — if you see repeated login attempts, check for errors.

---

## Victron (Cerbo GX / CCGX)

Victron inverters are polled via MQTT directly from the Cerbo GX on your
local network. No VRM cloud account or internet connection needed.

**Prerequisites:**
- The Cerbo GX must be on the same network as the SolarWatch server
- MQTT must be enabled on the Cerbo GX:
  - Cerbo GX screen → Settings → Services → MQTT on LAN → Enabled
  - Or via VRM: Device list → Cerbo GX → Settings → Services → MQTT on LAN

**Steps:**

1. Go to **Sites → Add site**
2. Fill in:
   - **Site name** — internal ID (e.g. `harmonia`)
   - **Display name** — shown on the dashboard (e.g. `Harmonia`)
   - **Source type** — select `Victron — MQTT (Cerbo GX / CCGX)`
   - **Location** and **Latitude / Longitude**
3. Click **Create site**
4. Under **Victron device — Cerbo GX / CCGX**, click **Add Cerbo GX**:
   - **Name** — label for logs (e.g. `Victron_1`)
   - **Cerbo GX IP address** — the local IP of the Cerbo GX (e.g. `10.0.1.80`)
     Leave serial fields blank — they are not used for Victron.
5. Enable the site
6. The collector will connect via MQTT within 5 minutes

**For sites with multiple inverters in parallel (e.g. 2× Quattro 5kW):**
You still add only **one** device entry. The Cerbo GX aggregates all
connected inverters, MPPTs, and battery monitors into a single set of
MQTT topics. The collector reads the aggregated totals — `pv1_power` shows
the combined PV from all MPPTs, `load_power` shows the total AC load.

**Checking it works:**
```bash
sudo journalctl -u solarwatch-collector -f
# Look for:  [Harmonia/Victron_1] SOC=72.0% BattV=54.1V PV=8500W ...
```

**Troubleshooting:**
- `No MQTT host configured` — the Cerbo GX IP was not saved. Go to
  Sites → Edit → Add Cerbo GX and enter the IP.
- `Connection timeout` — Cerbo GX unreachable. Check:
  - Is the Cerbo GX on the same network segment as the server?
  - `ping <cerbo-ip>` from the server
  - MQTT enabled on the Cerbo GX (Settings → Services → MQTT on LAN)
  - Firewall not blocking port 1883

---

## After adding a site

1. Wait up to 5 minutes for the collector to pick up the new config
2. Watch the collector logs: `sudo journalctl -u solarwatch-collector -f`
3. Once you see readings in the log, open the dashboard and select the site
4. If the site is for other users, go to **Sites → Edit → User access** and
   assign them

## Renaming a site

The `site_name` field (internal ID) cannot be changed via the UI — it is used
as a foreign key across multiple tables. To rename it, run these SQL queries
as `solarwatch_user` or `postgres`:

```sql
BEGIN;
UPDATE sites          SET site_name = 'new_name', display_name = 'New Display' WHERE site_name = 'old_name';
UPDATE solar_readings SET site_name = 'new_name' WHERE site_name = 'old_name';
UPDATE weather_readings SET site_name = 'new_name' WHERE site_name = 'old_name';
-- user_sites links by site_id (integer) — no update needed there
COMMIT;
```

## Disabling vs deleting a site

**Disabling** (`Enabled` toggle off) stops the collector from polling the site
and hides it from the dashboard. All historical data is kept. Re-enable at any
time.

**Deleting** (Danger zone) removes the site record and user assignments.
Solar readings and weather data are kept — they are owned by the postgres
superuser and are not cascaded.
