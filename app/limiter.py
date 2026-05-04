"""Rate limiting for the FastAPI SaaS template.

Uses slowapi, which is built on limits and integrates cleanly with FastAPI.
A single shared Limiter instance is created here and imported wherever
rate limiting is needed — currently the auth login route.

Default limits (configurable via env vars):
    RATE_LIMIT_LOGIN   — login attempts per IP  (default: 10/minute)
    RATE_LIMIT_DEFAULT — catch-all for API routes (default: 60/minute)

In production behind a load balancer / ingress, slowapi reads the real
client IP from X-Forwarded-For when APP_PROXY_FIX=true and
uvicorn's ProxyHeadersMiddleware is active. Without proxy fix enabled,
all requests will appear to come from the proxy IP — enable it.

Storage backends:
    Default: in-memory (per-process). Fine for single-process deployments
    or when approximate rate limiting across pods is acceptable.

    For exact cross-pod rate limiting (all replicas share counts), swap
    the storage backend to Redis:

        from slowapi import Limiter
        limiter = Limiter(
            key_func=get_remote_address,
            storage_uri="redis://your-redis-host:6379",
        )

    Redis is not included in requirements.txt by default — add
    `redis>=5.0,<6.0` if you go this route.
"""

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

# Rate limit strings — override via environment variables if needed.
RATE_LIMIT_LOGIN: str = os.getenv("RATE_LIMIT_LOGIN", "10/minute")
RATE_LIMIT_DEFAULT: str = os.getenv("RATE_LIMIT_DEFAULT", "60/minute")

# The limiter is a module-level singleton. Import it wherever you need
# to apply limits — don't create new instances in routers.
limiter = Limiter(key_func=get_remote_address, default_limits=[RATE_LIMIT_DEFAULT])
