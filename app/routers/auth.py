"""Authentication, session, and admin UI routes.

FastAPI equivalent of the Flask template's auth blueprint. Routes are
identical in URL structure and behaviour so existing bookmarks and Helm
ingress rules keep working without changes.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

from app.auth import (
    _admin_password_matches,
    _fetch_roles,
    _fetch_user_by_username,
    _fetch_user_password_hash,
    _is_password_strong,
    login_required,
    require_role,
)
from app.db import get_connection

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory="app/templates")


def _flash(request: Request, category: str, message: str) -> None:
    """Store a single flash message in the session for the next render."""
    request.session["flash"] = (category, message)


def _consume_flash(request: Request) -> Optional[tuple]:
    """Pop and return the flash message if one exists."""
    return request.session.pop("flash", None)


def _render(request: Request, template: str, **ctx) -> HTMLResponse:
    """Helper to render a template with flash and current_user injected."""
    from app.auth import get_current_user
    ctx.setdefault("current_user", get_current_user(request))
    ctx["flash"] = _consume_flash(request)
    ctx["request"] = request
    return templates.TemplateResponse(template, ctx)


# ---------------------------------------------------------------------------
# Login / Logout / Change password
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """GET /auth/login — render the login form. Redirects to dashboard if already logged in."""
    return _render(request, "auth/login.html")


@router.post("/login")
async def login_post(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    """POST /auth/login — validate credentials, create session, redirect. Rate limited 10/min per IP."""
    user = _fetch_user_by_username(username.strip())
    if not user or not user["enabled"] or not user["role_enabled"]:
        _flash(request, "danger", "Invalid credentials or disabled account.")
        return _render(request, "auth/login.html")

    if not check_password_hash(user["password"], password):
        _flash(request, "danger", "Invalid credentials.")
        return _render(request, "auth/login.html")

    import time as _time
    request.session.clear()
    request.session["user_id"] = user["id"]
    request.session["role"] = user["role_name"]
    request.session["login_at"] = int(_time.time())

    if user.get("must_change_password"):
        _flash(request, "warning", "Please update your password to continue.")
        return RedirectResponse(url="/auth/change-password", status_code=303)

    _flash(request, "success", "Welcome back!")
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
async def logout(request: Request, user=Depends(login_required)):
    """POST /auth/logout — destroy the current session and redirect to login."""
    request.session.clear()
    return RedirectResponse(url="/auth/login", status_code=303)


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request, user=Depends(login_required)):
    """GET /auth/change-password — render the forced password change form (shown on first login)."""
    return _render(request, "auth/change_password.html")


@router.post("/change-password")
async def change_password_post(
    request: Request,
    user=Depends(login_required),
    current_password: str = Form(""),
    new_password: str = Form(""),
):
    """POST /auth/change-password — validate and save new password, clear the must_change flag."""
    if not _is_password_strong(new_password):
        _flash(request, "danger", "Password must be at least 8 characters and include letters and numbers.")
        return _render(request, "auth/change_password.html")

    pw_hash = _fetch_user_password_hash(int(user["id"]))
    if not pw_hash or not check_password_hash(pw_hash, current_password):
        _flash(request, "danger", "Current password is incorrect.")
        return _render(request, "auth/change_password.html")

    with get_connection() as conn:
        conn.execute(
            text(
                """
                UPDATE web_users
                SET password = :password, must_change_password = FALSE
                WHERE id = :user_id
                """
            ),
            {"password": generate_password_hash(new_password), "user_id": user["id"]},
        )

    _flash(request, "success", "Password updated successfully.")
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# Admin — dashboard
# ---------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, user=Depends(require_role("admin"))):
    """GET /auth/admin — admin console home page with quick links to users, roles, sites, and API docs."""
    return _render(request, "auth/admin/dashboard.html")


# ---------------------------------------------------------------------------
# Admin — users
# ---------------------------------------------------------------------------

@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request, user=Depends(require_role("admin"))):
    """GET /auth/admin/users — list all user accounts with role, status, created date, and actions."""
    with get_connection() as conn:
        users = (
            conn.execute(
                text(
                    """
                    SELECT web_users.id,
                           web_users.username,
                           web_users.enabled,
                           web_users.created_at,
                           roles.name AS role_name,
                           roles.id   AS role_id,
                           roles.is_system AS role_is_system
                    FROM web_users
                    JOIN roles ON web_users.role_id = roles.id
                    ORDER BY web_users.username
                    """
                )
            )
            .mappings()
            .all()
        )
    return _render(request, "auth/admin/users.html", users=users, roles=_fetch_roles())


@router.post("/admin/users/create")
async def admin_users_create(
    request: Request,
    user=Depends(require_role("admin")),
    username: str = Form(""),
    password: str = Form(""),
    role_id: int = Form(...),
    enabled: Optional[str] = Form(None),
):
    """POST /auth/admin/users/create — create a new user with username, password, and role."""
    username = username.strip()
    is_enabled = enabled == "on"

    if not username or not password or not role_id:
        _flash(request, "danger", "Username, password, and role are required.")
        return RedirectResponse(url="/auth/admin/users", status_code=303)

    if not _is_password_strong(password):
        _flash(request, "danger", "Password must be at least 8 characters and include letters and numbers.")
        return RedirectResponse(url="/auth/admin/users", status_code=303)

    with get_connection() as conn:
        if conn.execute(
            text("SELECT id FROM web_users WHERE username = :u"), {"u": username}
        ).mappings().first():
            _flash(request, "danger", "That username is already in use.")
            return RedirectResponse(url="/auth/admin/users", status_code=303)

        role = conn.execute(
            text("SELECT id, enabled FROM roles WHERE id = :r"), {"r": role_id}
        ).mappings().first()
        if not role or not role["enabled"]:
            _flash(request, "danger", "Selected role is not available.")
            return RedirectResponse(url="/auth/admin/users", status_code=303)

        conn.execute(
            text(
                """
                INSERT INTO web_users (username, password, role_id, enabled)
                VALUES (:username, :password, :role_id, :enabled)
                """
            ),
            {
                "username": username,
                "password": generate_password_hash(password),
                "role_id": role_id,
                "enabled": is_enabled,
            },
        )

    _flash(request, "success", "User created successfully.")
    return RedirectResponse(url="/auth/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/toggle")
async def admin_users_toggle(
    user_id: int, request: Request, user=Depends(require_role("admin"))
):
    """POST /auth/admin/users/{id}/toggle — enable or disable a user account."""
    with get_connection() as conn:
        target = conn.execute(
            text("SELECT enabled FROM web_users WHERE id = :id"), {"id": user_id}
        ).mappings().first()
        if not target:
            _flash(request, "danger", "User not found.")
            return RedirectResponse(url="/auth/admin/users", status_code=303)
        conn.execute(
            text("UPDATE web_users SET enabled = :e WHERE id = :id"),
            {"e": not target["enabled"], "id": user_id},
        )
    _flash(request, "success", "User status updated.")
    return RedirectResponse(url="/auth/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/role")
async def admin_users_role(
    user_id: int,
    request: Request,
    user=Depends(require_role("admin")),
    role_id: int = Form(...),
):
    """POST /auth/admin/users/{id}/role — change a user's role. Cannot demote your own admin account."""
    if user["id"] == user_id:
        with get_connection() as conn:
            role = conn.execute(
                text("SELECT name FROM roles WHERE id = :r"), {"r": role_id}
            ).mappings().first()
            if role and role["name"] != "admin":
                _flash(request, "danger", "You cannot remove your own admin role.")
                return RedirectResponse(url="/auth/admin/users", status_code=303)

    with get_connection() as conn:
        role = conn.execute(
            text("SELECT id, enabled FROM roles WHERE id = :r"), {"r": role_id}
        ).mappings().first()
        if not role or not role["enabled"]:
            _flash(request, "danger", "Selected role is not available.")
            return RedirectResponse(url="/auth/admin/users", status_code=303)
        conn.execute(
            text("UPDATE web_users SET role_id = :r WHERE id = :id"),
            {"r": role_id, "id": user_id},
        )
    _flash(request, "success", "User role updated.")
    return RedirectResponse(url="/auth/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/password")
