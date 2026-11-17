"""Checks the launcher refuses a remote bind and a non-personal env file, and builds the node environment."""

import argparse
import sys

import pytest
from node_bundle_support import astral_node_launch as launch


def write_env(tmp_path, text):
    path = tmp_path / "node.env"
    path.write_text(text)
    return path


def arguments(path, host="127.0.0.1", port=18001, allow_remote=False):
    return argparse.Namespace(env_file=path, host=host, port=port, allow_remote=allow_remote)


def test_env_file_parsing_skips_comments_and_strips_quotes(tmp_path):
    path = write_env(tmp_path, "# note\n\nA=1\nB='two'\nC=x=y\n")
    assert launch.parse_env_file(path) == {"A": "1", "B": "two", "C": "x=y"}


@pytest.mark.parametrize("text", ["nonsense\n", "bad key=1\n", "=1\n"])
def test_malformed_env_lines_are_refused(tmp_path, text):
    with pytest.raises(ValueError):
        launch.parse_env_file(write_env(tmp_path, text))


def test_configure_sets_port_mailbox_and_no_bytecode(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "astral-mailbox").write_text("x")
    values = launch.configure(arguments(write_env(tmp_path, "ASTRAL_NODE_PROFILE=personal\n")), root=tmp_path)
    assert values["ORCHESTRATOR_PORT"] == "18001"
    assert values["PYTHONDONTWRITEBYTECODE"] == "1"
    assert values["ASTRAL_MAILBOX_BIN"] == str(tmp_path / "bin" / "astral-mailbox")


def test_an_explicit_mailbox_path_is_kept(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "astral-mailbox").write_text("x")
    env = write_env(tmp_path, "ASTRAL_NODE_PROFILE=personal\nASTRAL_MAILBOX_BIN=/opt/mailbox\n")
    assert launch.configure(arguments(env), root=tmp_path)["ASTRAL_MAILBOX_BIN"] == "/opt/mailbox"


def test_a_non_personal_profile_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="personal profile"):
        launch.configure(arguments(write_env(tmp_path, "ASTRAL_NODE_PROFILE=institution\n")), root=tmp_path)


def test_a_non_loopback_bind_needs_an_explicit_flag(tmp_path):
    env = write_env(tmp_path, "ASTRAL_NODE_PROFILE=personal\n")
    with pytest.raises(SystemExit, match="allow-remote"):
        launch.configure(arguments(env, host="0.0.0.0"), root=tmp_path)
    assert launch.configure(arguments(env, host="0.0.0.0", allow_remote=True), root=tmp_path)


def test_pin_bind_host_overrides_the_host_uvicorn_is_given():
    uvicorn = pytest.importorskip("uvicorn")
    original = uvicorn.Config.__init__
    try:
        launch.pin_bind_host("127.0.0.1")
        config = uvicorn.Config(lambda *a: None, host="0.0.0.0", port=1)
        assert config.host == "127.0.0.1"
    finally:
        uvicorn.Config.__init__ = original


def test_main_starts_the_orchestrator_with_logging_and_a_pinned_host(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr(launch, "PACK_ROOT", tmp_path)
    (tmp_path / "app" / "backend" / "orchestrator").mkdir(parents=True)
    monkeypatch.setattr(launch.runpy, "run_path", lambda path, run_name: calls.update(path=path, run_name=run_name))
    monkeypatch.setattr(launch, "pin_bind_host", lambda host: calls.update(host=host))
    from node_bundle_support import node_logging

    monkeypatch.setattr(node_logging, "capture_output", lambda directory, secrets: calls.update(log=directory, secrets=secrets))
    monkeypatch.chdir(tmp_path)
    env = write_env(tmp_path, "ASTRAL_NODE_PROFILE=personal\nWEB_SESSION_ENC_KEY=abcdef123456\n")
    monkeypatch.setattr(launch.os, "environ", {})
    launch.main(["--env-file", str(env), "--port", "18003", "--log-dir", str(tmp_path / "logs")])
    assert sys.dont_write_bytecode is True
    assert calls["host"] == "127.0.0.1" and calls["run_name"] == "__main__"
    assert calls["path"].endswith("orchestrator/orchestrator.py")
    assert calls["secrets"] == ["abcdef123456"] and calls["log"] == tmp_path / "logs"
