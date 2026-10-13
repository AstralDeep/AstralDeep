"""Serves Projection's dedicated joining-device page with no-store security headers.
The page removes enrollment fragments before using the ordinary mesh redemption API.
"""

import secrets

from astralprojection import template_path
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

mesh_web_router = APIRouter()


@mesh_web_router.get("/enroll", response_class=HTMLResponse, include_in_schema=False)
async def enrollment_page():
    from orchestrator.orchestrator import (
        _apply_asset_versions,
        _projection_static_filesystem_root,
    )

    nonce = secrets.token_urlsafe(16)
    try:
        content = template_path("enrollment.html").read_text(encoding="utf-8")
        content = _apply_asset_versions(content, _projection_static_filesystem_root())
    except (OSError, ValueError):
        return HTMLResponse("<p>Enrollment unavailable.</p>", status_code=503)
    response = HTMLResponse(content.replace("%%ASTRAL_NONCE%%", nonce))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self'; font-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=()"
    return response
