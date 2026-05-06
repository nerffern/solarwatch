#!/usr/bin/env python3
"""
SolarWatch — collector.py

Single process, runs continuously — one pod in K8s or one systemd service.
Reads site and inverter config from the DB (sites table) and polls on a
fixed interval. Config changes made via the web UI are picked up automatically
every CONFIG_RELOAD seconds — no restart needed.

Supported inverter types:
  deye      → direct Modbus over WAN via Solarman V5 (pysolarmanv5)
  sunsynk   → cloud API at api.sunsynk.net
  victron   → [placeholder] VRM API or direct Modbus — config pending
  sungrow   → [placeholder] direct Modbus or SolarmanV5 — config pending

Environment variables (same as the web app — one .env works for both):
  DATABASE_URL     postgresql+psycopg2://user:pass@host:5432/solarwatch
  — or DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME —
  POLL_INTERVAL    seconds between inverter poll cycles (default: 60)
  MAX_RETRIES      retries per inverter per cycle (default: 3)
  RETRY_DELAY      seconds between retries (default: 5)
  CONFIG_RELOAD    seconds between site config reloads from DB (default: 300)
  WEATHER_INTERVAL seconds between weather polls (default: 900)

Development:
  cp .env.example .env   # same .env as the web app
  python collector.py

Production (K8s):
  The Helm chart deploys the collector as a separate Deployment using the same
  image. Command override: python collector.py
  DB credentials come from the shared solarwatch-secrets K8s Secret.

Systemd (bare metal):
  sudo cp solarwatch.service /etc/systemd/system/
  sudo systemctl enable --now solarwatch
"""

import os
import sys
import time
import signal
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

load_dotenv()

# ── LOGGING ───────────────────────────────────────────────────────────────────
# Log to stdout (captured by K8s / journald) and optionally a local file.
# File logging is skipped if the directory is not writable (read-only K8s pod).

_handlers = [logging.StreamHandler(sys.stdout)]
_log_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
try:
    os.makedirs(_log_dir, exist_ok=True)
    _handlers.append(logging.FileHandler(os.path.join(_log_dir, "collector.log")))
except OSError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_handlers,
)
log = logging.getLogger("collector")

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Uses the same env var naming as the web app so one .env works for both.

POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL",    "60"))
MAX_RETRIES      = int(os.getenv("MAX_RETRIES",      "3"))
RETRY_DELAY      = int(os.getenv("RETRY_DELAY",      "5"))
CONFIG_RELOAD    = int(os.getenv("CONFIG_RELOAD",    "300"))
WEATHER_INTERVAL = int(os.getenv("WEATHER_INTERVAL", "900"))


def _build_database_url() -> str:
    """Build the SQLAlchemy database URL — identical logic to app/db.py.

    Priority:
      1. DATABASE_URL (full SQLAlchemy URL)
      2. DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME (individual vars)
    """
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    host     = os.getenv("DB_HOST",     "localhost")
    port     = os.getenv("DB_PORT",     "5432")
    user     = os.getenv("DB_USER",     "solarwatch_user")
    password = os.getenv("DB_PASSWORD", "")
    name     = os.getenv("DB_NAME",     "solarwatch")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


# ── DATABASE ──────────────────────────────────────────────────────────────────
# Uses SQLAlchemy — same library as the web app, same connection string format.
# pool_pre_ping=True handles HA Postgres failover: a broken connection is
# detected and replaced transparently on the next query, no manual pool
# rebuild needed.

_engine: Optional[Engine] = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    """Return the shared SQLAlchemy engine, creating it if needed."""
    global _engine
    with _engine_lock:
        if _engine is None:
            url = _build_database_url()
            _engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=3,
                max_overflow=2,
                pool_timeout=30,
                connect_args={
                    "application_name": "solarwatch_collector",
                    "connect_timeout":  10,
                },
            )
            # Log host/db only — never log credentials
            host_part = url.split("@")[-1] if "@" in url else url
            log.info(f"DB engine ready → {host_part}")
    return _engine


@contextmanager
def db_session():
    """Context manager: yield a SQLAlchemy connection in a transaction.

    pool_pre_ping handles HA failover — if the connection is stale,
    SQLAlchemy replaces it before the query executes.
    """
    with get_engine().begin() as conn:
        yield conn


# ── SITE CONFIG LOADING ────────────────────────────────────────────────────────

