"""
SolarWatch — victron_worker.py

Polls a Victron Cerbo GX (or CCGX) via its local MQTT broker using the
VenusOS MQTT protocol. Connects directly over the local network — no VRM
cloud required, no API key, works fully offline.

Confirmed working against:
  Harmonia:  10.0.1.80  portalId 4c3fd33ef99a  (15kW, 3× SmartSolar MPPT)
  Oppikop:   10.0.1.85  portalId 7c669d4e45e6  (2×5kW, 2× SmartSolar MPPT)
  VE.Bus device instance: 261 on both sites
  Battery BMS device instance: 512 on both sites

Topic map (confirmed from live captures on both sites):
  ┌──────────────────────────────────────────────────────────┬─────────────────────────┐
  │ Topic                                                    │ solar_readings column   │
  ├──────────────────────────────────────────────────────────┼─────────────────────────┤
  │ system/0/Dc/Pv/Power          ← BEST PV source          │ pv1_power               │
  │ system/0/Dc/Pv/Current                                   │ pv1_current             │
  │ solarcharger/+/Pv/V           ← highest non-noise       │ pv1_voltage             │
  │ solarcharger/+/History/Daily/0/Yield ← confirmed 10.4kWh│ daily_pv_energy (summed)│
  │ system/0/Dc/Battery/Power     ← negated (see note)      │ battery_power           │
  │ system/0/Dc/Battery/Current   ← negated                 │ battery_current         │
  │ system/0/Dc/Battery/Voltage   ← Harmonia confirmed      │ battery_voltage         │
  │ battery/512/Soc               ← PRIMARY SOC source      │ battery_soc             │
  │ battery/512/Dc/0/Voltage      ← PRIMARY voltage (BMS)   │ battery_voltage         │
  │ battery/512/Dc/0/Power                                   │ (cross-check)           │
  │ battery/512/System/MinCellTemperature ← Oppikop 19°C    │ battery_temp            │
  │ grid/30/Ac/L1/Power           ← confirmed positive=import│ grid_power             │
  │ grid/30/Ac/L1/Voltage                                    │ grid_voltage            │
  │ grid/30/Ac/L1/Current                                    │ grid_current            │
  │ grid/30/Ac/Frequency                                     │ grid_frequency          │
  │ system/0/Ac/Consumption/L1/Power ← confirmed            │ load_power              │
  │ vebus/261/Ac/Out/L1/V         ← confirmed 237.3V        │ load_voltage            │
  │ vebus/261/Ac/Out/L1/I         ← confirmed 21.9A         │ (load current)          │
  └──────────────────────────────────────────────────────────┴─────────────────────────┘

Sign convention note:
  VenusOS publishes battery_power as POSITIVE when charging, NEGATIVE when discharging.
  The SolarWatch dashboard expects the opposite (negative=charging).
  victron_worker negates both battery_power and battery_current before storing.

NOT available on these units (confirmed from captures):
  - system/0/Dc/Battery/Soc   (not published on Harmonia, use battery/512/Soc instead)
  - Dc/0/Temperature anywhere (battery_temp comes from battery/512/System/MinCellTemperature)
  - solarcharger/+/History/Overall/Yield (total_pv_energy will be NULL)
  - grid/30/History/*          (no grid history topics — daily_grid derived in SQL)

daily_load_energy and daily_grid_import are NULL for Victron.
The solar.py queries derive these from the power time-series instead.

Dependencies:
  pip install paho-mqtt
"""

import json
import logging
import os
import time
import threading
from typing import Optional

log = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    log.warning(
        "paho-mqtt not installed — Victron sites cannot be polled. "
        "Install with: pip install paho-mqtt"
    )

# How long to collect MQTT messages after sending keepalive.
# The Cerbo GX publishes all topics within ~2s of keepalive.
# 12s gives margin for slow networks and multiple MPPT devices.
COLLECT_SECONDS = 12
CONNECT_TIMEOUT = 10


