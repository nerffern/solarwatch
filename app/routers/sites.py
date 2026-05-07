"""SolarWatch — sites management routes.

Admin-only pages for managing sites, inverters, and user-site assignments.

Routes:
    GET  /sites                     → list all sites
    GET  /sites/new                 → create site form
    POST /sites/new                 → create site
    GET  /sites/{site_id}/edit      → edit site form
    POST /sites/{site_id}/edit      → save site changes
    POST /sites/{site_id}/toggle    → enable / disable site
    POST /sites/{site_id}/delete    → delete site (admin password required)

    -- Inverter management (Deye sites only) --
    POST /sites/{site_id}/inverters/add     → add an inverter
    POST /sites/{site_id}/inverters/{idx}/delete → remove an inverter

    -- User-site assignment (from user admin page) --
    POST /sites/assign              → assign a user to a site
    POST /sites/unassign            → remove a user from a site
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.auth import _admin_password_matches, login_required, require_role
from app.db import get_connection
from app.routers.auth import _consume_flash

log = logging.getLogger(__name__)
router = APIRouter(prefix="/sites", tags=["sites"])
templates = Jinja2Templates(directory="app/templates")


def _flash(request: Request, category: str, message: str) -> None:
    """Store a flash message (category + text) in the session for the next page load."""
    request.session["flash"] = (category, message)


def _render(request: Request, template: str, **ctx) -> HTMLResponse:
    """Render a Jinja2 template with request context, current user, flash message, and any extra kwargs."""
    ctx["request"] = request
    ctx["flash"] = _consume_flash(request)
    ctx.setdefault("current_user", None)
    return templates.TemplateResponse(template, ctx)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _fetch_all_sites() -> list[dict]:
    """Return all sites from the DB ordered by display name. Used by the admin sites list page."""
    with get_connection() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, site_name, display_name, source_type, enabled,
                       location, latitude, longitude,
                       inverters, sunsynk_username, sunsynk_plant_id,
                       created_at, updated_at
                FROM sites
                ORDER BY display_name
                """
            )
        ).mappings().all()
    return [dict(r) for r in rows]


