"""Owner canvas Save through actual IAM, research, publication and audit.

External JWT/JWKS, refresh and source/model replies are signed synthetic fixtures.
The registered router, completed research proof, sessions and all SQL are real.
"""
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest

from audit.repository import AuditRepository
from orchestrator.work_api import work_router
from tests.test_work_result_postgres_088 import (
    completed as completed, fixture as fixture, gate_orchestrator as gate_orchestrator,
    operation as operation, plane as plane, research as research, signing_key as signing_key,
)
from tests.test_work_write_http_postgres_088 import headers

runtime = plane
pytestmark = [pytest.mark.asyncio,
              pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)]


@pytest.fixture
async def saved(completed, fixture):
    op = completed
    orch = op.executor.orch
    orch.persistent_assignments = op.executor.service
    orch.web_sessions = op.sessions
    orch.audit_repo = AuditRepository(plane_runtime=op.runtime)
    orch.runtime_composition = SimpleNamespace(
        plane=SimpleNamespace(runtime=op.runtime, repositories=op.runtime.repositories))
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(work_router, prefix="/api")
    chat_id = str(uuid4())
    with op.runtime.transaction() as tx:
        op.runtime.repositories.history.conversations.create(tx, owner_id=op.owner,
            conversation_id=chat_id, title="Reviewed result destination", agent_id=None, created_at=1)
    return SimpleNamespace(op=op, app=app, fixture=fixture, chat_id=chat_id)


def proposal_body(value):
    return {"version": 1, "submission_id": str(uuid4()), "publication_id": str(uuid4()),
            "expected_revision": value.op.completed.state_version,
            "conversation_id": value.chat_id, "expected_workspace_revision": 0,
            "expected_workspace_publication_id": None}


def path(value):
    return f"/api/work/v1/operations/{value.op.completed.assignment_id}/result/proposals"


async def post(value, url, body, *, cookie=True):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=value.app),
                                 base_url="https://app.invalid") as client:
        return await client.post(url, json=body, headers=headers(value.fixture, cookie=cookie))


async def test_registered_proposal_is_review_only_then_exact_save_visible_once(saved):
    command = proposal_body(saved)
    proposed = await post(saved, path(saved), command)
    assert proposed.status_code == 200, proposed.text
    review = proposed.json()
    assert review["version"] == 1 and review["status"] == "review_required"
    assert review["proposal"]["action_id"] == command["submission_id"]
    assert "Public release 088" in proposed.text
    assert "synthetic-provider-key" not in proposed.text and "binding_key" not in proposed.text
    with saved.op.runtime.transaction() as tx:
        chat = saved.op.runtime.repositories.history.conversations.get(tx,
            owner_id=saved.op.owner, conversation_id=saved.chat_id)
        assert chat.render_revision == 0 and chat.conversation_commit_id is None
    approval = {"version": 1, "submission_id": str(uuid4()),
                "expected_revision": review["revision"],
                "proposal_digest": review["proposal"]["proposal_digest"]}
    target = path(saved) + "/" + command["submission_id"] + "/save"
    response = await post(saved, target, approval)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "saved" and response.json()["applied"] is True
    assert response.headers["cache-control"] == "no-store"
    with saved.op.runtime.transaction() as tx:
        chat = saved.op.runtime.repositories.history.conversations.get(tx,
            owner_id=saved.op.owner, conversation_id=saved.chat_id)
        assert chat.render_revision == 1
        assert chat.conversation_commit_id == command["publication_id"]
    before = len(saved.fixture[-1])
    saved.op.sessions.delete(saved.op.sid)
    replay = await post(saved, target, approval, cookie=False)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**response.json(), "applied": False}
    assert len(saved.fixture[-1]) == before
    rows, _ = saved.op.executor.orch.audit_repo.list_for_user(saved.op.owner)
    assert sum(row.action_type == "work.result.propose" for row in rows) == 1
    assert sum(row.action_type == "work.result.save" for row in rows) == 1


async def test_new_proposal_refuses_bare_bearer_without_refresh_or_publication(saved):
    before = len(saved.fixture[-1])
    response = await post(saved, path(saved), proposal_body(saved), cookie=False)
    assert response.status_code == 401
    assert len(saved.fixture[-1]) == before


@pytest.mark.parametrize("field,value", [("version", True), ("expected_revision", True),
    ("content", "private client replacement"), ("conversation_id", "foreign-destination")])
async def test_closed_proposal_denies_malformed_or_foreign_destination(saved, field, value):
    body = {**proposal_body(saved), field: value}
    response = await post(saved, path(saved), body)
    assert response.status_code == (404 if field == "conversation_id" else 422)
    assert "Public release 088" not in response.text
