#!/usr/bin/env python3
"""
SolarWatch — test_sungrow.py

Standalone validation script for the Sungrow iSolarCloud API worker.
Run this from PyCharm (or the terminal) to confirm:

  1. Authentication works with the plant owner's credentials
  2. Plant discovery returns the correct plant (Bitrad Factory)
  3. Device discovery returns the SG125CX-P2
  4. Real-time data returns values that match what you see in iSolarCloud

This script does NOT touch the database — it is purely a network test.
It is safe to run against the live API as many times as needed.

Usage (from the solarwatch project root):
    python test_sungrow.py

Or set credentials as environment variables before running:
    export SUNGROW_USERNAME="user@example.com"
    export SUNGROW_PASSWORD="yourpassword"
    export SUNGROW_APPKEY="ECEB3991B78091C1493ECDEC86C3556D"
    export SUNGROW_SECRET="4d9mwbixprcbukxxu0ndste60r82gy4i"
    python test_sungrow.py

Alternatively, create a .env file in the project root and this script
will load it automatically via python-dotenv if installed.

Expected output when everything works:
    ✓ Login OK — token received
    ✓ Plants: 1 found
      Plant 0: Bitrad Factory (ps_id=XXXXX)
    ✓ Devices for plant XXXXX: 2 found
      Device 0: Inverter1 (ps_key=XXXXX_11_1_1, sn=..., type=1)
    ✓ Real-time data: 18 points returned
      p13001 operating_status    = 2          (Running)
      p13003 total_active_power  = 33.5 kW
      p13002 total_dc_power      = 34.1 kW
      ...
    ✓ Normalised row:
      pv1_power    = 33500.0 W
      grid_voltage = 236.6 V
      grid_hz      = 50.13 Hz
      ...
"""

from __future__ import annotations

import os
import sys
import json
import logging

# ── Load .env if present (dev convenience) ────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — use env vars or edit the constants below

# ── Logging — show everything during testing ──────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Suppress noisy urllib3 debug output
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

log = logging.getLogger("test_sungrow")

# ── Credentials ───────────────────────────────────────────────────────────────
# Fill these in, or set as environment variables before running.
# Never commit real credentials to version control.

USERNAME = os.getenv("SUNGROW_USERNAME", "")   # Plant owner's iSolarCloud email
PASSWORD = os.getenv("SUNGROW_PASSWORD", "")   # Plant owner's iSolarCloud password
APPKEY   = os.getenv("SUNGROW_APPKEY",   "")   # Your developer portal Appkey
SECRET   = os.getenv("SUNGROW_SECRET",   "")   # Your developer portal Secret key

# If credentials are not set via env vars, you can hardcode them here for testing
# (remove before committing!):
# USERNAME = "plant_owner@example.com"
# PASSWORD = "their_password"
# APPKEY   = "ECEB3991B78091C1493ECDEC86C3556D"
# SECRET   = "4d9mwbixprcbukxxu0ndste60r82gy4i"


def check_credentials():
    """Abort early with a clear message if credentials are missing."""
    missing = []
    if not USERNAME: missing.append("SUNGROW_USERNAME")
    if not PASSWORD: missing.append("SUNGROW_PASSWORD")
    if not APPKEY:   missing.append("SUNGROW_APPKEY")
    if not SECRET:   missing.append("SUNGROW_SECRET")
    if missing:
        print(f"\n✗ Missing credentials: {', '.join(missing)}")
        print("  Set them as environment variables or edit the constants in this script.")
        sys.exit(1)


def section(title: str):
    """Print a test section header."""
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print('─' * 60)


def ok(msg: str):
    """Print a success line."""
    print(f"  ✓ {msg}")


def fail(msg: str):
    """Print a failure line."""
    print(f"  ✗ {msg}")


def info(msg: str):
    """Print an info line."""
    print(f"    {msg}")


