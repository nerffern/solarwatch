-- SolarWatch — migrate_victron.sql
--
-- FOR EXISTING DATABASES ONLY.
-- Fresh installs using the current setup.sql do NOT need this —
-- victron and sungrow are already in the CHECK constraint.
--
-- Run this as the postgres superuser BEFORE adding any Victron or Sungrow
-- sites via the web UI. Without it, inserting a site with source_type='victron'
-- will fail with:
--   ERROR: new row violates check constraint "sites_source_type_check"
--
-- Safe to run multiple times — the DROP/ADD is idempotent via the constraint name.
--
-- Run with:
--   psql -h your-postgres-host -U postgres -d solarwatch -f migrate_victron.sql

\connect solarwatch

-- Step 1: Drop the old constraint (only allows 'deye' and 'sunsynk')
ALTER TABLE sites
    DROP CONSTRAINT IF EXISTS sites_source_type_check;

-- Step 2: Add updated constraint allowing all four inverter types
ALTER TABLE sites
    ADD CONSTRAINT sites_source_type_check
    CHECK (source_type IN ('deye', 'sunsynk', 'victron', 'sungrow'));

-- Verify
SELECT
    conname        AS constraint_name,
    pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'sites'::regclass
AND   conname  = 'sites_source_type_check';
