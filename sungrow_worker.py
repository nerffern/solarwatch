"""
SolarWatch — sungrow_worker.py

iSolarCloud API poller for Sungrow grid-tie inverters with smart meter support.

All endpoints and field mappings confirmed from live API testing against
the SG125CX-P2 inverter and DTSD1352-A smart meter at Bitrad Factory
(ps_id=1713768, inverter ps_key=1713768_1_2_1, meter ps_key=1713768_7_1_1).

Endpoints used per poll:
  POST /openapi/login                   → authenticate, receive token
  POST /openapi/getPowerStationList     → list plants, get ps_id
  POST /openapi/getDeviceList           → list devices for a plant
  POST /openapi/getDeviceRealTimeData   → live data points (inverter + meter)

Authentication:
  Uses V1 (no OAuth2). The developer appkey + secret identify the application.
  The plant owner's iSolarCloud credentials are stored per-site in the DB
  and used to obtain a user-scoped token — same pattern as sunsynk_worker.py.

Inverter data point mapping (confirmed live, SG125CX-P2):
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
  p29  → operating status bitmask (see STATUS_MAP)

Meter data point mapping (confirmed live, DTSD1352-A):
  p8018 → meter active power (W)  +import / -export
  p8062 → daily forward active energy (Wh)  = grid import today
  p8063 → daily reverse active energy (Wh)  = grid export today
  p8030 → forward active energy (Wh)         = lifetime grid import
  p8031 → reverse active energy (Wh)         = lifetime grid export
  p8000 → Phase A voltage (V)
  p8001 → Phase B voltage (V)
  p8002 → Phase C voltage (V)
  p8006 → Phase A current (A)
  p8007 → Phase B current (A)
  p8008 → Phase C current (A)
  p8064 → frequency (Hz)
  p8014 → power factor
  p8076 → Phase A active power (W)
  p8077 → Phase B active power (W)
  p8078 → Phase C active power (W)

Load power derivation:
  load_power = inverter_ac_output_w + meter_active_power_w
  When exporting: ac=5800, meter=-5450 → load=350W (factory night base load)
  When factory running: ac=33500, meter=-28000 → load=5500W

Data update rate: iSolarCloud refreshes device data approximately every 5 minutes.
Recommended collector poll interval: 300 seconds (SUNGROW_POLL_INTERVAL).

Set SUNGROW_DEBUG=1 in .env to log raw API responses.

Region note:
  Plants on web3.isolarcloud.com.hk → HK gateway (default, SUNGROW_REGION=hk).
  European plants → gateway.isolarcloud.eu (SUNGROW_REGION=eu).
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

# ── Gateway URLs ──────────────────────────────────────────────────────────────
BASE_URL_HK = "https://gateway.isolarcloud.com.hk"
BASE_URL_EU = "https://gateway.isolarcloud.eu"
BASE_URL    = BASE_URL_HK if os.getenv("SUNGROW_REGION", "hk").lower() != "eu" else BASE_URL_EU

# ── Application credentials — set in .env, never hardcoded ───────────────────
APPKEY = os.getenv("SUNGROW_APPKEY", "")
SECRET = os.getenv("SUNGROW_SECRET", "")

# Token lifetime. iSolarCloud tokens last ~2 hours; we refresh at 90 minutes.
TOKEN_TTL = 5400  # seconds

# sys_code "900" identifies third-party developer applications to iSolarCloud.
SYS_CODE = "900"

# ── Inverter point IDs (device_type=1, SG125CX-P2) ───────────────────────────
# Confirmed from live API responses. All values returned as strings.
_INV_POINT = {
    "yield_today_wh":           "p1",   # Yield today (Wh)
    "total_yield_wh":           "p2",   # Total lifetime yield (Wh)
    "internal_air_temp_c":      "p4",   # Inverter internal temperature (°C)
    "total_dc_power_w":         "p14",  # Total DC input power (W)
    "phase_a_voltage_v":        "p18",  # Phase A (L1) voltage (V)
    "phase_b_voltage_v":        "p19",  # Phase B (L2) voltage (V)
    "phase_c_voltage_v":        "p20",  # Phase C (L3) voltage (V)
    "phase_a_current_a":        "p21",  # Phase A current (A)
    "phase_b_current_a":        "p22",  # Phase B current (A)
    "phase_c_current_a":        "p23",  # Phase C current (A)
    "total_active_power_w":     "p24",  # Total AC active power output (W)
    "total_reactive_power_var": "p25",  # Total reactive power (var)
    "power_factor":             "p26",  # Power factor (dimensionless)
    "grid_frequency_hz":        "p27",  # Grid frequency (Hz)
    "operating_status":         "p29",  # Status bitmask — see STATUS_MAP
}

# Point ID numbers sent in the getDeviceRealTimeData request for the inverter.
INV_POINT_ID_LIST = [
    "1", "2", "4", "14", "18", "19", "20",
    "21", "22", "23", "24", "25", "26", "27", "29",
]

# ── Meter point IDs (device_type=7, DTSD1352-A) ───────────────────────────────
# Confirmed from live API responses. All values returned as strings.
# Note: meter uses 4-digit point IDs (8xxx range) unlike the inverter (1-2 digit).
_METER_POINT = {
    "meter_active_power_w":     "p8018",  # Signed active power: +import / -export (W)
    "daily_grid_import_wh":     "p8062",  # Daily forward energy = grid import today (Wh)
    "daily_grid_export_wh":     "p8063",  # Daily reverse energy = grid export today (Wh)
    "total_grid_import_wh":     "p8030",  # Lifetime forward energy = total grid import (Wh)
    "total_grid_export_wh":     "p8031",  # Lifetime reverse energy = total grid export (Wh)
    "meter_phase_a_voltage_v":  "p8000",  # Phase A voltage (V)
    "meter_phase_b_voltage_v":  "p8001",  # Phase B voltage (V)
    "meter_phase_c_voltage_v":  "p8002",  # Phase C voltage (V)
    "meter_phase_a_current_a":  "p8006",  # Phase A current (A)
    "meter_phase_b_current_a":  "p8007",  # Phase B current (A)
    "meter_phase_c_current_a":  "p8008",  # Phase C current (A)
    "meter_frequency_hz":       "p8064",  # Grid frequency (Hz)
    "meter_power_factor":       "p8014",  # Power factor
    "meter_phase_a_power_w":    "p8076",  # Phase A active power (W)
    "meter_phase_b_power_w":    "p8077",  # Phase B active power (W)
    "meter_phase_c_power_w":    "p8078",  # Phase C active power (W)
}

# Point ID numbers sent in the getDeviceRealTimeData request for the meter.
# Uses the numeric portion only — the 'p' prefix is added by the response.
METER_POINT_ID_LIST = [
    "8018", "8062", "8063", "8030", "8031",
    "8000", "8001", "8002", "8006", "8007", "8008",
    "8064", "8014", "8076", "8077", "8078",
]

# ── Operating status bitmask values ───────────────────────────────────────────
# Confirmed from live SG125CX-P2 and iSolarCloud documentation.
STATUS_MAP = {
    0:     "Grid-connected",
    64:    "Running",
    128:   "Derated",
    256:   "Fault",
    5120:  "Standby",
    32768: "Shutdown",
    33024: "Derated running",
    33280: "Dispatched running",
}


class SungrowClient:
    """Authenticated iSolarCloud API client with automatic token refresh.

    One instance is created per set of plant owner credentials and cached
    by the collector for the lifetime of the process — same pattern as
    SunsynkClient in sunsynk_worker.py.

    Authentication uses /openapi/login (plain JSON, no RSA encryption).
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
        (result_code 301, 302, or E00003).

        Args:
            endpoint: API path relative to BASE_URL.
            payload:  Request parameters — appkey and token injected automatically.
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

            # Token expired — re-authenticate and retry once.
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
            ps_id   — integer plant ID used for all device queries
            ps_name — display name of the plant

        Returns an empty list on failure.
        """
        data = self._post("/openapi/getPowerStationList", {"curPage": 1, "size": 50})
        if not data:
            return []
        return data.get("pageList") or []

    def get_device_list(self, ps_id: int) -> list[dict]:
        """Return all devices registered under a plant.

        Calls: POST /openapi/getDeviceList

        Requests device types 1 (inverter), 7 (meter), 9 (logger).
        Each device dict contains:
            ps_key      — composite key for data queries
            device_type — integer type code (1=inverter, 7=meter, 9=logger)
            device_name — display name
            device_sn   — physical serial number

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

    def get_device_realtime(
        self,
        ps_key:       str,
        device_type:  int,
        point_id_list: list[str],
    ) -> Optional[dict]:
        """Return live data points for a single device.

        Calls: POST /openapi/getDeviceRealTimeData

        Returns a flat {point_id: value_string} dict plus a '_meta' key.
        All values are strings — use _safe_float() before arithmetic.

        Args:
            ps_key:        Device composite key from get_device_list().
            device_type:   Device type code (1=inverter, 7=meter).
            point_id_list: List of point ID strings to request.

        Returns None if no data is available or the request fails.
        """
        data = self._post(
            "/openapi/getDeviceRealTimeData",
            {
                "ps_key_list":   [ps_key],
                "device_type":   device_type,
                "point_id_list": point_id_list,
            },
        )
        if not data:
            return None

        if DEBUG:
            log.info(f"[DEBUG] realtime {ps_key}:\n{json.dumps(data, indent=2)}")

        devices = data.get("device_point_list") or []
        if not devices:
            return None

        device = devices[0].get("device_point") or {}

        # Flatten to {point_id: value} — keep only p* data fields.
        flat: dict = {k: v for k, v in device.items() if k.startswith("p")}

        # Attach metadata for logging.
        flat["_meta"] = {
            "ps_key":      device.get("ps_key"),
            "device_sn":   device.get("device_sn"),
            "device_name": device.get("device_name"),
            "device_time": device.get("device_time"),
        }

        return flat


