"""Tests for orchestrator/web_auth.py: the _validate_next open-redirect guard,
shell_gate's redirect decision for GET /, the bounded /auth/error page, and mock-mode
role derivation.
"""

import time
import uuid
from urllib.parse import quote

from orchestrator import web_auth


class _FakeURL:
    def __init__(self, path="/", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, cookies=None, path="/", query="", base_url="http://localhost:8001/"):
        self.cookies = cookies or {}
        self.url = _FakeURL(path, query)
        self.base_url = base_url


def test_validate_next_allows_root():
    assert web_auth._validate_next("/") == "/"


def test_validate_next_allows_relative_path_with_query():
    assert web_auth._validate_next("/x?y=1") == "/x?y=1"


def test_validate_next_rejects_protocol_relative_url():
    assert web_auth._validate_next("//evil.com") == "/"


def test_validate_next_rejects_absolute_url():
    assert web_auth._validate_next("https://evil.com") == "/"


def test_validate_next_rejects_javascript_scheme():
    assert web_auth._validate_next("javascript:alert(1)") == "/"


def test_validate_next_rejects_backslash_forms():
    assert web_auth._validate_next("/\\evil.com") == "/"
    assert web_auth._validate_next("\\/evil.com") == "/"
    assert web_auth._validate_next("\\\\evil.com") == "/"
    assert web_auth._validate_next("/\\/evil.com") == "/"


def test_validate_next_rejects_colon_in_path():
    assert web_auth._validate_next("/https://evil.com") == "/"
    assert web_auth._validate_next("/chat?at=10:30") == "/chat?at=10:30"


def test_validate_next_empty_falls_back_to_root():
    assert web_auth._validate_next("") == "/"
    assert web_auth._validate_next(None) == "/"
    assert web_auth._validate_next("   ") == "/"


def test_shell_gate_mock_mode_never_gates(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    assert web_auth.shell_gate(_FakeRequest()) is None


def test_shell_gate_unauthenticated_redirects_preserving_deep_link(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    chat_id = str(uuid.uuid4())
    req = _FakeRequest(path="/", query=f"chat={chat_id}")
    target = web_auth.shell_gate(req)
    assert target == "/auth/login?next=" + quote(f"/?chat={chat_id}", safe="")
    assert target.startswith("/auth/login?next=%2F")
    assert f"chat={chat_id}" not in target.split("next=", 1)[0]


def test_shell_gate_unauthenticated_plain_root(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    assert web_auth.shell_gate(_FakeRequest(path="/")) == "/auth/login?next=%2F"


def test_shell_gate_valid_session_passes(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    sid = f"sess-{uuid.uuid4()}"
    web_auth._SESSIONS[sid] = {
        "access_token": "tok-abc",
        "refresh_token": "r",
        "sub": f"user-{uuid.uuid4()}",
        "created_at": time.time(),
    }
    try:
        req = _FakeRequest(cookies={web_auth.COOKIE_NAME: web_auth._sign(sid)},
                           path="/", query="chat=123")
        assert web_auth.shell_gate(req) is None
    finally:
        web_auth._SESSIONS.pop(sid, None)


def test_shell_gate_tampered_cookie_gates(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    sid = f"sess-{uuid.uuid4()}"
    web_auth._SESSIONS[sid] = {
        "access_token": "tok-abc", "refresh_token": "r",
        "sub": f"user-{uuid.uuid4()}", "created_at": time.time(),
    }
    try:
        req = _FakeRequest(cookies={web_auth.COOKIE_NAME: web_auth._sign(sid) + "x"})
        assert web_auth.shell_gate(req) == "/auth/login?next=%2F"
    finally:
        web_auth._SESSIONS.pop(sid, None)


def test_error_page_bounded_with_retry_and_no_auto_redirect():
    reason = 'IdP said <no> & "denied"'
    resp = web_auth._error_page("/chat?x=1", reason)
    body = resp.body.decode("utf-8")
    assert resp.status_code == 200
    assert "IdP said &lt;no&gt; &amp; &quot;denied&quot;" in body
    assert "<no>" not in body
    assert '/auth/login?next=%2Fchat%3Fx%3D1' in body
    assert "http-equiv" not in body.lower()
    assert "<script" not in body.lower()


def test_error_page_sanitizes_malicious_next():
    resp = web_auth._error_page("https://evil.com", "boom")
    body = resp.body.decode("utf-8")
    assert '/auth/login?next=%2F"' in body
    assert "evil.com" not in body


def test_session_roles_mock_mode(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    assert web_auth.session_roles(_FakeRequest()) == ["admin", "user"]
