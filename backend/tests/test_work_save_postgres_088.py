"""Owner canvas Save through actual IAM, research, publication and audit.

External JWT/JWKS, refresh and source/model replies are signed synthetic fixtures.
The registered router, completed research proof, sessions and all SQL are real.
"""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest

from audit.repository import AuditRepository
from orchestrator.work_api import work_router
from tests.test_work_result_postgres_088 import (
    completed as completed, current, fixture as fixture, gate_orchestrator as gate_orchestrator,
    operation as operation, plane as plane, research as research, signing_key as signing_key,
)
from tests.test_work_write_http_postgres_088 import headers

runtime = plane
pytestmark = [pytest.mark.asyncio,
              pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)]


async def _mounted(op, fixture):
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


@pytest.fixture
async def saved(completed, fixture):
    return await _mounted(completed, fixture)


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


def _head(value):
    with value.op.runtime.transaction() as tx:
        chat = value.op.runtime.repositories.history.conversations.get(tx,
            owner_id=value.op.owner, conversation_id=value.chat_id)
    # Plane's ConversationRecord exposes the chat head pointer as
    # ``publication_id`` (the ``chats.conversation_commit_id`` column).
    return chat.render_revision, chat.publication_id


async def test_registered_proposal_is_review_only_then_exact_save_visible_once(saved):
    command = proposal_body(saved)
    proposed = await post(saved, path(saved), command)
    assert proposed.status_code == 200, proposed.text
    review = proposed.json()
    assert review["version"] == 1 and review["status"] == "review_required"
    assert review["proposal"]["action_id"] == command["submission_id"]
    assert "Public release 088" in proposed.text
    assert "synthetic-provider-key" not in proposed.text and "binding_key" not in proposed.text
    assert _head(saved) == (0, None)
    approval = {"version": 1, "submission_id": str(uuid4()),
                "expected_revision": review["revision"],
                "proposal_digest": review["proposal"]["proposal_digest"]}
    target = path(saved) + "/" + command["submission_id"] + "/save"
    response = await post(saved, target, approval)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "saved" and response.json()["applied"] is True
    assert response.headers["cache-control"] == "no-store"
    assert _head(saved) == (1, command["publication_id"])
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


def _clean(text):
    return all(secret not in text for secret in (
        "synthetic-provider-key", "binding_key", "Read one page.", "payload_binding",
        "synthetic-research-binding-key"))


async def _reviewed(value, command=None):
    command = command or proposal_body(value)
    proposed = await post(value, path(value), command)
    assert proposed.status_code == 200, proposed.text
    assert _clean(proposed.text)
    return command, proposed.json()


def approval_for(review):
    return {"version": 1, "submission_id": str(uuid4()), "expected_revision": review["revision"],
            "proposal_digest": review["proposal"]["proposal_digest"]}


def save_path(value, command):
    return path(value) + "/" + command["submission_id"] + "/save"


def _audit(value):
    rows, _ = value.op.executor.orch.audit_repo.list_for_user(value.op.owner)
    return [row for row in rows if row.action_type.startswith("work.result.")]


async def test_expired_proposal_is_refused_without_publication(saved, monkeypatch):
    from orchestrator import work_publication
    monkeypatch.setattr(work_publication, "PROPOSAL_SECONDS", 2)
    command, review = await _reviewed(saved)
    await asyncio.sleep(2.2)
    response = await post(saved, save_path(saved, command), approval_for(review))
    assert response.status_code == 409 and response.json() == {"error": "work_proposal_expired"}
    assert _head(saved) == (0, None)
    assert not any(row.action_type == "work.result.save" for row in _audit(saved))


