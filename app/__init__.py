"""FastAPI application factory.

Equivalent to the Flask template's create_app(). Sets up:
- Lifespan context (startup checks + engine disposal on shutdown)
- Session middleware (Starlette cookie sessions — same as Flask's server-side sessions)
- Security headers middleware (CSP, HSTS, X-Frame-Options, etc.)
- Proxy fix middleware for K8s / ingress deployments
- Router registration (auth, main, api)
- Startup error page (blocks all routes except /health if startup fails)
"""

from __future__ import annotations

import importlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_config
from app.db import dispose_engine
from app.routers import api, auth, main, solar, sites, share
from app.startup import run_startup_checks
from app.auth import _RedirectException

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")


def _load_dotenv_if_available() -> bool:
    if importlib.util.find_spec("dotenv") is None:
        return False
    load_dotenv = importlib.import_module("dotenv").load_dotenv
    return load_dotenv()


def _build_csp(config) -> str:
    allow_inline = getattr(config, "CSP_ALLOW_INLINE_SCRIPTS", False)
    script_src = "'self'" + (" 'unsafe-inline'" if allow_inline else "")
    return (
        "default-src 'self'; "
        f"script-src {script_src}; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "manifest-src 'self'; "
        "worker-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none';"
    )


def create_app() -> FastAPI:
    dotenv_loaded = _load_dotenv_if_available()
    LOGGER.info("Dotenv loaded: %s", dotenv_loaded)

    config = get_config()

    # ---------------------------------------------------------------------------
    # Lifespan: startup checks + graceful engine disposal on shutdown.
    # This is the FastAPI equivalent of Flask's before_first_request +
    # app teardown. Runs once per process — safe across K8s rolling deploys.
    # ---------------------------------------------------------------------------
    startup_errors: list[str] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        errors = run_startup_checks()
        startup_errors.extend(errors)
        if errors:
            LOGGER.error("Startup validation failed: %s", errors)
        else:
            LOGGER.info("Startup checks passed.")
        yield
        # Shutdown: return pooled connections cleanly.
        dispose_engine()
        LOGGER.info("Database engine disposed.")

    app = FastAPI(
        title="SolarWatch",
        docs_url="/api/docs" if getattr(config, "DEBUG", False) else None,
        redoc_url="/api/redoc" if getattr(config, "DEBUG", False) else None,
        lifespan=lifespan,
    )

    # ---------------------------------------------------------------------------
    # Session middleware.
    # Uses itsdangerous-signed cookies — identical security model to Flask.
    # SECRET_KEY must match across all pod replicas (store in K8s Secret).
    # ---------------------------------------------------------------------------
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.SECRET_KEY,
        session_cookie=config.SESSION_COOKIE_NAME,
        max_age=config.SESSION_MAX_AGE,
        https_only=getattr(config, "SESSION_COOKIE_HTTPS_ONLY", False),
        same_site="lax",
    )

    # ---------------------------------------------------------------------------
    # Proxy fix: trust X-Forwarded-* headers from Traefik / ingress-nginx.
    # Enable with APP_PROXY_FIX=true in your K8s deployment env vars.
    # ---------------------------------------------------------------------------
    if config.PROXY_FIX:
        # Use uvicorn's ProxyHeadersMiddleware for X-Forwarded-For handling.
        try:
            from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
            app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
            LOGGER.info("ProxyHeadersMiddleware enabled.")
        except ImportError:
            LOGGER.warning("uvicorn not available for ProxyHeadersMiddleware.")

    # ---------------------------------------------------------------------------
    # Security headers middleware (CSP, HSTS, etc.)
    # ---------------------------------------------------------------------------
    csp = _build_csp(config)
    report_only = getattr(config, "CSP_REPORT_ONLY", True)
    hsts_max_age = getattr(config, "HSTS_MAX_AGE", 0)
    hsts_include_subdomains = getattr(config, "HSTS_INCLUDE_SUBDOMAINS", False)
    is_debug = getattr(config, "DEBUG", True)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if report_only:
            response.headers["Content-Security-Policy-Report-Only"] = csp
        else:
            response.headers["Content-Security-Policy"] = csp
        if not is_debug and hsts_max_age:
            hsts = f"max-age={hsts_max_age}"
            if hsts_include_subdomains:
                hsts += "; includeSubDomains"
            response.headers["Strict-Transport-Security"] = hsts
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    # ---------------------------------------------------------------------------
    # Startup failure guard: block all routes except /health.
    # Mirrors the Flask template's before_request block.
    # ---------------------------------------------------------------------------
    @app.middleware("http")
    async def startup_guard(request: Request, call_next):
        if startup_errors and request.url.path != "/health":
            return templates.TemplateResponse(
                "startup_error.html",
                {"request": request, "errors": startup_errors},
                status_code=503,
            )
        return await call_next(request)

    # ---------------------------------------------------------------------------
    # Routers
    # ---------------------------------------------------------------------------
    app.include_router(main.router)
    app.include_router(auth.router)
    app.include_router(api.router)
    app.include_router(solar.router)
    app.include_router(sites.router)
    app.include_router(share.router)

    # ---------------------------------------------------------------------------
    # Exception handler for redirects raised inside Depends() guards.
    # login_required and require_role() raise _RedirectException; this converts
    # it to a proper 303 redirect response the browser follows correctly.
    # ---------------------------------------------------------------------------
    @app.exception_handler(_RedirectException)
    async def redirect_exception_handler(request: Request, exc: _RedirectException):
        return RedirectResponse(url=exc.url, status_code=exc.status_code)

    # Static files
    app.mount("/static", StaticFiles(directory="static"), name="static")

    return app
