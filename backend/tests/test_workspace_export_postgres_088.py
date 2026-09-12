"""Portable presentation HTTP reads the real isolated owner/revision repository."""
import asyncio
from types import SimpleNamespace

import pytest

from orchestrator import workspace_export as export
from persistent_agents.tests.test_engine_postgres import plane as plane
from tests.test_workspace_export_088 import assert_error, host as host, post


@pytest.fixture
def persisted(host, plane):
    conversations = plane.repositories.history.conversations
    with plane.transaction() as tx:
        for owner, chat in (("owner", "chat"), ("other", "foreign")):
            conversations.create(tx, conversation_id=chat, owner_id=owner, title="Committed A",
                                 agent_id=None, created_at=1)
    host.orch.plane_repository_source = SimpleNamespace(plane_runtime=plane, plane_repositories=plane.repositories)
    return host, plane


def records(plane):
    with plane.transaction() as tx:
        return tuple(plane.repositories.history.conversations.get(tx, owner_id=owner, conversation_id=chat)
                     for owner, chat in (("owner", "chat"), ("other", "foreign")))


@pytest.mark.asyncio
async def test_guarded_http_owner_revision_read_does_not_replace_committed_data(persisted):
    host, plane = persisted
    before = await asyncio.to_thread(records, plane)
    response = await post(host, query="render_revision=0")
    assert response.status_code == 200, response.text
    assert "Visible transient B" in response.json()["html"]
    assert "Committed A" not in response.json()["html"]
    assert response.headers["x-astral-render-revision"] == "0"
    assert await asyncio.to_thread(records, plane) == before
    assert_error(await post(host, query="render_revision=1"), 409, "presentation_revision_changed")
    assert_error(await post(host, query="render_revision=0", path="/api/export/canvas/foreign/presentation"),
                 404, "presentation_not_found")
    host.orch._save_user_profile.assert_not_called()


@pytest.mark.asyncio
async def test_guarded_http_deleted_during_render_cannot_deliver_capture(persisted, monkeypatch):
    host, plane = persisted
    original = export.render_presentation
    def render_and_delete(payload):
        result = original(payload)
        with plane.transaction() as tx:
            assert plane.repositories.history.conversations.delete(tx, owner_id="owner", conversation_id="chat")
        return result
    monkeypatch.setattr(export, "render_presentation", render_and_delete)
    assert_error(await post(host, query="render_revision=0"), 404, "presentation_not_found")
    owner, foreign = await asyncio.to_thread(records, plane)
    assert owner is None and foreign.title == "Committed A"