async def test_wrong_digest_stale_revision_and_second_approval_are_refused(saved):
    command, review = await _reviewed(saved)
    target = save_path(saved, command)
    wrong = {**approval_for(review), "proposal_digest": "a" * 64}
    response = await post(saved, target, wrong)
    assert response.status_code == 409 and response.json() == {"error": "assignment_publication_conflict"}
    stale = {**approval_for(review), "expected_revision": review["revision"] + 1}
    response = await post(saved, target, stale)
    assert response.status_code == 409 and response.json() == {"error": "assignment_revision_conflict"}
    approval = approval_for(review)
    accepted = await post(saved, target, approval)
    assert accepted.status_code == 200 and accepted.json()["applied"] is True
    assert _clean(accepted.text)
    different = approval_for(review)
    assert different["submission_id"] != approval["submission_id"]
    response = await post(saved, target, different)
    assert response.status_code == 409 and response.json() == {"error": "assignment_publication_conflict"}
    replay = await post(saved, target, approval)
    assert replay.status_code == 200 and replay.json() == {**accepted.json(), "applied": False}
    # A consumed proposal cannot be re-proposed under the same stable identity.
    again = await post(saved, path(saved), command)
    assert again.status_code == 409 and again.json() == {"error": "assignment_idempotency_conflict"}
    rows = _audit(saved)
    assert sum(row.action_type == "work.result.save" for row in rows) == 1
    serialized = json.dumps([row.model_dump(mode="json") for row in rows])
    assert _clean(serialized) and "Public release 088" not in serialized
    assert saved.op.executor.orch.audit_repo.verify_chain(saved.op.owner) is None


async def test_proposal_replay_is_stable_and_a_different_request_conflicts(saved):
    command, review = await _reviewed(saved)
    replayed = await post(saved, path(saved), command)
    assert replayed.status_code == 200, replayed.text
    assert replayed.json() == {**review, "created": False}
    changed = {**command, "publication_id": str(uuid4())}
    response = await post(saved, path(saved), changed)
    assert response.status_code == 409 and response.json() == {"error": "assignment_idempotency_conflict"}
    assert sum(row.action_type == "work.result.propose" for row in _audit(saved)) == 1


@pytest.mark.parametrize("field,value,status,error", [
    ("publication_id", "not-a-uuid-but-thirty-six-chars-long", 422, "work_control_invalid"),
    ("expected_workspace_publication_id", str(uuid4()), 422, "work_control_invalid"),
    ("conversation_id", " padded ", 422, "work_control_invalid"),
    ("expected_workspace_revision", 1, 422, "work_control_invalid"),
    ("expected_revision", 999, 409, "assignment_revision_conflict"),
])
async def test_closed_proposal_shape_and_revision(saved, field, value, status, error):
    body = {**proposal_body(saved), field: value}
    response = await post(saved, path(saved), body)
    assert response.status_code == status and response.json() == {"error": error}


async def test_changed_destination_head_or_foreign_chat_refuses_review(saved):
    stale = {**proposal_body(saved), "expected_workspace_revision": 1,
             "expected_workspace_publication_id": str(uuid4())}
    response = await post(saved, path(saved), stale)
    assert response.status_code == 409 and response.json() == {"error": "assignment_publication_conflict"}
    foreign = str(uuid4())
    with saved.op.runtime.transaction() as tx:
        saved.op.runtime.repositories.history.conversations.create(tx, owner_id="another-owner",
            conversation_id=foreign, title="Not yours", agent_id=None, created_at=1)
    response = await post(saved, path(saved), {**proposal_body(saved), "conversation_id": foreign})
    assert response.status_code == 404 and response.json() == {"error": "work_not_found"}
    assert _audit(saved) == []


async def test_unavailable_result_never_becomes_a_proposal(saved):
    from tests.test_work_result_postgres_088 import _mutate_record
    await _mutate_record(saved.op, lambda data: data["checkpoint"]["research_result"]["passages"][0]
                         .update(text="Private unsupported conclusion"))
    response = await post(saved, path(saved), proposal_body(saved))
    assert response.status_code == 409 and response.json() == {"error": "work_result_unavailable"}
    assert "Private unsupported conclusion" not in response.text
    assert _audit(saved) == []


async def test_unfinished_operation_cannot_be_proposed(research, fixture):
    op = research
    op.completed = await current(op)
    value = await _mounted(op, fixture)
    before = len(fixture[-1])
    response = await post(value, path(value), proposal_body(value))
    assert response.status_code == 409 and response.json() == {"error": "assignment_not_terminal"}
    assert len(fixture[-1]) == before


