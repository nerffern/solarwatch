-- SolarWatch — migrate_0004_topology_sungrow.sql
--
-- FOR EXISTING DATABASES ONLY.
-- Fresh installs using the updated setup.sql do NOT need this file.
--
-- What this migration does:
--   1. Adds inverter_topology column to sites (DEFAULT 'hybrid' — all
--      existing Deye/Sunsynk/Victron rows get 'hybrid' automatically,
--      no manual UPDATE required and no downtime needed).
--   2. Adds sungrow_username, sungrow_password, sungrow_plant_id,
--      sungrow_device_sn credential columns to sites.
--
-- Safe to run on a live database while the existing app is running.
-- The existing app never references these new columns, so it is
-- completely unaffected until the new app version is deployed.
--
-- Run with:
--   psql -h your-postgres-host -U postgres -d solarwatch \
--        -f migrate_0004_topology_sungrow.sql

\connect solarwatch

-- ── Step 1: Add inverter_topology ─────────────────────────────────────────
-- DEFAULT 'hybrid' means PostgreSQL fills every existing row instantly.
-- NOT NULL ensures the column always has a meaningful value.
-- The CHECK constraint documents all supported topologies in one place.
ALTER TABLE sites
    ADD COLUMN IF NOT EXISTS inverter_topology TEXT NOT NULL DEFAULT 'hybrid'
    CHECK (inverter_topology IN (
        'hybrid',                -- battery + grid, single phase  (Deye, Sunsynk today)
        'hybrid_three_phase',    -- battery + grid, three phase
        'grid_tie',              -- grid only, single phase
        'grid_tie_three_phase',  -- grid only, three phase        (Sungrow SG125CX-P2)
        'off_grid',              -- battery only, single phase
        'off_grid_three_phase'   -- battery only, three phase
    ));

-- ── Step 2: Add Sungrow credential columns ────────────────────────────────
-- All nullable — only populated for sites with source_type = 'sungrow'.
-- sungrow_plant_id : the ps_id integer returned by /openapi/getPowerStationList
-- sungrow_device_sn: the ps_key composite string (e.g. '1713768_1_2_1')
--                    used by /openapi/getDeviceRealTimeData
ALTER TABLE sites
    ADD COLUMN IF NOT EXISTS sungrow_username  TEXT,
    ADD COLUMN IF NOT EXISTS sungrow_password  TEXT,
    ADD COLUMN IF NOT EXISTS sungrow_plant_id  TEXT,
    ADD COLUMN IF NOT EXISTS sungrow_device_sn TEXT;

-- ── Verify ────────────────────────────────────────────────────────────────
SELECT
    column_name,
    data_type,
    column_default,
    is_nullable
FROM information_schema.columns
WHERE table_name = 'sites'
  AND column_name IN (
      'inverter_topology',
      'sungrow_username',
      'sungrow_password',
      'sungrow_plant_id',
      'sungrow_device_sn'
  )
ORDER BY column_name;