def run():
    """Execute the full validation sequence and print results."""

    # ── Enable debug output in the worker ─────────────────────────────────────
    os.environ["SUNGROW_DEBUG"] = "1"

    # Import the worker — must be in the same directory or on sys.path
    try:
        import sungrow_worker as w
    except ImportError as exc:
        print(f"\n✗ Cannot import sungrow_worker: {exc}")
        print("  Run this script from the solarwatch project root directory.")
        sys.exit(1)

    check_credentials()

    print("\n" + "═" * 60)
    print("  SolarWatch — Sungrow iSolarCloud API Test")
    print("═" * 60)
    print(f"  Gateway : {w.BASE_URL}")
    print(f"  Username: {USERNAME}")
    print(f"  Appkey  : {APPKEY[:8]}...{APPKEY[-4:]}")

    # ── STEP 1: Authentication ─────────────────────────────────────────────────
    section("Step 1: Authentication")

    client = w.SungrowClient(
        username=USERNAME,
        password=PASSWORD,
        appkey=APPKEY,
        secret=SECRET,
    )

    success = client.ensure_logged_in()
    if not success:
        fail("Login failed — check credentials and gateway URL")
        print("\n  Tip: Verify the plant owner can log in at https://web3.isolarcloud.com.hk/")
        sys.exit(1)

    ok(f"Login OK — token: {client._token[:12]}...")

    # ── STEP 2: Plant list ─────────────────────────────────────────────────────
    section("Step 2: Plant list")

    plants = client.get_plant_list()
    if not plants:
        fail("No plants returned — check account has registered plants")
        sys.exit(1)

    ok(f"Plants: {len(plants)} found")
    for i, p in enumerate(plants):
        ps_id = p.get("ps_id") or p.get("id")
        name  = p.get("ps_name") or p.get("name") or "unknown"
        info(f"Plant {i}: {name!r} (ps_id={ps_id})")
        info(f"  Full response: {json.dumps(p, indent=4, default=str)}")

    # Use the first plant for subsequent tests
    first_plant = plants[0]
    ps_id = str(first_plant.get("ps_id") or first_plant.get("id") or "")
    if not ps_id:
        fail(f"Cannot determine ps_id from: {first_plant}")
        sys.exit(1)

    info(f"\n  → Using ps_id={ps_id} for device discovery")

    # ── STEP 3: Device list ────────────────────────────────────────────────────
    section(f"Step 3: Device list (plant ps_id={ps_id})")

    devices = client.get_device_list(ps_id)
    if not devices:
        fail(f"No devices returned for plant {ps_id}")
        sys.exit(1)

    ok(f"Devices: {len(devices)} found")
    for i, d in enumerate(devices):
        ps_key   = d.get("ps_key")   or d.get("dev_sn")
        dev_name = d.get("dev_name") or d.get("name") or "unknown"
        dev_type = d.get("dev_type") or "?"
        dev_sn   = d.get("dev_sn")   or "?"
        info(f"Device {i}: {dev_name!r}  ps_key={ps_key}  sn={dev_sn}  type={dev_type}")
        info(f"  Full response: {json.dumps(d, indent=4, default=str)}")

    # Use the first device that looks like an inverter
    inverters = [
        d for d in devices
        if str(d.get("dev_type", "")).strip() in ("1", "inverter", "")
    ] or devices

    first_inv = inverters[0]
    ps_key = str(first_inv.get("ps_key") or first_inv.get("dev_sn") or "")
    if not ps_key:
        fail(f"Cannot determine ps_key from: {first_inv}")
        sys.exit(1)

    info(f"\n  → Using ps_key={ps_key} for real-time data")

    # ── STEP 4: Real-time data ─────────────────────────────────────────────────
    section(f"Step 4: Real-time data (ps_key={ps_key})")

    raw = client.get_device_realtime(ps_key)
    if raw is None:
        fail("No real-time data returned")
        sys.exit(1)

    ok(f"Real-time data: {len(raw)} data points returned")
    print()

    # Print known points with friendly names
    known = {v: k for k, v in w._POINT.items()}  # reverse map: point_id → name
    for point_id, value in sorted(raw.items()):
        name = known.get(point_id, "")
        name_str = f"  ({name})" if name else ""
        info(f"{point_id:<12} = {str(value):<15}{name_str}")

    # ── STEP 5: Normalisation ──────────────────────────────────────────────────
    section("Step 5: Normalised row (solar_readings columns)")

    row = w._normalise(raw, "TestSite", "Inverter_1")

    # Only print columns that have actual values
    print()
    for col, val in sorted(row.items()):
        if val is not None and col not in ("source_type", "poll_success", "poll_duration_ms"):
            info(f"{col:<30} = {val}")

    print()
    ok(f"source_type     = {row.get('source_type')}")
    ok(f"poll_success    = {row.get('poll_success')}")
    info(f"(battery columns all None — correct for grid-tie)")

    # ── STEP 6: Full poll() function ───────────────────────────────────────────
    section("Step 6: Full poll() function (as collector.py calls it)")

    # Simulate a site config dict — same structure as what collector.py passes
    mock_site = {
        "site_name":          "BitradFactory",
        "sungrow_plant_id":   ps_id,   # ps_id discovered in Step 2
        # sungrow_device_sn not needed here — poll() fetches the device list itself
    }

    results = w.poll(mock_site, client)
    if results:
        ok(f"poll() returned {len(results)} reading(s)")
        for r in results:
            info(f"  inverter_name = {r.get('inverter_name')}")
            info(f"  inverter_sn   = {r.get('inverter_sn')}")
            info(f"  pv1_power     = {r.get('pv1_power')} W")
            info(f"  grid_voltage  = {r.get('grid_voltage')} V")
            info(f"  grid_hz       = {r.get('grid_frequency')} Hz")
            info(f"  yield_today   = {r.get('daily_pv_energy')} kWh")
            info(f"  poll_ms       = {r.get('poll_duration_ms')}")
    else:
        fail("poll() returned no results — check logs above for details")

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    print()
    print("═" * 60)
    print("  IMPORTANT — record these values for the web UI:")
    print("═" * 60)
    print(f"  Plant ID (sungrow_plant_id) : {ps_id}")
    print(f"  Device Key (sungrow_device_sn): {ps_key}")
    print()
    print("  Enter these in the Sites → Edit page for the Bitrad Factory site.")
    print("  Without them the collector will re-discover them on every restart,")
    print("  which wastes 2 extra API calls per poll cycle.")
    print()


if __name__ == "__main__":
    run()