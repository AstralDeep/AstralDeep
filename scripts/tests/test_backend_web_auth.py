"""Tests for scripts/probe_backend_web_auth.py: real protocol sequencing, cross-owner
leak rejection, redirect/credential safety, and CLI output redaction.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import httpx
import pytest

from scripts import probe_backend_web_auth as probe

ID = "a" * 32
CHAT = "11111111-1111-4111-8111-111111111111"
ATTACHMENT = "22222222-2222-4222-8222-222222222222"
CONTENTS = b"Synthetic backend/browser qualification attachment.\n"
IDENTITIES = {name: "synthetic-fixture-password-only" for name in ("owner-a", "owner-b", "administrator")}


class Service:
    def __init__(self, username, failure=None):
        self.username, self.failure = username, failure
        self.logged_in = False
        self.sessions = 0

    def handle(self, request):
        path = request.url.path
        status, payload, headers = 200, {}, {}
        text = None
        if path == "/auth/session":
            self.sessions += 1
            payload = {"authenticated": self.logged_in, "user_id": "fixture-" + self.username,
                       "access_token": "synthetic-access-a" if self.sessions <= 3 or self.failure == "no-renewal" else "synthetic-access-b"}
            if not self.logged_in:
                payload = {"authenticated": False}
            if self.failure == "session-status":
                status = 503
            if self.failure == "session-shape":
                payload = []
            if self.failure == "initial-auth":
                payload = {"authenticated": True}
            if self.failure == "wrong-owner" and self.logged_in:
                payload["user_id"] = "foreign"
        elif path == "/auth/login":
            status = 302
            headers["location"] = probe.IAM + f"/realms/astral-bwq-{ID}/protocol/openid-connect/auth?code_challenge_method=S256&code_challenge=synthetic&state=bound"
            if self.failure == "login-status":
                status = 503
            if self.failure == "pkce":
                headers["location"] = headers["location"].replace("S256", "plain")
        elif path.endswith("/openid-connect/auth"):
            text = '<form id="kc-form-login" action="' + probe.IAM + f'/realms/astral-bwq-{ID}/login-actions/authenticate"><input type="hidden" name="state" value="bound"></form>'
            if self.failure == "form-status":
                status = 503
            if self.failure == "form-missing":
                text = "unavailable"
        elif path.endswith("/login-actions/authenticate"):
            assert b"username=" in request.content and b"password=" in request.content
            status = 302
            headers["location"] = probe.WEB + "/auth/callback?code=synthetic-code&state=bound"
            if self.failure == "credentials":
                status = 200
            if self.failure == "callback-path":
                headers["location"] = probe.WEB + "/auth/callback-other"
        elif path == "/auth/callback":
            if request.url.params.get("state") == "unissued":
                text = "Sign-in problem: invalid callback"
                if self.failure == "state-accepted":
                    text = "welcome"
            else:
                self.logged_in = True
                status, headers = 303, {"location": "/"}
                if self.failure == "exchange":
                    status = 401
        elif path == "/api/chats" and request.method == "POST":
            status, payload = 201, {"chat_id": CHAT}
            if self.failure == "create":
                status = 403
            if self.failure == "chat-identity":
                payload["chat_id"] = "../outside"
        elif path == "/api/chats":
            payload = {"chats": [{"chat_id": CHAT}] if self.username == "owner-a" else []}
            if self.failure == "list-status":
                status = 503
            if self.failure == "list-leak":
                payload = {"chats": [{"chat_id": CHAT}]}
        elif path.startswith("/api/chats/"):
            status = 200 if self.logged_in and self.username == "owner-a" else (404 if self.logged_in else 401)
            if self.failure == "owner-read":
                status = 404
            if self.failure == "foreign-read" or (self.failure == "foreign-steps" and path.endswith("/steps")):
                status = 200
            if self.failure == "signed-out-read" and not self.logged_in:
                status = 200
        elif path == "/api/upload":
            assert CONTENTS in request.content
            status = 201 if self.failure != "upload-status" else 503
            payload = {"attachment_id": ATTACHMENT, "sha256": hashlib.sha256(CONTENTS).hexdigest(), "size_bytes": len(CONTENTS)}
            if self.failure == "upload-digest":
                payload["sha256"] = "invalid"
        elif path == "/api/attachments":
            payload = {"attachments": [] if self.failure != "attachment-list-leak" else [{"attachment_id": ATTACHMENT}]}
        elif path.startswith("/api/attachments/"):
            status = 200 if self.username == "owner-a" else 404
            payload = {"sha256": hashlib.sha256(CONTENTS).hexdigest()}
            if self.failure == "attachment-restore":
                payload["sha256"] = "invalid"
            if self.failure == "attachment-read-leak":
                status = 200
            if self.failure == "attachment-delete-leak" and request.method == "DELETE":
                status = 202
        elif path == "/auth/logout":
            if self.failure != "logout-retained":
                self.logged_in = False
            status = 303 if self.failure != "logout-status" else 500
        else:
            raise AssertionError("unexpected test request")
        return httpx.Response(status, text=text, headers=headers) if text is not None else httpx.Response(status, json=payload, headers=headers)


def exercise(failure=None, foreign_failure=None, **kwargs):
    with (httpx.Client(transport=httpx.MockTransport(Service("owner-a", failure).handle)) as owner,
          httpx.Client(transport=httpx.MockTransport(Service("owner-b", foreign_failure).handle)) as foreign):
        return probe.exercise((owner, foreign), IDENTITIES, ID, **kwargs)


def test_actual_protocol_sequence_and_evidence_excludes_tokens():
    report = exercise()
    assert report["application_session_renewal"] == "passed"
    assert report["release_authorized"] is False
    assert "synthetic-access" not in json.dumps(report)
    assert "password" not in json.dumps(report)


@pytest.mark.parametrize("failure", ["session-status", "session-shape", "initial-auth", "wrong-owner",
    "login-status", "pkce", "form-status", "form-missing", "credentials", "callback-path", "exchange",
    "state-accepted", "create", "chat-identity", "owner-read", "list-status", "signed-out-read",
    "logout-retained", "logout-status", "upload-status", "upload-digest", "attachment-restore"])
def test_authentication_or_protocol_failure_cannot_pass(failure):
    with pytest.raises(probe.ProbeError):
        exercise(failure)


@pytest.mark.parametrize("failure", ["foreign-read", "foreign-steps", "list-leak", "attachment-read-leak",
    "attachment-delete-leak", "attachment-list-leak"])
def test_cross_owner_leaks_cannot_pass(failure):
    with pytest.raises(probe.ProbeError):
        exercise(foreign_failure=failure)


def test_renewal_uses_bounded_wait_without_session_mutation():
    ticks = iter([0, 1, 151])
    waits = []
    with pytest.raises(probe.ProbeError, match="renewal"):
        exercise("no-renewal", clock=lambda: next(ticks), wait=waits.append)
    assert waits == [2]


@pytest.mark.parametrize("url", ["http://ad-bwq-keycloak:8443/realms/test", "https://evil.invalid/realms/test",
    "https://user:secret@ad-bwq-keycloak:8443/realms/test", "https://ad-bwq-keycloak:8443/other",
    "https://ad-bwq-keycloak:8443/realms/test#fragment"])
def test_credentials_never_follow_untrusted_redirect(url):
    with pytest.raises(probe.ProbeError):
        probe.bound_url(url, probe.IAM, "/realms/")


def test_form_ignores_fields_outside_login_form():
    form = probe.LoginForm('<input type="hidden" name="bad"><form id="unrelated"><input type="hidden" name="also_bad"></form><form id="kc-form-login" action="/login"><input type="hidden" name="good" value="bound"></form><input type="hidden" name="trailing">')
    assert form.action == "/login" and form.fields == {"good": "bound"}


def materials(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/qualification.json").write_text(json.dumps({"schema_version": 1, "qualification_id": ID, "classification": "synthetic"}))
    (tmp_path / "app/ca-bundle.pem").write_text("synthetic")
    (tmp_path / "test-identities.json").write_text(json.dumps({"users": IDENTITIES}))
    for path in tmp_path.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    return tmp_path


def test_materials_are_bound_to_exact_synthetic_identity(tmp_path):
    root = materials(tmp_path)
    assert probe.read_identities(root, ID) == IDENTITIES
    with pytest.raises(probe.ProbeError):
        probe.read_identities(root, "b" * 32)
    with pytest.raises(probe.ProbeError):
        probe.read_identities(root, "../unsafe")
    with pytest.raises(probe.ProbeError):
        probe.read_identities(Path("relative"), ID)
    (root / "test-identities.json").write_text('{"users": {}}')
    with pytest.raises(probe.ProbeError):
        probe.read_identities(root, ID)
    (root / "app/qualification.json").unlink()
    with pytest.raises(probe.ProbeError):
        probe.read_identities(root, ID)


def test_main_reports_only_safe_result_and_refuses_overwrite(tmp_path, monkeypatch, capsys):
    root = materials(tmp_path)
    monkeypatch.setattr(probe.ssl, "create_default_context", lambda **kwargs: object())
    monkeypatch.setattr(probe, "exercise", lambda *args: {"synthetic_only": True, "release_authorized": False})
    output = tmp_path / "result.json"
    args = ["--qualification-id", ID, "--material-root", str(root), "--output", str(output)]
    assert probe.main(args) == 0
    before = output.read_bytes()
    assert probe.main(args) == 1 and output.read_bytes() == before
    assert "password" not in capsys.readouterr().out


def test_main_does_not_print_network_secret(tmp_path, monkeypatch, capsys):
    root = materials(tmp_path)
    monkeypatch.setattr(probe.ssl, "create_default_context", lambda **kwargs: object())
    monkeypatch.setattr(probe, "exercise", lambda *args: (_ for _ in ()).throw(ValueError("sensitive-code")))
    assert probe.main(["--qualification-id", ID, "--material-root", str(root), "--output", str(tmp_path / "result.json")]) == 1
    assert "sensitive-code" not in capsys.readouterr().out
    assert not (tmp_path / "result.json").exists()
