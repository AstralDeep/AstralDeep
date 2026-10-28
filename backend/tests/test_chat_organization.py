"""Tests for chat rename, pin and archive: HistoryManager persistence over Plane (title column and
the owner preferences document), the orchestrator/chat_organization.py ui_event handler with its
owner-only refusals and audit, and history_list carrying title, pinned and archived.
"""

from __future__ import annotations

import json
import uuid

import pytest

from orchestrator import chat_organization as co
from tests.helpers.voice_plane_runtime import history_manager, isolated_voice_plane_runtime


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_voice_plane_runtime("chat_organization") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def hm(plane_runtime):
    return history_manager(plane_runtime)


@pytest.fixture
def owner():
    return f"chat-org-{uuid.uuid4().hex[:12]}"


def _chat(hm, owner, text="hello"):
    chat_id = hm.create_chat(user_id=owner)
    hm.add_message(chat_id, "user", text, user_id=owner)
    return chat_id


def _listed(hm, owner, **kwargs):
    return {row["id"]: row for row in hm.get_recent_chats(user_id=owner, **kwargs)}


def test_rename_persists_the_title_for_the_owner_only(hm, owner):
    chat_id = _chat(hm, owner)
    assert hm.rename_chat(chat_id, "Lab follow-up", user_id=owner) is True
    assert _listed(hm, owner)[chat_id]["title"] == "Lab follow-up"
    assert hm.rename_chat(chat_id, "Taken", user_id="someone-else") is False
    assert hm.rename_chat("missing", "x", user_id=owner) is False
    assert _listed(hm, owner)[chat_id]["title"] == "Lab follow-up"


def test_pin_and_archive_mark_the_list_and_archived_chats_are_hidden_by_default(hm, owner):
    pinned, archived, plain = _chat(hm, owner, "a"), _chat(hm, owner, "b"), _chat(hm, owner, "c")
    assert hm.set_chat_flag(pinned, "pinned", True, user_id=owner)
    assert hm.set_chat_flag(archived, "archived", True, user_id=owner)
    listed = _listed(hm, owner)
    assert set(listed) == {pinned, plain}
    assert (listed[pinned]["pinned"], listed[pinned]["archived"]) == (True, False)
    assert (listed[plain]["pinned"], listed[plain]["archived"]) == (False, False)
    everything = _listed(hm, owner, include_archived=True)
    assert everything[archived]["archived"] is True and set(everything) == {pinned, archived, plain}
    assert hm.set_chat_flag(pinned, "pinned", False, user_id=owner)
    assert hm.set_chat_flag(archived, "archived", False, user_id=owner)
    assert all(not row["pinned"] and not row["archived"] for row in _listed(hm, owner).values())


def test_an_unknown_flag_is_refused_before_persistence(hm, owner):
    with pytest.raises(ValueError):
        hm.set_chat_flag(_chat(hm, owner), "starred", True, user_id=owner)


def test_a_foreign_chat_cannot_be_pinned_or_archived(hm, owner):
    chat_id = _chat(hm, owner)
    assert hm.set_chat_flag(chat_id, "archived", True, user_id="someone-else") is False
    assert chat_id in _listed(hm, owner)


def test_a_pinned_chat_older_than_the_recent_window_is_still_listed(hm, owner):
    old = _chat(hm, owner, "old")
    for index in range(3):
        _chat(hm, owner, f"newer {index}")
    assert old not in _listed(hm, owner, limit=2)
    hm.set_chat_flag(old, "pinned", True, user_id=owner)
    listed = hm.get_recent_chats(user_id=owner, limit=2)
    assert [row["id"] for row in listed if row["pinned"]] == [old] and len(listed) == 3


@pytest.mark.parametrize("raw,expected", [
    ("  Lab\tfollow-up \n", "Lab follow-up"),
    ("Résumé‮notes\u0000", "Résumé notes"),
    ("x" * 120, "x" * 120),
    ("x" * 121, None), ("   ", None), ("​", None), (None, None), (5, None),
])
def test_titles_are_sanitized_and_bounded(raw, expected):
    assert co.sanitized_title(raw) == expected


class _Recorder:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)


class _History:
    def __init__(self, result=True):
        self.calls, self.result = [], result

    def rename_chat(self, chat_id, title, user_id):
        self.calls.append(("rename", chat_id, title, user_id))
        return self.result

    def set_chat_flag(self, chat_id, flag, value, user_id):
        self.calls.append(("flag", chat_id, flag, value, user_id))
        return self.result


class _Orch:
    def __init__(self, result=True):
        self.history, self.sent, self.broadcasts = _History(result), [], []

    async def _safe_send(self, ws, data):
        self.sent.append(json.loads(data))
        return True

    async def _broadcast_user_history(self, user_id=None):
        self.broadcasts.append(user_id)


@pytest.fixture
def recorder(monkeypatch):
    from audit import recorder as module

    fake = _Recorder()
    monkeypatch.setattr(module, "get_recorder", lambda: fake)
    return fake


