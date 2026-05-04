"""Main (page) routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, login_required
from app.routers.auth import _consume_flash

router = APIRouter(tags=["main"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, user=Depends(login_required)):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "current_user": user,
            "flash": _consume_flash(request),
        },
    )


@router.get("/health")
async def health():
    """Kubernetes liveness / readiness probe. Always returns 200."""
    return {"status": "ok"}
