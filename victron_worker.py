"""
SolarWatch — victron_worker.py

Polls a Victron Cerbo GX (or CCGX) via its local MQTT broker using the
VenusOS MQTT protocol. Connects directly over the local network — no VRM
cloud required, no API key, works fully offline.

Confirmed working against:
  Site A: 10.0.1.80  portalId 4c3fd33ef99a  (3× SmartSolar MPPT, charger IDs 1,2,3)
  Site B: 10.0.1.85  portalId 7c669d4e45e6  (2× SmartSolar MPPT, charger IDs 258,260)

Protocol reference:
  https://github.com/victronenergy/venus-html5-app/blob/master/docs/MQTT.md

How it works:
  1. Connect to the Cerbo GX MQTT broker (port 1883, no auth on local network)
  2. Subscribe to N/+/system/0/Serial to discover the portalId
  3. Publish R/{portalId}/keepalive — triggers the Cerbo to immediately
     publish ALL registered service topics (battery SOC, PV power, etc.)
  4. Subscribe to all required topics using wildcards
  5. Collect for COLLECT_SECONDS, sum/average across multiple MPPTs
  6. Derive battery_voltage from Power/Current if not published directly
  7. Return a normalised dict matching solar_readings columns exactly

Inverter config (sites.inverters JSONB — set via web UI):
  {
    "name":      "Victron_1",
    "mqtt_host": "10.0.1.80",
    "mqtt_port": 1883           (optional, default 1883)
  }

Fallback: if inverters list is empty, reads VICTRON_MQTT_HOST from environment.

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
# Cerbo GX publishes all topics within ~2s of keepalive.
# 8s gives plenty of margin for slow networks.
COLLECT_SECONDS = 12  # 12s gives full keepalive burst time on slower networks
CONNECT_TIMEOUT = 10


class VictronCollector:
    """
    Connects to one Cerbo GX, sends keepalive, collects all power topics.

    Handles multiple solar chargers (any device instance) by summing
    PV power and PV voltage/current across all MPPTs.
    """

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

        # Raw accumulated values — keyed by topic suffix
        self._raw: dict[str, list[float]] = {}
        self._charger_ids: set = set()
        self._charger_power: dict[int, float] = {}   # charger_id → W
        self._charger_current: dict[int, float] = {} # charger_id → A
        self._charger_voltage: dict[int, float] = {} # charger_id → V
        self._charger_yield: dict[int, float] = {}   # charger_id → kWh today

        self._portal_id: Optional[str] = None
        self._connected = threading.Event()
        self._client: Optional[mqtt.Client] = None

    # ── helpers ──────────────────────────────────────────────────────────────

    def _store(self, key: str, value: float):
        """Accumulate readings — we'll average at the end."""
        if key not in self._raw:
            self._raw[key] = []
        self._raw[key].append(value)

    def _latest(self, key: str) -> Optional[float]:
        """Return the most recent reading for a key."""
        vals = self._raw.get(key)
        return vals[-1] if vals else None

    def _avg(self, key: str) -> Optional[float]:
        vals = self._raw.get(key)
        if not vals:
            return None
        return round(sum(vals) / len(vals), 3)

    # ── MQTT callbacks ────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error(f"[victron/{self.host}] MQTT connect failed rc={rc}")
            return

        # Step 1: discover portalId — Cerbo publishes Serial on any subscription
        client.subscribe("N/+/system/0/Serial", qos=0)
        log.debug(f"[victron/{self.host}] Connected, discovering portal ID...")
        self._connected.set()

    def _on_message(self, client, userdata, msg):
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

        # ── Discover portal ID ──────────────────────────────────────────────
        if not self._portal_id and service == "system" and suffix == "Serial":
            self._portal_id = portal_id
            log.debug(f"[victron/{self.host}] Portal ID: {portal_id}")

            # IMPORTANT: subscribe FIRST, then send keepalive.
            # The keepalive triggers the Cerbo to immediately republish all
            # registered topics. If subscriptions aren't set up yet, the
            # burst of messages arrives before we can receive them.
            base = f"N/{portal_id}"
            subs = [
                # Battery — system level aggregated
                f"{base}/system/0/Dc/Battery/+",
                # AC consumption
                f"{base}/system/0/Ac/Consumption/+/Power",
                # Grid meter (device instance varies — wildcard covers all)
                f"{base}/grid/+/Ac/+",
                # Solar charger — DC output and PV input
                f"{base}/solarcharger/+/Dc/0/+",
                f"{base}/solarcharger/+/Pv/+",
                # PV power — newer firmware publishes under Yield/Power
                f"{base}/solarcharger/+/Yield/Power",
                # Daily yield
                f"{base}/solarcharger/+/History/Daily/0/Yield",
                # VE.Bus inverter/charger
                f"{base}/vebus/+/Dc/0/Temperature",
                f"{base}/vebus/+/Ac/Out/L1/V",
                f"{base}/vebus/+/Ac/Out/L1/I",
                # Battery monitor (BMV/SmartShunt) — may have SOC if system level missing
                f"{base}/battery/+/Dc/0/+",
                f"{base}/battery/+/Soc",
            ]
            for sub in subs:
                client.subscribe(sub, qos=0)

            # Now send keepalive — Cerbo will burst all registered topics
            # The keepalive payload must be empty string, not None
            client.publish(f"R/{portal_id}/keepalive", "", qos=0)
            log.debug(f"[victron/{self.host}] Keepalive sent, waiting for topic burst...")
            return

        if not self._portal_id or portal_id != self._portal_id:
            return

        # ── Route to correct handler ────────────────────────────────────────
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
            # BMV / SmartShunt / BMS — may have more precise SOC than system level
            if suffix == "Soc" and "battery_soc" not in self._raw:
                self._store("battery_soc", val)
            elif suffix == "Dc/0/Soc" and "battery_soc" not in self._raw:
                self._store("battery_soc", val)
            elif suffix == "Dc/0/Voltage" and "battery_voltage" not in self._raw:
                self._store("battery_voltage", val)
            elif suffix == "Dc/0/Temperature" and "battery_temp" not in self._raw:
                self._store("battery_temp", val)

    def _handle_system(self, suffix: str, val: float):
        """system/0/Dc/Battery/* and system/0/Ac/Consumption/*/Power"""
        if suffix == "Dc/Battery/Power":
            self._store("battery_power", val)
        elif suffix == "Dc/Battery/Current":
            self._store("battery_current", val)
        elif suffix in ("Dc/Battery/Soc", "Dc/Battery/SOC"):
            # VenusOS publishes as Soc (mixed case) — handle both
            self._store("battery_soc", val)
        elif suffix == "Dc/Battery/Voltage":
            self._store("battery_voltage", val)
        elif suffix in ("Dc/Battery/Temperature", "Dc/Battery/Temp"):
            self._store("battery_temp", val)
        elif suffix.startswith("Ac/Consumption/") and suffix.endswith("/Power"):
            # Use L1/Power for single-phase; skip "combined" topic if both arrive
            if "/L1/" in suffix:
                self._store("load_power", val)
            elif "load_power" not in self._raw:
                self._store("load_power", val)

    def _handle_grid(self, suffix: str, val: float):
        """grid/30/Ac/* — grid meter readings"""
        if suffix == "Ac/L1/Power" or (suffix == "Ac/Power" and "grid_power" not in self._raw):
            self._store("grid_power", val)
        elif suffix == "Ac/L1/Voltage" or (suffix == "Ac/Voltage" and "grid_voltage" not in self._raw):
            self._store("grid_voltage", val)
        elif suffix == "Ac/L1/Current" or (suffix == "Ac/Current" and "grid_current" not in self._raw):
            self._store("grid_current", val)
        elif suffix == "Ac/Frequency":
            self._store("grid_frequency", val)

    def _handle_solarcharger(self, charger_id: int, suffix: str, val: float):
        """solarcharger/{id}/* — per MPPT data, summed across all chargers"""
        self._charger_ids.add(charger_id)

        if suffix == "Dc/0/Power":
            # DC output power to battery — this IS the PV production power
            self._charger_power[charger_id] = val
        elif suffix == "Yield/Power":
            # Newer firmware alternative for realtime PV power
            # Only use if Dc/0/Power not already received for this charger
            if charger_id not in self._charger_power:
                self._charger_power[charger_id] = val
        elif suffix == "Dc/0/Current":
            self._charger_current[charger_id] = val
        elif suffix == "Dc/0/Voltage":
            # Battery-side voltage from charger — same as battery voltage
            self._charger_voltage[charger_id] = val
        elif suffix == "Pv/V":
            # PV string input voltage
            key = f"pv_voltage_{charger_id}"
            self._store(key, val)
        elif suffix == "History/Daily/0/Yield":
            # Today's energy yield in kWh for this charger
            self._charger_yield[charger_id] = val

    def _handle_vebus(self, suffix: str, val: float):
        """vebus (Multiplus/Quattro) — AC output and temperature"""
        if suffix == "Dc/0/Temperature":
            self._store("battery_temp", val)
        elif suffix == "Ac/Out/L1/V":
            self._store("load_voltage", val)
        elif suffix == "Ac/Out/L1/I":
            self._store("load_current", val)

    # ── Collect and return ────────────────────────────────────────────────────

    def collect(self) -> Optional[dict]:
        if not MQTT_AVAILABLE:
            return None

        client = mqtt.Client(
            client_id=f"solarwatch_{self.host.replace('.','_')}",
            clean_session=True,
        )
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        self._client = client

        try:
            client.connect(self.host, self.port, keepalive=30)
            client.loop_start()

            if not self._connected.wait(timeout=CONNECT_TIMEOUT):
                log.error(f"[victron/{self.host}] Connection timeout after {CONNECT_TIMEOUT}s")
                return None

            # Wait for portal discovery + keepalive response
            # The Cerbo takes ~1s to process the keepalive
            deadline = time.monotonic() + COLLECT_SECONDS
            while time.monotonic() < deadline:
                time.sleep(0.5)

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
            log.error(f"[victron/{self.host}] Could not discover portal ID — is the Cerbo reachable?")
            return None

        return self._build_reading()

    def _build_reading(self) -> dict:
        """Assemble solar_readings compatible dict from collected MQTT values."""

        # ── PV power: sum across all MPPT chargers ────────────────────────────
        pv_total_w = sum(self._charger_power.values()) if self._charger_power else None

        # If Dc/0/Power wasn't published, derive from Voltage × Current
        if pv_total_w is None and self._charger_voltage and self._charger_current:
            derived = {}
            for cid in self._charger_ids:
                v = self._charger_voltage.get(cid)
                i = self._charger_current.get(cid)
                if v is not None and i is not None:
                    derived[cid] = v * i
            if derived:
                pv_total_w = sum(derived.values())

        # Assign pv1 = total across all MPPTs (single PV value for this site)
        # pv2 is unused for Victron — the dashboard handles None gracefully
        pv1_power = round(pv_total_w, 1) if pv_total_w is not None else None

        # ── PV voltage: highest Pv/V seen across chargers ────────────────────
        pv_voltages = []
        for cid in self._charger_ids:
            v = self._latest(f"pv_voltage_{cid}")
            if v is not None and v > 5:  # filter noise (0.47V = night reading)
                pv_voltages.append(v)
        pv1_voltage = round(max(pv_voltages), 1) if pv_voltages else None

        # ── Battery voltage: prefer direct reading, derive if missing ─────────
        batt_v = self._avg("battery_voltage")
        if batt_v is None:
            # Derive from charger DC output voltage (battery-side)
            charger_voltages = list(self._charger_voltage.values())
            if charger_voltages:
                batt_v = round(sum(charger_voltages) / len(charger_voltages), 2)

        # Fallback: derive V = P / I (e.g. -63W / -1.2A ≈ 52.5V)
        if batt_v is None:
            batt_p = self._latest("battery_power")
            batt_i = self._latest("battery_current")
            if batt_p is not None and batt_i is not None and abs(batt_i) > 0.05:
                batt_v = round(abs(batt_p / batt_i), 2)

        # ── Daily PV energy: sum all MPPT yields ──────────────────────────────
        daily_pv = None
        if self._charger_yield:
            daily_pv = round(sum(self._charger_yield.values()), 3)

        # ── Assemble row ──────────────────────────────────────────────────────
        row = {
            "source_type": "victron",

            # PV — from solar charger(s), summed
            "pv1_voltage":  pv1_voltage,
            "pv1_current":  None,          # not available via system MQTT
            "pv1_power":    pv1_power,
            "pv2_voltage":  None,          # single aggregated PV reading
            "pv2_current":  None,
            "pv2_power":    None,

            # Battery
            # VenusOS sign convention: positive=charging, negative=discharging
            # Dashboard convention:    negative=charging, positive=discharging
            # Negate both so the dashboard arrows and labels display correctly.
            "battery_voltage":  batt_v,
            "battery_current":  -self._avg("battery_current") if self._avg("battery_current") is not None else None,
            "battery_power":    -self._avg("battery_power")   if self._avg("battery_power")   is not None else None,
            "battery_soc":      self._avg("battery_soc"),
            "battery_temp":     self._avg("battery_temp"),

            # Grid
            "grid_voltage":     self._avg("grid_voltage"),
            "grid_frequency":   self._avg("grid_frequency"),
            "grid_power":       self._avg("grid_power"),
            "grid_current":     self._avg("grid_current"),

            # Load
            "load_power":       self._avg("load_power"),
            "load_voltage":     self._avg("load_voltage"),

            # Temperatures
            "inverter_temp":    self._avg("battery_temp"),  # best proxy available
            "dc_temp":          None,

            # Daily / total energy
            "daily_pv_energy":          daily_pv,
            "total_pv_energy":          None,   # not available via system MQTT
            "daily_battery_charge":     None,
            "daily_battery_discharge":  None,
            "daily_grid_import":        None,
            "daily_grid_export":        None,
            "daily_load_energy":        None,

            # CT (not applicable)
            "ct_power":      None,
            "ct_load_power": None,

            # Metadata
            "poll_duration_ms": 0,   # set by caller
            "poll_success":     True,
        }

        return row


