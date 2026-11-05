"""Tests for the Mac checkpoint verbs: policy registration, the server-side checkpoint
registry and restore whitelist, checkpoint reporting on mutating tool results, turn
linkage, the restore review card, and the audit trail.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from types import SimpleNamespace

import pytest

from agents.computer_use import mcp_tools
from orchestrator import computer_checkpoints as cc
from orchestrator import computer_use_policy as policy
from orchestrator import remote_confirmation as rc
from orchestrator import restore_review
from shared.protocol import COMPUTER_HOST_VERBS, ComputerHostDescriptor
from tests.test_computer_use_076 import OWNER, OTHER, _AgentOrch, _descriptor, _FakeHostSocket, _FakeOrch, _WS
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401

CHAT = "chat-1"
CTX = {"user_id": OWNER, "session_id": CHAT}
ROOT = "/Users/sam/Projects/x"
OTHER_ROOT = "/Users/sam/Elsewhere"
CHECKPOINT_ID = str(uuid.uuid4())
TREE_HASH = hashlib.sha256(b"tree").hexdigest()


class _FakeRecorder:
    def __init__(self):
        self.events = []

    def record_blocking(self, ev):
        self.events.append(ev)
        return ev

    async def record(self, ev):
        self.events.append(ev)
        return ev


@pytest.fixture
def audit_rec(monkeypatch):
    rec = _FakeRecorder()
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: rec)
    return rec


def _record(**over):
    from orchestrator.computer_checkpoints import CheckpointRecord

    fields = dict(checkpoint_id=CHECKPOINT_ID, owner=OWNER, chat=CHAT, host_id="host-1", verb="write_file",
                  root="x", root_digest=cc.root_digest(ROOT), tree_hash=TREE_HASH, file_count=3,
                  turn_id="41", created_at=123.0)
    fields.update(over)
    return CheckpointRecord(**fields)


def test_checkpoint_verbs_join_the_protocol_policy_and_registry_sets():
    for verb in ("list_checkpoints", "diff_checkpoint", "restore_checkpoint"):
        assert verb in COMPUTER_HOST_VERBS
        assert verb in policy.HOST_VERBS
        assert verb in policy.SESSION_REQUIRED
        assert verb not in policy.UNATTENDED_ALLOWED
    assert policy.HOST_VERBS == COMPUTER_HOST_VERBS
    assert set(mcp_tools.TOOL_REGISTRY) == policy.ALL_VERBS
    reg = mcp_tools.TOOL_REGISTRY
    assert (reg["list_checkpoints"]["scope"], reg["list_checkpoints"]["tier"]) == ("tools:read", "observe")
    assert (reg["diff_checkpoint"]["scope"], reg["diff_checkpoint"]["tier"]) == ("tools:read", "observe")
    assert (reg["restore_checkpoint"]["scope"], reg["restore_checkpoint"]["tier"],
            reg["restore_checkpoint"]["destructive"]) == ("tools:files", "consequential", "always")
    for name, entry in reg.items():
        assert (entry["scope"], entry["tier"], entry["timeout"]) == (
            policy.SCOPES[name], policy.TIERS[name], policy.TIMEOUTS[name])


def test_restore_never_runs_on_standing_bypass_and_is_a_policy_floor():
    assert policy.is_destructive("restore_checkpoint", {"checkpoint_id": CHECKPOINT_ID}) is True
    assert policy.bypass_floor("restore_checkpoint", {}, None) is True
    assert policy.bypass_floor("restore_checkpoint", {}, "com.apple.Terminal") is True
    assert policy.is_destructive("list_checkpoints", {}) is False
    assert policy.is_destructive("diff_checkpoint", {"checkpoint_id": CHECKPOINT_ID}) is False


def test_host_report_and_reason_parsing_is_strict():
    report = {"id": CHECKPOINT_ID, "root": "x", "root_digest": cc.root_digest(ROOT),
              "tree_hash": TREE_HASH, "file_count": 3}
    assert cc.parse_report(report) == report
    assert cc.parse_report({**report, "id": "not-a-uuid"}) is None
    assert cc.parse_report({**report, "tree_hash": "abc"}) is None
    assert cc.parse_report({**report, "file_count": -1}) is None
    assert cc.parse_report({**report, "file_count": True}) is None
    assert cc.parse_report({**report, "root": ""}) is None
    assert cc.parse_report("checkpoint") is None
    assert cc.parse_reason("over_cap") == "over_cap"
    assert cc.parse_reason("root_unavailable") == "root_unavailable"
    assert cc.parse_reason("because") is None
    assert cc.valid_id(CHECKPOINT_ID) and not cc.valid_id(CHECKPOINT_ID.upper())
    assert cc.root_digest("/abs") is not None and cc.root_digest("relative") is None
    assert cc.root_digest(None) is None


def test_registry_is_bounded_and_records_checkpoint_ids_per_turn():
    registry = cc.CheckpointRegistry()
    for index in range(cc.MAX_RECORDS + 4):
        registry.note_created(_record(checkpoint_id=str(uuid.uuid4()), turn_id=str(index)))
    assert len(registry._records) == cc.MAX_RECORDS
    turn_id, ids = registry.turn_checkpoint_ids(OWNER, CHAT)
    assert turn_id == str(cc.MAX_RECORDS + 3) and len(ids) == 1
    second = str(uuid.uuid4())
    registry.note_created(_record(checkpoint_id=CHECKPOINT_ID, turn_id="41"))
    registry.note_created(_record(checkpoint_id=second, turn_id="41"))
    turn_id, ids = registry.turn_checkpoint_ids(OWNER, CHAT)
    assert turn_id == "41" and ids == [CHECKPOINT_ID, second]
    registry.note_created(_record(checkpoint_id=str(uuid.uuid4()), turn_id="42"))
    turn_id, ids = registry.turn_checkpoint_ids(OWNER, CHAT)
    assert turn_id == "42" and len(ids) == 1
    assert registry.turn_checkpoint_ids(OTHER, CHAT) == (None, [])


def test_registry_turn_map_also_stays_bounded():
    registry = cc.CheckpointRegistry()
    for index in range(cc.MAX_TURNS + 2):
        registry.note_created(_record(checkpoint_id=str(uuid.uuid4()), turn_id=str(index),
                                      owner=f"owner-{index}", chat=f"chat-{index}"))
    assert len(registry._turns) == cc.MAX_TURNS


def test_review_ids_come_from_the_current_approval_context():
    import contextlib

    from orchestrator.hitl_confirmation import _APPROVAL, Approval

    assert cc._review_ids() == (None, None)
    call = SimpleNamespace(remote_proposal="prop-1", request_id="req-9")
    token = _APPROVAL.set(Approval(object(), call))
    try:
        assert cc._review_ids() == ("prop-1", "req-9")
    finally:
        with contextlib.suppress(ValueError):
            _APPROVAL.reset(token)
    call = SimpleNamespace(remote_proposal=None, request_id="")
    token = _APPROVAL.set(Approval(object(), call))
    try:
        assert cc._review_ids() == (None, None)
    finally:
        with contextlib.suppress(ValueError):
            _APPROVAL.reset(token)


def test_audit_event_failures_never_break_the_tool(monkeypatch):
    class _Orch:
        audit_recorder = None

    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
    cc._record_event(_Orch(), OWNER, "checkpoint.created", "d", CHAT, "c", {}, "success")

    class _Boom:
        def record_blocking(self, ev):
            raise RuntimeError("down")

    monkeypatch.setattr("audit.recorder.get_recorder", lambda: _Boom())
    cc._record_event(_Orch(), OWNER, "checkpoint.created", "d", CHAT, "c", {}, "success")

    class _Good:
        def __init__(self):
            self.events = []

        def record_blocking(self, ev):
            self.events.append(ev)
            return ev

    good = _Good()
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: good)
    cc._record_event(_Orch(), OWNER, "checkpoint.created", "d", CHAT, "c", {}, "success")
    assert len(good.events) == 1 and good.events[0].conversation_id == CHAT


def test_granted_root_failures_map_to_precise_refusals(monkeypatch):
    from orchestrator.computer_hosts import ComputerHostError

    class _Orch:
        pass

    orch = _Orch()
    host = SimpleNamespace(host_id="h")
    cc.registry_for(orch).note_created(_record(host_id="h"))

    def unavailable(*_args, **_kwargs):
        raise ComputerHostError("project_authority_unavailable", "reconnect")

    monkeypatch.setattr("orchestrator.project_authority.project_roots", unavailable)
    with pytest.raises(cc.CheckpointRefusal) as exc:
        cc.restore_check(orch, OWNER, CHAT, host, CHECKPOINT_ID)
    assert exc.value.code == "project_authority_unavailable"

    def broken(*_args, **_kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr("orchestrator.project_authority.project_roots", broken)
    with pytest.raises(cc.CheckpointRefusal) as exc:
        cc.restore_check(orch, OWNER, CHAT, host, CHECKPOINT_ID)
    assert exc.value.code == "checkpoint_root_unknown"

    monkeypatch.setattr("orchestrator.project_authority.project_roots", lambda *a, **k: None)
    with pytest.raises(cc.CheckpointRefusal) as exc:
        cc.restore_check(orch, OWNER, CHAT, host, CHECKPOINT_ID)
    assert exc.value.code == "checkpoint_root_not_granted"


def test_registry_for_replaces_a_foreign_attribute():
    class _Orch:
        _computer_checkpoints = "stale"

    orch = _Orch()
    registry = cc.registry_for(orch)
    assert isinstance(registry, cc.CheckpointRegistry) and orch._computer_checkpoints is registry


def test_turn_stamp_tracks_the_current_turn_and_stays_bounded():
    class _Orch:
        pass

    orch = _Orch()
    cc.note_turn(orch, CHAT, 41)
    assert cc.current_turn_id(orch, CHAT) == "41"
    cc.note_turn(orch, CHAT, 42)
    assert cc.current_turn_id(orch, CHAT) == "42"
    cc.note_turn(orch, CHAT, None)
    assert cc.current_turn_id(orch, CHAT) is None
    cc.note_turn(orch, None, 7)
    for index in range(cc.MAX_TURNS + 2):
        cc.note_turn(orch, f"chat-{index}", index)
    turns = getattr(orch, "_chat_turn_message_ids")
    assert len(turns) == cc.MAX_TURNS and "chat-0" not in turns and f"chat-{cc.MAX_TURNS + 1}" in turns


@pytest.fixture
async def world(monkeypatch):
    from orchestrator import terminal_host_review

    replies = {}

    def answers(verb, args):
        reply = replies.get(verb)
        if callable(reply):
            return reply(args)
        return reply if reply is not None else {"ok": True, "result": {}}

    orch = _AgentOrch()
    mcp_tools.register_deps(orch)
    monkeypatch.setattr(terminal_host_review, "computer_use_review_current", lambda current: current is orch)
    host_ws = orch.add(_FakeHostSocket(orch, answers))
    host, _ = orch.computer_hosts.register(OWNER, host_ws, ComputerHostDescriptor.from_dict(_descriptor(
        platform="macos", name="Mac", workspace_roots=[{"name": "x", "path": ROOT}])))
    orch.add(_WS(chat=CHAT))
    task = asyncio.create_task(asyncio.to_thread(mcp_tools.start_session, **CTX))
    for _ in range(400):
        await asyncio.sleep(0.005)
        live = orch.computer_sessions.live_for_host(OWNER, host.host_id)
        if live is not None:
            await orch.computer_sessions.on_host_event(OWNER, host.host_id, "heartbeat", live.session_id, None)
            break
    await task
    session = orch.computer_sessions.live_for_host(OWNER, host.host_id)
    yield _World(orch, host, host_ws, session, replies)
    await orch.computer_sessions.end_all("user_stop")


class _World:
    def __init__(self, orch, host, host_ws, session, replies):
        self.orch, self.host, self.host_ws, self.session, self.replies = orch, host, host_ws, session, replies

    async def call(self, tool, **kwargs):
        return await asyncio.to_thread(getattr(mcp_tools, tool), **CTX, **kwargs)

    def requests(self, verb=None):
        return [f for f in self.host_ws.frames("computer_request") if verb is None or f["verb"] == verb]

    def note(self, **over):
        registry = cc.registry_for(self.orch)
        registry.note_created(_record(host_id=self.host.host_id, **over))


async def test_a_host_that_does_not_advertise_the_verbs_never_receives_them(monkeypatch):
    from orchestrator import terminal_host_review

    orch = _AgentOrch()
    mcp_tools.register_deps(orch)
    monkeypatch.setattr(terminal_host_review, "computer_use_review_current", lambda current: current is orch)
    verbs = sorted(COMPUTER_HOST_VERBS - {"list_checkpoints", "diff_checkpoint", "restore_checkpoint"})
    host_ws = orch.add(_FakeHostSocket(orch, lambda verb, args: {"ok": True, "result": {}}))
    host, _ = orch.computer_hosts.register(OWNER, host_ws, ComputerHostDescriptor.from_dict(_descriptor(
        platform="macos", name="Mac", workspace_roots=[{"name": "x", "path": ROOT}], verbs=tuple(verbs))))
    orch.add(_WS(chat=CHAT))
    task = asyncio.create_task(asyncio.to_thread(mcp_tools.start_session, **CTX))
    for _ in range(400):
        await asyncio.sleep(0.005)
        live = orch.computer_sessions.live_for_host(OWNER, host.host_id)
        if live is not None:
            await orch.computer_sessions.on_host_event(OWNER, host.host_id, "heartbeat", live.session_id, None)
            break
    await task
    for tool, kwargs in (("list_checkpoints", {}), ("diff_checkpoint", {"checkpoint_id": CHECKPOINT_ID})):
        out = await asyncio.to_thread(getattr(mcp_tools, tool), **CTX, **kwargs)
        assert out["_data"]["code"] == "unsupported", tool
    out = await asyncio.to_thread(mcp_tools.restore_checkpoint, **CTX, checkpoint_id=CHECKPOINT_ID)
    assert out["_data"]["code"] == "unknown_checkpoint"
    sent = {frame["verb"] for frame in host_ws.frames("computer_request")}
    assert sent.isdisjoint({"list_checkpoints", "diff_checkpoint", "restore_checkpoint"})


async def test_write_file_reports_the_checkpoint_and_records_it(world, audit_rec):
    world.replies["write_file"] = {"ok": True, "result": {
        "bytes": 5, "checkpoint": {"id": CHECKPOINT_ID, "root": "x", "root_digest": cc.root_digest(ROOT),
                                   "tree_hash": TREE_HASH, "file_count": 3}}}
    cc.note_turn(world.orch, CHAT, 41)
    out = await world.call("write_file", path=f"{ROOT}/notes.txt", content="hello")
    assert out["_data"]["checkpoint"] == {"id": CHECKPOINT_ID, "root": "x", "tree_hash": TREE_HASH,
                                          "file_count": 3}
    record = cc.registry_for(world.orch).lookup(CHECKPOINT_ID)
    assert record is not None and record.owner == OWNER and record.host_id == world.host.host_id
    assert record.turn_id == "41" and record.root_digest == cc.root_digest(ROOT)
    created = [ev for ev in audit_rec.events if ev.action_type == "checkpoint.created"]
    assert len(created) == 1
    meta = created[0].inputs_meta
    assert meta["checkpoint_id"] == CHECKPOINT_ID and meta["turn_id"] == "41"
    assert meta["root_digest"] == cc.root_digest(ROOT) and meta["tree_hash"] == TREE_HASH
    assert meta["file_count"] == 3 and created[0].conversation_id == CHAT
    turn_id, ids = cc.turn_checkpoint_ids(world.orch, OWNER, CHAT)
    assert turn_id == "41" and ids == [CHECKPOINT_ID]


async def test_write_file_reports_why_no_checkpoint_was_taken(world, audit_rec):
    world.replies["write_file"] = {"ok": True, "result": {"bytes": 5, "checkpoint": None,
                                                          "checkpoint_reason": "over_cap"}}
    out = await world.call("write_file", path=f"{ROOT}/notes.txt", content="hello")
    assert out["_data"]["checkpoint_none"] == "over_cap"
    assert "checkpoint" not in out["_data"]
    assert cc.registry_for(world.orch).lookup(CHECKPOINT_ID) is None
    assert [ev for ev in audit_rec.events if ev.action_type == "checkpoint.created"] == []
    world.replies["delete_path"] = {"ok": True, "result": {"deleted": True, "checkpoint": None,
                                                           "checkpoint_reason": "root_unavailable"}}
    out = await world.call("delete_path", path=f"{ROOT}/notes.txt")
    assert out["_data"]["checkpoint_none"] == "root_unavailable" and out["_data"]["deleted"] is True


async def test_delete_path_reports_the_checkpoint_too(world, audit_rec):
    world.replies["delete_path"] = {"ok": True, "result": {
        "deleted": True, "checkpoint": {"id": CHECKPOINT_ID, "root": "x", "root_digest": cc.root_digest(ROOT),
                                        "tree_hash": TREE_HASH, "file_count": 2}}}
    out = await world.call("delete_path", path=f"{ROOT}/notes.txt")
    assert out["_data"]["checkpoint"]["id"] == CHECKPOINT_ID
    assert cc.registry_for(world.orch).lookup(CHECKPOINT_ID) is not None


async def test_malformed_host_checkpoint_reports_are_ignored(world, audit_rec):
    world.replies["write_file"] = {"ok": True, "result": {"bytes": 5, "checkpoint": {"id": "junk"}}}
    out = await world.call("write_file", path=f"{ROOT}/notes.txt", content="hello")
    assert "checkpoint" not in out["_data"] and "checkpoint_none" not in out["_data"]
    assert audit_rec.events == []


async def test_list_checkpoints_and_diff_checkpoint_reach_the_host(world):
    world.replies["list_checkpoints"] = {"ok": True, "result": {"checkpoints": [
        {"id": CHECKPOINT_ID, "root": "x", "root_digest": cc.root_digest(ROOT), "tree_hash": TREE_HASH,
         "file_count": 3}, {"id": "junk"}, "junk"]}}
    out = await world.call("list_checkpoints")
    assert out["_data"]["checkpoints"] == [{"id": CHECKPOINT_ID, "root": "x", "tree_hash": TREE_HASH,
                                            "file_count": 3}]
    world.replies["diff_checkpoint"] = {"ok": True, "result": {
        "root": "x", "tree_hash": TREE_HASH, "file_count": 3, "would_overwrite": 2, "would_delete": 1}}
    out = await world.call("diff_checkpoint", checkpoint_id=CHECKPOINT_ID)
    assert out["_data"] == {"computer": "Mac", "checkpoint_id": CHECKPOINT_ID, "root": "x",
                            "tree_hash": TREE_HASH, "file_count": 3, "would_overwrite": 2, "would_delete": 1}
    assert world.requests("diff_checkpoint")[0]["args"] == {"checkpoint_id": CHECKPOINT_ID}
    before = len(world.requests("diff_checkpoint"))
    bad = await world.call("diff_checkpoint", checkpoint_id="nope")
    assert bad["_data"]["code"] == "out_of_range"
    assert len(world.requests("diff_checkpoint")) == before


async def test_restore_refuses_unknown_foreign_or_ungranted_checkpoints_before_dispatch(world, audit_rec):
    out = await world.call("restore_checkpoint", checkpoint_id=CHECKPOINT_ID)
    assert out["_data"]["code"] == "unknown_checkpoint"
    out = await world.call("restore_checkpoint", checkpoint_id="not-a-uuid")
    assert out["_data"]["code"] == "out_of_range"
    assert world.requests("restore_checkpoint") == []
    cc.registry_for(world.orch).note_created(_record(host_id=world.host.host_id, owner=OTHER))
    out = await world.call("restore_checkpoint", checkpoint_id=CHECKPOINT_ID)
    assert out["_data"]["code"] == "unknown_checkpoint"
    cc.registry_for(world.orch).note_created(_record(host_id="another-host"))
    out = await world.call("restore_checkpoint", checkpoint_id=CHECKPOINT_ID)
    assert out["_data"]["code"] == "unknown_checkpoint"
    cc.registry_for(world.orch).note_created(_record(host_id=world.host.host_id,
                                                     root_digest=cc.root_digest(OTHER_ROOT), root="elsewhere"))
    out = await world.call("restore_checkpoint", checkpoint_id=CHECKPOINT_ID)
    assert out["_data"]["code"] == "checkpoint_root_not_granted"
    assert world.requests("restore_checkpoint") == []
    assert [ev.action_type for ev in audit_rec.events if ev.action_type == "checkpoint.restored"] == []


async def test_restore_dispatches_reports_and_audits(world, audit_rec):
    world.note()
    world.replies["restore_checkpoint"] = {"ok": True, "result": {"restored": 2, "skipped": 1}}
    out = await world.call("restore_checkpoint", checkpoint_id=CHECKPOINT_ID)
    assert out["_data"] == {"computer": "Mac", "checkpoint_id": CHECKPOINT_ID, "root": "x",
                            "restored": 2, "skipped": 1}
    assert world.requests("restore_checkpoint")[0]["args"] == {"checkpoint_id": CHECKPOINT_ID}
    events = [ev for ev in audit_rec.events if ev.action_type == "checkpoint.restored"]
    assert len(events) == 1
    assert events[0].inputs_meta["restored"] == 2 and events[0].inputs_meta["skipped"] == 1
    assert events[0].inputs_meta["checkpoint_id"] == CHECKPOINT_ID
    assert events[0].correlation_id == str(uuid.uuid5(uuid.NAMESPACE_URL, f"computer-checkpoint:{CHECKPOINT_ID}"))


async def test_a_partial_restore_surfaces_as_uncertain_and_is_not_retried(world, audit_rec):
    world.note()
    world.replies["restore_checkpoint"] = {"ok": True, "result": {"restored": 1, "skipped": 0,
                                                                  "complete": False}}
    out = await world.call("restore_checkpoint", checkpoint_id=CHECKPOINT_ID)
    assert out["_data"]["code"] == "outcome_uncertain"
    assert "partially" in out["_data"]["message"] and "do not retry" in out["_data"]["message"]
    assert out["_ui_components"][0]["variant"] == "error"
    assert len(world.requests("restore_checkpoint")) == 1
    events = [ev for ev in audit_rec.events if ev.action_type == "checkpoint.restored"]
    assert len(events) == 1 and events[0].outcome == "interrupted"
    assert events[0].inputs_meta["restored"] == 1


def test_restore_review_card_names_folder_turn_and_counts():
    record = _record()
    lines = restore_review._review_lines(record, {"would_overwrite": 2, "would_delete": 1})
    assert lines == ["Folder: x", "Turn: 41",
                     "Would overwrite 2 file(s) and delete 1 file(s) created since the checkpoint."]
    assert restore_review._review_lines(record, None)[2].startswith("The Mac did not report")


async def test_attach_restore_review_inserts_the_preview_into_the_card():
    card = {"type": "card", "id": "au_approval_1", "title": "Confirm an action on Mac",
            "content": [{"type": "text", "content": "Restore files on Mac to checkpoint x"},
                        {"type": "button", "label": "Approve", "action": "remote_op_decision",
                         "payload": {"decision": "approve", "proposal_id": "p-1"}}]}
    state = {"record": _record(), "counts": {"would_overwrite": 2, "would_delete": 1}}
    out = await restore_review.attach_restore_review(None, None, OWNER, CHAT,
                                                     {"checkpoint_id": CHECKPOINT_ID}, [card], snapshot=state)
    assert out[0] is not card and out[0]["content"][1]["type"] == "text"
    assert "Folder: x" in out[0]["content"][1]["content"] and "Turn: 41" in out[0]["content"][1]["content"]
    assert "Would overwrite 2 file(s)" in out[0]["content"][1]["content"]
    assert out[0]["content"][0] == card["content"][0] and out[0]["content"][2] == card["content"][1]
    missing = await restore_review.attach_restore_review(None, None, OWNER, CHAT,
                                                         {"checkpoint_id": CHECKPOINT_ID},
                                                         [{"type": "alert", "message": "x"}], snapshot=state)
    assert missing == [{"type": "alert", "message": "x"}]
    nostate = await restore_review.attach_restore_review(None, None, OWNER, CHAT,
                                                         {"checkpoint_id": CHECKPOINT_ID}, [card], snapshot=None)
    assert nostate == [card]


@pytest.mark.asyncio
async def test_note_helpers_ignore_non_dict_host_results(world, audit_rec):
    world.note()
    assert cc.note_host_checkpoint(world.orch, OWNER, CHAT, world.host, "write_file", "junk", None) is None
    cc.note_restored(world.orch, OWNER, CHAT, world.host,
                     cc.registry_for(world.orch).lookup(CHECKPOINT_ID), "junk")
    assert audit_rec.events == []


@pytest.mark.asyncio
async def test_prepare_restore_review_skips_invalid_or_already_approved_restores(world, audit_rec):
    from orchestrator.hitl_confirmation import approved_call

    out = await restore_review.prepare_restore_review(world.orch, None, OWNER, CHAT,
                                                      {"checkpoint_id": "nope"})
    assert out is None
    out = await restore_review.prepare_restore_review(world.orch, None, OWNER, CHAT,
                                                      {"checkpoint_id": CHECKPOINT_ID})
    assert out is None
    world.note()
    assert approved_call(world.orch, OWNER, CHAT, "computer-use-1", "restore_checkpoint") is False
    out = await restore_review.prepare_restore_review(world.orch, None, OWNER, CHAT,
                                                      {"checkpoint_id": CHECKPOINT_ID})
    assert out is not None and out["record"].checkpoint_id == CHECKPOINT_ID
    assert out["counts"] is None


@pytest.mark.asyncio
async def test_prepare_restore_review_fetches_live_counts_from_the_host(world):
    world.note()
    world.replies["diff_checkpoint"] = {"ok": True, "result": {"root": "x", "tree_hash": TREE_HASH,
                                                               "file_count": 3, "would_overwrite": 2,
                                                               "would_delete": 1}}
    controller = next(ws for ws in world.orch.ui_clients if getattr(ws, "chat", None) == CHAT)
    out = await restore_review.prepare_restore_review(world.orch, controller, OWNER, CHAT,
                                                      {"checkpoint_id": CHECKPOINT_ID,
                                                       "computer": world.host.host_id})
    assert out is not None
    assert out["record"].checkpoint_id == CHECKPOINT_ID
    assert out["counts"] == {"would_overwrite": 2, "would_delete": 1}
    assert len(world.requests("diff_checkpoint")) == 1
    world.replies["diff_checkpoint"] = {"ok": False, "error": {"code": "unknown_checkpoint",
                                                               "message": "gone"}}
    out = await restore_review.prepare_restore_review(world.orch, controller, OWNER, CHAT,
                                                      {"checkpoint_id": CHECKPOINT_ID,
                                                       "computer": world.host.host_id})
    assert out == {"record": out["record"], "counts": None}


@pytest.mark.asyncio
async def test_attach_restore_review_rebinds_the_display_digest(monkeypatch):
    seen = []

    async def fake_audit(*args, **kwargs):
        seen.append((args, kwargs))

    monkeypatch.setattr("orchestrator.remote_confirmation._audit_async", fake_audit)
    card = {"type": "card", "id": "au_approval_1", "display_digest": "old",
            "content": [{"type": "text", "content": "Restore files on Mac"},
                        {"type": "button", "label": "Approve", "action": "remote_op_decision",
                         "payload": {"decision": "approve", "proposal_id": "p-1"}}]}
    state = {"record": _record(), "counts": {"would_overwrite": 2, "would_delete": 1}}
    out = await restore_review.attach_restore_review(SimpleNamespace(), None, OWNER, CHAT,
                                                     {"checkpoint_id": CHECKPOINT_ID}, [card],
                                                     snapshot=state)
    assert out[0]["display_digest"] != "old" and len(seen) == 1
    assert seen[0][1]["proposal_id"] == "p-1" and seen[0][1]["verb"] == "restore_checkpoint"


@pytest.mark.asyncio
async def test_live_counts_degrades_on_stale_sessions_host_errors_and_timeouts(world, monkeypatch):
    world.note()
    controller = next(ws for ws in world.orch.ui_clients if getattr(ws, "chat", None) == CHAT)
    args = {"checkpoint_id": CHECKPOINT_ID, "computer": world.host.host_id}
    import orchestrator.remote_confirmation as rc_mod

    calls = {"n": 0}

    def current_then_stale(*_args, **_kwargs):
        calls["n"] += 1
        return calls["n"] == 1

    monkeypatch.setattr(rc_mod, "computer_approval_current", current_then_stale)
    out = await restore_review.prepare_restore_review(world.orch, controller, OWNER, CHAT, args)
    assert out is not None and out["counts"] is None and calls["n"] >= 2
    monkeypatch.undo()

    async def boom(*_args, **_kwargs):
        raise RuntimeError("host socket exploded")

    monkeypatch.setattr(world.orch.computer_hosts, "request", boom)
    out = await restore_review.prepare_restore_review(world.orch, controller, OWNER, CHAT, args)
    assert out is not None and out["counts"] is None
    monkeypatch.undo()

    world.replies["diff_checkpoint"] = {"ok": True, "result": {"root": "x"}}
    out = await restore_review.prepare_restore_review(world.orch, controller, OWNER, CHAT, args)
    assert out is not None and out["counts"] is None

    async def junk(*_args, **_kwargs):
        return "junk"

    monkeypatch.setattr(world.orch.computer_hosts, "request", junk)
    out = await restore_review.prepare_restore_review(world.orch, controller, OWNER, CHAT, args)
    assert out is not None and out["counts"] is None
    monkeypatch.undo()

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(1.4)

    monkeypatch.setattr(world.orch.computer_hosts, "request", slow)
    monkeypatch.setattr(restore_review, "DIFF_TIMEOUT_S", 0.01)
    out = await restore_review.prepare_restore_review(world.orch, controller, OWNER, CHAT, args)
    assert out is not None and out["counts"] is None


async def test_gate_stack_runs_the_restore_review_hooks(gate_orchestrator):
    from orchestrator.orchestrator import GateRefusal

    orch, ws = gate_orchestrator
    fake = _FakeOrch()
    orch.computer_hosts = fake.computer_hosts
    orch.computer_sessions = fake.computer_sessions
    orch.audit_recorder = SimpleNamespace(record=None)
    host, _ = fake.computer_hosts.register(OWNER, _WS(), ComputerHostDescriptor.from_dict(_descriptor(
        platform="macos", name="Mac", workspace_roots=[{"name": "x", "path": ROOT}])))
    out = await orch._authorize_and_prepare(ws, "computer-use-1", "restore_checkpoint",
                                            {"checkpoint_id": CHECKPOINT_ID, "computer": host.name},
                                            CHAT, OWNER, auto_subscribe_stream=False)
    assert isinstance(out, GateRefusal)
    assert "not known to this server" in (out.response.error or {}).get("message", "")


async def test_checkpoint_tools_refuse_unattended_and_checkpoint_note_needs_an_owner(world, monkeypatch):
    for tool, kwargs in (("list_checkpoints", {}), ("diff_checkpoint", {"checkpoint_id": CHECKPOINT_ID}),
                         ("restore_checkpoint", {"checkpoint_id": CHECKPOINT_ID})):
        out = await asyncio.to_thread(getattr(mcp_tools, tool), session_id=CHAT, **kwargs)
        assert out["_data"]["code"] == "unattended_refused", tool
    monkeypatch.setattr(mcp_tools, "_ORCH", None)
    assert mcp_tools._checkpoint_note(None, CHAT, world.host, "write_file", {}, {}) == {}


async def test_list_checkpoints_caps_and_restore_host_errors_surface(world, audit_rec):
    world.note()
    many = [{"id": str(uuid.uuid4()), "root": "x", "root_digest": cc.root_digest(ROOT),
             "tree_hash": TREE_HASH, "file_count": 1} for _ in range(cc.MAX_RECORDS + 10)]
    world.replies["list_checkpoints"] = {"ok": True, "result": {"checkpoints": many}}
    out = await world.call("list_checkpoints")
    assert len(out["_data"]["checkpoints"]) == mcp_tools._MAX_CHECKPOINTS
    world.replies["restore_checkpoint"] = {"ok": False, "error": {"code": "unknown_checkpoint",
                                                                  "message": "no such checkpoint"}}
    out = await world.call("restore_checkpoint", checkpoint_id=CHECKPOINT_ID)
    assert out["_data"]["code"] == "unknown_checkpoint"
    assert [ev for ev in audit_rec.events if ev.action_type == "checkpoint.restored"] == []


def test_summary_line_carries_the_checkpoint_id():
    text = policy.summary_for("restore_checkpoint", {"checkpoint_id": CHECKPOINT_ID}, "Mac")
    assert CHECKPOINT_ID in text and "Restore files on Mac" in text


async def test_evaluate_refuses_a_foreign_checkpoint_before_any_card(audit_rec):
    from tests.test_computer_use_076 import _FakeDB, _Hashable, _gate_orch

    db = _FakeDB()
    orch = _gate_orch(db)
    ws = _Hashable()
    orch.ui_sessions[ws] = {"sub": OWNER}
    host, _ = orch.computer_hosts.register(OWNER, _WS(), ComputerHostDescriptor.from_dict(_descriptor(
        platform="macos", name="Mac", workspace_roots=[{"name": "x", "path": ROOT}])))
    args = {"checkpoint_id": CHECKPOINT_ID, "computer": "Mac"}
    out = rc.evaluate(orch, ws, "computer-use-1", "restore_checkpoint", dict(args), CHAT, OWNER)
    assert out is not None and "not known to this server" in out[0]
    ghost = rc.evaluate(orch, ws, "computer-use-1", "restore_checkpoint",
                        {"checkpoint_id": CHECKPOINT_ID, "computer": "ghost"}, CHAT, OWNER)
    assert ghost is not None and "no computer called 'ghost'" in ghost[0]
    assert not db.rows
    refused = [ev for ev in audit_rec.events if ev.action_type == "remote_op.refused_checkpoint"]
    assert len(refused) == 1 and refused[0].inputs_meta["verb"] == "restore_checkpoint"
    cc.registry_for(orch).note_created(_record(host_id="some-other-host"))
    out = rc.evaluate(orch, ws, "computer-use-1", "restore_checkpoint", dict(args), CHAT, OWNER)
    assert out is not None and "different computer" in out[0]
    refused_after_two = [ev for ev in audit_rec.events if ev.action_type == "remote_op.refused_checkpoint"]
    cc.registry_for(orch).note_created(_record(host_id=host.host_id, root_digest=cc.root_digest(ROOT)))
    out = rc.evaluate(orch, ws, "computer-use-1", "restore_checkpoint", dict(args), CHAT, OWNER)
    assert out is not None and "cannot authorize" in out[0]
    assert [ev for ev in audit_rec.events if ev.action_type == "remote_op.refused_checkpoint"] == refused_after_two
