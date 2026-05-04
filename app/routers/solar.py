"""SolarWatch — solar data API routes.

All endpoints ported from powerflow_server.py. Key changes from the original:
  - asyncpg replaced with SQLAlchemy get_connection() — one DB library throughout
  - Auth added: login_required on all routes (dashboard + API)
  - TTL cache kept — same logic, same TTLs, same ~80% DB hit reduction
  - Routes live at /api/solar/* to avoid clashing with future template routes

Endpoints:
    GET /dashboard              → full-screen power flow page (login required)
    GET /api/solar/sites        → list of enabled sites
    GET /api/solar/flow         → live power data for a site
    GET /api/solar/weather      → latest weather reading for a site
    GET /api/solar/monthly      → this month's PV and grid kWh totals
    GET /api/solar/chart/{type} → chart data (pv, load, battery, grid, daily, temps, peaks)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.auth import login_required, get_accessible_sites, require_site_access
from app.db import get_connection
from app.routers.auth import _consume_flash

log = logging.getLogger(__name__)

router = APIRouter(tags=["solar"])
templates = Jinja2Templates(directory="app/templates")

# ---------------------------------------------------------------------------
# TTL cache — identical to powerflow_server.py
# Keyed by (endpoint, site). Cuts DB hits by ~80% on multi-browser deployments.
#   /api/solar/flow    → 10 s  (matches frontend poll interval)
#   /api/solar/weather → 60 s
#   /api/solar/monthly → 120 s
#   /api/solar/chart/* → 60 s
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str, ttl: float) -> Optional[Any]:
    entry = _cache.get(key)
    if entry and (time.monotonic() - entry[0]) < ttl:
        return entry[1]
    return None


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)


# ---------------------------------------------------------------------------
# DB query helpers — synchronous wrappers around get_connection()
# ---------------------------------------------------------------------------

def _query_one(sql: str, params: dict) -> dict:
    """Execute SQL and return first row as dict, or {}."""
    with get_connection() as conn:
        row = conn.execute(text(sql), params).mappings().first()
        return dict(row) if row else {}


def _query_all(sql: str, params: dict) -> list[dict]:
    """Execute SQL and return all rows as list of dicts."""
    with get_connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Business logic — ported directly from powerflow_server.py
# ---------------------------------------------------------------------------

def _get_sites() -> list[dict]:
    rows = _query_all(
        "SELECT site_name, display_name FROM sites WHERE enabled = TRUE ORDER BY display_name",
        {},
    )
    return [{"name": r["site_name"], "display": r["display_name"]} for r in rows]


def _resolve_site(site: Optional[str], accessible: list[str]) -> Optional[str]:
    """Return validated site name from the user's accessible sites.

    If site is given, verify it is in the user's accessible list.
    If no site is given, return the first accessible site.
    """
    if site:
        # Case-insensitive match
        match = next((s for s in accessible if s.lower() == site.lower()), None)
        return match  # None if not accessible — caller raises 400/403
    return accessible[0] if accessible else None


def _get_flow(site: str) -> dict:
    key = f"flow:{site}"
    cached = _cache_get(key, ttl=10)
    if cached is not None:
        return cached

    row = _query_one(
        """
        SELECT
            COALESCE(SUM(pv1_power + COALESCE(pv2_power,0)), 0)::int  AS solar_w,
            COALESCE(SUM(battery_power), 0)::int                       AS battery_w,
            COALESCE(SUM(grid_power),    0)::int                       AS grid_w,
            COALESCE(SUM(load_power),    0)::int                       AS load_w,
            COALESCE(AVG(battery_soc),   0)::numeric(5,1)              AS soc,
            COALESCE(AVG(battery_temp),  0)::numeric(5,1)              AS batt_temp,
            COALESCE(AVG(battery_voltage),0)::numeric(5,2)             AS batt_v,
            COALESCE(AVG(grid_voltage),  0)::numeric(5,1)              AS grid_v,
            COALESCE(AVG(grid_frequency),0)::numeric(5,2)              AS grid_hz,
            MAX(time)                                                   AS last_poll
        FROM (
            SELECT DISTINCT ON (inverter_name)
                inverter_name,
                pv1_power, pv2_power,
                battery_power, battery_soc, battery_temp, battery_voltage,
                grid_power, grid_voltage, grid_frequency,
                load_power, time
            FROM solar_readings
            WHERE site_name ILIKE :site
            AND time > NOW() - INTERVAL '10 minutes'
            ORDER BY inverter_name, time DESC
        ) latest
        """,
        {"site": site},
    )

    if not row or row.get("solar_w") is None:
        return {"error": "No recent data", "site": site}

    # Calculate data age in seconds
    last_poll = row.get("last_poll")
    age_s = None
    if last_poll:
        if hasattr(last_poll, "tzinfo") and last_poll.tzinfo is None:
            last_poll = last_poll.replace(tzinfo=timezone.utc)
        age_s = int((datetime.now(timezone.utc) - last_poll).total_seconds())

    d = {
        "site":      site,
        "solar_w":   int(row["solar_w"]   or 0),
        "batt_w":    int(row["battery_w"] or 0),
        "grid_w":    int(row["grid_w"]    or 0),
        "load_w":    int(row["load_w"]    or 0),
        "soc":       float(row["soc"]       or 0),
        "batt_temp": float(row["batt_temp"] or 0),
        "batt_v":    float(row["batt_v"]    or 0),
        "grid_v":    float(row["grid_v"]    or 0),
        "grid_hz":   float(row["grid_hz"]   or 0),
        "age_s":     age_s,
        "stale":     age_s is not None and age_s > 300,
    }

    # Daily counters — same query as powerflow_server.py
    # Starts at DATE_TRUNC('day') + 1 hour because Deye resets counters
    # between 00:00–01:00 SAST. Starting at 01:00 avoids the reset window.
    try:
        daily_row = _query_one(
            """
            SELECT
              COALESCE(
                MAX(load_val) FILTER (WHERE grid_val > 0),
                MAX(load_val)
              ) as load_kwh,
              MAX(grid_val)  as grid_kwh,
              SUM(pv_val)    as pv_kwh
            FROM (
              SELECT DISTINCT ON (inverter_name)
                inverter_name,
                daily_load_energy  as load_val,
                daily_grid_import  as grid_val,
                daily_pv_energy    as pv_val
              FROM solar_readings
              WHERE site_name ILIKE :site
              AND time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                           AT TIME ZONE 'Africa/Johannesburg' + INTERVAL '1 hour'
              AND daily_load_energy > 0
              AND daily_load_energy < 200
              ORDER BY inverter_name, time DESC
            ) sub
            """,
            {"site": site},
        )
        load = float(daily_row.get("load_kwh") or 1)
        grid = float(daily_row.get("grid_kwh") if daily_row.get("grid_kwh") is not None else 0)
        pv   = float(daily_row.get("pv_kwh") or 0)
        d["self_suff"]      = max(0, min(100, round((1 - grid / max(load, 0.001)) * 100)))
        d["daily_load_kwh"] = round(load, 1)
        d["daily_grid_kwh"] = round(grid, 1)
        d["daily_pv_kwh"]   = round(pv, 1)
    except Exception as e:
        d["self_suff"] = 0
        log.warning(f"Daily counters error: {e}")

    _cache_set(key, d)
    return d


def _get_monthly(site: str) -> dict:
    key = f"monthly:{site}"
    cached = _cache_get(key, ttl=120)
    if cached is not None:
        return cached

    pv_row = _query_one(
        """
        SELECT COALESCE(SUM(eod_pv), 0) AS month_pv_kwh
        FROM (
          SELECT DISTINCT ON (DATE(time AT TIME ZONE 'Africa/Johannesburg'), inverter_name)
            inverter_name,
            daily_pv_energy AS eod_pv
          FROM solar_readings
          WHERE DATE_TRUNC('month', time AT TIME ZONE 'Africa/Johannesburg')
                = DATE_TRUNC('month', NOW() AT TIME ZONE 'Africa/Johannesburg')
          AND site_name ILIKE :site
          AND daily_pv_energy IS NOT NULL
          AND daily_pv_energy > 0
          ORDER BY DATE(time AT TIME ZONE 'Africa/Johannesburg'), inverter_name, time DESC
        ) sub
        """,
        {"site": site},
    )

    grid_row = _query_one(
        """
        SELECT COALESCE(SUM(day_grid), 0) AS month_grid_kwh
        FROM (
          SELECT
            DATE(time AT TIME ZONE 'Africa/Johannesburg') AS day,
            MAX(daily_grid_import) FILTER (WHERE daily_grid_import > 0) AS day_grid
          FROM solar_readings
          WHERE DATE_TRUNC('month', time AT TIME ZONE 'Africa/Johannesburg')
                = DATE_TRUNC('month', NOW() AT TIME ZONE 'Africa/Johannesburg')
          AND site_name ILIKE :site
          AND daily_grid_import IS NOT NULL
          AND daily_grid_import BETWEEN 0.01 AND 9000
          GROUP BY 1
        ) sub
        """,
        {"site": site},
    )

    result = {
        "month_pv_kwh":   round(float(pv_row.get("month_pv_kwh")   or 0), 1),
        "month_grid_kwh": round(float(grid_row.get("month_grid_kwh") or 0), 1),
    }
    _cache_set(key, result)
    return result


# WMO weather interpretation codes — same mapping as powerflow_server.py
_WMO = {
    0:  ("☀️",  "Clear sky"),      1:  ("🌤️", "Mainly clear"),
    2:  ("⛅",  "Partly cloudy"),   3:  ("☁️",  "Overcast"),
    45: ("🌫️", "Foggy"),           48: ("🌫️", "Icy fog"),
    51: ("🌦️", "Light drizzle"),   53: ("🌦️", "Moderate drizzle"),
    55: ("🌧️", "Dense drizzle"),   61: ("🌧️", "Slight rain"),
    63: ("🌧️", "Moderate rain"),   65: ("🌧️", "Heavy rain"),
    71: ("🌨️", "Slight snow"),     73: ("🌨️", "Moderate snow"),
    75: ("❄️",  "Heavy snow"),      80: ("🌦️", "Slight showers"),
    81: ("🌧️", "Moderate showers"), 82: ("⛈️", "Violent showers"),
    95: ("⛈️",  "Thunderstorm"),    96: ("⛈️", "T-storm w/ hail"),
    99: ("⛈️",  "T-storm heavy hail"),
}


def _get_weather(site: str) -> dict:
    key = f"weather:{site}"
    cached = _cache_get(key, ttl=60)
    if cached is not None:
        return cached

    row = _query_one(
        """
        SELECT
            temp_c, feels_like_c, cloud_cover, precipitation,
            wind_speed, wind_direction, humidity,
            weather_code, uv_index, sunrise, sunset,
            solar_rad, is_day, time AS last_updated
        FROM weather_readings
        WHERE site_name ILIKE :site
        ORDER BY time DESC
        LIMIT 1
        """,
        {"site": site},
    )

    if not row:
        return {"error": "No weather data yet", "site": site}

    code = row.get("weather_code")
    emoji, desc = _WMO.get(code, ("🌡️", f"Code {code}"))
    row["emoji"]       = emoji
    row["description"] = desc

    # Convert datetime objects to ISO strings for JSON serialisation
    for key_name in ("sunrise", "sunset", "last_updated"):
        val = row.get(key_name)
        if val and hasattr(val, "isoformat"):
            row[key_name] = val.isoformat()

    result = dict(row)
    _cache_set(key, result)
    return result


def _get_chart(chart: str, site: str) -> Any:
    key = f"chart:{chart}:{site}"
    cached = _cache_get(key, ttl=60)
    if cached is not None:
        return cached

    # All chart queries ported directly from powerflow_server.py get_chart()
    # SQLAlchemy named params (:site) replace asyncpg positional params ($1)

    if chart == "pv":
        per_inv = _query_all(
            """
            SELECT DATE_TRUNC('minute', time) as time,
              inverter_name,
              AVG(pv1_power + COALESCE(pv2_power,0)) as pv_w
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE :site
            GROUP BY 1, 2 ORDER BY 1
            """,
            {"site": site},
        )
        combined = _query_all(
            """
            SELECT minute as time, SUM(avg_pv) as combined_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(pv1_power + COALESCE(pv2_power,0)) as avg_pv
              FROM solar_readings
              WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                            AT TIME ZONE 'Africa/Johannesburg'
              AND site_name ILIKE :site GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
            """,
            {"site": site},
        )
        result = {"per_inv": _serialise(per_inv), "combined": _serialise(combined)}

    elif chart == "load":
        per_inv = _query_all(
            """
            SELECT DATE_TRUNC('minute', time) as time,
              inverter_name, AVG(load_power) as load_w
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE :site
            GROUP BY 1, 2 ORDER BY 1
            """,
            {"site": site},
        )
        combined = _query_all(
            """
            SELECT minute as time, SUM(avg_load) as combined_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(load_power) as avg_load
              FROM solar_readings
              WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                            AT TIME ZONE 'Africa/Johannesburg'
              AND site_name ILIKE :site GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
            """,
            {"site": site},
        )
        result = {"per_inv": _serialise(per_inv), "combined": _serialise(combined)}

    elif chart == "battery":
        power = _query_all(
            """
            SELECT minute as time, SUM(avg_batt) as batt_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(battery_power) as avg_batt
              FROM solar_readings
              WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                            AT TIME ZONE 'Africa/Johannesburg'
              AND site_name ILIKE :site GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
            """,
            {"site": site},
        )
        soc = _query_all(
            """
            SELECT DATE_TRUNC('minute', time) as time,
              AVG(battery_soc) as soc
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE :site AND battery_soc IS NOT NULL
            GROUP BY 1 ORDER BY 1
            """,
            {"site": site},
        )
        result = {"power": _serialise(power), "soc": _serialise(soc)}

    elif chart == "grid":
        power = _query_all(
            """
            SELECT minute as time, SUM(avg_grid) as grid_w
            FROM (
              SELECT DATE_TRUNC('minute', time) as minute,
                inverter_name, AVG(grid_power) as avg_grid
              FROM solar_readings
              WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                            AT TIME ZONE 'Africa/Johannesburg'
              AND site_name ILIKE :site GROUP BY 1, 2
            ) sub GROUP BY minute ORDER BY minute
            """,
            {"site": site},
        )
        voltage = _query_all(
            """
            SELECT DATE_TRUNC('minute', time) as time,
              AVG(grid_voltage) as grid_v
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE :site AND grid_voltage IS NOT NULL
            GROUP BY 1 ORDER BY 1
            """,
            {"site": site},
        )
        result = {"power": _serialise(power), "voltage": _serialise(voltage)}

    elif chart == "daily":
        rows = _query_all(
            """
            SELECT day,
              SUM(eod_pv)   as pv,   MAX(eod_load) as load,
              MAX(eod_grid) as grid, SUM(eod_chg)  as chg,
              SUM(eod_dis)  as dis
            FROM (
              SELECT DISTINCT ON (DATE(time AT TIME ZONE 'Africa/Johannesburg'), inverter_name)
                DATE(time AT TIME ZONE 'Africa/Johannesburg')::text as day,
                inverter_name,
                daily_pv_energy         as eod_pv,
                daily_load_energy       as eod_load,
                daily_grid_import       as eod_grid,
                daily_battery_charge    as eod_chg,
                daily_battery_discharge as eod_dis
              FROM solar_readings
              WHERE time > NOW() - INTERVAL '14 days'
              AND site_name ILIKE :site
              AND daily_pv_energy IS NOT NULL
              ORDER BY DATE(time AT TIME ZONE 'Africa/Johannesburg'), inverter_name, time DESC
            ) sub GROUP BY day ORDER BY day
            """,
            {"site": site},
        )
        result = _serialise(rows)

    elif chart == "temps":
        rows = _query_all(
            """
            SELECT DATE_TRUNC('minute', time) as time,
              inverter_name,
              AVG(CASE WHEN inverter_temp < 100 THEN inverter_temp END) as inv_temp,
              AVG(CASE WHEN dc_temp < 100 THEN dc_temp END) as dc_temp,
              AVG(CASE WHEN battery_temp > 0 THEN battery_temp END) as batt_temp
            FROM solar_readings
            WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                          AT TIME ZONE 'Africa/Johannesburg'
            AND site_name ILIKE :site
            GROUP BY 1, 2 ORDER BY 1
            """,
            {"site": site},
        )
        result = _serialise(rows)

    elif chart == "peaks":
        row = _query_one(
            """
            SELECT
              COALESCE(MAX(pv_total),   0) AS peak_pv,
              COALESCE(MAX(load_total), 0) AS peak_load,
              COALESCE(MAX(grid_total), 0) AS peak_grid
            FROM (
              SELECT ts,
                SUM(avg_pv)   AS pv_total,
                SUM(avg_load) AS load_total,
                SUM(avg_grid) AS grid_total
              FROM (
                SELECT DATE_TRUNC('minute', time) AS ts, inverter_name,
                  AVG(pv1_power + COALESCE(pv2_power,0)) AS avg_pv,
                  AVG(load_power)  AS avg_load,
                  AVG(CASE WHEN grid_power > 0 THEN grid_power ELSE 0 END) AS avg_grid
                FROM solar_readings
                WHERE time >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Africa/Johannesburg')
                               AT TIME ZONE 'Africa/Johannesburg' + INTERVAL '1 hour'
                AND site_name ILIKE :site
                GROUP BY 1, 2
              ) inv GROUP BY ts
            ) totals
            """,
            {"site": site},
        )
        result = _serialise(row)

    else:
        return {"error": f"Unknown chart type: {chart}"}

    _cache_set(key, result)
    return result


def _serialise(obj: Any) -> Any:
    """Recursively convert datetime/Decimal objects to JSON-safe types.

    SQLAlchemy returns Python datetime and Decimal objects which FastAPI's
    JSON encoder handles, but being explicit here avoids surprises with
    chart.js date parsing.
    """
    import decimal
    if isinstance(obj, list):
        return [_serialise(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    return obj


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user=Depends(login_required)):
    """Full-screen power flow dashboard — requires login."""
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "current_user": user,
            "flash": _consume_flash(request),
        },
    )


@router.get("/api/solar/sites")
async def api_sites(user=Depends(login_required)):
    """List enabled sites accessible to the current user."""
    accessible = get_accessible_sites(user)
    all_sites = _get_sites()
    return [s for s in all_sites if s["name"] in accessible]


@router.get("/api/solar/flow")
async def api_flow(
    site: Optional[str] = Query(default=None),
    user=Depends(login_required),
):
    """Live power data for a site the user can access. Cached for 10 seconds."""
    accessible = get_accessible_sites(user)
    resolved = _resolve_site(site, accessible)
    if not resolved:
        raise HTTPException(400, "No accessible sites")
    return _get_flow(resolved)


@router.get("/api/solar/weather")
async def api_weather(
    site: Optional[str] = Query(default=None),
    user=Depends(login_required),
):
    """Latest weather for an accessible site. Cached for 60 seconds."""
    accessible = get_accessible_sites(user)
    resolved = _resolve_site(site, accessible)
    if not resolved:
        raise HTTPException(400, "No accessible sites")
    return _get_weather(resolved)


@router.get("/api/solar/monthly")
async def api_monthly(
    site: Optional[str] = Query(default=None),
    user=Depends(login_required),
):
    """This month's PV and grid kWh for an accessible site. Cached for 120 seconds."""
    accessible = get_accessible_sites(user)
    resolved = _resolve_site(site, accessible)
    if not resolved:
        raise HTTPException(400, "No accessible sites")
    return _get_monthly(resolved)


@router.get("/api/solar/chart/{chart}")
async def api_chart(
    chart: str,
    site: Optional[str] = Query(default=None),
    user=Depends(login_required),
):
    """Chart data for an accessible site. Cached for 60 seconds.

    chart types: pv, load, battery, grid, daily, temps, peaks
    """
    accessible = get_accessible_sites(user)
    resolved = _resolve_site(site, accessible)
    if not resolved:
        raise HTTPException(400, "No accessible sites")
    result = _get_chart(chart, resolved)
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(404, result["error"])
    return result