def load_sites() -> dict[str, list[dict]]:
    """Load enabled sites from the DB, grouped by source_type.

    Returns a dict keyed by source_type:
      {
        "deye":    [{"site_name": "Selati", "inverters": [...], ...}],
        "sunsynk": [{"site_name": "Penguin", "sunsynk_username": ..., ...}],
        "victron": [{"site_name": "FarmXX", ...}],   # when added
        "sungrow": [],
      }
    """
    with db_session() as conn:
        rows = conn.execute(
            text(
                """
                SELECT site_name, display_name, source_type,
                       inverters, sunsynk_username, sunsynk_password, sunsynk_plant_id,
                       latitude, longitude
                FROM   sites
                WHERE  enabled = TRUE
                ORDER  BY source_type, site_name
                """
            )
        ).mappings().all()

    sites: dict[str, list] = {
        "deye": [], "sunsynk": [], "victron": [], "sungrow": []
    }
    for row in rows:
        d      = dict(row)
        source = d.get("source_type", "unknown")
        if source in sites:
            sites[source].append(d)
        else:
            log.warning(
                f"Unknown source_type '{source}' for site {d['site_name']} — skipping"
            )
    return sites


# ── DB WRITE HELPERS ──────────────────────────────────────────────────────────

_INSERT_SQL = text(
    """
    INSERT INTO solar_readings (
        time, site_name, source_type, inverter_name, inverter_sn,
        pv1_voltage, pv1_current, pv1_power,
        pv2_voltage, pv2_current, pv2_power,
        battery_voltage, battery_current, battery_power, battery_soc, battery_temp,
        grid_voltage, grid_frequency, grid_power, grid_current,
        load_power, load_voltage, inverter_temp, dc_temp,
        daily_pv_energy, total_pv_energy,
        daily_battery_charge, daily_battery_discharge,
        daily_grid_import, daily_grid_export, daily_load_energy,
        poll_duration_ms, poll_success, ct_power, ct_load_power
    ) VALUES (
        :time, :site_name, :source_type, :inverter_name, :inverter_sn,
        :pv1_voltage, :pv1_current, :pv1_power,
        :pv2_voltage, :pv2_current, :pv2_power,
        :battery_voltage, :battery_current, :battery_power, :battery_soc, :battery_temp,
        :grid_voltage, :grid_frequency, :grid_power, :grid_current,
        :load_power, :load_voltage, :inverter_temp, :dc_temp,
        :daily_pv_energy, :total_pv_energy,
        :daily_battery_charge, :daily_battery_discharge,
        :daily_grid_import, :daily_grid_export, :daily_load_energy,
        :poll_duration_ms, :poll_success, :ct_power, :ct_load_power
    )
    ON CONFLICT (time, site_name, inverter_name) DO NOTHING
    """
)

_WEATHER_INSERT_SQL = text(
    """
    INSERT INTO weather_readings (
        time, site_name,
        temp_c, feels_like_c, cloud_cover, precipitation,
        wind_speed, wind_direction, humidity,
        weather_code, uv_index, sunrise, sunset,
        solar_rad, is_day
    ) VALUES (
        :time, :site_name,
        :temp_c, :feels_like_c, :cloud_cover, :precipitation,
        :wind_speed, :wind_direction, :humidity,
        :weather_code, :uv_index, :sunrise, :sunset,
        :solar_rad, :is_day
    )
    ON CONFLICT DO NOTHING
    """
)


def write_reading(site_name: str, inv_name: str, inv_sn: str, data: dict):
    """Write one inverter reading to solar_readings."""
    row = {
        "time":          datetime.now(timezone.utc),
        "site_name":     site_name,
        "inverter_name": inv_name,
        "inverter_sn":   inv_sn,
        **data,
    }
    try:
        with db_session() as conn:
            conn.execute(_INSERT_SQL, row)
    except Exception as exc:
        log.error(f"[{site_name}/{inv_name}] DB write failed: {exc}")


def write_weather(data: dict):
    """Write one weather reading — strips internal _emoji/_description keys."""
    row = {k: v for k, v in data.items() if not k.startswith("_")}
    try:
        with db_session() as conn:
            conn.execute(_WEATHER_INSERT_SQL, row)
    except Exception as exc:
        log.error(f"[weather/{row.get('site_name')}] DB write failed: {exc}")


# ── DEYE POLLING ──────────────────────────────────────────────────────────────

import deye_worker
import victron_worker


def poll_deye_inverter_with_retry(inv: dict, site_name: str) -> Optional[dict]:
    """Poll one Deye inverter with up to MAX_RETRIES attempts. Returns the reading dict or None after all retries fail."""
    for attempt in range(1, MAX_RETRIES + 1):
        result = deye_worker.poll(inv, site_name)
        if result is not None:
            return result
        if attempt < MAX_RETRIES:
            log.warning(
                f"[{site_name}/{inv['name']}] Retry {attempt}/{MAX_RETRIES} "
                f"in {RETRY_DELAY}s..."
            )
            time.sleep(RETRY_DELAY)
    log.error(f"[{site_name}/{inv['name']}] All {MAX_RETRIES} attempts failed")
    return None


