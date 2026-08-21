from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from orchestrator import api


OWNER = "owner-074"
DRAFT_ID = "draft-074"


def _draft(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": DRAFT_ID,
        "user_id": OWNER,
        "agent_name": "Plane Draft",
        "agent_slug": "plane_draft",
        "description": "A typed Plane-backed draft API record.",
        "tools_spec": None,
        "skill_tags": None,
        "packages": None,
        "status": "pending",
        "generation_log": None,
        "security_report": None,
        "validation_report": None,
        "error_message": None,
        "port": None,
        "review_notes": None,
        "reviewed_by": None,
        "refinement_history": None,
        "required_credentials": None,
        "created_at": 1,
        "updated_at": 1,
        "origin": "manual",
    }
    row.update(changes)
    return row


class DraftStore:
    def __init__(self) -> None:
        self.rows = {DRAFT_ID: _draft()}
        self.updates: list[tuple[str, dict[str, object]]] = []

    def get_user_draft_agents(self, owner_id: str):
        return [dict(row) for row in self.rows.values() if row["user_id"] == owner_id]

    def get_pending_review_drafts(self):
        return [dict(row) for row in self.rows.values() if row["status"] == "pending_review"]

    def get_owned_draft_agent(self, owner_id: str, draft_id: str):
        row = self.rows.get(draft_id)
        if row is None or row["user_id"] != owner_id:
            return None
        return dict(row)

    def update_draft_agent(self, draft_id: str, **updates: object):
        self.rows[draft_id].update(updates)
        self.updates.append((draft_id, dict(updates)))
        return True


@pytest.fixture()
def boundary():
    store = DraftStore()
    lifecycle = SimpleNamespace(
        draft_store=store,
        create_draft=AsyncMock(return_value=dict(store.rows[DRAFT_ID])),
        delete_draft=AsyncMock(return_value=True),
        generate_code=AsyncMock(return_value=_draft(status="generated")),
        refine_agent=AsyncMock(return_value=_draft(status="generated")),
        start_draft_agent=AsyncMock(return_value=_draft(status="testing")),
        stop_draft_agent=AsyncMock(),
        approve_agent=AsyncMock(return_value=_draft(status="live")),
    )
    credentials = MagicMock()
    credentials.list_credential_keys.return_value = ["API_KEY"]
    orch = SimpleNamespace(
        lifecycle_manager=lifecycle,
        agent_cards={},
        ui_sessions={},
        credential_manager=credentials,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(orchestrator=orch)))
    return request, orch, lifecycle, store


@pytest.mark.asyncio
async def test_owner_lists_and_reads_use_typed_draft_store(boundary) -> None:
    request, _orch, _lifecycle, store = boundary
    listed = await api.list_drafts(request, user_id=OWNER)
    assert [draft.id for draft in listed.drafts] == [DRAFT_ID]
    fetched = await api.get_draft(request, DRAFT_ID, user_id=OWNER)
    assert fetched.id == DRAFT_ID

    store.rows[DRAFT_ID]["user_id"] = "other-owner"
    with pytest.raises(HTTPException) as denied:
        await api.get_draft(request, DRAFT_ID, user_id=OWNER)
    assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_pending_review_requires_admin_and_uses_admin_repository(boundary) -> None:
    request, _orch, _lifecycle, store = boundary
    store.rows[DRAFT_ID]["status"] = "pending_review"
    with pytest.raises(HTTPException) as denied:
        await api.list_pending_review(request, OWNER, {"roles": ["user"]})
    assert denied.value.status_code == 403
    result = await api.list_pending_review(request, OWNER, {"roles": ["admin"]})
    assert [draft.id for draft in result.drafts] == [DRAFT_ID]


@pytest.mark.asyncio
async def test_stop_and_delete_keep_owner_gate_and_typed_mutation(boundary) -> None:
    request, _orch, lifecycle, store = boundary
    stopped = await api.stop_draft(request, DRAFT_ID, user_id=OWNER)
    assert stopped.status == "generated"
    lifecycle.stop_draft_agent.assert_awaited_once_with(DRAFT_ID)
    assert store.updates == [(DRAFT_ID, {"status": "generated"})]

    deleted = await api.delete_draft(request, DRAFT_ID, user_id=OWNER)
    assert "successfully" in deleted.message
    lifecycle.delete_draft.assert_awaited_once_with(DRAFT_ID)


@pytest.mark.asyncio
async def test_draft_credentials_are_owner_scoped_without_history_database(boundary) -> None:
    request, orch, _lifecycle, store = boundary
    store.rows[DRAFT_ID]["required_credentials"] = '["API_KEY"]'
    result = await api.get_draft_credentials(request, DRAFT_ID, user_id=OWNER)
    assert result["required_credentials"] == ["API_KEY"]
    assert result["stored_credential_keys"] == ["API_KEY"]

    body = api.CredentialSetRequest(credentials={"API_KEY": "secret"})
    updated = await api.set_draft_credentials(
        request,
        DRAFT_ID,
        body,
        user_id=OWNER,
    )
    assert updated["stored_credential_keys"] == ["API_KEY"]
    orch.credential_manager.set_bulk_credentials.assert_called_once()


@pytest.mark.asyncio
async def test_missing_draft_fails_closed_before_lifecycle_action(boundary) -> None:
    request, _orch, lifecycle, _store = boundary
    with pytest.raises(HTTPException) as missing:
        await api.generate_draft(request, "missing", user_id=OWNER)
    assert missing.value.status_code == 404
    lifecycle.generate_code.assert_not_awaited()