# ── Type helpers ──────────────────────────────────────────────────────────────

def _safe_float(value: object) -> Optional[float]:
    """Safely cast an iSolarCloud API value to float.

    All data point values are returned as strings (e.g. '5800.0', '700.0').
    None and empty string are treated as absent (sensor not available).

    Args:
        value: Raw string value from the API response.

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

def _normalise(
    inv_raw:    dict,
    meter_raw:  Optional[dict],
    site_name:  str,
    inv_name:   str,
) -> dict:
    """Map iSolarCloud inverter + meter data to SolarWatch solar_readings columns.

    Inverter data (inv_raw) provides PV generation, grid voltage/frequency,
    inverter temperature, and daily/lifetime yield.

    Meter data (meter_raw) provides signed grid power, per-phase measurements,
    daily import/export energy, and — crucially — load power:

        load_power (W) = inverter_ac_output_w + meter_active_power_w

    Sign convention for meter_active_power_w:
        Positive (+) = importing from grid (factory consuming > generation)
        Negative (-) = exporting to grid   (generation > factory consumption)

    This means:
        Daytime peak (33.5kW AC, factory using 5kW):
            meter_w = -28500 → load = 33500 + (-28500) = 5000W ✓
        Morning ramp (5.8kW AC, factory using 6.26kW):
            meter_w = +700   → load = 5800  + 700       = 6500W ✓
        Night (0W AC, factory on base load 350W):
            meter_w = +350   → load = 0     + 350       = 350W  ✓

    When meter_raw is None (meter not present or API failure), load_power
    remains None and grid_power falls back to the inverter-only estimate.

    For grid-tie inverters (no battery):
      - All battery_* columns remain None → stored as NULL in PostgreSQL.
      - grid_voltage = average of inverter phase voltages (or meter voltages).
      - grid_power: when meter available, use meter_active_power_w directly
        (signed, positive=import, negative=export — SolarWatch convention).
        When meter absent, derive from inverter AC output (always negative/export).
      - daily_grid_import / daily_grid_export: from meter daily energy counters.
      - daily_pv_energy = inverter yield today converted Wh → kWh.
      - total_pv_energy = inverter lifetime yield converted Wh → kWh.

    Args:
        inv_raw:    Flat {point_id: value_string} from inverter get_device_realtime().
        meter_raw:  Flat {point_id: value_string} from meter get_device_realtime(),
                    or None if the meter is not available.
        site_name:  Site name for log messages.
        inv_name:   Inverter display name for log messages.

    Returns:
        Dict with solar_readings column names ready for write_reading().
    """
    # ── Parse inverter points ─────────────────────────────────────────────────
    yield_today_wh  = _safe_float(inv_raw.get(_INV_POINT["yield_today_wh"]))
    total_yield_wh  = _safe_float(inv_raw.get(_INV_POINT["total_yield_wh"]))
    total_active_w  = _safe_float(inv_raw.get(_INV_POINT["total_active_power_w"]))
    total_dc_w      = _safe_float(inv_raw.get(_INV_POINT["total_dc_power_w"]))
    inv_va          = _safe_float(inv_raw.get(_INV_POINT["phase_a_voltage_v"]))
    inv_vb          = _safe_float(inv_raw.get(_INV_POINT["phase_b_voltage_v"]))
    inv_vc          = _safe_float(inv_raw.get(_INV_POINT["phase_c_voltage_v"]))
    inv_ia          = _safe_float(inv_raw.get(_INV_POINT["phase_a_current_a"]))
    inv_ib          = _safe_float(inv_raw.get(_INV_POINT["phase_b_current_a"]))
    inv_ic          = _safe_float(inv_raw.get(_INV_POINT["phase_c_current_a"]))
    freq            = _safe_float(inv_raw.get(_INV_POINT["grid_frequency_hz"]))
    inv_temp        = _safe_float(inv_raw.get(_INV_POINT["internal_air_temp_c"]))
    status_code     = _safe_int(inv_raw.get(_INV_POINT["operating_status"]))

    # ── Parse meter points (if available) ────────────────────────────────────
    meter_w         = None  # signed: +import / -export
    daily_import_wh = None
    daily_export_wh = None
    meter_va        = None
    meter_vb        = None
    meter_vc        = None

    if meter_raw:
        meter_w         = _safe_float(meter_raw.get(_METER_POINT["meter_active_power_w"]))
        daily_import_wh = _safe_float(meter_raw.get(_METER_POINT["daily_grid_import_wh"]))
        daily_export_wh = _safe_float(meter_raw.get(_METER_POINT["daily_grid_export_wh"]))
        meter_va        = _safe_float(meter_raw.get(_METER_POINT["meter_phase_a_voltage_v"]))
        meter_vb        = _safe_float(meter_raw.get(_METER_POINT["meter_phase_b_voltage_v"]))
        meter_vc        = _safe_float(meter_raw.get(_METER_POINT["meter_phase_c_voltage_v"]))
        # Use meter frequency if inverter didn't report it
        if freq is None:
            freq = _safe_float(meter_raw.get(_METER_POINT["meter_frequency_hz"]))

    # ── Derived: grid voltage ─────────────────────────────────────────────────
    # Prefer meter voltages (measured at grid connection point); fall back to
    # inverter voltages (measured at inverter terminals, may differ slightly).
    v_sources = [v for v in (meter_va, meter_vb, meter_vc) if v is not None]
    if not v_sources:
        v_sources = [v for v in (inv_va, inv_vb, inv_vc) if v is not None]
    grid_voltage = round(sum(v_sources) / len(v_sources), 1) if v_sources else None

    # ── Derived: grid current ─────────────────────────────────────────────────
    currents = [c for c in (inv_ia, inv_ib, inv_ic) if c is not None]
    grid_current = round(sum(currents) / len(currents), 2) if currents else None

    # ── Derived: grid power ───────────────────────────────────────────────────
    # Use meter_w directly — it is signed and accurate at the grid connection.
    # Without a meter, fall back to inverter AC output (always negative/exporting).
    if meter_w is not None:
        grid_power_w = meter_w   # +import / -export (matches SolarWatch convention)
    elif total_active_w is not None:
        grid_power_w = -total_active_w  # grid-tie: all generation is exported
    else:
        grid_power_w = None

    # ── Derived: load power ───────────────────────────────────────────────────
    # load_power = inverter_ac_output + meter_active_power
    # This is exact for grid-tie (no battery, no other generation sources).
    # Example: ac=5800W, meter=+700W → load=6500W (factory consuming 6.5kW)
    load_power_w = None
    if total_active_w is not None and meter_w is not None:
        load_power_w = round(total_active_w + meter_w, 1)
        # Clamp to zero — small measurement offsets can produce tiny negatives at night
        if load_power_w < 0:
            load_power_w = 0.0

    # ── Derived: daily energy ─────────────────────────────────────────────────
    daily_pv_kwh     = round(yield_today_wh / 1000, 3)   if yield_today_wh  is not None else None
    total_pv_kwh     = round(total_yield_wh  / 1000, 3)  if total_yield_wh  is not None else None
    daily_import_kwh = round(daily_import_wh / 1000, 3)  if daily_import_wh is not None else None
    daily_export_kwh = round(daily_export_wh / 1000, 3)  if daily_export_wh is not None else None

    # Daily load energy — approximate from available data:
    # daily_load = daily_pv + daily_grid_import - daily_grid_export
    # This is the energy balance identity for a grid-tie system.
    daily_load_kwh = None
    if daily_pv_kwh is not None and daily_import_kwh is not None and daily_export_kwh is not None:
        daily_load_kwh = round(daily_pv_kwh + daily_import_kwh - daily_export_kwh, 3)
        if daily_load_kwh < 0:
            daily_load_kwh = 0.0

    # ── Build solar_readings row ──────────────────────────────────────────────
    status_str = STATUS_MAP.get(status_code, str(status_code) if status_code is not None else "Unknown")

    row: dict = {
        # PV / AC power — total_active_w stored in pv1_power so dashboard
        # SUM(pv1_power + pv2_power) queries work without schema changes.
        "pv1_power":               total_active_w,
        "pv2_power":               None,
        "pv1_voltage":             None,  # Not available via cloud API
        "pv1_current":             None,
        "pv2_voltage":             None,
        "pv2_current":             None,

        # Battery — all None for grid-tie, stored as NULL in PostgreSQL.
        "battery_voltage":         None,
        "battery_current":         None,
        "battery_power":           None,
        "battery_soc":             None,
        "battery_temp":            None,
        "daily_battery_charge":    None,
        "daily_battery_discharge": None,

        # Grid — from meter when available, inverter-derived when not.
        "grid_voltage":            grid_voltage,   # Average of phase voltages (V)
        "grid_current":            grid_current,   # Average of phase currents (A)
        "grid_frequency":          freq,           # Hz
        "grid_power":              grid_power_w,   # W: +import / -export

        # Load — derived from inverter AC output + meter signed power.
        "load_power":              load_power_w,   # W (None if no meter)
        "load_voltage":            None,
        "daily_load_energy":       daily_load_kwh, # kWh (None if no meter)

        # Temperatures
        "inverter_temp":           inv_temp,       # °C
        "dc_temp":                 total_dc_w,     # W — DC power in dc_temp column

        # Energy — from inverter (PV yield) and meter (grid import/export).
        "daily_pv_energy":         daily_pv_kwh,   # kWh
        "total_pv_energy":         total_pv_kwh,   # kWh
        "daily_grid_import":       daily_import_kwh, # kWh (from meter, None if absent)
        "daily_grid_export":       daily_export_kwh, # kWh (from meter, None if absent)

        # CT clamp — not applicable for this installation.
        "ct_power":                None,
        "ct_load_power":           None,

        # Metadata
        "source_type":             "sungrow",
        "poll_success":            total_active_w is not None,
    }

    # ── Summary log ───────────────────────────────────────────────────────────
    meter_str = (
        f" MeterW={meter_w}W Load={load_power_w}W"
        f" Import={daily_import_kwh}kWh Export={daily_export_kwh}kWh"
    ) if meter_w is not None else " (no meter)"

    log.info(
        f"[{site_name}/{inv_name}] "
        f"Status={status_str} "
        f"AC={total_active_w}W DC={total_dc_w}W "
        f"Vgrid={grid_voltage}V Hz={freq} "
        f"YieldToday={daily_pv_kwh}kWh YieldTotal={total_pv_kwh}kWh "
        f"Temp={inv_temp}°C"
        f"{meter_str}"
    )

    return row


# ── Main poll entry point ─────────────────────────────────────────────────────

def poll(site: dict, client: SungrowClient) -> list[dict]:
    """Poll one Sungrow site (inverter + meter) and return reading dicts.

    Entry point called by collector.py — same signature as sunsynk_worker.poll().

    Steps:
      1. Ensure the client holds a valid token.
      2. Resolve ps_id from the site config (DB value or API discovery).
      3. Fetch the device list to find inverter(s) and meter(s).
      4. For each inverter (device_type=1):
           a. Fetch inverter real-time data.
           b. Fetch meter real-time data (device_type=7) if a meter exists.
           c. Normalise both into a solar_readings row.

    The meter fetch is best-effort — if it fails, the inverter row is still
    written with NULL for load_power, daily_grid_import, and daily_grid_export.

    Args:
        site:   Site config dict from collector.py load_sites(), containing:
                  site_name        — for logging
                  sungrow_plant_id — ps_id integer (auto-discovered if absent)
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
    # Guard against the string "None" that can arrive when a DB NULL passes
    # through SQLAlchemy mappings before the credentials are entered via the UI.
    _raw_ps_id = site.get("sungrow_plant_id")
    ps_id = (
        str(_raw_ps_id).strip()
        if _raw_ps_id and str(_raw_ps_id).strip().lower() not in ("none", "", "null")
        else None
    )
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

    # Step 3: Get device list
    try:
        ps_id_int = int(ps_id)
    except (TypeError, ValueError):
        log.error(f"[{site_name}] Invalid ps_id '{ps_id}'")
        return []

    devices = client.get_device_list(ps_id_int)
    if not devices:
        log.error(f"[{site_name}] No devices returned for ps_id={ps_id}")
        return []

    # Separate inverters (type=1) from meters (type=7)
    # There may be multiple inverters but typically one meter per plant.
    inverters = [d for d in devices if d.get("device_type") == 1]
    meters    = [d for d in devices if d.get("device_type") == 7]

    if not inverters:
        log.error(f"[{site_name}] No inverters found in device list")
        return []

    # Log meter discovery status
    if meters:
        meter_names = [m.get("device_name", m.get("ps_key")) for m in meters]
        log.debug(f"[{site_name}] Meter(s) found: {meter_names}")
    else:
        log.debug(f"[{site_name}] No meter in device list — load_power will be None")

    # Step 4: Poll each inverter
    results = []

    for inv_dev in inverters:
        inv_ps_key = inv_dev.get("ps_key")
        if not inv_ps_key:
            log.warning(f"[{site_name}] Inverter device has no ps_key — skipping: {inv_dev}")
            continue

        inv_name = inv_dev.get("device_name") or "Inverter_1"

        # Step 4a: Fetch inverter real-time data
        start   = time.monotonic()
        inv_raw = client.get_device_realtime(
            ps_key=inv_ps_key,
            device_type=1,
            point_id_list=INV_POINT_ID_LIST,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if inv_raw is None:
            log.error(f"[{site_name}/{inv_name}] No inverter data returned")
            continue

        # Step 4b: Fetch meter real-time data (best-effort)
        # Use the first available meter. If there are multiple meters in future
        # installations, each should have its own inverter association — for now
        # one meter serves the whole site.
        meter_raw = None
        if meters:
            meter_dev    = meters[0]
            meter_ps_key = meter_dev.get("ps_key")
            meter_name   = meter_dev.get("device_name", "Meter")
            if meter_ps_key:
                meter_raw = client.get_device_realtime(
                    ps_key=meter_ps_key,
                    device_type=7,
                    point_id_list=METER_POINT_ID_LIST,
                )
                if meter_raw is None:
                    log.warning(
                        f"[{site_name}/{meter_name}] Meter data unavailable — "
                        f"load_power will be None this cycle"
                    )

        # Step 4c: Normalise inverter + meter data into solar_readings row
        row = _normalise(inv_raw, meter_raw, site_name, inv_name)
        row["poll_duration_ms"] = elapsed_ms
        row["inverter_name"]    = inv_name
        row["inverter_sn"]      = inv_dev.get("device_sn") or inv_ps_key

        results.append(row)

    return results