class VictronCollector:
    """Connects to one Cerbo GX, sends keepalive, collects all power topics."""

    def __init__(self, host: str, port: int):
        """Initialise the collector with the Cerbo GX host and port."""
        self.host = host
        self.port = port

        # Accumulators — lists of readings, averaged in _build_reading
        self._raw: dict[str, list[float]] = {}

        # Per-MPPT data
        self._charger_ids: set = set()
        self._charger_yield: dict[int, float] = {}   # today's kWh
        self._charger_pv_v: dict[int, float] = {}    # PV string voltage

        self._portal_id: Optional[str] = None
        self._connected = threading.Event()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _store(self, key: str, value: float):
        """Append a raw sensor value to the accumulator list for a given key."""
        if key not in self._raw:
            self._raw[key] = []
        self._raw[key].append(value)

    def _latest(self, key: str) -> Optional[float]:
        """Return the most recently received value for a key, or None."""
        vals = self._raw.get(key)
        return vals[-1] if vals else None

    def _avg(self, key: str) -> Optional[float]:
        """Return the average of all accumulated readings for a key, rounded to 3dp."""
        vals = self._raw.get(key)
        if not vals:
            return None
        return round(sum(vals) / len(vals), 3)

    # ── MQTT callbacks ────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        """MQTT on_connect callback — subscribe to Serial topic to discover portal ID."""
        if rc != 0:
            log.error(f"[victron/{self.host}] MQTT connect failed rc={rc}")
            return
        client.subscribe("N/+/system/0/Serial", qos=0)
        log.debug(f"[victron/{self.host}] Connected, discovering portal ID...")
        self._connected.set()

    def _on_message(self, client, userdata, msg):
        """MQTT on_message callback — route to correct handler by VenusOS service type."""
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode())
            value   = payload.get("value")
        except Exception:
            return

        if value is None:
            return

        parts = topic.split("/")
        # N / portalId / service / deviceInstance / path...
        if len(parts) < 5:
            return

        portal_id = parts[1]
        service   = parts[2]
        dev_id    = parts[3]
        suffix    = "/".join(parts[4:])

        # ── Step 1: discover portal ID ────────────────────────────────────────
        if not self._portal_id and service == "system" and suffix == "Serial":
            self._portal_id = portal_id
            log.debug(f"[victron/{self.host}] Portal ID: {portal_id}")

            # CRITICAL: subscribe BEFORE keepalive so we don't miss the burst
            base = f"N/{portal_id}"
            subs = [
                # System aggregated — best sources for PV, battery, load
                f"{base}/system/0/Dc/Battery/+",
                f"{base}/system/0/Dc/Pv/+",
                f"{base}/system/0/Ac/Consumption/+/Power",

                # Grid meter (device instance 30 confirmed on both sites)
                f"{base}/grid/+/Ac/+",

                # Solar chargers — PV string voltage and daily yield per MPPT
                f"{base}/solarcharger/+/Dc/0/+",
                f"{base}/solarcharger/+/Pv/+",
                f"{base}/solarcharger/+/Yield/Power",
                f"{base}/solarcharger/+/History/Daily/0/Yield",

                # VE.Bus (device instance 261 confirmed on both sites)
                # AC output = load voltage/current
                f"{base}/vebus/+/Ac/Out/L1/V",
                f"{base}/vebus/+/Ac/Out/L1/I",
                f"{base}/vebus/+/Ac/ActiveIn/L1/V",

                # Battery BMS (device instance 512 confirmed on both sites)
                # PRIMARY source for SOC, voltage, and temperature
                f"{base}/battery/+/Soc",
                f"{base}/battery/+/Dc/0/+",
                f"{base}/battery/+/System/MinCellTemperature",
                f"{base}/battery/+/System/MaxCellTemperature",
            ]
            for sub in subs:
                client.subscribe(sub, qos=0)

            # Now send keepalive — Cerbo bursts all registered topics immediately
            client.publish(f"R/{portal_id}/keepalive", "", qos=0)
            log.debug(f"[victron/{self.host}] Subscribed + keepalive sent")
            return

        if not self._portal_id or portal_id != self._portal_id:
            return

        try:
            val = float(value)
        except (TypeError, ValueError):
            return

        if service == "system" and dev_id == "0":
            self._handle_system(suffix, val)
        elif service == "grid":
            self._handle_grid(suffix, val)
        elif service == "solarcharger":
            self._handle_solarcharger(int(dev_id), suffix, val)
        elif service == "vebus":
            self._handle_vebus(suffix, val)
        elif service == "battery":
            self._handle_battery(suffix, val)

    def _handle_system(self, suffix: str, val: float):
        """Handle system/0/* — aggregated PV power, battery state, AC consumption."""
        if suffix == "Dc/Pv/Power":
            # Best PV source — system aggregator sums all MPPTs including string losses
            self._store("pv_power_system", val)
        elif suffix == "Dc/Pv/Current":
            self._store("pv_current_system", val)
        elif suffix == "Dc/Battery/Power":
            # VenusOS: positive=charging, negative=discharging
            # Negate to match dashboard convention (negative=charging)
            self._store("battery_power", -val)
        elif suffix == "Dc/Battery/Current":
            self._store("battery_current", -val)
        elif suffix == "Dc/Battery/Voltage":
            # Secondary voltage source — battery/512/Dc/0/Voltage preferred
            self._store("battery_voltage_system", val)
        elif suffix in ("Dc/Battery/Soc", "Dc/Battery/SOC"):
            # Fallback SOC — battery/512/Soc preferred
            self._store("battery_soc_system", val)
        elif suffix.startswith("Ac/Consumption/") and suffix.endswith("/Power"):
            if "/L1/" in suffix:
                self._store("load_power", val)
            elif "load_power" not in self._raw:
                self._store("load_power", val)

    def _handle_grid(self, suffix: str, val: float):
        """Handle grid/+/Ac/* — grid meter readings. Positive=importing, negative=exporting."""
        # Use L1 values preferentially; fall back to combined if L1 not seen
        if suffix == "Ac/L1/Power":
            self._store("grid_power", val)
        elif suffix == "Ac/Power" and "grid_power" not in self._raw:
            self._store("grid_power", val)
        elif suffix == "Ac/L1/Voltage":
            self._store("grid_voltage", val)
        elif suffix == "Ac/Voltage" and "grid_voltage" not in self._raw:
            self._store("grid_voltage", val)
        elif suffix == "Ac/L1/Current":
            self._store("grid_current", val)
        elif suffix == "Ac/Current" and "grid_current" not in self._raw:
            self._store("grid_current", val)
        elif suffix == "Ac/Frequency":
            self._store("grid_frequency", val)

    def _handle_solarcharger(self, charger_id: int, suffix: str, val: float):
        """Handle solarcharger/+/* — per-MPPT data for voltage and daily yield."""
        self._charger_ids.add(charger_id)

        if suffix == "Pv/V":
            # PV string input voltage — store per charger, pick highest non-noise
            if val > 10.0:  # filter night noise (0.47V and 1.64V seen in captures at night)
                self._charger_pv_v[charger_id] = val
        elif suffix == "Dc/0/Voltage":
            # Battery-side DC voltage — secondary voltage source
            self._store("battery_voltage_charger", val)
        elif suffix == "Dc/0/Power":
            # Per-MPPT DC power — used only if system/0/Dc/Pv/Power not available
            self._store(f"charger_power_{charger_id}", val)
        elif suffix == "Yield/Power":
            # Newer firmware realtime PV power — fallback if Dc/0/Power missing
            self._store(f"charger_yield_power_{charger_id}", val)
        elif suffix == "History/Daily/0/Yield":
            # Today's energy yield in kWh for this charger — sum across MPPTs
            self._charger_yield[charger_id] = val

    def _handle_vebus(self, suffix: str, val: float):
        """Handle vebus/+/* — AC output voltage/current from Multiplus/Quattro."""
        if suffix == "Ac/Out/L1/V":
            self._store("load_voltage", val)
        elif suffix == "Ac/Out/L1/I":
            self._store("load_current", val)
        elif suffix == "Ac/ActiveIn/L1/V":
            # Grid voltage from VE.Bus — use if grid/30 didn't provide it
            if "grid_voltage" not in self._raw:
                self._store("grid_voltage", val)

    def _handle_battery(self, suffix: str, val: float):
        """Handle battery/+/* — BMS data. PRIMARY source for SOC, voltage, temperature."""
        if suffix == "Soc":
            # BMS SOC — most accurate, overrides system/0 if both present
            self._store("battery_soc_bms", val)
        elif suffix == "Dc/0/Voltage":
            # BMS terminal voltage — most accurate
            self._store("battery_voltage_bms", val)
        elif suffix == "Dc/0/Power":
            self._store("battery_power_bms", val)
        elif suffix == "System/MinCellTemperature":
            # Confirmed present on Oppikop (19°C) — use as battery_temp
            self._store("battery_temp", val)
        elif suffix == "System/MaxCellTemperature":
            # Also collect max in case min not available
            if "battery_temp" not in self._raw:
                self._store("battery_temp", val)

    # ── Build final reading ───────────────────────────────────────────────────

    def _build_reading(self) -> dict:
        """Assemble solar_readings-compatible dict from all accumulated MQTT values.

        Priority order for each field:
          PV power:        system/0/Dc/Pv/Power (best) → sum charger Dc/0/Power → Yield/Power
          Battery SOC:     battery/512/Soc (BMS, best) → system/0/Dc/Battery/Soc (fallback)
          Battery voltage: battery/512/Dc/0/Voltage (BMS, best) → system/0/Dc/Battery/Voltage → charger avg
          Battery temp:    battery/512/System/MinCellTemperature → None
          Grid voltage:    grid/30/Ac/L1/Voltage → vebus/Ac/ActiveIn/L1/V
        """

        # ── PV power ─────────────────────────────────────────────────────────
        # Prefer system/0/Dc/Pv/Power (aggregated by Cerbo GX, most reliable)
        pv_power = self._avg("pv_power_system")
        if pv_power is None:
            # Fallback: sum per-MPPT Dc/0/Power
            charger_powers = [
                self._latest(f"charger_power_{cid}")
                for cid in self._charger_ids
                if self._latest(f"charger_power_{cid}") is not None
            ]
            if charger_powers:
                pv_power = round(sum(charger_powers), 1)
            else:
                # Last resort: sum Yield/Power topics (newer firmware)
                yp = [
                    self._latest(f"charger_yield_power_{cid}")
                    for cid in self._charger_ids
                    if self._latest(f"charger_yield_power_{cid}") is not None
                ]
                pv_power = round(sum(yp), 1) if yp else None

        # ── PV voltage (highest string voltage across all MPPTs) ──────────
        # Filter values below 10V — night noise confirmed at 0.47V and 1.64V
        valid_pv_v = {cid: v for cid, v in self._charger_pv_v.items() if v > 10.0}
        pv_voltage = round(max(valid_pv_v.values()), 1) if valid_pv_v else None

        # ── PV current (from system/0/Dc/Pv/Current) ──────────────────────
        pv_current = self._avg("pv_current_system")

        # ── Battery SOC — BMS primary, system fallback ──────────────────────
        battery_soc = self._avg("battery_soc_bms") or self._avg("battery_soc_system")

        # ── Battery voltage — BMS primary, system fallback, charger fallback ─
        battery_voltage = (
            self._avg("battery_voltage_bms")
            or self._avg("battery_voltage_system")
        )
        if battery_voltage is None:
            # Last resort: average of charger DC output voltages
            charger_vs = list(self._raw.get("battery_voltage_charger", []))
            if charger_vs:
                battery_voltage = round(sum(charger_vs) / len(charger_vs), 2)

        # ── Daily PV energy — sum all MPPT History/Daily/0/Yield values ──────
        daily_pv = (
            round(sum(self._charger_yield.values()), 3)
            if self._charger_yield else None
        )

        return {
            "source_type": "victron",

            # PV — system-level aggregated (most reliable)
            "pv1_voltage":  pv_voltage,
            "pv1_current":  round(pv_current, 3) if pv_current is not None else None,
            "pv1_power":    round(pv_power, 1) if pv_power is not None else None,
            "pv2_voltage":  None,   # single aggregated PV reading
            "pv2_current":  None,
            "pv2_power":    None,

            # Battery — BMS values preferred
            "battery_voltage":  battery_voltage,
            "battery_current":  self._avg("battery_current"),   # already negated
            "battery_power":    self._avg("battery_power"),     # already negated
            "battery_soc":      battery_soc,
            "battery_temp":     self._avg("battery_temp"),      # MinCellTemperature

            # Grid — from grid meter (grid/30), positive=importing
            "grid_voltage":     self._avg("grid_voltage"),
            "grid_frequency":   self._avg("grid_frequency"),
            "grid_power":       self._avg("grid_power"),
            "grid_current":     self._avg("grid_current"),

            # Load — from VE.Bus AC output
            "load_power":       self._avg("load_power"),
            "load_voltage":     self._avg("load_voltage"),

            # Temperatures — MinCellTemperature used as inverter_temp proxy
            "inverter_temp":    self._avg("battery_temp"),
            "dc_temp":          None,

            # Daily / total energy
            "daily_pv_energy":          daily_pv,
            "total_pv_energy":          None,   # not published on these units
            "daily_battery_charge":     None,
            "daily_battery_discharge":  None,
            "daily_grid_import":        None,   # derived in solar.py SQL
            "daily_grid_export":        None,
            "daily_load_energy":        None,   # derived in solar.py SQL

            # CT (not applicable for Victron)
            "ct_power":      None,
            "ct_load_power": None,

            # Set by poll()
            "poll_duration_ms": 0,
            "poll_success":     True,
        }

    # ── Collect ───────────────────────────────────────────────────────────────

    def collect(self) -> Optional[dict]:
        """Connect to the Cerbo GX, send keepalive, collect all topics, return reading."""
        if not MQTT_AVAILABLE:
            return None

        client = mqtt.Client(
            client_id=f"solarwatch_{self.host.replace('.','_')}",
            clean_session=True,
        )
        client.on_connect = self._on_connect
        client.on_message = self._on_message

        try:
            client.connect(self.host, self.port, keepalive=30)
            client.loop_start()

            if not self._connected.wait(timeout=CONNECT_TIMEOUT):
                log.error(f"[victron/{self.host}] Connection timeout after {CONNECT_TIMEOUT}s")
                return None

            time.sleep(COLLECT_SECONDS)

            client.loop_stop()
            client.disconnect()

        except Exception as exc:
            log.error(f"[victron/{self.host}] MQTT error: {exc}")
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
            return None

        if not self._portal_id:
            log.error(
                f"[victron/{self.host}] Could not discover portal ID — "
                f"is the Cerbo GX reachable on port {self.port}?"
            )
            return None

        return self._build_reading()