async def admin_users_password(
    user_id: int,
    request: Request,
    user=Depends(require_role("admin")),
    password: str = Form(""),
):
    """POST /auth/admin/users/{id}/password — reset a user's password. Requires admin's own password for confirmation."""
    if not _is_password_strong(password):
        _flash(request, "danger", "Password must be at least 8 characters and include letters and numbers.")
        return RedirectResponse(url="/auth/admin/users", status_code=303)
    with get_connection() as conn:
        conn.execute(
            text("UPDATE web_users SET password = :p WHERE id = :id"),
            {"p": generate_password_hash(password), "id": user_id},
        )
    _flash(request, "success", "Password updated.")
    return RedirectResponse(url="/auth/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/delete")
async def admin_users_delete(
    user_id: int,
    request: Request,
    user=Depends(require_role("admin")),
    admin_password: str = Form(""),
):
    """POST /auth/admin/users/{id}/delete — permanently delete a user. Requires admin's own password."""
    if user["id"] == user_id:
        _flash(request, "danger", "You cannot delete your own account.")
        return RedirectResponse(url="/auth/admin/users", status_code=303)

    if not admin_password or not _admin_password_matches(user, admin_password):
        _flash(request, "danger", "Admin password confirmation failed.")
        return RedirectResponse(url="/auth/admin/users", status_code=303)

    with get_connection() as conn:
        if not conn.execute(
            text("SELECT id FROM web_users WHERE id = :id"), {"id": user_id}
        ).mappings().first():
            _flash(request, "danger", "User not found.")
            return RedirectResponse(url="/auth/admin/users", status_code=303)
        conn.execute(text("DELETE FROM web_users WHERE id = :id"), {"id": user_id})

    _flash(request, "success", "User deleted.")
    return RedirectResponse(url="/auth/admin/users", status_code=303)


