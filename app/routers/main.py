"""Main routes.

/ redirects to /dashboard (the power flow view).
/health is the K8s liveness/readiness probe — always returns 200.
/manifest.json and /sw.js must be served from the root scope —
  the browser requires the service worker to be at the root of its scope,
  and the PWA manifest link in dashboard.html points to /manifest.json.
  Both files live in static/ but need root-level URLs.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app.auth import require_role

router = APIRouter(tags=["main"])

_STATIC = Path(__file__).parent.parent.parent / "static"


@router.get("/")
async def home():
    """Redirect root to the dashboard."""
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/health")
async def health():
    """Kubernetes liveness / readiness probe. Always returns 200."""
    return {"status": "ok"}


@router.get("/manifest.json", include_in_schema=False)
async def manifest():
    """PWA manifest — must be served from root scope for install to work."""
    return FileResponse(
        _STATIC / "manifest.json",
        media_type="application/manifest+json",
    )


@router.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Service worker — must be served from root scope, not /static/js/.
    The browser restricts a SW's scope to the directory it is served from,
    so /static/js/sw.js could only control /static/js/* — useless for us.
    """
    response = FileResponse(
        _STATIC / "js" / "sw.js",
        media_type="application/javascript",
    )
    # Never cache the service worker itself — browser must always get latest
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Favicon — browsers request this from root automatically."""
    return FileResponse(
        _STATIC / "icons" / "favicon.ico",
        media_type="image/x-icon",
    )


# ---------------------------------------------------------------------------
# Protected API docs — admin only, available in all environments
# ---------------------------------------------------------------------------

@router.get("/api/docs", response_class=HTMLResponse, include_in_schema=False)
async def api_docs(request: Request, user=Depends(require_role("admin"))):
    """Swagger UI — admin only. Available in development and production."""
    return get_swagger_ui_html(
        openapi_url="/api/openapi.json",
        title="SolarWatch API — Swagger UI",
        swagger_favicon_url="/favicon.ico",
    )


@router.get("/api/redoc", response_class=HTMLResponse, include_in_schema=False)
async def api_redoc(request: Request, user=Depends(require_role("admin"))):
    """ReDoc — admin only."""
    return get_redoc_html(
        openapi_url="/api/openapi.json",
        title="SolarWatch API — ReDoc",
        redoc_favicon_url="/favicon.ico",
    )
