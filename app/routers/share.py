"""SolarWatch — public share link routes.

Provides read-only, token-authenticated access to a single site's dashboard
and data API. No session or login required — the URL token is the credential.

How it works:
  1. An admin generates a share token for a site via the site edit page.
     This writes a random 32-char hex token to sites.share_token.
  2. The share URL /share/{token} serves the dashboard with the token
     injected as a JS variable. The dashboard JS detects this and uses
     /api/share/{token}/* instead of the normal authenticated endpoints.
  3. Revoking access: regenerate or clear the token. The old URL stops
     working immediately — no session invalidation needed.

Security properties:
  - Read-only: only GET endpoints, no mutations possible
  - Rate limited: 30 requests/minute per IP (separate from login limit)
  - Token entropy: 32 hex chars = 128 bits — brute force infeasible
  - Revocable: clearing the token in the DB instantly kills all old links
  - No session: token-only auth, can't be used to access other sites
  - Scope limited: token resolves to exactly one site, nothing else visible
  - CSP: same strict policy as the authenticated dashboard

Routes:
    GET /share/{token}                  → dashboard (pre-filtered to site)
    GET /api/share/{token}/sites        → [{name, display}] for this site only
    GET /api/share/{token}/flow         → live power data
    GET /api/share/{token}/weather      → latest weather
    GET /api/share/{token}/monthly      → monthly totals
    GET /api/share/{token}/chart/{type} → chart data
    POST /sites/{site_id}/token/generate → generate/regenerate share token
    POST /sites/{site_id}/token/revoke  → clear share token (disables sharing)
"""

from __future__ import annotations

import secrets
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from app.auth import require_role
from app.db import get_connection
from app.limiter import limiter
from app.routers.auth import _consume_flash
from app.routers.solar import (
    _get_chart,
    _get_flow,
    _get_monthly,
    _get_weather,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["share"])
from app.templates_global import templates

