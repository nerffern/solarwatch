"""Main routes.

/ redirects to /dashboard (the power flow view).
/health is the K8s liveness/readiness probe — always returns 200.
/manifest.json and /sw.js must be served from the root scope —
  the browser requires the service worker to be at the root of its scope,
  and the PWA manifest link in dashboard.html points to /manifest.json.
  Both files live in static/ but need root-level URLs.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse

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
