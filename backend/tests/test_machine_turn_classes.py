"""T025/T026 (056-delegated-agent-chaining): all machine-turn classes inherit
the ONE shared authority seam (FR-012).

Parser replay (``attachment_autoparse.auto_continue_after_go_live``) and draft
self-tests (``agentic_creation._self_test_draft``) derive their root through
``orch.derive_machine_authority`` — the same call the scheduler makes — and bind
it to their virtual socket, so a real-agent tool inside those turns dispatches
delegated in production instead of being refused for having no session token.
"""
from __future__ import annotations

import os
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.chain_authority import AuthoritySkip, MachineAuthority  # noqa: E402


def _authority(turn_class):
    return MachineAuthority(
        access_token=f"tok-{turn_class}", allowed_scopes=["tools:read"],
        principal=f"machine:{turn_class}", user_id="u1",
        consent_ref="grant-1", turn_class=turn_class)


class _Orch:
    """Records the seam calls the machine-turn classes make."""

    def __init__(self, authority=None):
        self._authority = authority
        self.derived = []
        self.bound = []
        self.unbound = []
        self.turns = []
        self.history = MagicMock()

    async def derive_machine_authority(self, **kw):
        self.derived.append(kw)
        return self._authority if self._authority is not None \
            else AuthoritySkip("missing_consent")

    def _bind_machine_turn(self, vws, authority):
        self.bound.append((vws, authority))

    def _unbind_machine_turn(self, vws):
        self.unbound.append(vws)

    async def handle_chat_message(self, vws, message, chat_id, **kw):
        self.turns.append({"message": message, "chat_id": chat_id, **kw})


# --------------------------------------------------------------------------- #
# Parser replay (T025)
# --------------------------------------------------------------------------- #

def _autoparse_orch(authority=None):
    from orchestrator import attachment_autoparse  # noqa: F401

    orch = _Orch(authority)
    attachment = SimpleNamespace(filename="data.xyz", category="data")
    plane = SimpleNamespace(
        runtime=SimpleNamespace(transaction=lambda: nullcontext(object())),
        repositories=SimpleNamespace(
            artifacts=SimpleNamespace(
                message_attachments=SimpleNamespace(
                    list_for_conversation=lambda *a, **k: (
                        SimpleNamespace(
                            attachment_id="att-123456",
                            message_id="1",
                        ),
                    )
                ),
                attachments=SimpleNamespace(get=lambda *a, **k: attachment),
            ),
            history=SimpleNamespace(
                messages=SimpleNamespace(
                    get=lambda *a, **k: SimpleNamespace(
                        content="read my file please"
                    )
                )
            ),
        ),
    )
    orch.runtime_composition = SimpleNamespace(plane=plane)
    return orch


@pytest.mark.asyncio
async def test_parser_replay_authority(monkeypatch):
    from orchestrator import attachment_autoparse

    orch = _autoparse_orch(_authority("parser_replay"))
    repo = MagicMock()
    repo.get_by_id = MagicMock(
        return_value=MagicMock(filename="data.xyz", category="data"))
    monkeypatch.setattr(
        "orchestrator.attachments.repository.AttachmentRepository",
        MagicMock(return_value=repo))

    ok = await attachment_autoparse.auto_continue_after_go_live(
        orch, requested_by="u1", source_chat_id="c1",
        source_attachment_id="att-123456", extension="xyz", category="data")

    assert ok is True
    assert orch.derived == [{"user_id": "u1", "agent_id": None,
                             "turn_class": "parser_replay"}]
    assert len(orch.bound) == 1
    assert orch.bound[0][1].principal == "machine:parser_replay"
    assert len(orch.unbound) == 1  # always released
    assert orch.turns[0]["message"] == "read my file please"


@pytest.mark.asyncio
async def test_parser_replay_without_consent_still_runs(monkeypatch):
    """An AuthoritySkip is not fatal for the replay — it simply runs unbound,
    and production then refuses its real-agent dispatches as it does today."""
    from orchestrator import attachment_autoparse

    orch = _autoparse_orch(None)  # derive → AuthoritySkip
    repo = MagicMock()
    repo.get_by_id = MagicMock(
        return_value=MagicMock(filename="data.xyz", category="data"))
    monkeypatch.setattr(
        "orchestrator.attachments.repository.AttachmentRepository",
        MagicMock(return_value=repo))

    ok = await attachment_autoparse.auto_continue_after_go_live(
        orch, requested_by="u1", source_chat_id="c1",
        source_attachment_id="att-123456", extension="xyz", category="data")

    assert ok is True
    assert not orch.bound          # nothing bound without consent
    assert orch.turns              # but the replay still happened


# --------------------------------------------------------------------------- #
# Draft self-test (T026)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_self_test_authority():
    from orchestrator import agentic_creation

    orch = _Orch(_authority("draft_self_test"))
    orch.history.create_chat = MagicMock(return_value="test-chat-1")

    verdict = await agentic_creation._self_test_draft(
        orch, {"id": "draft-abc123"}, "do the thing", "u1")

    assert orch.derived == [{"user_id": "u1", "agent_id": None,
                             "turn_class": "draft_self_test"}]
    assert len(orch.bound) == 1
    assert orch.bound[0][1].principal == "machine:draft_self_test"
    assert len(orch.unbound) == 1
    assert orch.turns[0]["draft_agent_id"] == "draft-abc123"
    assert isinstance(verdict, dict)


@pytest.mark.asyncio
async def test_self_test_unbinds_even_on_crash():
    from orchestrator import agentic_creation

    orch = _Orch(_authority("draft_self_test"))
    orch.history.create_chat = MagicMock(return_value="test-chat-1")

    async def _boom(*a, **k):
        raise RuntimeError("tool exploded")

    orch.handle_chat_message = _boom
    verdict = await agentic_creation._self_test_draft(
        orch, {"id": "draft-abc123"}, "do the thing", "u1")
    assert verdict["status"] == "failed"
    assert len(orch.unbound) == 1  # released despite the crash


# --------------------------------------------------------------------------- #
# One seam (FR-012)
# --------------------------------------------------------------------------- #

def test_all_three_classes_use_the_same_derivation():
    """The three machine-turn classes must not drift apart: each names its
    class to the SAME orchestrator seam, which is the only place authority is
    derived."""
    import inspect

    from orchestrator import agentic_creation, attachment_autoparse
    from scheduler import runner

    autoparse_src = inspect.getsource(attachment_autoparse)
    creation_src = inspect.getsource(agentic_creation._self_test_draft)
    runner_src = inspect.getsource(runner.JobRunner.run_job)

    assert 'turn_class="parser_replay"' in autoparse_src
    assert 'turn_class="draft_self_test"' in creation_src
    assert 'turn_class="scheduled_job"' in runner_src
    # None of them mints or intersects on its own.
    for src in (autoparse_src, creation_src):
        assert "mint_access_token" not in src
        assert "_intersect_scopes" not in src