@pytest.mark.parametrize("action", ["unknown", "model"])
async def test_save_requires_an_existing_publication_proposal(saved, action):
    identity = str(uuid4()) if action == "unknown" else saved.op.model.action_id
    target = path(saved) + "/" + identity + "/save"
    approval = {"version": 1, "submission_id": str(uuid4()),
                "expected_revision": saved.op.completed.state_version, "proposal_digest": "b" * 64}
    response = await post(saved, target, approval)
    assert response.status_code == 404 and response.json() == {"error": "work_not_found"}


@pytest.mark.parametrize("field,value", [("version", True), ("proposal_digest", "Z" * 64),
                                         ("submission_id", "short")])
async def test_malformed_approval_is_closed(saved, field, value):
    command, review = await _reviewed(saved)
    response = await post(saved, save_path(saved, command), {**approval_for(review), field: value})
    assert response.status_code == 422 and response.json() == {"error": "work_control_invalid"}
    assert _head(saved) == (0, None)


async def test_permission_change_between_review_and_save_is_refused(saved):
    orch = saved.op.executor.orch
    await asyncio.to_thread(orch.tool_permissions.set_agent_scopes, saved.op.owner, "web-research-1",
                            {"tools:read": True, "tools:search": True})
    command, review = await _reviewed(saved)
    # The same read-only tool now carries a different (still consented) scope:
    # the reviewed permission digest no longer describes the current policy.
    orch.tool_permissions.register_tool_scopes("web-research-1", {"fetch_page": "tools:search"})
    response = await post(saved, save_path(saved, command), approval_for(review))
    assert response.status_code == 403 and response.json() == {"error": "assignment_scope_changed"}
    assert not any(row.action_type == "work.result.save" for row in _audit(saved))
    assert _head(saved) == (0, None)


async def test_changed_rendering_between_review_and_save_is_refused(saved, monkeypatch):
    from orchestrator import work_publication
    command, review = await _reviewed(saved)
    original = work_publication.result_component
    monkeypatch.setattr(work_publication, "result_component",
                        lambda record, content: {**original(record, content), "variant": "changed"})
    response = await post(saved, save_path(saved, command), approval_for(review))
    assert response.status_code == 409 and response.json() == {"error": "assignment_precondition_changed"}
    assert _head(saved) == (0, None)
    assert not any(row.action_type == "work.result.save" for row in _audit(saved))


async def test_save_rebases_the_existing_canvas_and_supersedes_the_same_result(saved):
    from astralplane.repositories.workspaces import CanvasComponentRecord
    op = saved.op
    with op.runtime.transaction() as tx:
        op.runtime.repositories.workspaces.canvas.create(tx, CanvasComponentRecord(
            str(uuid4()), saved.chat_id, op.owner, "au_prior", {"type": "text", "content": "Prior note"},
            "text", "Prior", 0, 1, 1, None, None))

    def rows():
        with op.runtime.transaction() as tx:
            listed = op.runtime.repositories.workspaces.canvas.list_current(
                tx, owner_id=op.owner, conversation_id=saved.chat_id)
        return [(row.component_id, row.position) for row in listed]

    command, review = await _reviewed(saved)
    identity = review["component"]["component_id"]
    assert review["component"]["position"] == 1 and identity.startswith("au_work_result_")
    accepted = await post(saved, save_path(saved, command), approval_for(review))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["render_revision"] == 1
    assert rows() == [("au_prior", 0), (identity, 1)]
    second = {**proposal_body(saved), "expected_revision": accepted.json()["revision"],
              "expected_workspace_revision": 1,
              "expected_workspace_publication_id": command["publication_id"]}
    command, review = await _reviewed(saved, second)
    assert review["component"]["position"] == 1 and review["proposal"]["base_render_revision"] == 1
    accepted = await post(saved, save_path(saved, command), approval_for(review))
    assert accepted.status_code == 200 and accepted.json()["render_revision"] == 2
    assert rows() == [("au_prior", 0), (identity, 1)]
    assert _head(saved) == (2, command["publication_id"])


