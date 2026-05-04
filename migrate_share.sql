-- SolarWatch — migrate_share.sql
--
-- Adds the share_token column to the sites table.
-- Must be run as the postgres superuser (or any role that owns the sites table),
-- because solarwatch_user does not have DDL privileges on tables it does not own.
--
-- Run with:
--   psql -h your-postgres-host -U postgres -d solarwatch -f migrate_share.sql
--
-- This is safe to run multiple times — ADD COLUMN IF NOT EXISTS and
-- CREATE UNIQUE INDEX IF NOT EXISTS are both idempotent.

\connect solarwatch

-- Add the column if it does not already exist
ALTER TABLE sites
    ADD COLUMN IF NOT EXISTS share_token TEXT DEFAULT NULL;

-- Unique partial index — fast token lookup, allows multiple NULL values
CREATE UNIQUE INDEX IF NOT EXISTS idx_sites_share_token
    ON sites (share_token)
    WHERE share_token IS NOT NULL;

-- Grant read/write on the new column to solarwatch_user
-- (they already have ALL on the table but this makes intent explicit)
GRANT UPDATE (share_token) ON sites TO solarwatch_user;

-- Verify
SELECT
    column_name,
    data_type,
    is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
AND   table_name   = 'sites'
AND   column_name  = 'share_token';