@pytest.mark.parametrize("action,call", [
    ("chat_pin", ("flag", "c1", "pinned", True, "u1")), ("chat_unpin", ("flag", "c1", "pinned", False, "u1")),
    ("chat_archive", ("flag", "c1", "archived", True, "u1")),
    ("chat_unarchive", ("flag", "c1", "archived", False, "u1")),
])
async def test_flag_actions_persist_audit_and_refresh_every_owner_list(recorder, action, call):
    orch = _Orch()
    await co.handle(orch, object(), "u1", action, {"chat_id": "c1"})
    assert orch.history.calls == [call] and orch.broadcasts == ["u1"] and orch.sent == []
    event, = recorder.events
    expected = {"chat_pin": "chat.pinned", "chat_unpin": "chat.unpinned", "chat_archive": "chat.archived",
                "chat_unarchive": "chat.unarchived"}[action]
    assert (event.action_type, event.conversation_id, event.actor_user_id) == (expected, "c1", "u1")


async def test_rename_sends_the_sanitized_title_and_keeps_it_out_of_the_audit(recorder):
    orch = _Orch()
    await co.handle(orch, object(), "u1", "chat_rename", {"chat_id": "c1", "title": " Plan\nB "})
    assert orch.history.calls == [("rename", "c1", "Plan B", "u1")] and orch.broadcasts == ["u1"]
    event, = recorder.events
    assert event.action_type == "chat.renamed" and "Plan" not in json.dumps(event.model_dump(mode="json"))


@pytest.mark.parametrize("action,payload,code", [
    ("chat_rename", {"chat_id": "c1", "title": "x" * 121}, "invalid_title"),
    ("chat_rename", {"chat_id": "c1"}, "invalid_title"),
    ("chat_pin", {}, "invalid_request"),
    ("chat_pin", {"chat_id": ""}, "invalid_request"),
    ("chat_pin", {"chat_id": "c" * 257}, "invalid_request"),
    ("chat_pin", {"chat_id": "c1", "title": "x"}, "invalid_request"),
    ("chat_archive", "not-a-dict", "invalid_request"),
])
async def test_malformed_requests_are_refused_without_touching_history(recorder, action, payload, code):
    orch = _Orch()
    await co.handle(orch, object(), "u1", action, payload)
    assert orch.history.calls == [] and orch.broadcasts == [] and recorder.events == []
    assert orch.sent == [{"type": "error", "code": code, "message": co.MESSAGES[code], "action": action}]


@pytest.mark.parametrize("action,payload", [("chat_rename", {"chat_id": "c1", "title": "x"}),
                                            ("chat_archive", {"chat_id": "c1"})])
async def test_a_foreign_or_unknown_chat_is_a_bounded_refusal(recorder, action, payload):
    orch = _Orch(result=False)
    await co.handle(orch, object(), "u1", action, payload)
    assert orch.broadcasts == [] and recorder.events == []
    assert orch.sent == [{"type": "error", "code": "chat_not_found", "message": co.MESSAGES["chat_not_found"],
                          "action": action}]


async def test_an_unavailable_audit_does_not_hide_the_change(monkeypatch):
    from audit import recorder as module

    monkeypatch.setattr(module, "get_recorder", lambda: None)
    orch = _Orch()
    await co.handle(orch, object(), "u1", "chat_pin", {"chat_id": "c1"})
    assert orch.broadcasts == ["u1"]


async def test_a_failing_audit_recorder_is_logged_not_raised(monkeypatch):
    from audit import recorder as module

    class _Broken:
        async def record(self, event):
            raise RuntimeError("down")
    monkeypatch.setattr(module, "get_recorder", lambda: _Broken())
    orch = _Orch()
    await co.handle(orch, object(), "u1", "chat_pin", {"chat_id": "c1"})
    assert orch.broadcasts == ["u1"]


async def test_a_persistence_failure_is_reported_not_raised(recorder):
    orch = _Orch()

    def broken(*args, **kwargs):
        raise RuntimeError("database down")
    orch.history.set_chat_flag = broken
    await co.handle(orch, object(), "u1", "chat_pin", {"chat_id": "c1"})
    assert orch.sent == [{"type": "error", "code": "chat_update_failed", "message": co.MESSAGES["chat_update_failed"],
                          "action": "chat_pin"}]
    assert orch.broadcasts == [] and recorder.events == []


def test_history_list_chats_from_the_rest_model_carry_the_flags():
    from orchestrator.models import ChatSummary

    summary = ChatSummary(id="c1", title="t", updated_at=1, pinned=True, archived=False)
    assert summary.model_dump()["pinned"] is True and ChatSummary(id="c", title="t", updated_at=1).archived is False


def test_dispatch_names_every_action():
    assert set(co.ACTIONS) == {"chat_rename", "chat_pin", "chat_unpin", "chat_archive", "chat_unarchive"}
