"""Tests for registered Work presentation
(backend/orchestrator/work_surface_authority.py, work_service.py,
projection_surfaces/work.py): public-row rendering, WS propose/save parity, and
refusal of stale or foreign requests.
"""

import json
from uuid import uuid4

import pytest

from shared.protocol import ChromeRender, ChromeSurface, ProtocolValidationError


@pytest.mark.parametrize("frame", [ChromeRender(), ChromeSurface(surface_key="agents")])
def test_legacy_chrome_wire_fields_remain_absent(frame):
    value = json.loads(frame.to_json())
    assert "request_generation" not in value
    if isinstance(frame, ChromeRender):
        assert "surface_key" not in value


@pytest.mark.parametrize("frame", [ChromeRender(surface_key="work"), ChromeSurface(surface_key="work"),
    ChromeRender(request_generation=str(uuid4()))])
def test_work_correlation_is_mandatory_and_scoped(frame):
    with pytest.raises(ProtocolValidationError):
        frame.to_json()


