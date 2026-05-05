"""Shared Jinja2Templates instance used by all routers.

All routers must import templates from here — not create their own instance.
This ensures APP_VERSION and other globals are available in every template.
"""
import pathlib
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

# Inject APP_VERSION at module load time so it is available in all templates
# regardless of when create_app() runs relative to router imports.
_version_file = pathlib.Path(__file__).parent.parent / "VERSION"
templates.env.globals["APP_VERSION"] = (
    _version_file.read_text().strip() if _version_file.exists() else "dev"
)
