"""
SolarWatch — sungrow_worker.py

iSolarCloud API poller for Sungrow grid-tie inverters.

All endpoints and field mappings confirmed from live API testing against
the SG125CX-P2 at Bitrad Factory (ps_id=1713768, ps_key=1713768_1_2_1).

Endpoints used per poll:
  POST /openapi/login                   → authenticate, receive token
  POST /openapi/getPowerStationList     → list plants, get ps_id
  POST /openapi/getDeviceList           → list devices for a plant, get ps_key
  POST /openapi/getDeviceRealTimeData   → live inverter data points

Authentication:
  Uses V1 (no OAuth2). The developer appkey + secret identify the application.
  The plant owner's iSolarCloud credentials are stored per-site in the DB
  and used to obtain a user-scoped token — the same pattern as sunsynk_worker.py.

Data point mapping (confirmed live from SG125CX-P2 response):
  p1   → yield today (Wh)
  p2   → total lifetime yield (Wh)
  p4   → internal air temperature (°C)
  p14  → total DC power (W)
  p18  → Phase A voltage (V)
  p19  → Phase B voltage (V)
  p20  → Phase C voltage (V)
  p21  → Phase A current (A)
  p22  → Phase B current (A)
  p23  → Phase C current (A)
  p24  → total active AC power (W)
  p25  → total reactive power (var)
  p26  → power factor
  p27  → grid frequency (Hz)
  p29  → operating status code (see STATUS_MAP)

Data update rate: iSolarCloud refreshes device data approximately every 5 minutes.
Recommended collector poll interval: 300 seconds.

Set SUNGROW_DEBUG=1 in .env to log raw API responses.

Region note:
  Plants on web3.isolarcloud.com.hk use the HK gateway (default).
  European plants use gateway.isolarcloud.eu — set SUNGROW_REGION=eu in .env.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import requests

log   = logging.getLogger(__name__)
DEBUG = os.getenv("SUNGROW_DEBUG", "0") == "1"

# ── Gateway URLs — selected by SUNGROW_REGION env var ────────────────────────
BASE_URL_HK = "https://gateway.isolarcloud.com.hk"
BASE_URL_EU = "https://gateway.isolarcloud.eu"
BASE_URL    = BASE_URL_HK if os.getenv("SUNGROW_REGION", "hk").lower() != "eu" else BASE_URL_EU

# ── Application credentials — set in .env, never hardcoded ───────────────────
# APPKEY and SECRET come from developer-api.isolarcloud.com → your application page.
APPKEY = os.getenv("SUNGROW_APPKEY", "")
SECRET = os.getenv("SUNGROW_SECRET", "")

# Token is valid ~2 hours; we refresh at 90 minutes to stay ahead of expiry.
TOKEN_TTL = 5400  # seconds

# sys_code "900" identifies third-party developer applications to iSolarCloud.
SYS_CODE = "900"

# ── Data point IDs confirmed from live SG125CX-P2 response ───────────────────
# Used in the POINT_ID_LIST request parameter and for parsing the response.
# Names are the human-readable label; values are the API's short point IDs.
_POINT = {
    "yield_today_wh":          "p1",   # Yield today (Wh)
    "total_yield_wh":          "p2",   # Total lifetime yield (Wh)
    "internal_air_temp_c":     "p4",   # Inverter internal temperature (°C)
    "total_dc_power_w":        "p14",  # Total DC input power (W)
    "phase_a_voltage_v":       "p18",  # Phase A (L1) voltage (V)
    "phase_b_voltage_v":       "p19",  # Phase B (L2) voltage (V)
    "phase_c_voltage_v":       "p20",  # Phase C (L3) voltage (V)
    "phase_a_current_a":       "p21",  # Phase A current (A)
    "phase_b_current_a":       "p22",  # Phase B current (A)
    "phase_c_current_a":       "p23",  # Phase C current (A)
    "total_active_power_w":    "p24",  # Total AC active power output (W)
    "total_reactive_power_var":"p25",  # Total reactive power (var)
    "power_factor":            "p26",  # Power factor (dimensionless, ~1.0)
    "grid_frequency_hz":       "p27",  # Grid frequency (Hz)
    "operating_status":        "p29",  # Status bitmask — see STATUS_MAP
}

# Point ID numbers as strings — sent in the getDeviceRealTimeData request.
# The API only returns points in this list, keeping the response compact.
POINT_ID_LIST = ["1", "2", "4", "14", "18", "19", "20",
                 "21", "22", "23", "24", "25", "26", "27", "29"]

# Operating status bitmask values confirmed from live SG125CX-P2 responses.
# The inverter returns a numeric bitmask in p29 (e.g. 33280 = "Dispatched running").
STATUS_MAP = {
    0:     "Grid-connected",
    64:    "Running",
    128:   "Derated",
    256:   "Fault",
    5120:  "Standby",
    32768: "Shutdown",
    33280: "Dispatched running",
}


class SungrowClient:
    """Authenticated iSolarCloud API client with automatic token refresh.

    One instance is created per set of plant owner credentials and cached
    by the collector for the lifetime of the process — matching the pattern
    used by SunsynkClient in sunsynk_worker.py.

    Authentication uses /openapi/login (plain JSON, no encryption).
    All data queries use /openapi/* endpoints confirmed from live API testing.
    """

    def __init__(
        self,
        username: str,
        password: str,
        appkey:   str = APPKEY,
        secret:   str = SECRET,
    ):
        """Initialise the client. Network calls are deferred to first use.

        Args:
            username: iSolarCloud account email of the plant owner.
            password: iSolarCloud account password of the plant owner.
            appkey:   Developer portal Appkey — identifies this application.
            secret:   Developer portal Secret key — sent as x-access-key header.
        """
        self.username = username
        self.password = password
        self.appkey   = appkey
        self.secret   = secret

        self._token:        Optional[str] = None
        self._token_expiry: float         = 0.0

        # Persistent session reuses TCP connections across poll cycles.
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent":   "Mozilla/5.0",
            # x-access-key authenticates the *application* on every request.
            # The token (obtained at login) authenticates the *user*.
            "x-access-key": self.secret,
        })

    # ── Authentication ────────────────────────────────────────────────────────

    def ensure_logged_in(self, force: bool = False) -> bool:
        """Log in to iSolarCloud if not authenticated or if the token has expired.

        Args:
            force: Re-authenticate even if the current token appears valid.
                   Set by _post() when the API returns a token-expired error code.

        Returns:
            True if a valid token is held after this call, False on any failure.
        """
        if not force and self._token and time.time() < self._token_expiry:
            return True

        log.info(f"[{self.username}] Logging in to iSolarCloud ({BASE_URL})...")

        try:
            resp = self.session.post(
                f"{BASE_URL}/openapi/login",
                json={
                    "appkey":        self.appkey,
                    "user_account":  self.username,
                    "user_password": self.password,
                    "sys_code":      SYS_CODE,
                },
                timeout=20,
            )
            resp.raise_for_status()
            body = resp.json()

            if DEBUG:
                log.info(f"[DEBUG] login response:\n{json.dumps(body, indent=2)}")

            # result_code "1" = success; anything else is an error.
            if str(body.get("result_code")) != "1":
                log.error(
                    f"[{self.username}] Login failed: "
                    f"code={body.get('result_code')} msg={body.get('result_msg')}"
                )
                return False

            token = (body.get("result_data") or {}).get("token")
            if not token:
                log.error(f"[{self.username}] Login OK but no token in response")
                return False

            self._token        = token
            self._token_expiry = time.time() + TOKEN_TTL
            log.info(f"[{self.username}] iSolarCloud login successful")
            return True

        except Exception as exc:
            log.error(f"[{self.username}] Login error: {exc}", exc_info=True)
            return False

    # ── Internal POST helper ──────────────────────────────────────────────────

    def _post(self, endpoint: str, payload: dict, _retry: bool = True) -> Optional[dict]:
        """POST to an iSolarCloud /openapi/* endpoint and return result_data.

        Injects appkey and token into every request body.
        Re-authenticates and retries once when the API signals a token failure
        (result_code 301, 302, or E00003) — matching sunsynk_worker._get().

        Args:
            endpoint: API path relative to BASE_URL (e.g. '/openapi/getDeviceList').
            payload:  Request parameters dict — appkey and token are added automatically.
            _retry:   Internal flag — prevents infinite re-auth loops.

        Returns:
            result_data on success (dict or list), or None on any failure.
        """
        if not self._token:
            log.error("_post() called without a token — call ensure_logged_in() first")
            return None

        body = {"appkey": self.appkey, "token": self._token, **payload}

        try:
            resp = self.session.post(
                f"{BASE_URL}{endpoint}", json=body, timeout=20,
            )
            resp.raise_for_status()
            result = resp.json()

            if DEBUG:
                log.info(f"[DEBUG] {endpoint}\n{json.dumps(result, indent=2)}")

            code = str(result.get("result_code", ""))

            # Token expired or invalidated — re-authenticate and retry once.
            if code in ("301", "302", "E00003") and _retry:
                log.warning(f"[{self.username}] Token rejected (code {code}) — re-logging in")
                if self.ensure_logged_in(force=True):
                    return self._post(endpoint, payload, _retry=False)
                return None

            if code != "1":
                log.error(
                    f"[{self.username}] {endpoint} error: "
                    f"code={code} msg={result.get('result_msg')}"
                )
                return None

            return result.get("result_data")

        except Exception as exc:
            log.error(f"[{self.username}] {endpoint} failed: {exc}", exc_info=True)
            return None

    # ── Plant and device discovery ────────────────────────────────────────────

    def get_plant_list(self) -> list[dict]:
        """Return all plants registered to this iSolarCloud account.

        Calls: POST /openapi/getPowerStationList

        Each plant dict contains at minimum:
            ps_id     — integer plant ID used for all device queries
            ps_name   — display name of the plant (e.g. 'Bitrad Factory')

        Returns an empty list on failure.
        """
        data = self._post("/openapi/getPowerStationList", {"curPage": 1, "size": 50})
        if not data:
            return []
        return data.get("pageList") or []

    def get_device_list(self, ps_id: int) -> list[dict]:
        """Return all devices registered under a plant.

        Calls: POST /openapi/getDeviceList

        Requests device types 1 (inverter), 7 (meter), and 9 (logger).
        For polling we only use type 1 (inverters) — other types are returned
        for completeness and logged during the test script.

        Each device dict contains:
            ps_key      — composite key for data queries (e.g. '1713768_1_2_1')
            device_type — integer type code (1 = inverter)
            device_name — display name (e.g. 'Inverter1')
            device_sn   — physical serial number on the inverter label

        Args:
            ps_id: Plant ID from get_plant_list().

        Returns an empty list on failure.
        """
        data = self._post(
            "/openapi/getDeviceList",
            {"curPage": 1, "size": 100, "ps_id": ps_id, "device_type_list": [1, 7, 9]},
        )
        if not data:
            return []
        return data.get("pageList") or []

    def get_device_realtime(self, ps_key: str, device_type: int = 1) -> Optional[dict]:
        """Return live data points for a single device.

        Calls: POST /openapi/getDeviceRealTimeData

        Requests only the point IDs in POINT_ID_LIST (confirmed working for
        the SG125CX-P2). Returns a flat {point_id: value} dict (e.g. {"p24": "2349.0"})
        plus a "_meta" key with device identifiers for logging.

        All values are strings — use _safe_float() before arithmetic.

        Args:
            ps_key:      Device composite key from get_device_list().
            device_type: Device type code (default 1 = inverter).

        Returns None if no data is available or the request fails.
        """
        data = self._post(
            "/openapi/getDeviceRealTimeData",
            {
                "ps_key_list":   [ps_key],
                "device_type":   device_type,
                "point_id_list": POINT_ID_LIST,
            },
        )
        if not data:
            return None

        if DEBUG:
            log.info(f"[DEBUG] realtime raw:\n{json.dumps(data, indent=2)}")

        devices = data.get("device_point_list") or []
        if not devices:
            return None

        # The response wraps each device in a {"device_point": {...}} dict.
        device = devices[0].get("device_point") or {}

        # Flatten to {point_id: value} — keep only the p* data fields.
        flat: dict = {k: v for k, v in device.items() if k.startswith("p")}

        # Attach metadata so _normalise() and poll() can log device identity.
        flat["_meta"] = {
            "ps_key":      device.get("ps_key"),
            "device_sn":   device.get("device_sn"),
            "device_name": device.get("device_name"),
            "device_time": device.get("device_time"),
        }

        return flat


# ── Type helpers ──────────────────────────────────────────────────────────────

def _safe_float(value: object) -> Optional[float]:
    """Safely cast an API value to float.

    iSolarCloud returns numeric values as strings (e.g. '2349.0').
    None and empty string are treated as absent (sensor not available).

    Args:
        value: Raw value from the API response.

    Returns:
        Float, or None if the value is absent or non-numeric.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _safe_int(value: object) -> Optional[int]:
    """Safely cast an API value to int, via float to handle '33280.0' strings.

    Args:
        value: Raw value from the API response.

    Returns:
        Int, or None if the value is absent or non-numeric.
    """
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


# ── Normalisation ─────────────────────────────────────────────────────────────

def _normalise(raw: dict, site_name: str, inverter_name: str) -> dict:
    """Map iSolarCloud data points to SolarWatch solar_readings column names.

    This function is the bridge between the raw iSolarCloud point IDs (p1, p24, etc.)
    and the column names expected by write_reading() in collector.py.

    For grid-tie inverters (no battery):
      - All battery_* columns remain None → stored as NULL in PostgreSQL.
      - grid_voltage = average of three phase voltages (appropriate for display;
        avoids showing three separate values on a dashboard built for a single value).
      - grid_power = negative of total_active_power (SolarWatch export convention:
        negative = exporting to grid, positive = importing from grid).
      - pv1_power = total_active_power in watts — stored here so the dashboard's
        SUM(pv1_power + pv2_power) query returns the correct total without schema changes.
      - daily_pv_energy = yield today converted from Wh to kWh.
      - total_pv_energy = lifetime yield converted from Wh to kWh.
      - daily_grid_export = daily_pv_energy (grid-tie exports all generation).
      - load_power = None (grid-tie does not measure site consumption).

    Args:
        raw:            Flat {point_id: value_string} dict from get_device_realtime().
        site_name:      Site name for log messages.
        inverter_name:  Inverter display name for log messages.

    Returns:
        Dict with solar_readings column names as keys, ready for write_reading().
    """
    # ── Parse raw points ──────────────────────────────────────────────────────
    yield_today_wh    = _safe_float(raw.get(_POINT["yield_today_wh"]))
    total_yield_wh    = _safe_float(raw.get(_POINT["total_yield_wh"]))
    total_active_w    = _safe_float(raw.get(_POINT["total_active_power_w"]))
    total_dc_w        = _safe_float(raw.get(_POINT["total_dc_power_w"]))
    va                = _safe_float(raw.get(_POINT["phase_a_voltage_v"]))
    vb                = _safe_float(raw.get(_POINT["phase_b_voltage_v"]))
    vc                = _safe_float(raw.get(_POINT["phase_c_voltage_v"]))
    ia                = _safe_float(raw.get(_POINT["phase_a_current_a"]))
    ib                = _safe_float(raw.get(_POINT["phase_b_current_a"]))
    ic                = _safe_float(raw.get(_POINT["phase_c_current_a"]))
    freq              = _safe_float(raw.get(_POINT["grid_frequency_hz"]))
    temp              = _safe_float(raw.get(_POINT["internal_air_temp_c"]))
    status_code       = _safe_int(raw.get(_POINT["operating_status"]))

    # ── Derived values ────────────────────────────────────────────────────────
    # Average the three phase voltages for a single representative grid_voltage.
    voltages = [v for v in (va, vb, vc) if v is not None]
    grid_voltage = round(sum(voltages) / len(voltages), 1) if voltages else None

    # Average phase currents the same way.
    currents = [c for c in (ia, ib, ic) if c is not None]
    grid_current = round(sum(currents) / len(currents), 2) if currents else None

    # Energy: API returns Wh, solar_readings stores kWh.
    daily_pv_kwh = round(yield_today_wh / 1000, 3) if yield_today_wh is not None else None
    total_pv_kwh = round(total_yield_wh  / 1000, 3) if total_yield_wh  is not None else None

    # Grid-tie always exports → grid_power is negative (export convention).
    grid_power_w = -total_active_w if total_active_w is not None else None

    status_str = STATUS_MAP.get(status_code, str(status_code) if status_code is not None else "Unknown")

    # ── Build solar_readings row ──────────────────────────────────────────────
    row: dict = {
        # PV / AC power
        "pv1_power":              total_active_w,  # AC output in W (pv2_power stays None)
        "pv2_power":              None,
        "pv1_voltage":            None,             # Not available via cloud API
        "pv1_current":            None,
        "pv2_voltage":            None,
        "pv2_current":            None,

        # Battery — all None for grid-tie, stored as NULL in PostgreSQL
        "battery_voltage":        None,
        "battery_current":        None,
        "battery_power":          None,
        "battery_soc":            None,
        "battery_temp":           None,
        "daily_battery_charge":   None,
        "daily_battery_discharge":None,

        # Grid
        "grid_voltage":           grid_voltage,      # Average of phases A/B/C (V)
        "grid_current":           grid_current,      # Average of phases A/B/C (A)
        "grid_frequency":         freq,              # Hz
        "grid_power":             grid_power_w,      # W, negative = exporting

        # Load — not measured by grid-tie inverter
        "load_power":             None,
        "load_voltage":           None,
        "daily_load_energy":      None,

        # Temperatures
        "inverter_temp":          temp,              # °C
        "dc_temp":                total_dc_w,        # W — DC power stored here (no dedicated column yet)

        # Energy
        "daily_pv_energy":        daily_pv_kwh,      # kWh
        "total_pv_energy":        total_pv_kwh,      # kWh
        "daily_grid_import":      None,              # Grid-tie does not import
        "daily_grid_export":      daily_pv_kwh,      # kWh — all generation is exported

        # CT clamp — not applicable
        "ct_power":               None,
        "ct_load_power":          None,

        # Metadata
        "source_type":            "sungrow",
        "poll_success":           total_active_w is not None,
    }

    # ── Summary log — same style as sunsynk_worker ────────────────────────────
    log.info(
        f"[{site_name}/{inverter_name}] "
        f"Status={status_str} "
        f"AC={total_active_w}W "
        f"DC={total_dc_w}W "
        f"Vgrid={grid_voltage}V "
        f"Hz={freq} "
        f"YieldToday={daily_pv_kwh}kWh "
        f"YieldTotal={total_pv_kwh}kWh "
        f"Temp={temp}°C"
    )

    return row


# ── Main poll entry point ─────────────────────────────────────────────────────

def poll(site: dict, client: SungrowClient) -> list[dict]:
    """Poll one Sungrow site and return a list of reading dicts.

    This is the entry point called by collector.py — same signature as
    sunsynk_worker.poll(site, client).

    Steps:
      1. Ensure the client holds a valid token.
      2. Resolve ps_id from the site config (set via the web UI admin page).
         Falls back to API discovery if not yet configured — useful on first setup.
      3. Fetch the device list to get the ps_key for each inverter.
      4. For each inverter (device_type=1), fetch real-time data and normalise.

    Args:
        site:   Site config dict from collector.py load_sites(), containing:
                  site_name           — for logging
                  sungrow_plant_id    — ps_id integer (auto-discovered if absent)
        client: SungrowClient instance, created and cached by collector.py.

    Returns:
        List of reading dicts (one per inverter), each ready for write_reading().
        Returns an empty list on any failure — never raises.
    """
    site_name = site.get("site_name", "unknown")

    # Step 1: Ensure authenticated
    if not client.ensure_logged_in():
        log.error(f"[{site_name}] Not logged in — skipping poll")
        return []

    # Step 2: Resolve plant ID (ps_id)
    # Use the value stored in the DB (entered via the admin web UI after first run).
    # Fall back to API discovery on first setup before ps_id is known.
    # Guard against the string "None" — this can arrive when a DB NULL passes through
    # SQLAlchemy mappings or when the site was created before credentials were saved.
    _raw_ps_id = site.get("sungrow_plant_id")
    ps_id = str(_raw_ps_id).strip() if _raw_ps_id and str(_raw_ps_id).strip().lower() not in ('none', '', 'null') else None
    if not ps_id:
        log.info(f"[{site_name}] No plant ID configured — discovering via API...")
        plants = client.get_plant_list()
        if not plants:
            log.error(f"[{site_name}] No plants returned — check account credentials")
            return []
        ps_id = plants[0].get("ps_id")
        if not ps_id:
            log.error(f"[{site_name}] Cannot determine ps_id from: {plants[0]}")
            return []
        log.info(
            f"[{site_name}] Auto-discovered plant: "
            f"'{plants[0].get('ps_name', 'unknown')}' (ps_id={ps_id})"
        )

    # Step 3: Get device list — one API call returns all devices for the plant
    # ps_id may be a string (from DB) or int (from API discovery) — normalise to int
    try:
        ps_id_int = int(ps_id)
    except (TypeError, ValueError):
        log.error(f"[{site_name}] Invalid ps_id '{ps_id}' — cannot fetch device list")
        return []
    devices = client.get_device_list(ps_id_int)
    if not devices:
        log.error(f"[{site_name}] No devices returned for ps_id={ps_id}")
        return []

    # Step 4: Poll each inverter (device_type=1) and collect readings
    results = []

    for dev in devices:
        if dev.get("device_type") != 1:
            continue  # Skip meters (7) and loggers (9)

        ps_key = dev.get("ps_key")
        if not ps_key:
            log.warning(f"[{site_name}] Device has no ps_key — skipping: {dev}")
            continue

        inv_name = dev.get("device_name") or "Inverter_1"

        start = time.monotonic()
        raw   = client.get_device_realtime(ps_key=ps_key, device_type=1)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if raw is None:
            log.error(f"[{site_name}/{inv_name}] No real-time data returned")
            continue

        row = _normalise(raw, site_name, inv_name)
        row["poll_duration_ms"] = elapsed_ms
        row["inverter_name"]    = inv_name
        row["inverter_sn"]      = dev.get("device_sn") or ps_key

        results.append(row)

    return results