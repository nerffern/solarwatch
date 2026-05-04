# FastAPI SaaS Template

A production-ready FastAPI starter template. Clone it, set four environment
variables, and you have a running application with authentication, role-based
access control, database migrations, rate limiting, and security hardening
already done.

Designed to scale horizontally across multiple pods and datacentres without
any shared server state — every instance is identical and stateless.

---

## How this app scales

FastAPI is built on [Starlette](https://www.starlette.io/) and served by
[uvicorn](https://www.uvicorn.org/), an async ASGI server. In production,
[gunicorn](https://gunicorn.org/) manages multiple uvicorn worker processes
per pod, and Kubernetes manages multiple pods.

```
Internet
    │
    ▼
Load balancer / Ingress (Traefik, ingress-nginx, HAProxy + BGP VIP)
    │
    ├── Pod A  (gunicorn → 2× uvicorn workers)
    ├── Pod B  (gunicorn → 2× uvicorn workers)
    └── Pod C  (gunicorn → 2× uvicorn workers)
                          │
                          ▼
              HA PostgreSQL cluster
              (primary + replicas, HAProxy VIP)
```

**Why this scales without coordination between pods:**

- Session state lives in a signed cookie on the client — not in server memory.
  A request can land on any pod, any worker, and the session is valid as long
  as `SECRET_KEY` is the same across all pods.
- Database connections are pooled per-worker. No pod needs to know about
  any other pod's connections.
- Rate limiting is per-worker by default (in-memory). For exact cross-pod
  counting, configure a shared Redis backend — see `app/limiter.py`.
- The `/health` endpoint is always available so Kubernetes can probe any pod
  independently without routing through a shared state store.

**Scaling up:**

- Add more pods — no config change needed, just increase replicas.
- Add more workers per pod — increase `--workers` in the Dockerfile `CMD`.
- Add more datacentres — deploy the same pods, point them at the same
  `DATABASE_URL`, share the same `SECRET_KEY` via your secrets manager.

---

## What's included

| Feature | Detail |
|---|---|
| **Authentication** | Session-based, signed cookies — no JWT complexity |
| **RBAC** | `admin` / `user` system roles + custom roles via admin UI |
| **Admin UI** | Create, enable/disable, reassign, and delete users and roles |
| **Startup validation** | Checks env vars, DB, creates tables, seeds roles, provisions admin |
| **Rate limiting** | Login endpoint limited per IP via slowapi — configurable |
| **Database migrations** | Alembic wired to the same DB credentials as the app |
| **Security headers** | CSP, HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy |
| **Proxy support** | X-Forwarded-* headers for Traefik / ingress-nginx |
| **API docs** | Swagger + ReDoc auto-generated in development, off in production |
| **Health probe** | `/health` always returns 200 — K8s liveness/readiness safe |
| **Bootstrap 5** | Bundled locally — no CDN dependency, works in air-gapped environments |
| **Docker** | Multi-stage Dockerfile + docker-compose for local development |

---

## Why uvicorn + gunicorn

**uvicorn** is an async ASGI server — it is the correct server for FastAPI.
FastAPI uses Python's `async`/`await` system and requires an ASGI server.
The traditional WSGI servers (like plain gunicorn) cannot run FastAPI correctly.

**gunicorn** is used as a process manager on top of uvicorn in production.
It handles worker crashes, graceful restarts, and signal handling. Each gunicorn
worker is a full uvicorn process. This is the officially recommended production
pattern for FastAPI.

```
gunicorn (process manager)
  └── uvicorn worker 1  ← handles requests concurrently via async
  └── uvicorn worker 2  ← handles requests concurrently via async
```

In development (`run.py`), uvicorn runs directly with `--reload` so file
changes restart the server automatically. There is no gunicorn in development.

---

## Why Bootstrap is bundled locally

All Bootstrap CSS and JS files live in `static/` rather than being loaded
from a CDN. This is intentional:

- **Works in restricted environments** — air-gapped networks, internal
  deployments, or anywhere CDN access is blocked.
- **No third-party dependency at runtime** — the app is fully self-contained.
  A CDN outage cannot break your UI.
- **Consistent versions** — the exact Bootstrap version is locked in the repo.
  A CDN update cannot silently change your app's behaviour.
- **Strict CSP** — the Content Security Policy in production only allows
  `script-src 'self'`. Loading scripts from a CDN would require relaxing this.

The templates only use `bootstrap.min.css`, `theme.css`, `material-symbols.css`,
and `bootstrap.bundle.min.js`. The other Bootstrap variants (RTL, grid-only,
utilities-only, ESM) are included so future projects forked from this template
have them available without needing to re-download Bootstrap.

---

## Quick start

### Option A — Local Python (PyCharm or terminal)

```bash
# 1. Clone the repo and open it in PyCharm (or your editor of choice)

# 2. Create a virtual environment
python -m venv .venv
# PyCharm will detect this automatically. In terminal:
source .venv/bin/activate          # Linux / Mac
.venv\Scripts\activate             # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — set these four values:
#   SECRET_KEY     — generate with: python -c "import secrets; print(secrets.token_hex(32))"
#   DB_HOST        — your Postgres host (or use DATABASE_URL)
#   APP_ADMIN_USERNAME
#   APP_ADMIN_PASSWORD

# 5. Start the development server
python run.py
```

Visit http://localhost:8000 — sign in with your admin credentials.
On first start the app creates `roles` and `web_users` automatically.
No manual database setup needed.

**PyCharm run configuration:**  
Script path: `run.py` — just press Run. Hot reload is on by default.  
Alternatively configure a uvicorn run: `uvicorn main:app --reload`

### Option B — Docker Compose (zero config)

```bash
docker compose up
```

Starts the app and a local Postgres container. Visit http://localhost:8000.
Default credentials: `admin` / `Admin1234`.

---

## Project structure

```
fastapi-saas-template/
│
├── app/                          # Application package
│   ├── __init__.py               # App factory — wires together everything
│   ├── auth.py                   # Depends() guards: login_required, require_role()
│   ├── config.py                 # DevelopmentConfig / ProductionConfig
│   ├── db.py                     # Shared SQLAlchemy engine + get_connection()
│   ├── limiter.py                # Rate limiter singleton (slowapi)
│   ├── startup.py                # 5-step startup validation + bootstrap
│   └── routers/
│       ├── auth.py               # /auth/* — login, logout, admin user/role CRUD
│       ├── main.py               # / home page and /health probe
│       └── api.py                # /api/* — JSON endpoints (add yours here)
│
├── app/templates/                # Jinja2 server-rendered HTML
│   ├── base.html                 # Navbar, flash messages, Bootstrap
│   ├── index.html                # Home — replace with your app's dashboard
│   ├── startup_error.html        # Shown when startup checks fail (503)
│   └── auth/
│       ├── login.html
│       ├── change_password.html
│       └── admin/
│           ├── dashboard.html
│           ├── users.html
│           └── roles.html
│
├── static/                       # Served at /static/ — no CDN required
│   ├── css/                      # Bootstrap 5 (all variants) + theme + Material Symbols
│   ├── js/                       # Bootstrap bundle JS
│   ├── fonts/                    # Material Symbols woff2
│   ├── icons/                    # PWA icons + favicon
│   ├── branding/                 # Replace with your project logo
│   └── manifest.json             # PWA manifest
│
├── alembic/                      # Database migrations
│   ├── env.py                    # Reads same DATABASE_URL as the app
│   ├── script.py.mako            # Template for generated migration files
│   └── versions/
│       └── 0001_initial_schema.py   # Baseline: roles + web_users
│
├── alembic.ini                   # Alembic config (URL set by env.py at runtime)
├── main.py                       # Production entrypoint: gunicorn main:app
├── run.py                        # Development entrypoint: python run.py
├── Dockerfile                    # Multi-stage build, non-root user
├── docker-compose.yml            # Local dev: app + Postgres with health check
├── requirements.txt              # All dependencies with version pins
└── .env.example                  # All supported environment variables
```

---

## Environment variables

Copy `.env.example` to `.env` and configure the values below.

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | **Yes** | `change-me` | Signs session cookies. Must be the same across all pods. Generate: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `APP_ADMIN_USERNAME` | **Yes** | — | Bootstrap admin username |
| `APP_ADMIN_PASSWORD` | **Yes** | — | Bootstrap admin password — user must change it on first login |
| `APP_ENV` | No | `development` | Set `production` for strict CSP, HSTS, HTTPS-only cookies |
| `DATABASE_URL` | Yes* | — | `postgresql+psycopg2://user:pass@host:5432/db` |
| `DB_HOST` | Yes* | `127.0.0.1` | Alternative to `DATABASE_URL` |
| `DB_PORT` | Yes* | `5432` | |
| `DB_USER` | Yes* | `app_user` | |
| `DB_PASSWORD` | Yes* | `changeme` | |
| `DB_NAME` | Yes* | `app_db` | |
| `DB_POOL_SIZE` | No | `5` | Connections per worker process |
| `DB_MAX_OVERFLOW` | No | `10` | Extra connections beyond pool size |
| `DB_POOL_TIMEOUT` | No | `30` | Seconds to wait for a free connection |
| `APP_PROXY_FIX` | No | `false` | Set `true` behind Traefik / ingress-nginx |
| `APP_ADMIN_RESET_PASSWORD` | No | `false` | Set `true` temporarily to reset the admin password |
| `RATE_LIMIT_LOGIN` | No | `10/minute` | Max login attempts per IP before 429 |
| `RATE_LIMIT_DEFAULT` | No | `60/minute` | Default limit on all other routes |

*Provide `DATABASE_URL` **or** all five `DB_*` variables. `DATABASE_URL` takes priority.

---

## Database schema

Created automatically on first start using `CREATE TABLE IF NOT EXISTS` — safe
to run against any existing database, including one that already has your
application's own tables.

```sql
-- Permission roles. is_system = TRUE prevents admin/user from being deleted.
CREATE TABLE IF NOT EXISTS roles (
    id          INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    is_system   BOOLEAN NOT NULL DEFAULT FALSE,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- User accounts. Passwords are always stored as werkzeug hashes — never plaintext.
CREATE TABLE IF NOT EXISTS web_users (
    id                   INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    username             TEXT NOT NULL UNIQUE,
    password             TEXT NOT NULL,
    role_id              INTEGER NOT NULL REFERENCES roles(id),
    enabled              BOOLEAN NOT NULL DEFAULT TRUE,
    must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
    created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

These table names are generic and do not clash with application-specific tables.
Multiple projects can share the same Postgres database and reuse these auth
tables — no duplication needed.

---

## Authentication

### How sessions work

On login, the user's database `id` is stored in a signed Starlette session
cookie. On every request, `get_current_user()` re-fetches the full record from
the database. This means:

- Disabling a user or role takes effect **on the next request** — no token
  invalidation or session expiry to wait for.
- No server-side session store is needed. The signed cookie is the token,
  verified by `SECRET_KEY`. Any pod that knows `SECRET_KEY` can validate it.

### Protecting routes

```python
from app.auth import login_required, require_role
from fastapi import APIRouter, Depends, Request

router = APIRouter()

# Any authenticated user
@router.get("/dashboard")
async def dashboard(request: Request, user=Depends(login_required)):
    # user dict: id, username, role_name, role_id, enabled, must_change_password
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "current_user": user,
    })

# Admin only
@router.get("/settings")
async def settings(request: Request, user=Depends(require_role("admin"))):
    ...

# Multiple roles
@router.get("/reports")
async def reports(request: Request, user=Depends(require_role("admin", "manager"))):
    ...
```

### Adding a new router

Create `app/routers/myfeature.py`, then register it in `app/__init__.py`:

```python
# app/routers/myfeature.py
from fastapi import APIRouter, Depends, Request
from app.auth import login_required

router = APIRouter(prefix="/myfeature", tags=["myfeature"])

@router.get("/")
async def my_page(request: Request, user=Depends(login_required)):
    return {"user": user["username"]}
```

```python
# app/__init__.py — add inside create_app():
from app.routers import myfeature
app.include_router(myfeature.router)
```

---

## Database migrations (Alembic)

The app auto-creates tables on first start. Alembic manages all schema changes
after that. It reads the same `DATABASE_URL` as the app — credentials are never
duplicated.

```bash
# Apply all pending migrations (run after each deployment)
alembic upgrade head

# Check what version the database is at
alembic current

# View full migration history
alembic history

# Roll back one step
alembic downgrade -1

# Generate a new migration after changing your schema
alembic revision --autogenerate -m "add api_key column to web_users"
# Then edit the generated file and run: alembic upgrade head
```

The initial migration (`0001_initial_schema.py`) captures the baseline `roles`
and `web_users` schema so Alembic's history is accurate from day one.

---

## Rate limiting

Login attempts are limited per IP using [slowapi](https://github.com/laurents/slowapi).
Default: 10 attempts per minute — returns `429 Too Many Requests` when exceeded.

Configure via environment variables:
```
RATE_LIMIT_LOGIN=10/minute      # login endpoint
RATE_LIMIT_DEFAULT=60/minute    # all other routes
```

**Multi-pod note:** The default in-memory backend counts per-worker. Each pod
enforces its own limit independently. For shared cross-pod counting, configure
a Redis backend in `app/limiter.py` — the comments there explain how.

**Proxy note:** Rate limiting uses the client IP from `X-Forwarded-For` when
`APP_PROXY_FIX=true`. Without this, all requests appear to come from the
ingress IP and rate limiting will not work correctly behind a load balancer.

---

## Production deployment

### Docker

```bash
docker build -t fastapi-saas-template .
docker run -p 8000:8000 \
  -e SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))") \
  -e APP_ENV=production \
  -e DATABASE_URL=postgresql+psycopg2://user:pass@your-db-host:5432/mydb \
  -e APP_ADMIN_USERNAME=admin \
  -e APP_ADMIN_PASSWORD=YourSecurePassword1 \
  fastapi-saas-template
