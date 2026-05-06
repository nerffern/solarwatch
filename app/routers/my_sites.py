"""SolarWatch — my-sites routes.

Gives site_admin users a limited management page for their own assigned sites.
They can edit display details, manage inverters, and control the share link —
but cannot see other sites, create users, change roles, or access global admin.

Admin users are redirected to the full /sites page — they don't need this.

Routes:
    GET  /my-sites                     → list user's assigned sites
    GET  /my-sites/{site_id}/edit      → edit own site (limited form)
    POST /my-sites/{site_id}/edit      → save changes
    POST /my-sites/{site_id}/inverters/add       → add inverter
    POST /my-sites/{site_id}/inverters/{idx}/delete → remove inverter
    POST /my-sites/{site_id}/token/generate → generate share token
    POST /my-sites/{site_id}/token/revoke   → revoke share token
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.auth import get_accessible_sites, login_required, require_role
from app.auth import _admin_password_matches
from app.db import get_connection
from app.routers.auth import _consume_flash

log = logging.getLogger(__name__)
router = APIRouter(prefix="/my-sites", tags=["my-sites"])
templates = Jinja2Templates(directory="app/templates")

# Roles that can access this page
_SITE_ADMIN_ROLES = {"site_admin"}


def _flash(request: Request, category: str, message: str) -> None:
    """Store a flash message in the session for display on the next page load."""
    request.session["flash"] = (category, message)


def _render(request: Request, template: str, **ctx) -> HTMLResponse:
    """Render a Jinja2 template with request context, current user, and flash message."""
    ctx["request"] = request
    ctx["flash"] = _consume_flash(request)
    return templates.TemplateResponse(template, ctx)


def _require_site_admin(user: dict) -> dict:
    """Redirect admins to /sites, block non-site-admins."""
    from app.auth import _RedirectException
    if user["role_name"] == "admin":
        raise _RedirectException("/sites")
    if user["role_name"] not in _SITE_ADMIN_ROLES:
        raise _RedirectException("/dashboard")
    return user


def _fetch_my_site(site_id: int, user: dict) -> Optional[dict]:
    """Return site if it belongs to the user, otherwise None."""
    accessible = get_accessible_sites(user)
    with get_connection() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, site_name, display_name, source_type, enabled,
                       location, latitude, longitude,
                       inverters, sunsynk_username, sunsynk_password,
                       sunsynk_plant_id, share_token, created_at, updated_at
                FROM sites
                WHERE id = :id
                AND site_name = ANY(:accessible)
                """
            ),
            {"id": site_id, "accessible": accessible},
        ).mappings().first()
    return dict(row) if row else None


def _fetch_my_sites(user: dict) -> list[dict]:
    """Return sites assigned to this user (filtered by site_name), ordered by display name."""
    accessible = get_accessible_sites(user)
    if not accessible:
        return []
    with get_connection() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, site_name, display_name, source_type, enabled,
                       location, inverters, share_token, updated_at
                FROM sites
                WHERE site_name = ANY(:accessible)
                ORDER BY display_name
                """
            ),
            {"accessible": accessible},
        ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# My sites list
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def my_sites_list(request: Request, user=Depends(login_required)):
    """GET /my-sites — show the site_admin's assigned sites list."""
    user = _require_site_admin(user)
    sites = _fetch_my_sites(user)
    return _render(request, "my_sites/list.html", sites=sites, current_user=user)


# ---------------------------------------------------------------------------
# Edit my site
# ---------------------------------------------------------------------------

@router.get("/{site_id}/edit", response_class=HTMLResponse)
async def my_sites_edit_form(
    site_id: int, request: Request, user=Depends(login_required)
):
    """GET /my-sites/{id}/edit — render the limited site edit form for a site_admin."""
    user = _require_site_admin(user)
    site = _fetch_my_site(site_id, user)
    if not site:
        _flash(request, "danger", "Site not found or you don't have access.")
        return RedirectResponse(url="/my-sites", status_code=303)

    inverters = []
    if site.get("inverters"):
        try:
            raw = site["inverters"]
            inverters = raw if isinstance(raw, list) else json.loads(raw)
        except Exception:
            inverters = []

    return _render(
        request, "my_sites/edit.html",
        site=site, inverters=inverters, current_user=user,
    )