# ── Public API ────────────────────────────────────────────────────────────────

def poll(inv: dict, site_name: str = "unknown") -> Optional[dict]:
    """Poll one Victron Cerbo GX via MQTT and return a solar_readings-compatible dict.

    inv dict keys (set via web UI, stored in sites.inverters JSONB):
      name       string  display name for logging
      mqtt_host  string  Cerbo GX local IP address (e.g. "10.0.1.80")
      ip         string  legacy field name — accepted as fallback for mqtt_host
      mqtt_port  int     MQTT port, default 1883

    Returns a normalised dict or None on failure.
    Same contract as deye_worker.poll() — drop-in compatible.
    """
    if not MQTT_AVAILABLE:
        log.warning(f"[{site_name}] paho-mqtt not installed. Run: pip install paho-mqtt")
        return None

    label     = f"{site_name}/{inv.get('name', 'victron')}"
    mqtt_host = str(inv.get("mqtt_host") or inv.get("ip") or "").strip()
    mqtt_port = int(inv.get("mqtt_port") or 1883)

    if not mqtt_host:
        log.error(
            f"[{label}] No MQTT host configured. "
            f"Set the Cerbo GX IP address via the web UI: Sites → Edit → Add device."
        )
        return None

    start     = time.monotonic()
    collector = VictronCollector(host=mqtt_host, port=mqtt_port)
    row       = collector.collect()
    elapsed   = int((time.monotonic() - start) * 1000)

    if row is None:
        return None

    row["poll_duration_ms"] = elapsed

    # Log summary — flag any critical missing fields
    missing = [
        k for k in ("battery_soc", "battery_voltage", "pv1_power", "load_power", "grid_power")
        if row.get(k) is None
    ]

    log.info(
        f"[{label}] "
        f"SOC={row.get('battery_soc')}% "
        f"BattV={row.get('battery_voltage')}V "
        f"PV={row.get('pv1_power') or 0:.0f}W "
        f"Grid={row.get('grid_power')}W "
        f"Load={row.get('load_power')}W "
        f"Temp={row.get('battery_temp')}°C "
        f"({elapsed}ms)"
        + (f" | missing: {missing}" if missing else " | OK")
    )

    return row