def poll_deye_sites(sites: list[dict]):
    """Poll all Deye inverter sites in sequence. Each site can have multiple inverters polled one after another."""
    for site in sites:
        site_name = site["site_name"]
        for inv in (site.get("inverters") or []):
            if not running:
                return
            if inv.get("inverter_sn", "").startswith("CONFIRM"):
                log.warning(
                    f"[{site_name}/{inv['name']}] Skipping — inverter_sn not yet confirmed"
                )
                continue
            data = poll_deye_inverter_with_retry(inv, site_name)
            if data:
                write_reading(site_name, inv["name"], inv["inverter_sn"], data)


# ── SUNSYNK POLLING ───────────────────────────────────────────────────────────

import sunsynk_worker

_sunsynk_clients: dict[str, sunsynk_worker.SunsynkClient] = {}


def _sync_sunsynk_clients(sunsynk_sites: list[dict]):
    """Keep client cache in sync with enabled Sunsynk sites."""
    active = {s["sunsynk_username"] for s in sunsynk_sites}
    for u in list(_sunsynk_clients):
        if u not in active:
            log.info(f"Removing Sunsynk client for {u} (site disabled or removed)")
            del _sunsynk_clients[u]
    for site in sunsynk_sites:
        u = site["sunsynk_username"]
        if u not in _sunsynk_clients:
            _sunsynk_clients[u] = sunsynk_worker.SunsynkClient(
                u, site["sunsynk_password"]
            )


def poll_sunsynk_sites(sites: list[dict]):
    """Poll all Sunsynk cloud sites. Uses cached SunsynkClient instances keyed by username to avoid re-authenticating every cycle."""
    for site in sites:
        site_name = site["site_name"]
        try:
            client   = _sunsynk_clients[site["sunsynk_username"]]
            readings = sunsynk_worker.poll(site, client)
            for reading in readings:
                if not running:
                    return
                write_reading(
                    site_name,
                    reading.pop("inverter_name"),
                    reading.pop("inverter_sn"),
                    reading,
                )
        except Exception as exc:
            log.error(f"[{site_name}] Sunsynk poll error: {exc}")


# ── VICTRON POLLING ───────────────────────────────────────────────────────────
# Placeholder — implementation pending inverter connection details.
#
# Planned approach (priority order):
#   1. Direct Modbus/TCP to the Victron Cerbo GX / CCGX local IP
#      Register map: https://github.com/victronenergy/venus-html5-app
#   2. VRM API (cloud) as fallback
#
# Config fields needed in sites.inverters JSONB:
#   { "name": "Victron_1", "ip": "192.168.x.x" }
# Or for VRM cloud fallback:
#   { "name": "Victron_1", "vrm_site_id": "xxx", "vrm_token": "xxx" }
#
# The poll result must return a dict matching the solar_readings schema
# (same keys as deye_worker.poll() returns).

def poll_victron_sites(sites: list[dict]):
    """Poll Victron sites via MQTT (Cerbo GX / CCGX direct connection).

    Each Victron site has one Cerbo GX that aggregates all MPPTs and battery
    monitors. One MQTT connection per site → one row in solar_readings per poll.

    The inverter config in sites.inverters should have exactly one entry:
      [{"name": "Victron_1", "mqtt_host": "10.0.1.80", "mqtt_port": 1883}]

    The Cerbo GX IP is read from the inverters JSONB config stored in the DB
    and set via the web UI (Sites → Edit → Add device).
    """
    for site in sites:
        if not running:
            return
        site_name = site["site_name"]
        inverters = site.get("inverters") or []

        if not inverters:
            log.warning(
                f"[{site_name}] Victron site has no inverter config — "
                f"add the Cerbo GX IP via the web UI: Sites → Edit → Add device."
            )
            continue

        inv = inverters[0]

        inv_name = inv.get("name", "Victron")

        for attempt in range(1, MAX_RETRIES + 1):
            data = victron_worker.poll(inv, site_name)
            if data is not None:
                # Use inv_name as both inverter_name and inverter_sn
                # (Victron sites have one logical inverter per Cerbo GX)
                write_reading(site_name, inv_name, inv_name, data)
                break
            if attempt < MAX_RETRIES:
                log.warning(
                    f"[{site_name}/{inv_name}] "
                    f"Retry {attempt}/{MAX_RETRIES} in {RETRY_DELAY}s..."
                )
                time.sleep(RETRY_DELAY)
        else:
            log.error(f"[{site_name}/{inv_name}] All {MAX_RETRIES} attempts failed")