# ── PUBLIC API ─────────────────────────────────────────────────────────────────

def poll(inv: dict, site_name: str = "unknown") -> Optional[dict]:
    """
    Poll one Victron Cerbo GX via MQTT.

    inv dict (from sites.inverters JSONB — configured via web UI):
      name       string  display name for logging
      mqtt_host  string  Cerbo GX IP address (e.g. "10.0.1.80")
      mqtt_port  int     MQTT port, default 1883

    Returns a normalised dict matching solar_readings columns, or None on failure.
    Same contract as deye_worker.poll() — drop-in compatible.
    """
    if not MQTT_AVAILABLE:
        log.warning(
            f"[{site_name}] paho-mqtt not installed. Run: pip install paho-mqtt"
        )
        return None

    label     = f"{site_name}/{inv.get('name', 'victron')}"
    # Accept mqtt_host (new) or ip (legacy — stored before mqtt_host field was introduced)
    mqtt_host = str(inv.get("mqtt_host") or inv.get("ip") or os.getenv("VICTRON_MQTT_HOST", "")).strip()
    mqtt_port = int(inv.get("mqtt_port") or os.getenv("VICTRON_MQTT_PORT", "1883"))

    if not mqtt_host:
        log.error(
            f"[{label}] No MQTT host configured. "
            f"Set mqtt_host in the inverter config or VICTRON_MQTT_HOST env var."
        )
        return None

    start     = time.monotonic()
    collector = VictronCollector(host=mqtt_host, port=mqtt_port)
    row       = collector.collect()
    elapsed   = int((time.monotonic() - start) * 1000)

    if row is None:
        return None

    row["poll_duration_ms"] = elapsed

    # Log summary — flag missing critical fields
    pv_w   = row.get("pv1_power") or 0
    soc    = row.get("battery_soc")
    batt_v = row.get("battery_voltage")

    missing = []
    if soc is None:
        missing.append("battery_soc")
    if batt_v is None:
        missing.append("battery_voltage")
    if row.get("pv1_power") is None:
        missing.append("pv1_power")
    if row.get("load_power") is None:
        missing.append("load_power")

    log.info(
        f"[{label}] "
        f"SOC={soc}% "
        f"BattV={batt_v}V "
        f"PV={pv_w:.0f}W "
        f"Grid={row.get('grid_power')}W "
        f"Load={row.get('load_power')}W "
        f"({elapsed}ms)"
        + (f" | missing: {missing}" if missing else " | OK")
    )

    return row
