"""Tests for persistent_agents/approvals.py: linking approvals to assignments without
premature dispatch, replay safety, digest/executor mismatch refusal, and
remote-decision routing between assignment and chat.
"""

from types import SimpleNamespace
from unittest.mock import Mock



def test_persistent_conditional_confirmation_never_stats_remote_target(monkeypatch):
    from orchestrator import remote_confirmation as rc
    monkeypatch.setattr("persistent_agents.dispatch_context.current_dispatch", lambda: object())
    monkeypatch.setattr(rc, "_is_destructive", Mock(side_effect=AssertionError("unreserved stat")))
    monkeypatch.setattr(rc, "_consume_if_valid", lambda *args: True)
    monkeypatch.setattr(rc, "_audit_sync", Mock())
    socket = object()
    orch = SimpleNamespace(ui_sessions={socket: {"sub": "owner"}})
    arguments = {"machine_id": "m", "remote_path": "/x", rc._MARKER: "p"}
    assert rc.evaluate(orch, socket, "remote-compute-1", "upload_file", arguments, "chat", "owner") is None
    assert rc._MARKER not in arguments


def test_real_confirmation_creation_links_in_the_creation_transaction(monkeypatch):
    from orchestrator import remote_confirmation as rc
    from tests.test_remote_confirmation_063 import _FakeDB, _orch
    orch = _orch(_FakeDB())
    policy = SimpleNamespace(summary=lambda *args: "Remove reviewed path.", machine_key="machine_id",
        machine_id=None, card_title="Review operation", card_caption="Approve exact parameters", card_as_result=False)
    monkeypatch.setattr(rc, "policy_for", lambda agent: policy)
    monkeypatch.setattr(rc, "_audit_sync", Mock())
    calls = []
    def link(transaction, proposal_id):
        row = orch.runtime_composition.plane.repositories.remote_operation_proposals.get(
            transaction, owner_id="owner", proposal_id=proposal_id)
        assert row is not None and row.status == "pending"
        assert row.arguments == {"machine_id": "m", "remote_path": "/x"}
        calls.append(proposal_id)
    proposal_id, card = rc._create_proposal(orch, "owner", "chat-1", "remote-compute-1", "remove_path",
        {"machine_id": "m", "remote_path": "/x"}, on_created=link)
    assert calls == [proposal_id] and card["type"] == "card"


