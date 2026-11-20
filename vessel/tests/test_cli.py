# The command line: every command parses, and the commands reach the operator functions without Docker.

from __future__ import annotations

import pytest
from conftest import needs_posix_modes

from astralvessel import caller as callers
from astralvessel import cli, node, operate


def run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["astralvessel", *argv])
    cli.main()


def test_commands_reach_their_operator_functions(monkeypatch):
    called = []
    for name in ("up", "check", "down", "pair", "ask", "chat"):
        monkeypatch.setattr(operate, name, lambda args, name=name: called.append((name, args)))
    run(monkeypatch, "up", "--port", "9000", "--example")
    run(monkeypatch, "check", "--isolation-only")
    run(monkeypatch, "down", "--volumes")
    run(monkeypatch, "pair", "--executor")
    run(monkeypatch, "ask", "hello?", "--ticket-file", "t")
    run(monkeypatch, "chat")
    names = [n for n, _ in called]
    assert names == ["up", "check", "down", "pair", "ask", "chat"]
    up, check, down, pair, ask, _ = (a for _, a in called)
    assert check.isolation_only
    assert (up.port, up.example) == (9000, True) and not hasattr(up, "issuer")
    assert down.volumes and pair.executor and ask.question == "hello?"
    assert ask.ticket_file == "t" and ask.key_file is None and ask.token_file is None


@pytest.mark.parametrize("argv", [("up", "--issuer", "x"), ("up", "--client-id", "x"), ("up", "--subject", "x"),
                                  ("pair", "--label", "x")])
def test_the_removed_owner_options_are_refused(monkeypatch, argv):
    with pytest.raises(SystemExit):
        run(monkeypatch, *argv)


def test_an_unknown_command_or_a_missing_question_is_refused(monkeypatch):
    with pytest.raises(SystemExit):
        run(monkeypatch, "serve")
    with pytest.raises(SystemExit):
        run(monkeypatch, "ask")


def test_deploy_writes_compose_and_prints_the_start_command(monkeypatch, tmp_path, capsys):
    (tmp_path / "deployment.toml").write_text(
        '[executor]\nkernel_artifacts = "kernel"\n')
    (tmp_path / "kernel").mkdir()
    (tmp_path / "kernel" / "astral_kernel-0.2.0-py3-none-manylinux_2_36_aarch64.whl").write_bytes(b"w")
    (tmp_path / ".env").write_text("VESSEL_SANDBOX_TOKEN=0123456789abcdef0123\n")
    monkeypatch.delenv("VESSEL_SANDBOX_TOKEN", raising=False)
    run(monkeypatch, "deploy", str(tmp_path / "deployment.toml"))
    assert (tmp_path / "compose.generated.yml").exists()
    assert "start it with: docker compose -f" in capsys.readouterr().out


def test_deploy_can_start_it(monkeypatch, tmp_path):
    (tmp_path / "deployment.toml").write_text(
        '[executor]\nkernel_artifacts = "kernel"\n')
    (tmp_path / "kernel").mkdir()
    (tmp_path / "kernel" / "astral_kernel-0.2.0-py3-none-manylinux_2_36_aarch64.whl").write_bytes(b"w")
    monkeypatch.setenv("VESSEL_SANDBOX_TOKEN", "0123456789abcdef0123")
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **kw: ran.append(command))
    run(monkeypatch, "deploy", str(tmp_path / "deployment.toml"), "--up", "--out", str(tmp_path / "c.yml"))
    assert ran[0][-4:] == ["up", "-d", "--build", "--remove-orphans"] and ran[0][3] == str(tmp_path / "c.yml")


def test_the_sandbox_needs_a_package(monkeypatch):
    monkeypatch.delenv("VESSEL_PACKAGE", raising=False)
    with pytest.raises(SystemExit, match="agent package"):
        run(monkeypatch, "sandbox")