def _fetch_site(site_id: int) -> Optional[dict]:
    """Return a single site row by integer ID, or None if not found."""
    with get_connection() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, site_name, display_name, source_type, enabled,
                       location, latitude, longitude,
                       inverters, sunsynk_username, sunsynk_password, sunsynk_plant_id,
                       share_token, created_at, updated_at
                FROM sites WHERE id = :id
                """
            ),
            {"id": site_id},
        ).mappings().first()
    return dict(row) if row else None


def _fetch_all_users() -> list[dict]:
    """Return all web_users with their role for the assignment UI."""
    with get_connection() as conn:
        rows = conn.execute(
            text(
                """
                SELECT wu.id, wu.username, wu.enabled, r.name AS role_name
                FROM web_users wu
                JOIN roles r ON wu.role_id = r.id
                ORDER BY wu.username
                """
            )
        ).mappings().all()
    return [dict(r) for r in rows]


def _fetch_site_users(site_id: int) -> list[dict]:
    """Return users assigned to a specific site."""
    with get_connection() as conn:
        rows = conn.execute(
            text(
                """
                SELECT wu.id, wu.username, r.name AS role_name
                FROM user_sites us
                JOIN web_users wu ON wu.id = us.user_id
                JOIN roles r ON wu.role_id = r.id
                WHERE us.site_id = :site_id
                ORDER BY wu.username
                """
            ),
            {"site_id": site_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def _fetch_user_assigned_sites(user_id: int) -> list[int]:
    """Return site IDs assigned to a user."""
    with get_connection() as conn:
        rows = conn.execute(
            text("SELECT site_id FROM user_sites WHERE user_id = :uid"),
            {"uid": user_id},
        ).mappings().all()
    return [r["site_id"] for r in rows]


# ---------------------------------------------------------------------------
# Sites list
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def sites_list(request: Request, user=Depends(require_role("admin"))):
    """List all sites with status and inverter counts."""
    sites = _fetch_all_sites()
    return _render(request, "sites/list.html", sites=sites, current_user=user)


# ---------------------------------------------------------------------------
# Create site
# ---------------------------------------------------------------------------

@router.get("/new", response_class=HTMLResponse)
async def sites_new_form(request: Request, user=Depends(require_role("admin"))):
    """GET /sites/new — render the new site creation form."""
    return _render(request, "sites/form.html", site=None, current_user=user,
                   mode="create")


@router.post("/new")
async def sites_new_post(
    request: Request,
    user=Depends(require_role("admin")),
    site_name: str = Form(""),
    display_name: str = Form(""),
    source_type: str = Form("deye"),
    location: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    enabled: Optional[str] = Form(None),
):
    """POST /sites/new — validate inputs, create the site row, redirect to its edit page."""
    site_name = site_name.strip()
    display_name = display_name.strip()

    if not site_name or not display_name:
        _flash(request, "danger", "Site name and display name are required.")
        return RedirectResponse(url="/sites/new", status_code=303)

    if source_type not in ("deye", "sunsynk"):
        _flash(request, "danger", "Source type must be deye or sunsynk.")
        return RedirectResponse(url="/sites/new", status_code=303)

    lat = float(latitude) if latitude.strip() else None
    lon = float(longitude) if longitude.strip() else None

    try:
        with get_connection() as conn:
            existing = conn.execute(
                text("SELECT id FROM sites WHERE site_name = :n"), {"n": site_name}
            ).first()
            if existing:
                _flash(request, "danger", f"A site named '{site_name}' already exists.")
                return RedirectResponse(url="/sites/new", status_code=303)

            result = conn.execute(
                text(
                    """
                    INSERT INTO sites (site_name, display_name, source_type,
                                       location, latitude, longitude, enabled, inverters)
                    VALUES (:site_name, :display_name, :source_type,
                            :location, :latitude, :longitude, :enabled,
                            CAST(:inverters AS jsonb))
                    RETURNING id
                    """
                ),
                {
                    "site_name": site_name,
                    "display_name": display_name,
                    "source_type": source_type,
                    "location": location.strip() or None,
                    "latitude": lat,
                    "longitude": lon,
                    "enabled": enabled == "on",
                    "inverters": "[]" if source_type == "deye" else None,
                },
            )
            new_id = result.fetchone()[0]
    except Exception as e:
        _flash(request, "danger", f"Failed to create site: {e}")
        return RedirectResponse(url="/sites/new", status_code=303)

    _flash(request, "success", f"Site '{display_name}' created.")
    return RedirectResponse(url=f"/sites/{new_id}/edit", status_code=303)


# ---------------------------------------------------------------------------
# Edit site
# ---------------------------------------------------------------------------

@router.get("/{site_id}/edit", response_class=HTMLResponse)
async def sites_edit_form(
    site_id: int, request: Request, user=Depends(require_role("admin"))
):
    """GET /sites/{id}/edit — render the edit form with current config, inverters, user access, and share link."""
    site = _fetch_site(site_id)
    if not site:
        _flash(request, "danger", "Site not found.")
        return RedirectResponse(url="/sites", status_code=303)

    site_users = _fetch_site_users(site_id)
    all_users = _fetch_all_users()
    # Exclude already-assigned users and admins from the add dropdown
    assigned_ids = {u["id"] for u in site_users}
    available_users = [
        u for u in all_users
        if u["id"] not in assigned_ids and u["role_name"] != "admin"
    ]

    # Parse inverters JSON for the template
    inverters = []
    if site.get("inverters"):
        try:
            raw = site["inverters"]
            inverters = raw if isinstance(raw, list) else json.loads(raw)
        except Exception:
            inverters = []

    return _render(
        request, "sites/form.html",
        site=site, inverters=inverters,
        site_users=site_users, available_users=available_users,
        mode="edit", current_user=user,
    )


@router.post("/{site_id}/edit")
async def sites_edit_post(
    site_id: int,
    request: Request,
    user=Depends(require_role("admin")),
    display_name: str = Form(""),
    location: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    enabled: Optional[str] = Form(None),
    # Sunsynk fields
    sunsynk_username: str = Form(""),
    sunsynk_password: str = Form(""),
    sunsynk_plant_id: str = Form(""),
    sunsynk_inverter_sns: str = Form(""),  # comma-separated SNs, optional fallback
):
    """POST /sites/{id}/edit — save site detail changes (display name, location, coordinates, enabled)."""
    site = _fetch_site(site_id)
    if not site:
        _flash(request, "danger", "Site not found.")
        return RedirectResponse(url="/sites", status_code=303)

    display_name = display_name.strip()
    if not display_name:
        _flash(request, "danger", "Display name is required.")
        return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)

    lat = float(latitude.strip()) if latitude.strip() else None
    lon = float(longitude.strip()) if longitude.strip() else None

    try:
        with get_connection() as conn:
            if site["source_type"] == "sunsynk":
                # Only update password if a new one was provided
                new_password = sunsynk_password.strip()
                if not new_password:
                    # Keep existing password — fetch it
                    existing_pw = conn.execute(
                        text("SELECT sunsynk_password FROM sites WHERE id = :id"),
                        {"id": site_id},
                    ).scalar()
                    new_password = existing_pw

                conn.execute(
                    text(
                        """
                        UPDATE sites SET
                            display_name = :display_name,
                            location = :location,
                            latitude = :latitude,
                            longitude = :longitude,
                            enabled = :enabled,
                            sunsynk_username = :sunsynk_username,
                            sunsynk_password = :sunsynk_password,
                            sunsynk_plant_id = :sunsynk_plant_id,
                            updated_at = NOW()
                        WHERE id = :id
                        """
                    ),
                    {
                        "display_name": display_name,
                        "location": location.strip() or None,
                        "latitude": lat,
                        "longitude": lon,
                        "enabled": enabled == "on",
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
                            location = :location,
                            latitude = :latitude,
                            longitude = :longitude,
                            enabled = :enabled,
                            updated_at = NOW()
                        WHERE id = :id
                        """
                    ),
                    {
                        "display_name": display_name,
                        "location": location.strip() or None,
                        "latitude": lat,
                        "longitude": lon,
                        "enabled": enabled == "on",
                        "id": site_id,
                    },
                )
    except Exception as e:
        _flash(request, "danger", f"Failed to update site: {e}")
        return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)

    # If inverter SNs were provided for a Sunsynk site, store as fallback
    sns_raw = sunsynk_inverter_sns.strip()
    if sns_raw:
        import json as _json
        inv_list = [
            {"name": f"Inverter_{i+1}", "inverter_sn": sn.strip()}
            for i, sn in enumerate(sns_raw.split(","))
            if sn.strip()
        ]
        try:
            with get_connection() as conn:
                conn.execute(
                    text("UPDATE sites SET inverters = CAST(:inv AS jsonb) WHERE id = :id"),
                    {"inv": _json.dumps(inv_list), "id": site_id},
                )
        except Exception as e:
            _flash(request, "danger", f"Failed to save inverter SNs: {e}")
            return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)

    _flash(request, "success", "Site updated.")
    return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)


