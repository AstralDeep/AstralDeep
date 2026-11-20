
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import evidence_context
from orchestrator.projection_surfaces import evidence
from personalization import phi_gate
from personalization.phi_gate import PHIGate
from persistent_agents.models import AssignmentError
from shared.protocol import MCPResponse

pytestmark = pytest.mark.asyncio
TEXT = '<script>untrusted source</script> Full permitted source. ' * 110
SUMMARY = '<img src=x onerror=alert(1)> Generated summary stays untrusted.'


async def registered(state):
    state.register()
    await state.registrations.get()
    await state.barrier()


def surfaces(state):
    return [value for value in state.socket.payloads() if value.get("type") in {"chrome_render", "chrome_surface"}
        and value.get("surface_key") == "evidence"]


async def test_privacy_fingerprint_tracks_presidio_recognizer_and_model_configuration(monkeypatch):
    pattern = SimpleNamespace(name="synthetic", regex="ORBIT", score=0.5)
    recognizer = SimpleNamespace(patterns=[pattern], supported_entities=["PERSON"], supported_language="en")
    model = SimpleNamespace(low_score_entity_names={"ORG", "CITY"})
    analyzer = SimpleNamespace(analyze=lambda **_: [], registry=SimpleNamespace(recognizers=[recognizer]),
        nlp_engine=SimpleNamespace(ner_model_configuration=model))
    gate = PHIGate(analyzer=analyzer)
    monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: gate)
    initial = evidence_context._privacy_snapshot()[0]
    model.low_score_entity_names = frozenset({"CITY", "ORG"})
    assert evidence_context._privacy_snapshot()[0] == initial
    pattern.score = 0.6
    changed = evidence_context._privacy_snapshot()[0]
    assert changed != initial
    recognizer.supported_entities.append("SYNTHETIC")
    assert evidence_context._privacy_snapshot()[0] != changed
    analyzer.nlp_engine.nlp = {"en": object()}
    loaded = evidence_context._privacy_snapshot()[0]
    analyzer.nlp_engine.nlp["en"] = object()
    assert evidence_context._privacy_snapshot()[0] != loaded


@pytest.mark.parametrize("invalid", ["unavailable", "registry", "recognizers", "patterns", "set_size",
    "set_type", "key", "list_size", "string", "object", "depth", "nodes", "bytes", "loaded"])
async def test_uncanonical_or_over_budget_privacy_configuration_is_denied(monkeypatch, invalid):
    recognizer = SimpleNamespace(patterns=[])
    analyzer = SimpleNamespace(analyze=lambda **_: [], registry=SimpleNamespace(recognizers=[recognizer]))
    gate = PHIGate(analyzer=analyzer)
    if invalid == "unavailable":
        gate._analyzer = None
    elif invalid == "registry":
        analyzer.registry.recognizers = {}
    elif invalid == "recognizers":
        analyzer.registry.recognizers = [recognizer] * 129
    elif invalid == "patterns":
        recognizer.patterns = {}
    elif invalid == "set_size":
        recognizer.supported_entities = {str(n) for n in range(257)}
    elif invalid == "set_type":
        recognizer.supported_entities = {1}
    elif invalid == "key":
        analyzer.nlp_engine = SimpleNamespace(models={1: "invalid"})
    elif invalid == "list_size":
        analyzer.nlp_engine = SimpleNamespace(models=["invalid"] * 257)
    elif invalid == "string":
        recognizer.name = "x" * 16385
    elif invalid == "object":
        recognizer.name = object()
    elif invalid == "depth":
        nested = "synthetic"
        for _ in range(9):
            nested = [nested]
        analyzer.nlp_engine = SimpleNamespace(models=nested)
    elif invalid == "nodes":
        analyzer.nlp_engine = SimpleNamespace(models=[[0] * 256] * 33)
    elif invalid == "loaded":
        analyzer.nlp_engine = SimpleNamespace(nlp=[])
    else:
        analyzer.nlp_engine = SimpleNamespace(models=["x" * 4096] * 17)
    monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: gate)
    with pytest.raises(evidence_context.EvidenceDenied):
        evidence_context._privacy_snapshot()


@pytest.mark.parametrize("value", [None, 1, "chat", "9F1C2B7E-3D4A-4C5B-8E6F-7A8B9C0D1E2F"])
async def test_active_chat_must_be_a_canonical_uuid(value):
    with pytest.raises(AssignmentError):
        evidence._chat(value)


@pytest.mark.parametrize("pending", [None, SimpleNamespace(owner_id="owner"), "captured read"])
async def test_navigation_cannot_be_minted_from_public_or_duck_typed_authority(pending):
    with pytest.raises(AssignmentError):
        evidence.capture_navigation(SimpleNamespace(), pending=pending, chat_id=str(uuid4()))