# ── SUNGROW POLLING ───────────────────────────────────────────────────────────
# Placeholder — implementation pending inverter connection details.
#
# Planned approach (priority order):
#   1. Direct Modbus/TCP to the Sungrow inverter local IP
#      Sungrow uses SolarmanV5 protocol (same as Deye) with different register maps.
#      Register reference: SG-MODBUS-TCP protocol document
#   2. iSolarCloud API (cloud) as fallback
#
# Config fields needed in sites.inverters JSONB:
#   { "name": "Sungrow_1", "ip": "192.168.x.x",
#     "dongle_serial": 12345678, "inverter_sn": "B2200...", "model": "SH10RT" }

def poll_sungrow_sites(sites: list[dict]):
    """Poll Sungrow inverter sites — placeholder, implementation pending."""
    for site in sites:
        log.warning(
            f"[{site['site_name']}] Sungrow polling not yet implemented. "
            f"Add sungrow_worker.py and wire it here when inverter details are available."
        )


# ── WEATHER POLLING ───────────────────────────────────────────────────────────

import weather_worker

_last_weather: dict[str, float] = {}
_weather_lock = threading.Lock()


def _weather_thread_fn(sites: list[dict]):
    """Background thread function — fetch weather for each site that has coordinates set and hasn't been updated within WEATHER_INTERVAL seconds."""
    now = time.monotonic()
    for site in sites:
        site_name = site["site_name"]
        lat       = site.get("latitude")
        lon       = site.get("longitude")
        if lat is None or lon is None:
            continue
        with _weather_lock:
            last = _last_weather.get(site_name, 0)
        if now - last < WEATHER_INTERVAL:
            continue
        data = weather_worker.fetch(site_name, float(lat), float(lon))
        if data:
            write_weather(data)
            with _weather_lock:
                _last_weather[site_name] = now


def poll_weather_async(all_sites: list[dict]):
    """Fire weather polling in a background thread — never blocks poll cycle."""
    threading.Thread(
        target=_weather_thread_fn,
        args=(all_sites,),
        daemon=True,
        name="weather-poll",
    ).start()


# ── GRACEFUL SHUTDOWN ─────────────────────────────────────────────────────────

running = True


def handle_signal(sig, frame):
    """Signal handler for SIGTERM and SIGINT — sets the running flag to False so the main loop exits cleanly after the current poll."""
    global running
    log.info(f"Signal {sig} — shutting down gracefully...")
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT,  handle_signal)


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    """Main collector loop — verifies DB, loads site config, polls all inverter types every POLL_INTERVAL seconds. Runs until SIGTERM/SIGINT."""
    log.info("=" * 60)
    log.info("SolarWatch Collector starting")
    host_part = _build_database_url().split("@")[-1]
    log.info(f"DB              : {host_part}")
    log.info(f"Poll interval   : {POLL_INTERVAL}s")
    log.info(f"Config reload   : every {CONFIG_RELOAD}s")
    log.info(f"Weather interval: every {WEATHER_INTERVAL}s")
    log.info("=" * 60)

    try:
        with db_session() as conn:
            conn.execute(text("SELECT 1"))
        log.info("DB connection verified")
    except Exception as exc:
        log.error(f"DB connection failed at startup: {exc}")
        sys.exit(1)

    sites_by_type: dict[str, list] = {
        "deye": [], "sunsynk": [], "victron": [], "sungrow": []
    }
    last_cfg_load = 0

    while running:
        cycle_start = time.monotonic()

        # Reload site config periodically
        if time.monotonic() - last_cfg_load > CONFIG_RELOAD:
            try:
                sites_by_type = load_sites()
                last_cfg_load = time.monotonic()
                log.info("Sites loaded:")
                for src, sites in sites_by_type.items():
                    if sites:
                        log.info(f"  {src}: {[s['site_name'] for s in sites]}")
                _sync_sunsynk_clients(sites_by_type["sunsynk"])
            except Exception as exc:
                log.error(f"Failed to load sites: {exc}")

        if sites_by_type["deye"]:
            poll_deye_sites(sites_by_type["deye"])

        if sites_by_type["sunsynk"]:
            poll_sunsynk_sites(sites_by_type["sunsynk"])

        if sites_by_type["victron"]:
            poll_victron_sites(sites_by_type["victron"])

        if sites_by_type["sungrow"]:
            poll_sungrow_sites(sites_by_type["sungrow"])

        all_sites = [s for lst in sites_by_type.values() for s in lst]
        if all_sites:
            poll_weather_async(all_sites)

        elapsed    = time.monotonic() - cycle_start
        sleep_time = max(0, POLL_INTERVAL - elapsed)
        log.debug(f"Cycle done in {elapsed:.1f}s — sleeping {sleep_time:.1f}s")

        deadline = time.monotonic() + sleep_time
        while running and time.monotonic() < deadline:
            time.sleep(1)

    global _engine
    if _engine:
        _engine.dispose()
    log.info("SolarWatch collector stopped cleanly")


if __name__ == "__main__":
    main()