@router.post("/{site_id}/edit")
async def my_sites_edit_post(
    site_id: int,
    request: Request,
    user=Depends(login_required),
    display_name: str = Form(""),
    location: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    sunsynk_username: str = Form(""),
    sunsynk_password: str = Form(""),
    sunsynk_plant_id: str = Form(""),
):
    """POST /my-sites/{id}/edit — save allowed site detail changes (display name, location, Sunsynk creds)."""
    user = _require_site_admin(user)
    site = _fetch_my_site(site_id, user)
    if not site:
        _flash(request, "danger", "Site not found or you don't have access.")
        return RedirectResponse(url="/my-sites", status_code=303)

    display_name = display_name.strip()
    if not display_name:
        _flash(request, "danger", "Display name is required.")
        return RedirectResponse(url=f"/my-sites/{site_id}/edit", status_code=303)

    lat = float(latitude.strip()) if latitude.strip() else None
    lon = float(longitude.strip()) if longitude.strip() else None

    try:
        with get_connection() as conn:
            if site["source_type"] == "sunsynk":
                new_password = sunsynk_password.strip()
                if not new_password:
                    existing_pw = conn.execute(
                        text("SELECT sunsynk_password FROM sites WHERE id = :id"),
                        {"id": site_id},
                    ).scalar()
                    new_password = existing_pw

                conn.execute(
                    text(
                        """
                        UPDATE sites SET
                            display_name     = :display_name,
                            location         = :location,
                            latitude         = :latitude,
                            longitude        = :longitude,
                            sunsynk_username = :sunsynk_username,
                            sunsynk_password = :sunsynk_password,
                            sunsynk_plant_id = :sunsynk_plant_id,
                            updated_at       = NOW()
                        WHERE id = :id
                        """
                    ),
                    {
                        "display_name": display_name,
                        "location": location.strip() or None,
                        "latitude": lat, "longitude": lon,
                        "sunsynk_username": sunsynk_username.strip() or None,
                        "sunsynk_password": new_password,
                        "sunsynk_plant_id": sunsynk_plant_id.strip() or None,
                        "id": site_id,
                    },
                )
            else:
                conn.execute(
                    text(
                        """
                        UPDATE sites SET
                            display_name = :display_name,
                            location     = :location,
                            latitude     = :latitude,
                            longitude    = :longitude,
                            updated_at   = NOW()
                        WHERE id = :id
                        """
                    ),
                    {
                        "display_name": display_name,
                        "location": location.strip() or None,
                        "latitude": lat, "longitude": lon,
                        "id": site_id,
                    },
                )
    except Exception as e:
        _flash(request, "danger", f"Failed to update site: {e}")
        return RedirectResponse(url=f"/my-sites/{site_id}/edit", status_code=303)

    _flash(request, "success", "Site updated.")
    return RedirectResponse(url=f"/my-sites/{site_id}/edit", status_code=303)


# ---------------------------------------------------------------------------
# Inverter management (Deye only)
# ---------------------------------------------------------------------------

@router.post("/{site_id}/inverters/add")
async def my_inverters_add(
    site_id: int,
    request: Request,
    user=Depends(login_required),
    inv_name: str = Form(""),
    inv_ip: str = Form(""),
    dongle_serial: str = Form(""),
    inverter_sn: str = Form(""),
):
    """POST /my-sites/{id}/inverters/add — add an inverter or Cerbo GX device entry to the site."""
    user = _require_site_admin(user)
    site = _fetch_my_site(site_id, user)
    if not site or site["source_type"] != "deye":
        _flash(request, "danger", "Site not found or not a Deye site.")
        return RedirectResponse(url=f"/my-sites/{site_id}/edit", status_code=303)

    if not inv_name.strip() or not inv_ip.strip():
        _flash(request, "danger", "Inverter name and IP are required.")
        return RedirectResponse(url=f"/my-sites/{site_id}/edit", status_code=303)

    try:
        raw = site["inverters"]
        inverters = raw if isinstance(raw, list) else (json.loads(raw) if raw else [])
        inverters.append({
            "name": inv_name.strip(),
            "ip": inv_ip.strip(),
            "dongle_serial": int(dongle_serial) if dongle_serial.strip() else 0,
            "inverter_sn": inverter_sn.strip(),
        })
        with get_connection() as conn:
            conn.execute(
                text("UPDATE sites SET inverters = :inv::jsonb, updated_at = NOW() WHERE id = :id"),
                {"inv": json.dumps(inverters), "id": site_id},
            )
    except Exception as e:
        _flash(request, "danger", f"Failed to add inverter: {e}")

    _flash(request, "success", f"Inverter '{inv_name}' added.")
    return RedirectResponse(url=f"/my-sites/{site_id}/edit", status_code=303)