```

### Kubernetes / Helm

```yaml
# Store secrets — never put credentials in ConfigMaps or container images
apiVersion: v1
kind: Secret
metadata:
  name: app-secrets
stringData:
  secret-key: "your-generated-32-char-key"
  database-url: "postgresql+psycopg2://user:pass@your-db-host:5432/mydb"
  admin-username: "admin"
  admin-password: "YourSecurePassword1"
```

```yaml
# Deployment env section
env:
  - name: SECRET_KEY
    valueFrom:
      secretKeyRef: { name: app-secrets, key: secret-key }
  - name: DATABASE_URL
    valueFrom:
      secretKeyRef: { name: app-secrets, key: database-url }
  - name: APP_ADMIN_USERNAME
    valueFrom:
      secretKeyRef: { name: app-secrets, key: admin-username }
  - name: APP_ADMIN_PASSWORD
    valueFrom:
      secretKeyRef: { name: app-secrets, key: admin-password }
  - name: APP_ENV
    value: "production"
  - name: APP_PROXY_FIX
    value: "true"
```

### Multi-DC / high-availability

`SECRET_KEY` must be **identical across every pod replica in every datacentre**.
A request can land on any pod — if the key differs, the session cookie is
rejected and the user is logged out.

Store `SECRET_KEY` once as a Kubernetes Secret and reference it from all
deployments. Never generate a new key per pod or per datacentre.

### Gunicorn workers

```bash
# In Dockerfile CMD — adjust --workers to match your pod CPU allocation
gunicorn main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers 2 \
  --bind 0.0.0.0:8000
