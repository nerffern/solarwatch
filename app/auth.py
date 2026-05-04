"""Authentication dependencies and helpers.

This is the FastAPI equivalent of the Flask template's auth blueprint helpers.
Instead of decorators, these are Depends() functions injected into route
signatures. The pattern is identical in outcome — a route either gets a
validated user object or the request is rejected before the handler runs.

Usage:
    @router.get("/dashboard")
    async def dashboard(request: Request, user=Depends(login_required)):
        ...

    @router.get("/admin")
    async def admin(request: Request, user=Depends(require_role("admin"))):
        ...
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import get_connection


# ---------------------------------------------------------------------------
# User fetching helpers (identical SQL to Flask template)
# ---------------------------------------------------------------------------

def _fetch_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        return (
            conn.execute(
                text(
                    """
                    SELECT web_users.id,
                           web_users.username,
                           web_users.password,
                           web_users.enabled,
                           web_users.must_change_password,
                           roles.id   AS role_id,
                           roles.name AS role_name,
                           roles.enabled AS role_enabled,
                           roles.is_system AS role_is_system
                    FROM web_users
                    JOIN roles ON web_users.role_id = roles.id
                    WHERE web_users.username = :username
                    """
                ),
                {"username": username},
            )
            .mappings()
            .first()
        )


def _fetch_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        return (
            conn.execute(
                text(
                    """
                    SELECT web_users.id,
                           web_users.username,
                           web_users.enabled,
                           web_users.must_change_password,
                           roles.id   AS role_id,
                           roles.name AS role_name,
                           roles.enabled AS role_enabled,
                           roles.is_system AS role_is_system
                    FROM web_users
                    JOIN roles ON web_users.role_id = roles.id
                    WHERE web_users.id = :user_id
                    """
                ),
                {"user_id": user_id},
            )
            .mappings()
            .first()
        )


def _fetch_user_password_hash(user_id: int) -> Optional[str]:
    with get_connection() as conn:
        result = conn.execute(
            text("SELECT password FROM web_users WHERE id = :user_id"),
            {"user_id": user_id},
        ).mappings().first()
    return result["password"] if result else None


def _fetch_roles() -> list:
    with get_connection() as conn:
        return (
            conn.execute(
                text(
                    """
                    SELECT id, name, description, is_system, enabled, created_at
                    FROM roles
                    ORDER BY name
                    """
                )
            )
            .mappings()
            .all()
        )


def _is_password_strong(password: str) -> bool:
    if len(password) < 8:
        return False
    return any(c.isalpha() for c in password) and any(c.isdigit() for c in password)


def _admin_password_matches(current_user: Dict[str, Any], password: str) -> bool:
    pw_hash = _fetch_user_password_hash(int(current_user["id"]))
    if not pw_hash:
        return False
    return check_password_hash(pw_hash, password)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def get_current_user(request: Request) -> Optional[Dict[str, Any]]:
    """Read the session and return the hydrated user, or None.

    Re-fetches from DB on every request so disabling an account takes effect
    immediately. Enforces role-based session expiry:
      - admin      → 8 hours  (short, high-privilege)
      - all others → 30 days  (stay logged in for normal users)
    """
    import time as _time
    from app.config import get_config

    user_id = request.session.get("user_id")
    if not user_id:
        return None

    # Role-based session expiry check
    config = get_config()
    login_at = request.session.get("login_at", 0)
    role = request.session.get("role", "user")
    max_age = (
        config.SESSION_MAX_AGE           # 8 hours for admin
        if role == "admin"
        else config.SESSION_MAX_AGE_USER  # 30 days for everyone else
    )
    if _time.time() - login_at > max_age:
        request.session.clear()
        return None

    user = _fetch_user_by_id(int(user_id))
    if not user or not user["enabled"] or not user["role_enabled"]:
        request.session.clear()
        return None

    return dict(user)


# ---------------------------------------------------------------------------
# Depends() guards — drop-in replacements for Flask decorators
# ---------------------------------------------------------------------------

def login_required(request: Request, user=Depends(get_current_user)):
    """Redirect to login if no session is present.

    Inject as: user=Depends(login_required)
    """
    if user is None:
        request.session["flash"] = ("warning", "Please sign in to continue.")
        _redirect("/auth/login")
    # Enforce password change before anything else.
    if user.get("must_change_password"):
        allowed = {"/auth/change-password", "/auth/logout"}
        if request.url.path not in allowed:
            _redirect("/auth/change-password")
    return user


def require_role(*role_names: str) -> Callable:
    """Return a Depends() guard that checks the user's role.

    Usage:
        user=Depends(require_role("admin"))
        user=Depends(require_role("admin", "manager"))
    """

    def guard(request: Request, user=Depends(login_required)):
        if user["role_name"] not in role_names:
            request.session["flash"] = (
                "danger",
                "You do not have permission to access that page.",
            )
            _redirect("/")
        return user

    return guard


class _RedirectException(Exception):
    """Raised inside Depends() guards to trigger a redirect response.

    FastAPI's Depends() system does not support returning responses directly —
    you must raise an exception and catch it with an exception handler registered
    on the app. This exception is caught in create_app() in app/__init__.py.
    """
    def __init__(self, url: str, status_code: int = 303):
        self.url = url
        self.status_code = status_code


def _redirect(url: str) -> None:
    """Raise a redirect from inside a Depends() guard."""
    raise _RedirectException(url=url)


# ---------------------------------------------------------------------------
# Site access helpers — used by solar routes and the settings page
# ---------------------------------------------------------------------------

def _fetch_user_sites(user_id: int) -> list[str]:
    """Return list of site_names the user is explicitly assigned to.

    Admins bypass this — they always see all sites.
    For site_admin and site_viewer, only assigned sites are returned.
    """
    with get_connection() as conn:
        rows = conn.execute(
            text(
                """
                SELECT s.site_name
                FROM user_sites us
                JOIN sites s ON s.id = us.site_id
                WHERE us.user_id = :user_id
                AND s.enabled = TRUE
                ORDER BY s.display_name
                """
            ),
            {"user_id": user_id},
        ).mappings().all()
    return [r["site_name"] for r in rows]


def get_accessible_sites(user: dict) -> list[str]:
    """Return the site_names this user can access.

    - admin: all enabled sites (no restriction)
    - site_admin / site_viewer / user: only their assigned sites
    """
    if user["role_name"] == "admin":
        with get_connection() as conn:
            rows = conn.execute(
                text("SELECT site_name FROM sites WHERE enabled = TRUE ORDER BY display_name")
            ).mappings().all()
        return [r["site_name"] for r in rows]
    return _fetch_user_sites(int(user["id"]))


def require_site_access(site_name: str, user: dict) -> bool:
    """Return True if the user can access the given site. Raise 403 otherwise."""
    from fastapi import HTTPException
    accessible = get_accessible_sites(user)
    if site_name.lower() not in [s.lower() for s in accessible]:
        raise HTTPException(403, f"You do not have access to site: {site_name}")
    return True
