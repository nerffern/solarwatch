"""Configuration for the FastAPI SaaS template.

Settings are read from environment variables. The app picks the right
config class based on APP_ENV (development | production).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class BaseConfig:
    # SECRET_KEY signs session cookies. Must be stable across all pod
    # replicas and restarts so sessions survive a pod reschedule or a
    # rolling deploy. Store it as a K8s Secret and mount it as an env var.
    SECRET_KEY: str = field(default_factory=lambda: os.getenv("SECRET_KEY", "change-me"))

    # Session cookie settings (used by Starlette's SessionMiddleware).
    SESSION_COOKIE_NAME: str = "session"
    SESSION_MAX_AGE: int = 60 * 60 * 8  # 8 hours

    # Proxy fix: set APP_PROXY_FIX=true when running behind Nginx/Traefik/Cloudflare.
    PROXY_FIX: bool = field(
        default_factory=lambda: os.getenv("APP_PROXY_FIX", "false").lower()
        in {"true", "1", "yes"}
    )

    # Admin bootstrap credentials — required on first start.
    ADMIN_USERNAME: str = field(default_factory=lambda: os.getenv("APP_ADMIN_USERNAME", ""))
    ADMIN_PASSWORD: str = field(default_factory=lambda: os.getenv("APP_ADMIN_PASSWORD", ""))
    ADMIN_RESET_PASSWORD: bool = field(
        default_factory=lambda: os.getenv("APP_ADMIN_RESET_PASSWORD", "false").lower()
        in {"true", "1", "yes"}
    )


@dataclass(frozen=True)
class DevelopmentConfig(BaseConfig):
    DEBUG: bool = True
    # Relaxed CSP for local dev — allows inline scripts so hot-reload tools work.
    CSP_REPORT_ONLY: bool = True
    CSP_ALLOW_INLINE_SCRIPTS: bool = True
    # HTTP-only cookies are fine locally.
    SESSION_COOKIE_HTTPS_ONLY: bool = False


@dataclass(frozen=True)
class ProductionConfig(BaseConfig):
    DEBUG: bool = False
    # Strict CSP in production.
    CSP_REPORT_ONLY: bool = False
    CSP_ALLOW_INLINE_SCRIPTS: bool = False
    # Enforce HTTPS cookies — required for multi-DC K8s behind TLS termination.
    SESSION_COOKIE_HTTPS_ONLY: bool = True
    HSTS_MAX_AGE: int = 31536000
    HSTS_INCLUDE_SUBDOMAINS: bool = True
    HSTS_PRELOAD: bool = False


def get_config() -> BaseConfig:
    """Return the correct config object based on APP_ENV."""
    env = os.getenv("APP_ENV", "development").lower()
    if env == "production":
        return ProductionConfig()
    return DevelopmentConfig()