```

Rule of thumb: `workers = (2 × CPU cores) + 1`. For a 1-vCPU pod, 2 workers
is correct. Each worker is independent — session state is in cookies, DB
connections are pooled per-worker, so there is no coordination overhead.

### Health probes

```yaml
livenessProbe:
  httpGet: { path: /health, port: 8000 }
  initialDelaySeconds: 5
  periodSeconds: 10
readinessProbe:
  httpGet: { path: /health, port: 8000 }
  initialDelaySeconds: 5
  periodSeconds: 5
```

`/health` always returns `200 {"status": "ok"}` — even if startup checks
failed. The startup guard serves `503` on all other routes until the database
is ready, so traffic is held back without removing the pod from the load
balancer rotation.

---

## Adapting for a new project

1. Fork or copy the repo and rename it.
2. Update `app/templates/base.html` (app name in navbar) and `app/templates/index.html` (home content).
3. Replace `static/branding/` with your project's logo files.
4. Add project tables to `_ensure_foundational_tables()` in `app/startup.py`.
5. Create an Alembic migration for those tables: `alembic revision -m "add myapp tables"`.
6. Add your routes to `app/routers/api.py` or new router files.
7. Update `.env.example` with any new variables your project needs.
8. Update this README.

---

## Flask → FastAPI reference

| Flask | This template |
|---|---|
| `Blueprint` | `APIRouter` |
| `@login_required` decorator | `user=Depends(login_required)` |
| `@role_required("admin")` | `user=Depends(require_role("admin"))` |
| `g.current_user` | `user` argument from `Depends()` |
| `before_app_request` | `Depends(get_current_user)` runs per-request |
| `flash()` / `get_flashed_messages()` | Session-stored, consumed by `_render()` |
| `create_app()` | `create_app()` — identical concept |
| `before_first_request` + teardown | `lifespan` async context manager |
| Flask-WTF CSRF tokens | Not needed — `SameSite=Lax` + POST-only mutations |
| `ProxyFix` WSGI middleware | `uvicorn.ProxyHeadersMiddleware` |
| `wsgi.py` | `main.py` |
