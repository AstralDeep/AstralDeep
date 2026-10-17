"""Tests for scripts/probe_backend_web_functional.py: browser wire fencing,
provider-input validation, and credential-free functional receipts.
"""

from contextlib import contextmanager
import hashlib
import json
from uuid import uuid4

import httpx
import pytest

from scripts import probe_backend_web_functional as probe

ID = "a" * 32
CHAT = "11111111-1111-4111-8111-111111111111"
FILE = "22222222-2222-4222-8222-222222222222"
CONFIG = {"provider": "custom", "base_url": "https://provider.invalid/v1", "model": "test", "api_key": "synthetic-only"}


class Socket:
    def __init__(self, failure=None):
        self.failure, self.sent, self.frames = failure, [], []

    def send(self, raw):
        frame = json.loads(raw)
        self.sent.append(frame)
        if frame["type"] == "register_ui":
            self.frames.append({"type": "rote_config"})
        else:
            terminal = {"type": "operation_status", "terminal": True, "state": "completed", "error": None,
                        "operation_id": str(uuid4()), "chat_id": frame.get("session_id"),
                        "action": frame["action"], "connection_generation": frame["connection_generation"],
                        "request_generation": frame["request_generation"]}
            if self.failure == "terminal-owner":
                terminal["connection_generation"] = str(uuid4())
            if self.failure == "terminal-failed":
                terminal["state"] = "failed"
            self.frames.extend([{"type": "heartbeat"}, terminal])

    def recv(self, **kwargs):
        if self.failure == "timeout":
            raise TimeoutError("secret must not escape")
        if self.failure == "binary":
            return b"binary"
        if self.failure == "shape":
            return "[]"
        if self.failure == "auth":
            return '{"type":"auth_required"}'
        if self.failure == "count":
            return '{"type":"heartbeat"}'
        return json.dumps(self.frames.pop(0))


def test_wire_uses_browser_connection_submission_and_request_fences():
    socket = Socket()
    browser = probe.BrowserWire(socket, "synthetic-token")
    frames = browser.action("chat_message", {"message": "synthetic"}, chat_id=CHAT)
    sent = socket.sent[-1]
    assert sent["connection_generation"] == browser.generation
    assert sent["payload"]["request_generation"] == sent["request_generation"]
    assert sent["payload"]["submission_id"] == sent["submission_id"]
    assert sent["session_id"] == sent["payload"]["chat_id"] == CHAT
    assert frames[-1]["state"] == "completed"
    assert probe.canonical_uuid(None) is False and probe.canonical_uuid("invalid") is False
    with pytest.raises(probe.ProbeError, match="chat identity"):
        browser.action("chat_message", {}, chat_id="foreign-path")


@pytest.mark.parametrize("failure", ["timeout", "binary", "shape", "auth", "count", "terminal-owner", "terminal-failed"])
def test_wire_refuses_false_success(failure):
    with pytest.raises(probe.ProbeError):
        browser = probe.BrowserWire(Socket(failure), "synthetic-token")
        browser.action("chat_message", {}, chat_id=CHAT)


def test_wire_expired_collection_window():
    browser = probe.BrowserWire(Socket(), "synthetic-token")
    times = iter([0, 2])
    browser.clock = lambda: next(times)
    with pytest.raises(probe.ProbeError, match="deadline"):
        browser.until(lambda frame: True, timeout=1)


def provider_file(tmp_path, config=None, identifier=ID):
    path = tmp_path / "provider.json"
    path.write_text(json.dumps({"qualification_id": identifier, "config": config if config is not None else CONFIG}))
    path.chmod(0o600)
    return path


def test_provider_input_is_namespace_bound_and_private(tmp_path):
    path = provider_file(tmp_path)
    assert probe.approved_provider(path, ID) == CONFIG
    with pytest.raises(probe.ProbeError, match="namespace"):
        probe.approved_provider(path, "b" * 32)
    with pytest.raises(probe.ProbeError, match="private"):
        probe.approved_provider(path.parent / "missing", ID)


@pytest.mark.parametrize("config", [{}, dict(CONFIG, model=""), dict(CONFIG, api_key=7),
    dict(CONFIG, base_url="http://provider.invalid"), dict(CONFIG, base_url="https://user:secret@provider.invalid"),
    dict(CONFIG, base_url="https://provider.invalid/?key=secret")])
def test_provider_input_refuses_malformed_or_insecure_values(tmp_path, config):
    with pytest.raises(probe.ProbeError):
        probe.approved_provider(provider_file(tmp_path, config), ID)


