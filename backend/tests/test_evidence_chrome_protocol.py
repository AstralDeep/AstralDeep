"""Checks strict evidence modal correlation at Deep's chrome serialization boundary.
Only a canonical request generation can serialize the requested owner modal.
"""

import json
from uuid import uuid1, uuid4

import pytest

from shared.protocol import ChromeRender, ProtocolValidationError


def test_requested_evidence_modal_preserves_its_exact_generation_and_payload():
    generation = str(uuid4())
    frame = ChromeRender(surface_key="evidence", request_generation=generation,
                         html="<section>Escaped evidence</section>")
    data = json.loads(frame.to_json())
    assert data["type"] == "chrome_render" and data["region"] == "modal" and data["mode"] == "replace"
    assert data["surface_key"] == "evidence" and data["request_generation"] == generation
    assert data["html"] == frame.html


@pytest.mark.parametrize("generation", [None, "", "stale", 1, True, [], {}, str(uuid1()),
                                       str(uuid4()).upper(), " " + str(uuid4())])
def test_evidence_modal_cannot_serialize_a_missing_or_malformed_generation(generation):
    with pytest.raises(ProtocolValidationError):
        ChromeRender(surface_key="evidence", request_generation=generation).to_json()


@pytest.mark.parametrize("region", [None, "", "topbar", "chat", "main"])
def test_evidence_correlation_cannot_address_a_non_modal_region(region):
    with pytest.raises(ProtocolValidationError):
        ChromeRender(surface_key="evidence", region=region, request_generation=str(uuid4())).to_json()


@pytest.mark.parametrize("surface", ["Evidence", "agents", "evidence/foreign", "", None])
def test_public_surface_keys_cannot_borrow_evidence_modal_correlation(surface):
    with pytest.raises(ProtocolValidationError):
        ChromeRender(surface_key=surface, request_generation=str(uuid4())).to_json()


def test_uncorrelated_ordinary_chrome_keeps_its_existing_serialization():
    data = json.loads(ChromeRender(html="ordinary", region="topbar").to_json())
    assert data["html"] == "ordinary" and data["region"] == "topbar"
    assert "surface_key" not in data and "request_generation" not in data