@router.post("/{site_id}/inverters/{inv_idx}/delete")
async def my_inverters_delete(
    site_id: int,
    inv_idx: int,
    request: Request,
    user=Depends(login_required),
):
    """POST /my-sites/{id}/inverters/{idx}/delete — remove a device entry by list index."""
    user = _require_site_admin(user)
    site = _fetch_my_site(site_id, user)
    if not site or site["source_type"] != "deye":
        _flash(request, "danger", "Site not found or not a Deye site.")
        return RedirectResponse(url=f"/my-sites/{site_id}/edit", status_code=303)

    try:
        raw = site["inverters"]
        inverters = raw if isinstance(raw, list) else (json.loads(raw) if raw else [])
        if 0 <= inv_idx < len(inverters):
            removed = inverters.pop(inv_idx)
            with get_connection() as conn:
                conn.execute(
                    text("UPDATE sites SET inverters = :inv::jsonb, updated_at = NOW() WHERE id = :id"),
                    {"inv": json.dumps(inverters), "id": site_id},
                )
            _flash(request, "success", f"Inverter '{removed.get('name', '')}' removed.")
        else:
            _flash(request, "danger", "Inverter not found.")
    except Exception as e:
        _flash(request, "danger", f"Failed to remove inverter: {e}")

    return RedirectResponse(url=f"/my-sites/{site_id}/edit", status_code=303)


# ---------------------------------------------------------------------------
# Share token management
# ---------------------------------------------------------------------------

import secrets as _secrets


@router.post("/{site_id}/token/generate")
async def my_token_generate(
    site_id: int, request: Request, user=Depends(login_required)
):
    """POST /my-sites/{id}/token/generate — generate a new 256-bit hex share token for the site."""
    user = _require_site_admin(user)
    site = _fetch_my_site(site_id, user)
    if not site:
        _flash(request, "danger", "Site not found.")
        return RedirectResponse(url="/my-sites", status_code=303)

    new_token = _secrets.token_hex(32)
    try:
        with get_connection() as conn:
            conn.execute(
                text("UPDATE sites SET share_token = :t, updated_at = NOW() WHERE id = :id"),
                {"t": new_token, "id": site_id},
            )
        _flash(request, "success", "Share link generated.")
    except Exception as e:
        _flash(request, "danger", f"Failed to generate token: {e}")

    return RedirectResponse(url=f"/my-sites/{site_id}/edit", status_code=303)


@router.post("/{site_id}/token/revoke")
async def my_token_revoke(
    site_id: int, request: Request, user=Depends(login_required)
):
    """POST /my-sites/{id}/token/revoke — delete the share token, making the public share link invalid."""
    user = _require_site_admin(user)
    site = _fetch_my_site(site_id, user)
    if not site:
        _flash(request, "danger", "Site not found.")
        return RedirectResponse(url="/my-sites", status_code=303)

    try:
        with get_connection() as conn:
            conn.execute(
                text("UPDATE sites SET share_token = NULL, updated_at = NOW() WHERE id = :id"),
                {"id": site_id},
            )
        _flash(request, "success", "Share link revoked.")
    except Exception as e:
        _flash(request, "danger", f"Failed to revoke token: {e}")

    return RedirectResponse(url=f"/my-sites/{site_id}/edit", status_code=303)
