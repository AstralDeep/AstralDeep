"""Checks shared search construction and current owner-bound source opening without
duplicating scanners, including revoked, malformed, and revision-changed references.
"""

from types import SimpleNamespace

import pytest

from personalization import federated_search as module


def test_api_and_ui_share_the_same_search_factory(monkeypatch):
    import personalization.search_api as api

    orch = SimpleNamespace()
    monkeypatch.setattr(module, "FederatedSearchService", lambda owner: SimpleNamespace(orch=owner))
    factory = getattr(module, "get_search_service", None)
    assert callable(factory)
    first = factory(orch)
    assert factory(orch) is first
    assert api._service(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(orchestrator=orch)))) is first


@pytest.mark.asyncio
async def test_open_memory_rechecks_current_owner_and_excludes_atlas_envelopes(monkeypatch):
    service = object.__new__(module.FederatedSearchService)
    owners = []
    value = {"id": "memory", "category": "preference", "value": "current source", "updated_at": 1}

    def get(owner, identity):
        owners.append((owner, identity))
        return value if owner == "owner" else None

    service.repo = SimpleNamespace(get_memory=get)
    view = await service.open_source(owner_id="owner", ref={"kind": "memory", "memory_id": "memory"})
    assert view.text == "current source" and view.source == "memory"
    assert owners == [("owner", "memory")]
    assert await service.open_source(owner_id="foreign", ref=view.ref) is None
    value["superseded_at"] = 1
    assert await service.open_source(owner_id="owner", ref=view.ref) is None


@pytest.mark.parametrize("ref", [None, {}, {"kind": "unknown"}, {"kind": "memory", "memory_id": ""},
    {"kind": "memory", "memory_id": "m", "owner_id": "foreign"},
    {"kind": "note", "note_id": "n", "expected_revision": True},
    {"kind": "conversation", "conversation_id": "c", "message_id": "foreign"}])
@pytest.mark.asyncio
async def test_malformed_source_references_fail_closed_before_io(ref):
    service = object.__new__(module.FederatedSearchService)
    with pytest.raises(module.SearchQueryInvalid):
        await service.open_source(owner_id="owner", ref=ref)