@needs_posix_modes
def test_ask_prints_the_answer_and_chat_keeps_its_context(monkeypatch, tmp_path, capsys):
    (tmp_path / "deployment.toml").write_text("")
    (tmp_path / "compose.generated.yml").write_text("services: {}\n")
    ticket = tmp_path / "ticket"
    ticket.write_text(callers.TICKET_TEXT + callers.b64(b'{"id": "t1"}') + "\n")
    callers.create_key(tmp_path)
    seen = []

    def converse(section, caller, text, context=None):
        seen.append((caller.ticket_id, text, context))
        if text == "boom":
            raise node.NodeError("the node said no")
        return f"echo {text}", "ctx-9"

    monkeypatch.setattr(node, "converse", converse)
    base = ["--deployment", str(tmp_path / "deployment.toml"), "--ticket-file", str(ticket)]
    run(monkeypatch, "ask", "hi", *base)
    assert capsys.readouterr().out == "echo hi\n"
    lines = iter(["one", "", "boom", "two"])

    def line(prompt=""):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", line)
    run(monkeypatch, "chat", *base)
    printed = capsys.readouterr().out
    assert "node> echo one" in printed and "error: the node said no" in printed and "node> echo two" in printed
    assert [context for _, _, context in seen[1:]] == [None, "ctx-9", "ctx-9"]
    assert {who for who, _, _ in seen} == {"t1"}
    with pytest.raises(SystemExit, match="the node said no"):
        run(monkeypatch, "ask", "boom", *base)


@needs_posix_modes
def test_conformance_passes_the_caller_and_an_offer_reader_to_the_suite(monkeypatch, tmp_path, capsys):
    (tmp_path / "deployment.toml").write_text("")
    (tmp_path / "compose.generated.yml").write_text("services: {}\n")
    (tmp_path / "ticket").write_text(callers.TICKET_TEXT + callers.b64(b'{"id": "t1"}'))
    monkeypatch.setenv(callers.TICKET_FILE_VARIABLE, str(tmp_path / "ticket"))
    callers.create_key(tmp_path)
    got = {}

    def suite(deployment, caller, offer):
        got.update(caller=caller.ticket_id, offer=offer)
        return got.pop("fail", [])

    monkeypatch.setattr(operate.conformance, "run", suite)
    run(monkeypatch, "conformance", "--deployment", str(tmp_path / "deployment.toml"))
    assert got["caller"] == "t1" and callable(got["offer"])
    assert "passed, 0 failed" in capsys.readouterr().out
    monkeypatch.setattr(operate.conformance, "run", lambda *a: ["x"])
    with pytest.raises(SystemExit):
        run(monkeypatch, "conformance", "--deployment", str(tmp_path / "deployment.toml"))


def test_pair_prints_the_offer_and_explains_a_refusal(monkeypatch, tmp_path, capsys):
    (tmp_path / "deployment.toml").write_text("")
    (tmp_path / "compose.generated.yml").write_text("services: {}\n")
    replies = [{"status": 200, "state": "offer", "code": "9999 0000", "payload": "PAY", "expires_ms": 5000},
               {"status": 403, "detail": "node.loopback_required"}]
    monkeypatch.setattr(operate, "node_offer", lambda stack: replies.pop(0))
    run(monkeypatch, "pair", "--deployment", str(tmp_path / "deployment.toml"))
    assert "9999 0000" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="own machine"):
        run(monkeypatch, "pair", "--deployment", str(tmp_path / "deployment.toml"))


@needs_posix_modes
def test_ask_without_a_key_or_ticket_says_what_to_run(monkeypatch, tmp_path):
    (tmp_path / "deployment.toml").write_text("")
    (tmp_path / "compose.generated.yml").write_text("services: {}\n")
    monkeypatch.delenv(callers.TICKET_FILE_VARIABLE, raising=False)
    base = ["--deployment", str(tmp_path / "deployment.toml")]
    with pytest.raises(SystemExit, match="run astralvessel caller key"):
        run(monkeypatch, "ask", "hi", "--ticket-file", str(tmp_path / "t"), *base)
    callers.create_key(tmp_path)
    with pytest.raises(SystemExit, match="--ticket-file or set ASTRAL_CALLER_TICKET_FILE"):
        run(monkeypatch, "ask", "hi", *base)
    (tmp_path / "caller-key").chmod(0o644)
    with pytest.raises(SystemExit, match="chmod 600"):
        run(monkeypatch, "chat", "--ticket-file", str(tmp_path / "t"), *base)
    with pytest.raises(SystemExit, match="chmod 600"):
        run(monkeypatch, "conformance", "--ticket-file", str(tmp_path / "t"), *base)


def test_caller_key_writes_the_key_beside_the_deployment(monkeypatch, tmp_path, capsys):
    run(monkeypatch, "caller", "key", "--deployment", str(tmp_path / "deployment.toml"))
    assert (tmp_path / "caller-key").exists() and "astral-caller-holder:v1:" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="already exists"):
        run(monkeypatch, "caller", "key", "--deployment", str(tmp_path / "deployment.toml"))
