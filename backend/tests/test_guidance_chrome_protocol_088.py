"""Only named owner surfaces may carry independent response generations."""
import json
from uuid import uuid4

import pytest

from shared.protocol import ChromeRender, ChromeSurface, ProtocolValidationError


@pytest.mark.parametrize("surface", ["work", "guidance"])
@pytest.mark.parametrize("frame_type", [ChromeRender, ChromeSurface])
def test_owner_surface_preserves_exact_correlation(surface, frame_type):
    generation = str(uuid4())
    frame = frame_type(surface_key=surface, request_generation=generation)
    wire = json.loads(frame.to_json())
    assert wire["surface_key"] == surface
    assert wire["request_generation"] == generation


@pytest.mark.parametrize("frame_type", [ChromeRender, ChromeSurface])
@pytest.mark.parametrize("generation", [None, "", "stale", 1, True])
def test_guidance_requires_uuid_generation(frame_type, generation):
    with pytest.raises(ProtocolValidationError):
        frame_type(surface_key="guidance", request_generation=generation).to_json()


@pytest.mark.parametrize("frame_type", [ChromeRender, ChromeSurface])
def test_other_surfaces_cannot_borrow_correlation(frame_type):
    with pytest.raises(ProtocolValidationError):
        frame_type(surface_key="agents", request_generation=str(uuid4())).to_json()


def test_correlated_web_response_requires_modal():
    with pytest.raises(ProtocolValidationError):
        ChromeRender(surface_key="guidance", region="topbar",
                     request_generation=str(uuid4())).to_json()