# Rate limit for public share endpoints — more generous than login but
# still protects against scraping. Separate from authenticated limits.
SHARE_RATE_LIMIT = "30/minute"


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _lookup_token(token: str) -> Optional[dict]:
    """Return the site row for a valid share token, or None."""
    with get_connection() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, site_name, display_name, enabled
                FROM sites
                WHERE share_token = :token
                """
            ),
            {"token": token},
        ).mappings().first()
    return dict(row) if row else None


def _require_token(token: str) -> dict:
    """Validate token and return site, or raise 404.

    Returns 404 (not 403) for invalid tokens — we don't want to confirm
    whether a token exists or not to a potential enumerator.
    """
    site = _lookup_token(token)
    if not site:
        raise HTTPException(404, "Share link not found or has been revoked.")
    if not site["enabled"]:
        raise HTTPException(404, "This site is currently disabled.")
    return site


# ---------------------------------------------------------------------------
# Public dashboard
# ---------------------------------------------------------------------------

@router.get("/share/{token}", response_class=HTMLResponse)
@limiter.limit(SHARE_RATE_LIMIT)
async def share_dashboard(token: str, request: Request):
    """Serve the dashboard pre-locked to the token's site.

    The dashboard HTML is identical to the authenticated version.
    A JS variable SHARE_TOKEN is injected so the frontend knows to use
    the /api/share/* endpoints instead of /api/solar/*.
    """
    site = _require_token(token)

    # Inject the token and site name so the dashboard JS can use them.
    # We render the full dashboard template and patch the JS init block.
    return templates.TemplateResponse(
        "dashboard_share.html",
        {
            "request": request,
            "share_token": token,
            "site_name": site["site_name"],
            "site_display": site["display_name"],
        },
    )


# ---------------------------------------------------------------------------
# Public data API — mirrors /api/solar/* but token-authenticated
# ---------------------------------------------------------------------------

@router.get("/api/share/{token}/sites")
@limiter.limit(SHARE_RATE_LIMIT)
async def share_sites(token: str, request: Request):
    """Return just this site — so the dashboard site selector works correctly."""
    site = _require_token(token)
    return [{"name": site["site_name"], "display": site["display_name"]}]


@router.get("/api/share/{token}/flow")
@limiter.limit(SHARE_RATE_LIMIT)
async def share_flow(token: str, request: Request):
    """Live power data for the shared site. Cached 10 seconds."""
    site = _require_token(token)
    return _get_flow(site["site_name"])


@router.get("/api/share/{token}/weather")
@limiter.limit(SHARE_RATE_LIMIT)
async def share_weather(token: str, request: Request):
    """Latest weather for the shared site. Cached 60 seconds."""
    site = _require_token(token)
    return _get_weather(site["site_name"])


@router.get("/api/share/{token}/monthly")
@limiter.limit(SHARE_RATE_LIMIT)
async def share_monthly(token: str, request: Request):
    """Monthly totals for the shared site. Cached 120 seconds."""
    site = _require_token(token)
    return _get_monthly(site["site_name"])


@router.get("/api/share/{token}/chart/{chart}")
@limiter.limit(SHARE_RATE_LIMIT)
async def share_chart(token: str, chart: str, request: Request):
    """Chart data for the shared site. Cached 60 seconds."""
    site = _require_token(token)
    result = _get_chart(chart, site["site_name"])
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(404, result["error"])
    return result




@router.get("/share/{token}/manifest.json", include_in_schema=False)
@limiter.limit(SHARE_RATE_LIMIT)
async def share_manifest(token: str, request: Request):
    """Per-site PWA manifest for share links.

    Returns a manifest scoped to /share/{token} so the PWA installs with
    the site's display name (e.g. "Selati — SolarWatch") and opens
    directly to the share dashboard rather than the main app.
    """
    site = _require_token(token)
    from fastapi.responses import JSONResponse
    return JSONResponse({
        "name": f"{site['display_name']} — SolarWatch",
        "short_name": site["display_name"],
        "description": f"Live solar monitoring for {site['display_name']}",
        "start_url": f"/share/{token}",
        "scope": f"/share/{token}",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0a0c10",
        "theme_color": "#0a0c10",
        "lang": "en",
        "icons": [
            {"src": "/static/icons/icon-192x192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512x512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-maskable-512x512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            {"src": "/static/icons/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
        ],
    })

# ---------------------------------------------------------------------------
# Token management (admin only — lives here to keep share logic together)
# ---------------------------------------------------------------------------

@router.post("/sites/{site_id}/token/generate")
async def token_generate(
    site_id: int,
    request: Request,
    user=Depends(require_role("admin")),
):
    """Generate or regenerate a share token for a site.

    Regenerating immediately invalidates the previous share URL — anyone
    who had the old link will get a 404.
    """
    new_token = secrets.token_hex(32)  # 64 hex chars = 256 bits of entropy
    try:
        with get_connection() as conn:
            site = conn.execute(
                text("SELECT display_name FROM sites WHERE id = :id"),
                {"id": site_id},
            ).mappings().first()
            if not site:
                request.session["flash"] = ("danger", "Site not found.")
                return __import__("fastapi.responses", fromlist=["RedirectResponse"]).RedirectResponse(
                    url=f"/sites/{site_id}/edit", status_code=303
                )
            conn.execute(
                text(
                    "UPDATE sites SET share_token = :token, updated_at = NOW() WHERE id = :id"
                ),
                {"token": new_token, "id": site_id},
            )
        log.info("Share token generated for site %s", site_id)
        request.session["flash"] = ("success", "Share link generated. Copy it from the site edit page.")
    except Exception as e:
        request.session["flash"] = ("danger", f"Failed to generate token: {e}")

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)


@router.post("/sites/{site_id}/token/revoke")
async def token_revoke(
    site_id: int,
    request: Request,
    user=Depends(require_role("admin")),
):
    """Clear the share token — immediately disables all existing share links."""
    try:
        with get_connection() as conn:
            conn.execute(
                text(
                    "UPDATE sites SET share_token = NULL, updated_at = NOW() WHERE id = :id"
                ),
                {"id": site_id},
            )
        log.info("Share token revoked for site %s", site_id)
        request.session["flash"] = ("success", "Share link revoked. Existing links will no longer work.")
    except Exception as e:
        request.session["flash"] = ("danger", f"Failed to revoke token: {e}")

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)
