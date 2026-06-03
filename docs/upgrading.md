# Upgrading SolarWatch

---

## Bare-metal / systemd (standard upgrade)

```bash
# 1. Pull the latest code
sudo -u solarwatch git -C /opt/solarwatch pull

# 2. Install any new Python dependencies
sudo -u solarwatch /opt/solarwatch/venv/bin/pip install -r /opt/solarwatch/requirements.txt

# 3. Check the release notes / git log for any new migration files
git -C /opt/solarwatch log --oneline -10

# 4. Run any new migration SQL files if listed in release notes
# psql -h your-postgres-host -U postgres -d solarwatch -f /opt/solarwatch/migrate_xyz.sql

# 5. Restart both services
sudo systemctl restart solarwatch-collector
sudo systemctl restart solarwatch-web

# 6. Verify
sudo systemctl status solarwatch-collector solarwatch-web
sudo journalctl -u solarwatch-web -n 20 --no-pager
```

The whole process takes under 30 seconds. During the restart the web app is
briefly unavailable (~2s). The collector misses one poll cycle (~60s of data).

---

## Removing old service files

If you have leftover service files from the old powerflow/collector setup,
clean them up:

```bash
# Stop and disable old services (adjust names to match what you have)
sudo systemctl stop solarwatch solarwatch-powerflow 2>/dev/null
sudo systemctl disable solarwatch solarwatch-powerflow 2>/dev/null

# Remove old service files
sudo rm -f /etc/systemd/system/solarwatch.service
sudo rm -f /etc/systemd/system/solarwatch-powerflow.service
sudo systemctl daemon-reload

# Verify only the new services remain
sudo systemctl list-units | grep -i solar
# Expected:
#   solarwatch-collector.service   active running
#   solarwatch-web.service         active running
```

---

## Kubernetes

```bash
# Update the image tag in your tenant values file, then:
helm upgrade --install solarwatch deploy/helm/solarwatch \
  -f deploy/tenants/hfi/values.yaml \
  --namespace solarwatch

# Monitor the rollout
kubectl rollout status deployment/solarwatch -n solarwatch
kubectl rollout status deployment/solarwatch-collector -n solarwatch
```

Zero-downtime rolling update — `maxUnavailable: 0` keeps one web pod serving
traffic throughout. The collector restarts briefly — one poll cycle is missed.

---

## Migration SQL files

Migration files are **only needed when upgrading from an older installation**.
A fresh install using the current `setup.sql` already includes everything.

| File | When needed |
|---|---|
| `migrate_share.sql` | DB created before v1.0 — adds `share_token` column |
| `migrate_indexes.sql` | DB created before v1.0 — adds performance indexes |
| `migrate_weather.sql` | DB created before weather support — adds `weather_readings` table |
| `migrate_victron.sql` | DB created before Victron support — updates `source_type` CHECK constraint |

Run as postgres superuser:
```bash
psql -h your-postgres-host -U postgres -d solarwatch -f /opt/solarwatch/migrate_victron.sql
```

Each migration file is safe to run multiple times — they use `IF NOT EXISTS`
or `IF EXISTS` guards.

---

## Rolling back

```bash
# Roll back to a specific git commit
sudo -u solarwatch git -C /opt/solarwatch checkout <commit-hash>
sudo systemctl restart solarwatch-collector solarwatch-web
```

Or to go back to the previous version:
```bash
sudo -u solarwatch git -C /opt/solarwatch revert HEAD
sudo systemctl restart solarwatch-collector solarwatch-web
```
