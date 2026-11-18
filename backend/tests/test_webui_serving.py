"""Headless FastAPI TestClient checks of the orchestrator's web-UI serving: the
token-injected shell route and the StaticFiles mount serving client.js/astral.css
from AstralProjection's packaged static resources.
"""

from pathlib import Path

from astralprojection.resources import static_root, template_path, vendor_path

STATIC_ROOT = Path(str(static_root()))
SHELL = template_path("shell.html")


def test_vendor_assets_present():
    assert len(vendor_path("tailwind.js").read_bytes()) > 10000
    assert len(vendor_path("plotly.min.js").read_bytes()) > 100000
