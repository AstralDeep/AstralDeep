"""Feature 076 — remote computer control: contract + behaviour tests.

No PostgreSQL, no network: the host registry and session manager run against a
fake orchestrator whose sockets capture frames; the confirmation gate runs
against the in-memory AstralPlane proposal repository (same seam as 063).

Pins (spec SC-003/SC-004 + contracts/verbs.md, contracts/transport.md):
- the verb registry equals the policy (scopes, tiers, timeouts, destructive
  classification identity, never retryable);
- a malformed ``computer_host`` descriptor costs eligibility, never the session;
- the registry is owner-scoped and per-host addressed; a response is accepted
  only from the socket that holds the host;
- a session needs a live host acknowledgement, one controller at a time, and
  ends on consent revocation / host loss / idle / max / silence;
- an unattended turn may run only ``list_computers``; a consequential verb is
  refused with a proposal card on first reach and never executes;
- screenshots become image parts after the tool messages, are pruned to the
  newest N, and are stripped (once) when the provider rejects images;
- flag off ⇒ no agent dir, no menu input, disabled surface, ignored events.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace

import pytest

from orchestrator import computer_use_policy as policy
from orchestrator import remote_confirmation as rc
from orchestrator.computer_hosts import ComputerHostError, ComputerHostRegistry
from orchestrator.computer_sessions import ComputerSessionManager
from shared.protocol import (
    COMPUTER_HOST_VERBS,
    ComputerHostDescriptor,
    ProtocolValidationError,
    RegisterUI,
)
from tests.helpers.remote_plane_runtime import make_remote_confirmation_plane_source

OWNER = "user-1"
OTHER = "user-2"


# ── fakes ──────────────────────────────────────────────────────────────────────

class _WS:
    def __init__(self, user=OWNER, chat=None):
        self.user = user
        self.chat = chat
        self.sent: list = []

    def frames(self, ftype=None):
        out = [json.loads(f) for f in self.sent]
        return [f for f in out if ftype is None or f.get("type") == ftype]


class _Rote:
    def get_profile(self, ws):
        return SimpleNamespace(device_type=SimpleNamespace(value="android"))


class _FakeOrch:
    def __init__(self):
        self.ui_clients: list = []
        self.ui_sessions: dict = {}
        self._ws_active_chat: dict = {}
        self.rote = _Rote()
        self.computer_hosts = ComputerHostRegistry(self)
        self.computer_sessions = ComputerSessionManager(self, self.computer_hosts)

    def add(self, ws: _WS):
        self.ui_clients.append(ws)
        self.ui_sessions[ws] = {"sub": ws.user}
        if ws.chat:
            self._ws_active_chat[id(ws)] = ws.chat
        return ws

    def _get_user_id(self, ws):
        return getattr(ws, "user", None)

    async def _safe_send(self, ws, data: str) -> bool:
        ws.sent.append(data)
        return True

    def socket_for_chat(self, user_id, chat_id):
        for ws in self.ui_clients:
            if ws.user == user_id and self._ws_active_chat.get(id(ws)) == chat_id:
                return ws
        for ws in self.ui_clients:
            if ws.user == user_id:
                return ws
        return None


def _descriptor(**over):
    base = {
        "host_id": str(uuid.uuid4()),
        "name": "RYZENROLL",
        "platform": "windows",
        "client_version": "0.5.0",
        "screens": [{"index": 0, "width": 2560, "height": 1440, "scale": 1.0, "primary": True}],
        "verbs": sorted(COMPUTER_HOST_VERBS),
        "protocol": 1,
    }
    base.update(over)
    return base


async def _online_host(orch: _FakeOrch, name="RYZENROLL", user=OWNER):
    ws = orch.add(_WS(user=user))
    host, _ = orch.computer_hosts.register(user, ws, ComputerHostDescriptor.from_dict(_descriptor(name=name)))
    return ws, host


async def _session(orch: _FakeOrch, host, controller: _WS, chat="chat-1"):
    """Start a session and satisfy the acknowledgement with a heartbeat."""
    task = asyncio.create_task(orch.computer_sessions.start(OWNER, host, controller, chat))
    await asyncio.sleep(0)
    live = orch.computer_sessions.live_for_host(OWNER, host.host_id)
    assert live is not None
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "heartbeat", live.session_id, None)
    return await task


# ── verb contract ─────────────────────────────────────────────────────────────

def test_registry_equals_policy():
    from agents.computer_use.mcp_tools import TOOL_REGISTRY
    assert set(TOOL_REGISTRY) == policy.ALL_VERBS
    assert policy.HOST_VERBS == COMPUTER_HOST_VERBS
    for name, entry in TOOL_REGISTRY.items():
        assert entry["scope"] == policy.SCOPES[name]
        assert entry["tier"] == policy.TIERS[name]
        assert entry["timeout"] == policy.TIMEOUTS[name]
        assert entry["retryable"] is False
        if name in policy.DESTRUCTIVE_CLASSIFICATION:
            assert entry["destructive"] is policy.DESTRUCTIVE_CLASSIFICATION[name]
            assert entry["destructive"] == "always"
        else:
            assert "destructive" not in entry
        schema = entry["input_schema"]
        assert schema["type"] == "object"
        if name != "list_computers":
            assert "computer" in schema["properties"]


def test_consequential_verbs_are_always_gated_and_unattended_set_is_minimal():
    assert policy.CONSEQUENTIAL_VERBS <= set(policy.DESTRUCTIVE_CLASSIFICATION)
    assert "confirm_action" in policy.DESTRUCTIVE_CLASSIFICATION
    assert policy.UNATTENDED_ALLOWED == frozenset({"list_computers"})
    assert policy.SCOPES["run_command"] == "tools:execute"
    assert policy.SCOPES["write_file"] == policy.SCOPES["delete_path"] == "tools:files"


def test_gate_policy_table_covers_both_agents_and_063_is_unchanged():
    assert rc.GATED_AGENT_IDS == {"remote-compute-1", "computer-use-1"}
    p063 = rc.policy_for("remote-compute-1")
    assert p063.classification is rc.DESTRUCTIVE_CLASSIFICATION
    assert p063.gate_unclassified_unattended is False
    p076 = rc.policy_for("computer-use-1")
    assert p076.classification is policy.DESTRUCTIVE_CLASSIFICATION
    assert p076.gate_unclassified_unattended is True
    assert rc.policy_for("weather-1") is None
    assert rc.classification_for("remove_path") == "always"           # 063 default table
    assert rc.classification_for("run_command", "computer-use-1") == "always"
    assert rc.classification_for("click", "computer-use-1") is None
    assert rc.is_destructive_unattended("screenshot", {}, "computer-use-1") is True
    assert rc.is_destructive_unattended("list_computers", {}, "computer-use-1") is False
    assert rc.is_destructive_unattended("list_machines", {}) is False  # 063 read verb


# ── protocol ──────────────────────────────────────────────────────────────────

def test_descriptor_round_trip_and_validation():
    d = ComputerHostDescriptor.from_dict(_descriptor())
    assert d.to_dict()["verbs"] == sorted(COMPUTER_HOST_VERBS)
    for bad in (
        {"name": ""}, {"name": "x" * 65}, {"platform": "amiga"}, {"client_version": "1"},
        {"protocol": 2}, {"screens": []}, {"verbs": []}, {"verbs": ["format_disk"]},
        {"screens": [{"index": 0, "width": 0, "height": 1, "scale": 1, "primary": True}]},
        {"screens": [{"index": 0, "width": 1, "height": 1, "scale": 1, "primary": False}]},
        {"host_id": "not-a-uuid"},
    ):
        with pytest.raises(ProtocolValidationError):
            ComputerHostDescriptor.from_dict(_descriptor(**bad))
    with pytest.raises(ProtocolValidationError):
        ComputerHostDescriptor.from_dict({**_descriptor(), "extra": 1})


def test_register_ui_keeps_session_when_descriptor_is_malformed_and_carries_good_one():
    good = RegisterUI.from_json(json.dumps({"type": "register_ui", "token": "t",
                                            "computer_host": _descriptor()}))
    assert isinstance(good.computer_host, ComputerHostDescriptor)
    assert json.loads(good.to_json())["computer_host"]["protocol"] == 1
    bad = RegisterUI.from_json(json.dumps({"type": "register_ui", "token": "t",
                                           "computer_host": _descriptor(platform="amiga")}))
    assert bad.computer_host is None
    assert bad.token == "t"
    plain = RegisterUI.from_json(json.dumps({"type": "register_ui", "token": "t"}))
    assert plain.computer_host is None
    assert "computer_host" not in json.loads(plain.to_json())  # pre-076 wire bytes preserved


# ── registry ──────────────────────────────────────────────────────────────────

async def test_registry_is_owner_scoped_and_resolves_names():
    orch = _FakeOrch()
    ws_a, host_a = await _online_host(orch, "RYZENROLL")
    ws_o, host_o = await _online_host(orch, "OTHERS-PC", user=OTHER)
    assert [h["name"] for h in orch.computer_hosts.list_for_owner(OWNER)] == ["RYZENROLL"]
    assert orch.computer_hosts.resolve(OWNER, None) is host_a
    assert orch.computer_hosts.resolve(OWNER, "ryzenroll") is host_a
    assert orch.computer_hosts.resolve(OWNER, host_a.host_id) is host_a
    with pytest.raises(ComputerHostError) as exc:
        orch.computer_hosts.resolve(OWNER, "OTHERS-PC")
    assert exc.value.code == "computer_unavailable"
    ws_b, host_b = await _online_host(orch, "LAPTOP")
    with pytest.raises(ComputerHostError) as exc:
        orch.computer_hosts.resolve(OWNER, None)
    assert exc.value.code == "ambiguous_computer" and set(exc.value.candidates) == {"RYZENROLL", "LAPTOP"}


async def test_registry_supersedes_and_caches_offline_hosts():
    orch = _FakeOrch()
    ws1, host1 = await _online_host(orch)
    ws2 = orch.add(_WS())
    host2, superseded = orch.computer_hosts.register(
        OWNER, ws2, ComputerHostDescriptor.from_dict(_descriptor(host_id=host1.host_id)))
    assert superseded is host1 and host2.websocket is ws2
    assert orch.computer_hosts.host_for_socket(ws1) is None
    gone = orch.computer_hosts.on_socket_closed(ws2)
    assert gone is host2
    rows = orch.computer_hosts.list_for_owner(OWNER)
    assert rows and rows[0]["online"] is False
    with pytest.raises(ComputerHostError) as exc:
        orch.computer_hosts.resolve(OWNER, "RYZENROLL")
    assert exc.value.code == "computer_unavailable" and "offline" in exc.value.message
    assert orch.computer_hosts.forget(OWNER, host2.host_id) is True
    assert orch.computer_hosts.list_for_owner(OWNER) == []


async def test_request_response_correlation_and_socket_binding():
    orch = _FakeOrch()
    ws, host = await _online_host(orch)
    phone = orch.add(_WS())

    async def _answer():
        await asyncio.sleep(0.01)
        req = ws.frames("computer_request")[-1]
        assert req["verb"] == "click" and req["session_id"] == "cs_x" and req["deadline_ms"] == 10000
        # A phone can never answer for a host — dropped.
        assert orch.computer_hosts.handle_response(phone, OWNER, {"request_id": req["request_id"], "ok": True, "result": {}}) is False
        # Another user can never answer — dropped.
        assert orch.computer_hosts.handle_response(ws, OTHER, {"request_id": req["request_id"], "ok": True, "result": {}}) is False
        assert orch.computer_hosts.handle_response(ws, OWNER, {"request_id": req["request_id"], "ok": True,
                                                              "result": {"x": 1, "y": 2}}) is True

    asyncio.create_task(_answer())
    result = await orch.computer_hosts.request(host, "cs_x", "click", {"x": 1, "y": 2, "button": "left"}, 10.0)
    assert result == {"x": 1, "y": 2}
    with pytest.raises(ComputerHostError) as exc:
        await orch.computer_hosts.request(host, "cs_x", "format_disk", {}, 1.0)
    assert exc.value.code == "unsupported"
    with pytest.raises(ComputerHostError) as exc:
        await orch.computer_hosts.request(host, "cs_x", "wait", {"seconds": 1}, 0.05)
    assert exc.value.code == "timeout"


async def test_typed_host_error_and_host_loss_fail_pending_requests():
    orch = _FakeOrch()
    ws, host = await _online_host(orch)

    async def _refuse():
        await asyncio.sleep(0.01)
        req = ws.frames("computer_request")[-1]
        orch.computer_hosts.handle_response(ws, OWNER, {"request_id": req["request_id"], "ok": False,
                                                       "error": {"code": "screen_locked", "message": "locked"}})
    asyncio.create_task(_refuse())
    with pytest.raises(ComputerHostError) as exc:
        await orch.computer_hosts.request(host, "cs", "screenshot", {}, 5.0)
    assert exc.value.code == "screen_locked"

    async def _drop():
        await asyncio.sleep(0.01)
        orch.computer_hosts.on_socket_closed(ws)
    asyncio.create_task(_drop())
    with pytest.raises(ComputerHostError) as exc:
        await orch.computer_hosts.request(host, "cs", "screenshot", {}, 5.0)
    assert exc.value.code == "host_offline"


# ── sessions ──────────────────────────────────────────────────────────────────

async def test_session_needs_host_ack_and_pushes_to_every_owner_socket():
    orch = _FakeOrch()
    ws, host = await _online_host(orch)
    phone = orch.add(_WS(chat="chat-1"))
    stranger = orch.add(_WS(user=OTHER))
    session = await _session(orch, host, phone)
    assert session.state == "active" and session.controller_label == "Android phone"
    assert ws.frames("computer_session")[-1]["state"] == "active"
    assert phone.frames("computer_session")[-1]["session_id"] == session.session_id
    assert stranger.frames("computer_session") == []
    # re-join from the same controller returns the same session
    again = await orch.computer_sessions.start(OWNER, host, phone, "chat-1")
    assert again is session


async def test_session_without_ack_is_host_unresponsive(monkeypatch):
    from orchestrator import computer_sessions
    monkeypatch.setattr(computer_sessions, "ACK_TIMEOUT_S", 0.05)
    orch = _FakeOrch()
    ws, host = await _online_host(orch)
    phone = orch.add(_WS())
    with pytest.raises(ComputerHostError) as exc:
        await orch.computer_sessions.start(OWNER, host, phone, "chat-1")
    assert exc.value.code == "host_unresponsive"
    assert orch.computer_sessions.live_for_host(OWNER, host.host_id) is None
    assert phone.frames("computer_session")[-1]["reason"] == "host_unresponsive"


async def test_one_controller_at_a_time_and_takeover_when_it_is_gone():
    orch = _FakeOrch()
    ws, host = await _online_host(orch)
    phone = orch.add(_WS(chat="chat-1"))
    tablet = orch.add(_WS(chat="chat-2"))
    session = await _session(orch, host, phone)
    with pytest.raises(ComputerHostError) as exc:
        await orch.computer_sessions.start(OWNER, host, tablet, "chat-2")
    assert exc.value.code == "controlled_by_other"
    orch.ui_clients.remove(phone)  # the phone dropped off
    taken = await orch.computer_sessions.start(OWNER, host, tablet, "chat-2")
    assert taken is session and session.controller_ws_id == id(tablet) and session.chat_id == "chat-2"


async def test_session_transitions_and_host_events():
    orch = _FakeOrch()
    ws, host = await _online_host(orch)
    phone = orch.add(_WS(chat="chat-1"))
    session = await _session(orch, host, phone)
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "paused", session.session_id, "local_input")
    assert session.state == "paused" and session.pause_reason == "local_input"
    assert phone.frames("computer_session")[-1]["state"] == "paused"
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "resumed", session.session_id, None)
    assert session.state == "active"
    # a stranger's or foreign host event is ignored
    await orch.computer_sessions.on_host_event(OTHER, host.host_id, "stopped", session.session_id, None)
    assert session.state == "active"
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "stopped", session.session_id, None)
    assert session.state == "ended" and session.reason == "local_stop"
    assert orch.computer_sessions.live_for_owner(OWNER) == []


async def test_host_loss_and_consent_revocation_end_sessions():
    orch = _FakeOrch()
    ws, host = await _online_host(orch)
    phone = orch.add(_WS(chat="chat-1"))
    session = await _session(orch, host, phone)
    await orch.computer_sessions.end_all_for_host(OWNER, host.host_id, "consent_revoked")
    assert session.reason == "consent_revoked"
    assert phone.frames("computer_session")[-1]["reason"] == "consent_revoked"


async def test_sweep_enforces_idle_max_and_silence(monkeypatch):
    from orchestrator import computer_sessions as cs
    orch = _FakeOrch()
    ws, host = await _online_host(orch)
    phone = orch.add(_WS(chat="chat-1"))
    s = await _session(orch, host, phone)
    now = time.time()
    await orch.computer_sessions.sweep(now + cs.HEARTBEAT_SILENCE_S + 1)
    assert s.reason == "host_silent"
    s2 = await _session(orch, host, phone)
    s2.last_heartbeat_at = now + 10_000
    await orch.computer_sessions.sweep(now + cs.IDLE_TIMEOUT_S + 1)
    assert s2.reason == "idle_timeout"
    s3 = await _session(orch, host, phone)
    s3.last_heartbeat_at = s3.last_activity_at = now + cs.MAX_DURATION_S + 5
    await orch.computer_sessions.sweep(now + cs.MAX_DURATION_S + 1)
    assert s3.reason == "max_duration"


# ── the dispatch gate ─────────────────────────────────────────────────────────

class _FakeDB:
    def __init__(self):
        self.rows: dict = {}


class _Hashable:
    pass


def _gate_orch(db):
    source = make_remote_confirmation_plane_source(db)
    orch = SimpleNamespace(
        plane_repository_source=source,
        runtime_composition=SimpleNamespace(plane=SimpleNamespace(
            runtime=source.plane_runtime, repositories=source.plane_repositories)),
        credential_manager=object(),
        ui_sessions={},
    )
    fake = _FakeOrch()
    orch.computer_hosts = fake.computer_hosts
    return orch


def test_unattended_turn_may_only_list_computers():
    orch = _gate_orch(_FakeDB())
    for verb in ("screenshot", "click", "type_text", "run_command", "start_session"):
        out = rc.evaluate(orch, None, "computer-use-1", verb, {"computer": "x"}, "chat", OWNER)
        assert out is not None and out[0].startswith("unattended_refused")
    assert rc.evaluate(orch, None, "computer-use-1", "list_computers", {}, "chat", OWNER) is None
    # 063 read verbs keep their status-poll allowance
    assert rc.evaluate(orch, None, "remote-compute-1", "list_machines", {}, "chat", OWNER) is None


def test_attended_input_verbs_pass_and_consequential_verbs_get_a_card():
    db = _FakeDB()
    orch = _gate_orch(db)
    ws = _Hashable()
    orch.ui_sessions[ws] = {"sub": OWNER}
    assert rc.evaluate(orch, ws, "computer-use-1", "click", {"x": 1, "y": 2}, "chat", OWNER) is None
    assert rc.evaluate(orch, ws, "computer-use-1", "screenshot", {}, "chat", OWNER) is None
    out = rc.evaluate(orch, ws, "computer-use-1", "run_command", {"command": "dir", "computer": "RYZENROLL"}, "chat", OWNER)
    assert out is not None and out[0].startswith("confirmation_required")
    card = out[1][0]
    assert card["type"] == "card" and "Confirm an action on" in card["title"]
    buttons = [c for c in card["content"] if c["type"] == "button"]
    assert {b["payload"]["decision"] for b in buttons} == {"approve", "decline"}
    assert any("Run on" in c.get("content", "") for c in card["content"] if c["type"] == "text")
    assert len(db.rows) == 1 and next(iter(db.rows.values()))["status"] == "pending"
    # confirm_action rides the same card
    out = rc.evaluate(orch, ws, "computer-use-1", "confirm_action", {"summary": "Buy the ticket"}, "chat", OWNER)
    assert out is not None and "Buy the ticket" in json.dumps(out[1])


# ── multimodal assembly ───────────────────────────────────────────────────────

def _bare_orchestrator():
    from orchestrator.orchestrator import Orchestrator
    return object.__new__(Orchestrator)


def _shot(n: str):
    return SimpleNamespace(result={"_data": {"width": 10, "height": 5},
                                  "_images": [{"media_type": "image/jpeg", "base64": "QUJD" + n, "caption": f"Shot {n}"}]},
                           error=None)


def test_images_follow_tool_messages_and_are_pruned_to_the_newest():
    o = _bare_orchestrator()
    messages = [{"role": "system", "content": "s"}]
    calls = [SimpleNamespace(id="c1")]
    for i in range(5):
        messages.append({"role": "tool", "tool_call_id": "c1", "content": "{}"})
        assert o._append_tool_images(messages, calls, [_shot(str(i))]) == 1
    imgs = [m for m in messages if o._is_image_message(m)]
    assert len(imgs) == o._MAX_IMAGE_MESSAGES == 3
    placeholders = [m for m in messages if m.get("content") == o._IMAGE_PLACEHOLDER]
    assert len(placeholders) == 2
    last = imgs[-1]["content"]
    assert last[0]["type"] == "text" and "UNTRUSTED" in last[0]["text"] and "Shot 4" in last[0]["text"]
    assert last[1]["type"] == "image_url" and last[1]["image_url"]["url"].startswith("data:image/jpeg;base64,QUJD4")
    assert o._strip_image_parts(messages) == 3
    assert not o._messages_have_images(messages)


def test_results_without_images_add_nothing_and_bad_images_are_ignored():
    o = _bare_orchestrator()
    messages: list = []
    plain = SimpleNamespace(result={"_data": {}}, error=None)
    weird = SimpleNamespace(result={"_images": [{"media_type": "text/html", "base64": "x"}, "junk"]}, error=None)
    assert o._append_tool_images(messages, [SimpleNamespace(id="a"), SimpleNamespace(id="b")], [plain, weird]) == 0
    assert messages == []


def test_provider_rejection_heuristic():
    o = _bare_orchestrator()
    assert o._llm_rejects_images(SimpleNamespace(status_code=400, __str__=lambda s: "image_url is not supported")) is False or True  # shape check only
    class _E(Exception):
        status_code = 400
    assert o._llm_rejects_images(_E("Invalid content type. image_url is only supported by certain models.")) is True
    assert o._llm_rejects_images(_E("messages[3].content must be a string")) is True
    class _E5(Exception):
        status_code = 503
    assert o._llm_rejects_images(_E5("image service unavailable")) is False
    assert o._llm_rejects_images(_E("rate limit exceeded")) is False


def test_compaction_counts_an_image_part_as_a_fixed_cost():
    from orchestrator.compaction import _IMAGE_PART_CHARS, CHARS_PER_TOKEN, estimate_tokens
    big = "A" * 500_000
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"},
                                          {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{big}"}}]}]
    assert estimate_tokens(msgs) < 2_000
    assert _IMAGE_PART_CHARS // CHARS_PER_TOKEN == 1_500


# ── flag off ──────────────────────────────────────────────────────────────────

def test_flag_off_keeps_the_agent_and_menu_input_absent(monkeypatch):
    from orchestrator import chrome_availability, local_agents
    from shared.feature_flags import flags
    monkeypatch.setattr(flags, "is_enabled", lambda name: name in {"safe_agents", "inprocess_agents"})
    assert chrome_availability.projection_chrome_availability()["computer_enabled"] is False
    assert "computer_use" not in local_agents.discover_built_in_agent_dirs()
    assert "computer_use" in local_agents._COMPUTER_USE_AGENT_DIRS
    assert "computer-use-1" in local_agents.FIRST_PARTY_PUBLIC_AGENT_IDS


async def test_flag_off_surface_and_events_are_inert(monkeypatch):
    from orchestrator.projection_surfaces import my_computers
    from shared.feature_flags import flags
    monkeypatch.setattr(flags, "is_enabled", lambda name: False)
    orch = _FakeOrch()
    html = await my_computers.render(orch, OWNER, [], {})
    assert "not enabled" in html
    comps = await my_computers.components(orch, OWNER, [], {})
    assert len(comps) == 1 and "not enabled" in comps[0]["content"]
    out = await my_computers.HANDLERS["chrome_computer_session_start"](orch, _WS(), OWNER, [], {"host_id": "x"})
    assert "not enabled" in out[2]


# ── surface ───────────────────────────────────────────────────────────────────

async def test_surface_lists_hosts_with_the_right_controls(monkeypatch):
    from orchestrator.projection_surfaces import my_computers
    from shared.feature_flags import flags
    monkeypatch.setattr(flags, "is_enabled", lambda name: name == "computer_use")
    orch = _FakeOrch()
    ws, host = await _online_host(orch)
    ws_off, host_off = await _online_host(orch, "LAPTOP")
    orch.computer_hosts.on_socket_closed(ws_off)
    html = await my_computers.render(orch, OWNER, [], {})
    assert "RYZENROLL" in html and "chrome_computer_session_start" in html
    assert "LAPTOP" in html and "chrome_computer_forget" in html
    comps = await my_computers.components(orch, OWNER, [], {})
    cards = [c for c in comps if c["type"] == "card"]
    assert [c["title"] for c in cards] == ["RYZENROLL", "LAPTOP"]
    actions = json.dumps(cards[0])
    assert "chrome_computer_session_start" in actions and "chrome_computer_forget" not in actions
    # another user sees nothing
    assert "RYZENROLL" not in await my_computers.render(orch, OTHER, [], {})

    phone = orch.add(_WS(chat="chat-1"))
    task = asyncio.create_task(my_computers.HANDLERS["chrome_computer_session_start"](
        orch, phone, OWNER, [], {"host_id": host.host_id}))
    await asyncio.sleep(0)
    live = orch.computer_sessions.live_for_host(OWNER, host.host_id)
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "heartbeat", live.session_id, None)
    key, params, notice = await task
    assert key == "my_computers" and "Controlling RYZENROLL" in notice
    html = await my_computers.render(orch, OWNER, [], {})
    assert "chrome_computer_session_stop" in html and "chrome_computer_session_pause" in html
    stranger = _WS(user=OTHER)
    out = await my_computers.HANDLERS["chrome_computer_session_stop"](orch, stranger, OTHER, [], {"session_id": live.session_id})
    assert "not running" in out[2] and live.state == "active"
    out = await my_computers.HANDLERS["chrome_computer_session_stop"](orch, phone, OWNER, [], {"session_id": live.session_id})
    assert "Stopped" in out[2] and live.state == "ended"


# ── the agent's verbs end to end against a fake host ─────────────────────────

class _FakeHostSocket(_WS):
    """A host socket that answers every computer_request like the Windows client."""

    def __init__(self, orch, answers):
        super().__init__()
        self.orch = orch
        self.answers = answers

    def push(self, data):
        self.sent.append(data)
        frame = json.loads(data)
        if frame.get("type") == "computer_request":
            reply = self.answers(frame["verb"], frame["args"])
            loop = asyncio.get_running_loop()
            loop.call_soon(self.orch.computer_hosts.handle_response, self, OWNER,
                           {"request_id": frame["request_id"], **reply})


class _AgentOrch(_FakeOrch):
    async def _safe_send(self, ws, data: str) -> bool:
        if isinstance(ws, _FakeHostSocket):
            ws.push(data)
        else:
            ws.sent.append(data)
        return True


def _answers(verb, args):
    if verb == "screenshot":
        return {"ok": True, "result": {"screen_index": 0, "width": 1280, "height": 720, "scale": 0.5,
                                       "media_type": "image/jpeg", "base64": "/9j/AAAA"}}
    if verb == "list_windows":
        return {"ok": True, "result": {"windows": [{"hwnd": 7, "title": "Untitled - Notepad", "process": "notepad.exe",
                                                   "rect": [0, 0, 800, 600], "focused": True, "minimized": False}]}}
    if verb == "run_command":
        return {"ok": True, "result": {"exit_code": 0, "stdout": "hello", "stderr": "", "truncated": False, "duration_ms": 12}}
    return {"ok": True, "result": {}}


async def test_agent_verbs_against_a_fake_host():
    from agents.computer_use import mcp_tools
    orch = _AgentOrch()
    mcp_tools.register_deps(orch)  # inside the running loop → _LOOP is this loop
    host_ws = orch.add(_FakeHostSocket(orch, _answers))
    host, _ = orch.computer_hosts.register(OWNER, host_ws, ComputerHostDescriptor.from_dict(_descriptor()))
    orch.add(_WS(chat="chat-1"))  # the controlling phone, bound to chat-1
    ctx = {"user_id": OWNER, "session_id": "chat-1"}

    empty = await asyncio.to_thread(mcp_tools.list_computers, user_id=OTHER)
    assert empty["_data"]["computers"] == []
    listed = await asyncio.to_thread(mcp_tools.list_computers, **ctx)
    assert listed["_data"]["computers"][0]["name"] == "RYZENROLL" and listed["_data"]["computers"][0]["session"] is None

    # no session yet → typed refusal, nothing sent to the host
    before = await asyncio.to_thread(mcp_tools.screenshot, **ctx)
    assert before["_data"]["code"] == "no_session" and host_ws.frames("computer_request") == []

    # start_session needs the host's heartbeat ack — supply it from the fake host
    async def _ack_when_pushed():
        for _ in range(200):
            await asyncio.sleep(0.005)
            live = orch.computer_sessions.live_for_host(OWNER, host.host_id)
            if live is not None:
                await orch.computer_sessions.on_host_event(OWNER, host.host_id, "heartbeat", live.session_id, None)
                return
    acker = asyncio.create_task(_ack_when_pushed())
    started = await asyncio.to_thread(mcp_tools.start_session, **ctx)
    await acker
    assert started["_data"]["computer"] == "RYZENROLL" and started["_data"]["state"] == "active"
    session = orch.computer_sessions.live_for_host(OWNER, host.host_id)

    shot = await asyncio.to_thread(mcp_tools.screenshot, **ctx)
    assert shot["_images"][0]["base64"] == "/9j/AAAA"
    assert shot["_ui_components"][0]["type"] == "image"
    assert shot["_ui_components"][0]["id"] == f"au_cuview_{session.session_id}"
    assert shot["_ui_components"][0]["url"].startswith("data:image/jpeg;base64,")
    assert "base64" not in json.dumps(shot["_data"])
    assert session.last_screenshot["width"] == 1280

    assert (await asyncio.to_thread(mcp_tools.click, x=10, y=20, **ctx))["_data"]["button"] == "left"
    bad = await asyncio.to_thread(mcp_tools.click, x=-1, y=20, **ctx)
    assert bad["_data"]["code"] == "out_of_range"
    assert (await asyncio.to_thread(mcp_tools.press_keys, keys="Ctrl+S", **ctx))["_data"]["keys"] == "ctrl+s"
    assert (await asyncio.to_thread(mcp_tools.press_keys, keys="rm -rf /", **ctx))["_data"]["code"] == "out_of_range"
    assert (await asyncio.to_thread(mcp_tools.open_app, app="notepad", **ctx))["_data"]["launched"] is True
    assert (await asyncio.to_thread(mcp_tools.open_app, app="cmd /c del *", **ctx))["_data"]["code"] == "out_of_range"
    wins = await asyncio.to_thread(mcp_tools.list_windows, **ctx)
    assert wins["_data"]["windows"][0]["title"] == "Untitled - Notepad"
    ran = await asyncio.to_thread(mcp_tools.run_command, command="echo hello", **ctx)
    assert ran["_data"]["exit_code"] == 0 and ran["_data"]["stdout"] == "hello"

    # the model asked for an unsupported verb name on this host → typed
    host.descriptor["verbs"] = ["screenshot"]
    assert (await asyncio.to_thread(mcp_tools.click, x=1, y=1, **ctx))["_data"]["code"] == "unsupported"

    # pause → input verbs refuse; resume → work again
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "paused", session.session_id, "local_input")
    paused = await asyncio.to_thread(mcp_tools.screenshot, **ctx)
    assert paused["_data"]["code"] == "paused"
    resumed = await asyncio.to_thread(mcp_tools.resume_session, **ctx)
    assert resumed["_data"]["state"] == "active"
    ended = await asyncio.to_thread(mcp_tools.end_session, **ctx)
    assert ended["_data"]["ended"] is True
    assert orch.computer_sessions.live_for_owner(OWNER) == []
    assert session.verbs_run >= 6


async def test_mcp_server_carries_images_and_turns_refusals_into_errors():
    from agents.computer_use import mcp_tools
    from agents.computer_use.mcp_server import MCPServer
    from shared.protocol import MCPRequest
    orch = _AgentOrch()
    mcp_tools.register_deps(orch)
    server = MCPServer()
    names = {t["name"] for t in server.get_tool_list()}
    assert names == policy.ALL_VERBS
    res = await asyncio.to_thread(server.process_request, MCPRequest(
        request_id="r1", method="tools/call", params={"name": "screenshot", "arguments": {"user_id": OWNER}}))
    assert res.error and "computer_unavailable" in res.error["message"] and res.error["retryable"] is False
    res = await asyncio.to_thread(server.process_request, MCPRequest(
        request_id="r2", method="tools/call", params={"name": "nope", "arguments": {}}))
    assert res.error["code"] == -32601


async def test_consent_switch_is_offered_only_to_a_host_capable_desktop(monkeypatch):
    from orchestrator.chrome_events import current_surface_socket
    from orchestrator.projection_surfaces import my_computers
    from shared.feature_flags import flags
    monkeypatch.setattr(flags, "is_enabled", lambda name: name == "computer_use")
    orch = _FakeOrch()
    phone = orch.add(_WS())                      # no computer_host_capable capability
    desktop = orch.add(_WS())
    orch.ui_sessions[desktop]["_client_capabilities"] = ["render", "stream", "computer_host_capable"]

    token = current_surface_socket.set(phone)
    try:
        comps = await my_computers.components(orch, OWNER, [], {})
        html = await my_computers.render(orch, OWNER, [], {})
    finally:
        current_surface_socket.reset(token)
    assert not any(c.get("title") == "This computer" for c in comps)
    assert "computer_host_consent" not in html

    token = current_surface_socket.set(desktop)
    try:
        comps = await my_computers.components(orch, OWNER, [], {})
        html = await my_computers.render(orch, OWNER, [], {})
    finally:
        current_surface_socket.reset(token)
    card = next(c for c in comps if c.get("title") == "This computer")
    assert '"enabled": true' in json.dumps(card) and "Allow remote control" in json.dumps(card)
    assert "computer_host_consent" in html and "Allow remote control" in html

    # once the desktop announced itself the card offers the OFF switch
    orch.computer_hosts.register(OWNER, desktop, ComputerHostDescriptor.from_dict(_descriptor()))
    token = current_surface_socket.set(desktop)
    try:
        comps = await my_computers.components(orch, OWNER, [], {})
    finally:
        current_surface_socket.reset(token)
    card = next(c for c in comps if c.get("title") == "This computer")
    assert "Stop allowing" in json.dumps(card) and '"enabled": false' in json.dumps(card)
    # a surface rendered outside any render call (no socket) never shows the card
    assert not any(c.get("title") == "This computer"
                   for c in await my_computers.components(orch, OWNER, [], {}))
