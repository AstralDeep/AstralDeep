# The command line: every command parses, and the commands reach the operator functions without Docker.

from __future__ import annotations

import pytest

from astralvessel import cli, node, operate

ISSUER = "https://keycloak.test/realms/astral"


def run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["astralvessel", *argv])
    cli.main()


def test_commands_reach_their_operator_functions(monkeypatch):
    called = []
    for name in ("up", "check", "down", "pair", "ask", "chat"):
        monkeypatch.setattr(operate, name, lambda args, name=name: called.append((name, args)))
    run(monkeypatch, "up", "--issuer", ISSUER, "--client-id", "c", "--port", "9000", "--example")
    run(monkeypatch, "check", "--isolation-only")
    run(monkeypatch, "down", "--volumes")
    run(monkeypatch, "pair", "--executor", "--label", "x")
    run(monkeypatch, "ask", "hello?", "--token-file", "t")
    run(monkeypatch, "chat")
    names = [n for n, _ in called]
    assert names == ["up", "check", "down", "pair", "ask", "chat"]
    up, check, down, pair, ask, _ = (a for _, a in called)
    assert check.isolation_only
    assert (up.issuer, up.client_id, up.port, up.example) == (ISSUER, "c", 9000, True)
    assert down.volumes and pair.executor and pair.label == "x" and ask.question == "hello?"
    assert ask.token_file == "t"


def test_an_unknown_command_or_a_missing_question_is_refused(monkeypatch):
    with pytest.raises(SystemExit):
        run(monkeypatch, "serve")
    with pytest.raises(SystemExit):
        run(monkeypatch, "ask")


def test_deploy_writes_compose_and_prints_the_start_command(monkeypatch, tmp_path, capsys):
    (tmp_path / "deployment.toml").write_text(
        f'[executor]\nkernel_artifacts = "kernel"\n[owner]\nissuer = "{ISSUER}"\nclient_id = "c"\n')
    (tmp_path / "kernel").mkdir()
    (tmp_path / "kernel" / "astral_kernel-0.1.0-py3-none-manylinux_2_36_aarch64.whl").write_bytes(b"w")
    (tmp_path / ".env").write_text("VESSEL_SANDBOX_TOKEN=0123456789abcdef0123\n")
    monkeypatch.delenv("VESSEL_SANDBOX_TOKEN", raising=False)
    run(monkeypatch, "deploy", str(tmp_path / "deployment.toml"))
    assert (tmp_path / "compose.generated.yml").exists()
    assert "start it with: docker compose -f" in capsys.readouterr().out


def test_deploy_can_start_it(monkeypatch, tmp_path):
    (tmp_path / "deployment.toml").write_text(
        f'[executor]\nkernel_artifacts = "kernel"\n[owner]\nissuer = "{ISSUER}"\nclient_id = "c"\n')
    (tmp_path / "kernel").mkdir()
    (tmp_path / "kernel" / "astral_kernel-0.1.0-py3-none-manylinux_2_36_aarch64.whl").write_bytes(b"w")
    monkeypatch.setenv("VESSEL_SANDBOX_TOKEN", "0123456789abcdef0123")
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **kw: ran.append(command))
    run(monkeypatch, "deploy", str(tmp_path / "deployment.toml"), "--up", "--out", str(tmp_path / "c.yml"))
    assert ran[0][-4:] == ["up", "-d", "--build", "--remove-orphans"] and ran[0][3] == str(tmp_path / "c.yml")


def test_the_sandbox_needs_a_package(monkeypatch):
    monkeypatch.delenv("VESSEL_PACKAGE", raising=False)
    with pytest.raises(SystemExit, match="agent package"):
        run(monkeypatch, "sandbox")


def test_ask_prints_the_answer_and_chat_keeps_its_context(monkeypatch, tmp_path, capsys):
    (tmp_path / "deployment.toml").write_text(f'[owner]\nissuer = "{ISSUER}"\nclient_id = "c"\n')
    (tmp_path / "compose.generated.yml").write_text("services: {}\n")
    token = tmp_path / "token"
    token.write_text("tok\n")
    seen = []

    def converse(section, tok, text, context=None):
        seen.append((tok, text, context))
        if text == "boom":
            raise node.NodeError("the node said no")
        return f"echo {text}", "ctx-9"

    monkeypatch.setattr(node, "converse", converse)
    base = ["--deployment", str(tmp_path / "deployment.toml"), "--token-file", str(token)]
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
    with pytest.raises(SystemExit, match="the node said no"):
        run(monkeypatch, "ask", "boom", *base)


def test_pair_prints_the_offer_and_explains_a_refusal(monkeypatch, tmp_path, capsys):
    (tmp_path / "deployment.toml").write_text(f'[owner]\nissuer = "{ISSUER}"\nclient_id = "c"\n')
    (tmp_path / "compose.generated.yml").write_text("services: {}\n")
    monkeypatch.setenv("ASTRAL_OWNER_TOKEN", "tok")
    offer = {"pairing": {"expires_at": 5000}, "mesh": {"state": "offer", "code": "9999 0000", "payload": "PAY"}}
    monkeypatch.setattr(node, "request_pairing", lambda section, token, label: offer)
    run(monkeypatch, "pair", "--deployment", str(tmp_path / "deployment.toml"))
    assert "9999 0000" in capsys.readouterr().out

    def refuse(section, token, label):
        raise node.NodeError("the node refused the owner token")

    monkeypatch.setattr(node, "request_pairing", refuse)
    with pytest.raises(SystemExit, match="refused the owner token"):
        run(monkeypatch, "pair", "--deployment", str(tmp_path / "deployment.toml"))