class Application:
    def __init__(self, failure=None, audit_event=None):
        self.failure, self.actions = failure, []
        self.audit_queries = []
        self.audit_event = {
            "event_id": "33333333-3333-4333-8333-333333333333",
            "event_class": "llm_config_change", "action_type": "llm_config.created",
            "description": "User saved their personal LLM configuration (test)",
            "agent_id": None, "conversation_id": None,
            "correlation_id": "44444444-4444-4444-8444-444444444444",
            "outcome": "success", "outcome_detail": None,
            "inputs_meta": {"action": "created", "transport": "ws", "scope": "user",
                            "base_url": CONFIG["base_url"], "model": CONFIG["model"]},
            "outputs_meta": {}, "artifact_pointers": [],
            "started_at": "2026-10-17T00:00:00Z", "completed_at": "2026-10-17T00:00:00Z",
            "recorded_at": "2026-10-17T00:00:00Z", **(audit_event or {}),
        }

    def handle(self, request):
        path = request.url.path
        code, payload = 200, {}
        if path == "/auth/session":
            payload = {"authenticated": True, "user_id": "fixture-owner-a", "access_token": "current-token"}
        elif path == "/api/chats":
            code, payload = 201, {"chat_id": CHAT}
            if self.failure == "create":
                code = 500
        elif path.startswith("/api/chats/"):
            payload = {"chat": {"id": CHAT, "title": "Synthetic conversation", "agent_id": None,
                                "updated_at": 1011, "messages": [
                                    {"role": "user", "content": probe.MESSAGE, "timestamp": 1010},
                                    {"role": "assistant", "content": "ORBITAL turquoise otter", "timestamp": 1011},
                                ]}}
            if self.failure == "read":
                code = 404
            if self.failure == "retention":
                payload["chat"]["messages"] = []
        elif path == "/api/upload":
            assert probe.ATTACHMENT in request.content
            code, payload = 201, {"attachment_id": FILE, "sha256": hashlib.sha256(probe.ATTACHMENT).hexdigest()}
            if self.failure == "upload":
                code = 500
            if self.failure == "attachment-id":
                payload["attachment_id"] = "invalid"
        elif path == "/api/audit":
            self.audit_queries.append(dict(request.url.params))
            payload = {"items": [self.audit_event]}
            if self.failure == "audit":
                code = 503
            if self.failure == "audit-missing":
                payload["items"] = []
        else:
            raise AssertionError("unexpected route")
        return httpx.Response(code, json=payload)

    @contextmanager
    def wire(self):
        yield self

    def action(self, name, payload, **kwargs):
        self.actions.append((name, payload, kwargs))
        if name == "chrome_llm_save":
            assert payload["fields"]["data_sharing_acknowledged"] is True
            return []
        if self.failure == "snapshot":
            return []
        return [{"type": "conversation_snapshot", "chat_id": CHAT,
                 "snapshot_purpose": "hydration" if name == "load_chat" else "commit",
                 "transcript": ["synthetic"],
                 "canvas": {"components": []} if self.failure != "hydration" or name != "load_chat" else {}}]


class ExpiringApplication(Application):
    def __init__(self, refresh_failure=None, read_status=None):
        super().__init__()
        self.refresh_failure = refresh_failure
        self.read_status = read_status
        self.current_token = "original-access-token"
        self.generation = 0
        self.refreshes = 0
        self.business_requests = []
        self.rejected_bearers = 0

    def advance_token(self):
        self.generation += 1
        self.current_token = "current-access-token-" + str(self.generation)

    def action(self, *args, **kwargs):
        frames = super().action(*args, **kwargs)
        if args[0] != "chrome_llm_save":
            self.advance_token()
        return frames

    def handle(self, request):
        if request.url.path == "/auth/session":
            self.refreshes += 1
            assert "Authorization" not in request.headers
            assert request.headers["Cookie"] == "astral_session=synthetic-session"
            payload = {"authenticated": True, "user_id": "fixture-owner-a", "access_token": self.current_token}
            if self.refreshes == 2:
                if self.refresh_failure == "transport":
                    raise httpx.ConnectError("synthetic refresh transport failure", request=request)
                if self.refresh_failure == "status":
                    return httpx.Response(503, json={"detail": "unavailable"})
                if self.refresh_failure == "malformed":
                    return httpx.Response(200, content=b"not-json")
                if self.refresh_failure == "shape":
                    payload = []
                if self.refresh_failure == "owner":
                    payload["user_id"] = "fixture-owner-b"
                if self.refresh_failure == "no-owner":
                    payload.pop("user_id")
                if self.refresh_failure == "expired":
                    payload["authenticated"] = False
                if self.refresh_failure == "truthy-auth":
                    payload["authenticated"] = "true"
                if self.refresh_failure == "no-token":
                    payload.pop("access_token")
                if self.refresh_failure == "empty-token":
                    payload["access_token"] = ""
                if self.refresh_failure == "blank-token":
                    payload["access_token"] = "   "
                if self.refresh_failure == "token-type":
                    payload["access_token"] = 7
            return httpx.Response(200, json=payload)
        self.business_requests.append((request.method, request.url.path))
        if request.headers.get("Authorization") != "Bearer " + self.current_token:
            self.rejected_bearers += 1
            return httpx.Response(401, json={"detail": "Token expired"})
        if request.url.path.startswith("/api/chats/") and self.read_status is not None:
            return httpx.Response(self.read_status, json={"detail": "Read refused"})
        response = super().handle(request)
        self.advance_token()
        return response