def recalled_response(**changes):
    data = {"reference": "obs_" + "a" * 43, "digest": "b" * 64, "start": 0, "end": 4, "total": 4,
        "text": "text", "untrusted": True, "next_offset": None, "at_end": True, "partial": False,
        "outcome": "success"}
    data.update(changes)
    return MCPResponse(result=data, ui_components=[{"type": "badge", "label": "Captured source"}])


@pytest.mark.parametrize("change", [
    {"reference": "obs_" + "z" * 43}, {"untrusted": False}, {"text": 1}, {"text": "X" * 16385},
    {"start": True}, {"start": 1}, {"end": -1}, {"end": 5}, {"total": -1}, {"digest": "short"},
    {"digest": None},
])
async def test_unverified_page_shape_cannot_reach_a_renderer(change):
    with pytest.raises(AssignmentError):
        evidence._components(recalled_response(**change), {"reference": "obs_" + "a" * 43, "offset": 0}, "source")


@pytest.mark.parametrize("response", ["untyped", MCPResponse(result=None),
    MCPResponse(result={}, ui_components=None), MCPResponse(result={}, ui_components=["raw HTML"])])
async def test_non_contract_responses_cannot_publish_source(response):
    with pytest.raises(AssignmentError):
        evidence._components(response, {}, "usage")


@pytest.mark.parametrize("kind,data,args", [
    ("usage", {"usage": {}, "model_calls": True}, {}),
    ("usage", {"usage": [], "model_calls": 1}, {}),
    ("summary", {"view": "generated_summary", "view_id": "view_" + "a" * 43,
        "text": "summary", "untrusted": True, "source_refs": ["obs_short"]}, {"view_id": "view_" + "a" * 43}),
    ("summary", {"view": "generated_summary", "view_id": "view_" + "a" * 43,
        "text": "", "untrusted": True, "source_refs": []}, {"view_id": "view_" + "a" * 43}),
])
async def test_usage_and_summary_metadata_cannot_claim_a_verified_wrong_shape(kind, data, args):
    with pytest.raises(AssignmentError):
        evidence._components(MCPResponse(result=data, ui_components=[]), args, kind)


async def test_partial_preview_clamps_utf8_without_splitting_a_character():
    text = "🐍" * 400
    response = recalled_response(text=text, end=len(text.encode()), total=len(text.encode()))
    components = evidence._components(response, {"reference": "obs_" + "a" * 43, "offset": 0}, "preview")
    values = [item["value"] for component in components if component["type"] == "keyvalue"
              for item in component["items"] if item["key"] == "Permitted text"]
    assert values == ["🐍" * 256]


async def test_watch_preview_retains_only_the_exact_utf8_preview_extent():
    text = "🐍" * 400
    response = recalled_response(text=text, end=len(text.encode()), total=len(text.encode()))
    components = evidence._watch_components(response,
        {"reference": "obs_" + "a" * 43, "offset": 0}, "preview")
    rendered = json.dumps(components)
    assert "0\\u20131024 of 1600" in rendered and "phone or desktop" in rendered
    assert "Permitted text" not in rendered and "\\ud83d\\udc0d" not in rendered
    assert not any(component["type"] == "button" for component in components)
    assert response.result["text"] == text and response.result["end"] == 1600


@pytest.mark.parametrize("kind", ["source", "summary"])
async def test_watch_authorized_unavailable_view_has_no_invented_reference_or_inspection_action(kind):
    response = MCPResponse(result={"status": "unavailable", "reason": "deleted"},
        ui_components=[{"type": "badge", "label": "Source unavailable"}])
    components = evidence._watch_components(response, {"view_id": "view_" + "a" * 43}, kind)
    rendered = json.dumps(components)
    assert "Source unavailable" in rendered and "phone or desktop" in rendered
    assert "view_" not in rendered and "chrome_open" not in rendered


@pytest.mark.parametrize("response", [None, MCPResponse(error={"message": "private source details"})])
async def test_watch_failed_read_still_hands_off_without_private_error_content(response):
    components = evidence._watch_components(response, {}, "source")
    rendered = json.dumps(components)
    assert "Recall blocked" in rendered and "phone or desktop" in rendered
    assert "private source details" not in rendered and "chrome_open" not in rendered


async def test_blocked_and_authorized_unavailable_states_do_not_echo_response_bodies():
    components = evidence._components(MCPResponse(error={"message": "private"}), {}, "source")
    assert "private" not in json.dumps(components) and components[0]["label"] == "Recall blocked"
    unavailable = MCPResponse(result={"status": "unavailable", "reason": "deleted"},
        ui_components=[{"type": "badge", "label": "Source unavailable"}])
    assert evidence._components(unavailable, {}, "source") == unavailable.ui_components