# ---------------------------------------------------------------------------
# Toggle enabled
# ---------------------------------------------------------------------------

@router.post("/{site_id}/toggle")
async def sites_toggle(
    site_id: int, request: Request, user=Depends(require_role("admin"))
):
    """POST /sites/{id}/toggle — enable or disable a site. Disabled sites are skipped by the collector and hidden from the dashboard."""
    with get_connection() as conn:
        site = conn.execute(
            text("SELECT enabled FROM sites WHERE id = :id"), {"id": site_id}
        ).mappings().first()
        if not site:
            _flash(request, "danger", "Site not found.")
            return RedirectResponse(url="/sites", status_code=303)
        conn.execute(
            text("UPDATE sites SET enabled = :e, updated_at = NOW() WHERE id = :id"),
            {"e": not site["enabled"], "id": site_id},
        )
    _flash(request, "success", "Site status updated.")
    return RedirectResponse(url="/sites", status_code=303)


# ---------------------------------------------------------------------------
# Delete site
# ---------------------------------------------------------------------------

@router.post("/{site_id}/delete")
async def sites_delete(
    site_id: int,
    request: Request,
    user=Depends(require_role("admin")),
    admin_password: str = Form(""),
):
    """POST /sites/{id}/delete — delete the site and its user assignments. Solar readings are preserved."""
    if not admin_password or not _admin_password_matches(user, admin_password):
        _flash(request, "danger", "Admin password confirmation failed.")
        return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)

    with get_connection() as conn:
        site = conn.execute(
            text("SELECT display_name FROM sites WHERE id = :id"), {"id": site_id}
        ).mappings().first()
        if not site:
            _flash(request, "danger", "Site not found.")
            return RedirectResponse(url="/sites", status_code=303)
        # Cascade: user_sites rows deleted automatically via ON DELETE CASCADE
        conn.execute(text("DELETE FROM sites WHERE id = :id"), {"id": site_id})

    _flash(request, "success", f"Site '{site['display_name']}' deleted.")
    return RedirectResponse(url="/sites", status_code=303)