def test_completed_socket_actions_refresh_cookie_session_before_each_http_use():
    app = ExpiringApplication()
    with httpx.Client(transport=httpx.MockTransport(app.handle),
                      headers={"Authorization": "Bearer original-access-token"},
                      cookies={"astral_session": "synthetic-session"}) as client:
        report = probe.exercise(client, CONFIG, ID, open_wire=app.wire)
    assert report["real_provider_chat"] == report["workspace_reconnection"] == "passed"
    assert app.rejected_bearers == 0
    assert app.refreshes == len(app.business_requests) == 7
    assert app.actions[-1][0] == "chat_message"
    assert "access-token" not in json.dumps(report)


@pytest.mark.parametrize("failure", ["transport", "status", "malformed", "shape", "owner", "no-owner", "expired",
                                     "truthy-auth", "no-token", "empty-token", "blank-token", "token-type"])
def test_session_refresh_failure_stops_before_post_socket_http_delivery(failure):
    app = ExpiringApplication(failure)
    with httpx.Client(transport=httpx.MockTransport(app.handle),
                      headers={"Authorization": "Bearer original-access-token"},
                      cookies={"astral_session": "synthetic-session"}) as client:
        with pytest.raises((probe.ProbeError, httpx.ConnectError, ValueError)):
            probe.exercise(client, CONFIG, ID, open_wire=app.wire)
        assert "Authorization" not in client.headers
    assert app.refreshes == 2
    assert app.business_requests == [("POST", "/api/chats")]
    assert app.rejected_bearers == 0
    assert [name for name, *_ in app.actions] == ["chrome_llm_save", "chat_message"]


@pytest.mark.parametrize("status", [401, 403, 503])
def test_fresh_session_does_not_retry_or_mask_a_refused_business_read(status):
    app = ExpiringApplication(read_status=status)
    with httpx.Client(transport=httpx.MockTransport(app.handle),
                      headers={"Authorization": "Bearer original-access-token"},
                      cookies={"astral_session": "synthetic-session"}) as client:
        with pytest.raises(probe.ProbeError, match="completed chat could not be restored"):
            probe.exercise(client, CONFIG, ID, open_wire=app.wire)
    assert app.refreshes == 2 and app.rejected_bearers == 0
    assert app.business_requests == [("POST", "/api/chats"), ("GET", "/api/chats/" + CHAT)]
    assert [name for name, *_ in app.actions] == ["chrome_llm_save", "chat_message"]


def test_functional_probe_checks_real_paths_but_receipt_contains_no_credentials():
    app = Application()
    with httpx.Client(transport=httpx.MockTransport(app.handle)) as client:
        report = probe.exercise(client, CONFIG, ID, open_wire=app.wire)
    assert report["release_authorized"] is False and report["real_provider_chat"] == "passed"
    assert CONFIG["api_key"] not in json.dumps(report)
    assert [action[0] for action in app.actions] == ["chrome_llm_save", "chat_message", "load_chat", "chat_message"]


@pytest.mark.parametrize("action", ["created", "updated"])
def test_provider_audit_accepts_the_real_saved_event_class_and_action(action):
    app = Application(audit_event={"action_type": "llm_config." + action,
                                  "inputs_meta": {"action": action, "scope": "user", "transport": "ws"}})
    with httpx.Client(transport=httpx.MockTransport(app.handle)) as client:
        report = probe.exercise(client, CONFIG, ID, open_wire=app.wire)
    assert report["provider_audit_observed"] == "passed"
    assert app.audit_queries == [{"limit": "200", "event_class": "llm_config_change", "outcome": "success"}]


@pytest.mark.parametrize("change", [
    {"event_class": "llm_call"}, {"event_class": None},
    {"action_type": "llm_config_change"}, {"action_type": "llm_config.tested"},
    {"action_type": "llm_config.cleared"}, {"action_type": None},
    {"outcome": "failure"}, {"outcome": None},
])
def test_provider_audit_rejects_unrelated_or_unsaved_events(change):
    app = Application(audit_event=change)
    with httpx.Client(transport=httpx.MockTransport(app.handle)) as client, pytest.raises(
        probe.ProbeError, match="provider settings produced no retained audit event"
    ):
        probe.exercise(client, CONFIG, ID, open_wire=app.wire)