async def test_reused_submission_identity_of_another_action_is_a_conflict(saved):
    body = {**proposal_body(saved), "submission_id": saved.op.model.action_id}
    response = await post(saved, path(saved), body)
    assert response.status_code == 409 and response.json() == {"error": "assignment_idempotency_conflict"}
    assert _audit(saved) == []


@pytest.mark.parametrize("failure", ["unavailable", "foreign_incarnation"])
async def test_original_session_refresh_failures_are_closed_refusals(saved, monkeypatch, failure):
    from dataclasses import replace
    from orchestrator import session_authority
    from orchestrator.session_authority import SessionAuthorityUnavailable
    original = session_authority._refresh_operation_session

    async def refresh(**kwargs):
        if failure == "unavailable":
            raise SessionAuthorityUnavailable("session_authority_unavailable")
        result = await original(**kwargs)
        credential = replace(result.observation.credential, incarnation_id=str(uuid4()))
        return replace(result, observation=replace(result.observation, credential=credential))

    monkeypatch.setattr(session_authority, "_refresh_operation_session", refresh)
    response = await post(saved, path(saved), proposal_body(saved))
    expected = (403, "work_authority_unavailable") if failure == "unavailable" else (401, "work_authentication_required")
    assert (response.status_code, response.json()["error"]) == expected
    assert _audit(saved) == [] and _head(saved) == (0, None)


async def test_publication_helpers_refuse_malformed_inputs(saved):
    from dataclasses import replace
    from types import SimpleNamespace as NS
    from orchestrator import work_publication as module
    from orchestrator.session_authority import SessionAuthorityUnavailable
    from orchestrator.work_control_audit import WorkControlAudit
    from persistent_agents.models import AssignmentError
    op = saved.op
    with op.runtime.transaction() as tx:
        read = op.runtime.repositories.assignments.get_operation(tx, owner_id=op.owner,
            assignment_id=op.completed.assignment_id)
        result = module.project_research_result(tx, op.runtime.repositories.assignments,
            owner_id=op.owner, read=read)
    # An empty selection renders an honest card, never invented passages.
    empty = {**result["content"], "passages": [], "disposition": "insufficient_evidence"}
    card = module.result_component(read.assignment, empty)
    assert "Insufficient evidence" in json.dumps(card) and "Public release 088" not in json.dumps(card)
    # Only a completed retained research operation is an original-session context.
    with pytest.raises(SessionAuthorityUnavailable):
        module._completed_context(replace(read, disposition="active"), op.owner, read.assignment.assignment_id)
    # Foreign action shapes never decode into a proposal; receipts are typed.
    assert module._stored_proposal(NS(action_id="x", intent=NS(request={"kind": "result_publication",
        "version": 1, "proposal": {"action_id": "x"}}))) is None
    with pytest.raises(AssignmentError, match="work_control_unavailable"):
        module._receipt(object(), op.owner, read.assignment.assignment_id, str(uuid4()))
    # Audit rows are identifiers only; anything else is refused before insert.
    audit = WorkControlAudit(op.executor.service)
    cases = [dict(command="delete", record=read.assignment, action_id=str(uuid4()),
                  publication_id=str(uuid4()), conversation_id=saved.chat_id),
             dict(command="result.propose", record=read.assignment, action_id=str(uuid4()),
                  publication_id=str(uuid4()), conversation_id=""),
             dict(command="result.propose", record=read.assignment, action_id=str(uuid4()),
                  publication_id=str(uuid4()), conversation_id=saved.chat_id, submission_id=str(uuid4())),
             dict(command="result.save", record=read.assignment, action_id=str(uuid4()),
                  publication_id=str(uuid4()), conversation_id=saved.chat_id, submission_id=None)]
    for case in cases:
        with op.runtime.transaction() as tx, pytest.raises(AssignmentError, match="work_control_unavailable"):
            audit.append_publication(tx, owner_id=op.owner, **case)
    assert _audit(saved) == []