# ---------------------------------------------------------------------------
# Inverter management (Deye sites only)
# ---------------------------------------------------------------------------

@router.post("/{site_id}/inverters/add")
async def inverters_add(
    site_id: int,
    request: Request,
    user=Depends(require_role("admin")),
    inv_name: str = Form(""),
    inv_ip: str = Form(""),
    dongle_serial: str = Form(""),
    inverter_sn: str = Form(""),
):
    """POST /sites/{id}/inverters/add — append an inverter entry to the site JSONB inverters list."""
    site = _fetch_site(site_id)
    if not site or site["source_type"] != "deye":
        _flash(request, "danger", "Site not found or not a Deye site.")
        return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)

    if not inv_name.strip() or not inv_ip.strip():
        _flash(request, "danger", "Inverter name and IP are required.")
        return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)

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
                text(
                    "UPDATE sites SET inverters = CAST(:inv AS jsonb), updated_at = NOW() WHERE id = :id"
                ),
                {"inv": json.dumps(inverters), "id": site_id},
            )
    except Exception as e:
        _flash(request, "danger", f"Failed to add inverter: {e}")
        return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)

    _flash(request, "success", f"Inverter '{inv_name}' added.")
    return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)


@router.post("/{site_id}/inverters/{inv_idx}/delete")
async def inverters_delete(
    site_id: int,
    inv_idx: int,
    request: Request,
    user=Depends(require_role("admin")),
    admin_password: str = Form(""),
):
    """POST /sites/{id}/inverters/{idx}/delete — remove an inverter entry from the JSONB list by index."""
    if not admin_password or not _admin_password_matches(user, admin_password):
        _flash(request, "danger", "Admin password confirmation failed.")
        return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)

    site = _fetch_site(site_id)
    if not site or site["source_type"] != "deye":
        _flash(request, "danger", "Site not found or not a Deye site.")
        return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)

    try:
        raw = site["inverters"]
        inverters = raw if isinstance(raw, list) else (json.loads(raw) if raw else [])
        if inv_idx < 0 or inv_idx >= len(inverters):
            _flash(request, "danger", "Inverter index out of range.")
            return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)

        removed = inverters.pop(inv_idx)
        with get_connection() as conn:
            conn.execute(
                text(
                    "UPDATE sites SET inverters = CAST(:inv AS jsonb), updated_at = NOW() WHERE id = :id"
                ),
                {"inv": json.dumps(inverters), "id": site_id},
            )
    except Exception as e:
        _flash(request, "danger", f"Failed to remove inverter: {e}")
        return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)

    _flash(request, "success", f"Inverter '{removed.get('name', '')}' removed.")
    return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)


# ---------------------------------------------------------------------------
# User-site assignment
# ---------------------------------------------------------------------------

@router.post("/assign")
async def assign_user(
    request: Request,
    user=Depends(require_role("admin")),
    user_id: int = Form(...),
    site_id: int = Form(...),
):
    """Assign a user to a site."""
    try:
        with get_connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO user_sites (user_id, site_id)
                    VALUES (:user_id, :site_id)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"user_id": user_id, "site_id": site_id},
            )
    except Exception as e:
        _flash(request, "danger", f"Failed to assign user: {e}")

    return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)


@router.post("/unassign")
async def unassign_user(
    request: Request,
    user=Depends(require_role("admin")),
    user_id: int = Form(...),
    site_id: int = Form(...),
):
    """Remove a user from a site."""
    with get_connection() as conn:
        conn.execute(
            text(
                "DELETE FROM user_sites WHERE user_id = :user_id AND site_id = :site_id"
            ),
            {"user_id": user_id, "site_id": site_id},
        )
    return RedirectResponse(url=f"/sites/{site_id}/edit", status_code=303)