@pytest.mark.parametrize("content", ["An orbital response", [{"type": "text", "text": "An orbital response"}]])
def test_retained_history_accepts_matching_nested_assistant_content(content):
    document = {"chat": {"id": CHAT, "title": "Synthetic conversation", "agent_id": None,
                         "updated_at": 1011, "messages": [
                             {"role": "user", "content": probe.MESSAGE, "timestamp": 1010},
                             {"role": "assistant", "content": content, "timestamp": 1011},
                         ]}}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document))) as client:
        assert probe.retained_chat(client, CHAT, "ORBITAL") == document


@pytest.mark.parametrize("document", [
    [], {}, {"chat": None}, {"chat": []},
    {"messages": [{"role": "assistant", "content": "ORBITAL"}]},
    {"chat": {"id": FILE, "messages": [{"role": "assistant", "content": "ORBITAL"}]}},
    {"chat": {"messages": [{"role": "assistant", "content": "ORBITAL"}]}},
    {"chat": {"id": CHAT}},
    {"chat": {"id": CHAT, "messages": None}},
    {"chat": {"id": CHAT, "messages": {"role": "assistant", "content": "ORBITAL"}}},
    {"chat": {"id": CHAT, "messages": "ORBITAL"}},
    {"chat": {"id": CHAT, "messages": []}},
    {"chat": {"id": CHAT, "messages": [None]}},
    {"chat": {"id": CHAT, "messages": ["ORBITAL"]}},
    {"chat": {"id": CHAT, "messages": [{"role": "assistant", "content": "ORBITAL"}, None]}},
    {"chat": {"id": CHAT, "messages": [{"role": "user", "content": "ORBITAL"}]}},
    {"chat": {"id": CHAT, "messages": [{"role": "assistant", "content": "unrelated"}]}},
    {"chat": {"id": CHAT, "messages": [{"role": "assistant", "preview": "ORBITAL"}]}},
    {"chat": {"id": CHAT, "title": "ORBITAL", "preview": "ORBITAL", "messages": []}},
    {"chat": {"id": CHAT, "messages": []}, "messages": [{"role": "assistant", "content": "ORBITAL"}]},
])
def test_malformed_or_unrelated_retained_history_cannot_pass(document):
    with (httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document))) as client,
          pytest.raises(probe.ProbeError)):
        probe.retained_chat(client, CHAT, "ORBITAL")


@pytest.mark.parametrize("failure", ["create", "read", "retention", "upload", "attachment-id", "audit",
                                     "audit-missing", "snapshot", "hydration"])
def test_missing_functional_behavior_refuses_receipt(failure):
    app = Application(failure)
    with httpx.Client(transport=httpx.MockTransport(app.handle)) as client, pytest.raises(probe.ProbeError):
        probe.exercise(client, CONFIG, ID, open_wire=app.wire)


def test_network_wire_uses_tls_and_refreshes_browser_session(monkeypatch):
    @contextmanager
    def connection(url, **kwargs):
        assert url == "wss://ad-bwq-web:9443/ws" and kwargs["origin"] == probe.WEB
        assert kwargs["ssl"] == "verified-context"
        yield Socket()
    monkeypatch.setattr(probe, "connect", connection)
    monkeypatch.setattr(probe, "session", lambda client: {"authenticated": True, "user_id": "fixture-owner-a", "access_token": "new-token"})
    with httpx.Client() as client, probe.wire(client, "verified-context") as browser:
        assert client.headers["Authorization"] == "Bearer new-token"
        assert browser.generation
    monkeypatch.setattr(probe, "session", lambda client: {"authenticated": False})
    with httpx.Client() as client, pytest.raises(probe.ProbeError):
        with probe.wire(client, "verified-context"):
            pass


def test_cli_safe_output_and_exclusive_receipt(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(probe, "read_identities", lambda *args: {"owner-a": "synthetic-password"})
    monkeypatch.setattr(probe.ssl, "create_default_context", lambda **kwargs: object())
    monkeypatch.setattr(probe, "login", lambda *args: None)
    monkeypatch.setattr(probe, "exercise", lambda *args, **kwargs: {"release_authorized": False})
    output = tmp_path / "receipt.json"
    args = ["--qualification-id", ID, "--material-root", str(tmp_path), "--approved-provider",
            str(provider_file(tmp_path)), "--output", str(output)]
    assert probe.main(args) == 0
    assert probe.main(args) == 1
    assert CONFIG["api_key"] not in capsys.readouterr().out
    output.unlink()
    monkeypatch.setattr(probe, "exercise", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sensitive-key")))
    assert probe.main(args) == 1 and not output.exists()
    assert "sensitive-key" not in capsys.readouterr().out