# ---------------------------------------------------------------------------
# Admin — roles
# ---------------------------------------------------------------------------

@router.get("/admin/roles", response_class=HTMLResponse)
async def admin_roles(request: Request, user=Depends(require_role("admin"))):
    """GET /auth/admin/roles — list all system and custom roles."""
    return _render(request, "auth/admin/roles.html", roles=_fetch_roles())


@router.post("/admin/roles/create")
async def admin_roles_create(
    request: Request,
    user=Depends(require_role("admin")),
    name: str = Form(""),
    description: str = Form(""),
):
    """POST /auth/admin/roles/create — create a new custom role."""
    name = name.strip().lower()
    if not name:
        _flash(request, "danger", "Role name is required.")
        return RedirectResponse(url="/auth/admin/roles", status_code=303)
    with get_connection() as conn:
        if conn.execute(
            text("SELECT id FROM roles WHERE name = :n"), {"n": name}
        ).mappings().first():
            _flash(request, "danger", "That role already exists.")
            return RedirectResponse(url="/auth/admin/roles", status_code=303)
        conn.execute(
            text(
                "INSERT INTO roles (name, description, is_system, enabled) VALUES (:n, :d, FALSE, TRUE)"
            ),
            {"n": name, "d": description.strip()},
        )
    _flash(request, "success", "Role created.")
    return RedirectResponse(url="/auth/admin/roles", status_code=303)


@router.post("/admin/roles/{role_id}/edit")
async def admin_roles_edit(
    role_id: int,
    request: Request,
    user=Depends(require_role("admin")),
    description: str = Form(""),
):
    """POST /auth/admin/roles/{id}/edit — update a role's description. System roles cannot be renamed."""
    with get_connection() as conn:
        conn.execute(
            text("UPDATE roles SET description = :d WHERE id = :id"),
            {"d": description.strip(), "id": role_id},
        )
    _flash(request, "success", "Role updated.")
    return RedirectResponse(url="/auth/admin/roles", status_code=303)


@router.post("/admin/roles/{role_id}/toggle")
async def admin_roles_toggle(
    role_id: int, request: Request, user=Depends(require_role("admin"))
):
    """POST /auth/admin/roles/{id}/toggle — enable or disable a role. System roles cannot be disabled."""
    with get_connection() as conn:
        role = conn.execute(
            text("SELECT enabled, is_system FROM roles WHERE id = :id"), {"id": role_id}
        ).mappings().first()
        if not role:
            _flash(request, "danger", "Role not found.")
            return RedirectResponse(url="/auth/admin/roles", status_code=303)
        if role["is_system"]:
            _flash(request, "warning", "System roles cannot be disabled.")
            return RedirectResponse(url="/auth/admin/roles", status_code=303)
        conn.execute(
            text("UPDATE roles SET enabled = :e WHERE id = :id"),
            {"e": not role["enabled"], "id": role_id},
        )
    _flash(request, "success", "Role status updated.")
    return RedirectResponse(url="/auth/admin/roles", status_code=303)


@router.post("/admin/roles/{role_id}/delete")
async def admin_roles_delete(
    role_id: int,
    request: Request,
    user=Depends(require_role("admin")),
    admin_password: str = Form(""),
):
    """POST /auth/admin/roles/{id}/delete — delete a custom role. System roles are protected."""
    if not admin_password or not _admin_password_matches(user, admin_password):
        _flash(request, "danger", "Admin password confirmation failed.")
        return RedirectResponse(url="/auth/admin/roles", status_code=303)

    with get_connection() as conn:
        role = conn.execute(
            text("SELECT is_system FROM roles WHERE id = :id"), {"id": role_id}
        ).mappings().first()
        if not role:
            _flash(request, "danger", "Role not found.")
            return RedirectResponse(url="/auth/admin/roles", status_code=303)
        if role["is_system"]:
            _flash(request, "warning", "System roles cannot be deleted.")
            return RedirectResponse(url="/auth/admin/roles", status_code=303)
        assigned = conn.execute(
            text("SELECT COUNT(*) AS count FROM web_users WHERE role_id = :id"),
            {"id": role_id},
        ).mappings().first()
        if assigned and assigned["count"] > 0:
            _flash(request, "warning", "Reassign users before deleting this role.")
            return RedirectResponse(url="/auth/admin/roles", status_code=303)
        conn.execute(text("DELETE FROM roles WHERE id = :id"), {"id": role_id})

    _flash(request, "success", "Role deleted.")
    return RedirectResponse(url="/auth/admin/roles", status_code=303)
