"""JSON API routes.

No CSRF tokens needed here — API routes are stateless and protected by
the login_required dependency which checks the session cookie.

Add your product-specific endpoints here following the same Depends() pattern.
"""

from fastapi import APIRouter, Depends, Request

from app.auth import login_required, require_role

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/me")
async def api_me(request: Request, user=Depends(login_required)):
    """Return the current user's public profile."""
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role_name"],
    }


# ---------------------------------------------------------------------------
# Add product-specific API routes below this line.
# ---------------------------------------------------------------------------
