# SolarWatch — Grafana Dashboards

Two Grafana dashboards for historical analysis and advanced charting.
Both query the same PostgreSQL `solar_readings` table as the SolarWatch web app
so they work immediately against your existing database with no extra setup.

## Dashboards

| File | Description |
|---|---|
| `SolarWatch_Dashboard_v1.json` | Full 70-panel desktop dashboard — all inverter types, all metrics |
| `SolarWatch_Mobile.json` | 29-panel mobile-optimised dashboard |

## When to use Grafana vs the SolarWatch web app

| SolarWatch web app | Grafana |
|---|---|
| Live power flow (10s updates) | Historical deep-dives |
| Today's totals | Custom date ranges |
| Share links / PWA | Correlating multiple sites side-by-side |
| Mobile dashboard | Building new chart panels |

## Importing into Grafana

1. Open Grafana → **Dashboards → Import**
2. Upload the JSON file or paste its contents
3. Select your PostgreSQL datasource when prompted
4. The `site` variable auto-populates from your DB — select any site

## Compatibility with all inverter types

The dashboards use `site_name` as the filter variable. All inverter types
(Deye, Sunsynk, Victron, Sungrow) are selectable from the site dropdown.

Panels that use Deye/Sunsynk-specific columns (`daily_load_energy`,
`daily_grid_import`, `pv2_power`, `inverter_temp`) will show empty or zero
for Victron sites — this is expected. All power flow panels (`pv1_power`,
`battery_*`, `grid_power`, `load_power`, `daily_pv_energy`) work for all
inverter types.

## PostgreSQL datasource setup in Grafana

```
Host:     your-postgres-host:5432
Database: solarwatch
User:     solarwatch_user
Password: your-password
SSL Mode: prefer
```

The `solarwatch_user` account has SELECT access to `solar_readings` and
`weather_readings` — no elevated privileges needed for Grafana.
