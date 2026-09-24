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
    def __init__(self, failure=None):
        self.failure, self.actions = failure, []

    def handle(self, request):
        path = request.url.path
        code, payload = 200, {}
        if path == "/api/chats":
            code, payload = 201, {"chat_id": CHAT}
            if self.failure == "create":
                code = 500
        elif path.startswith("/api/chats/"):
            payload = {"messages": [{"role": "assistant", "content": "ORBITAL turquoise otter"}]}
            if self.failure == "read":
                code = 404
            if self.failure == "retention":
                payload["messages"] = []
        elif path == "/api/upload":
            assert probe.ATTACHMENT in request.content
            code, payload = 201, {"attachment_id": FILE, "sha256": hashlib.sha256(probe.ATTACHMENT).hexdigest()}
            if self.failure == "upload":
                code = 500
            if self.failure == "attachment-id":
                payload["attachment_id"] = "invalid"
        elif path == "/api/audit":
            payload = {"items": [{"action_type": "llm_config_change"}]}
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


def test_functional_probe_checks_real_paths_but_receipt_contains_no_credentials():
    app = Application()
    with httpx.Client(transport=httpx.MockTransport(app.handle)) as client:
        report = probe.exercise(client, CONFIG, ID, open_wire=app.wire)
    assert report["release_authorized"] is False and report["real_provider_chat"] == "passed"
    assert CONFIG["api_key"] not in json.dumps(report)
    assert [action[0] for action in app.actions] == ["chrome_llm_save", "chat_message", "load_chat", "chat_message"]


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
