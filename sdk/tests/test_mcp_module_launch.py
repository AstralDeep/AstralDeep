"""Tests for the documented `python -m astral_sdk.mcp_bridge` module entry point: a real
subprocess serves server/discover and tools/list over stdio, exits cleanly on EOF, and
fails closed without configuration while keeping credentials out of stdout and errors;
the package root re-exports Bridge lazily so the module launch runs a single module copy
without a runpy duplicate-module warning.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import astral_sdk
import pytest

from astral_sdk.mcp_bridge import _main

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}

MODULE_COMMAND = [sys.executable, "-m", "astral_sdk.mcp_bridge"]
SENTINEL_BASE_URL = "https://astral-sentinel.example"
SENTINEL_TOKEN = "synthetic-credential-never-echo"


def _bridge_env(fake_server) -> dict[str, str]:
    package_root = Path(astral_sdk.__file__).resolve().parent.parent
    env = dict(os.environ)
    env["ASTRAL_BASE_URL"] = fake_server.base_url
    env["ASTRAL_TOKEN"] = fake_server.state.valid_token
    env["PYTHONPATH"] = str(package_root) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _launch(env: dict[str, str], payload: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        MODULE_COMMAND, input=payload, capture_output=True, text=True,
        env=env, timeout=60)


def _stderr_config_error(stderr: str) -> dict[str, str]:
    payload_lines = [line for line in stderr.splitlines() if line.startswith("{")]
    assert payload_lines, stderr
    return json.loads(payload_lines[-1])


def test_module_launch_answers_discovery_and_listing_then_eof(fake_server, validate_mcp):
    requests = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": "discover-1",
                    "method": "server/discover", "params": {"_meta": META}}),
        json.dumps({"jsonrpc": "2.0", "id": 2,
                    "method": "tools/list", "params": {"_meta": META}}),
    ]) + "\n"
    process = _launch(_bridge_env(fake_server), requests)
    assert process.returncode == 0, process.stderr
    messages = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    assert [message.get("id") for message in messages] == ["discover-1", 2]
    for message in messages:
        assert message["jsonrpc"] == "2.0"
    validate_mcp("DiscoverResult", messages[0]["result"])
    validate_mcp("ListToolsResult", messages[1]["result"])
    assert fake_server.state.valid_token not in process.stdout
    assert fake_server.state.valid_token not in process.stderr
    assert process.stderr == ""


def test_module_launch_exits_cleanly_on_immediate_eof(fake_server):
    process = _launch(_bridge_env(fake_server))
    assert process.returncode == 0, process.stderr
    assert process.stdout == ""


def test_module_launch_without_configuration_fails_closed(fake_server):
    env = _bridge_env(fake_server)
    del env["ASTRAL_BASE_URL"]
    del env["ASTRAL_TOKEN"]
    process = _launch(env)
    assert process.returncode != 0
    assert process.stdout == ""
    error = _stderr_config_error(process.stderr)
    assert "ASTRAL_BASE_URL" in error["error"]
    assert "ASTRAL_TOKEN" in error["error"]


@pytest.mark.parametrize("missing", ["ASTRAL_BASE_URL", "ASTRAL_TOKEN"])
def test_module_launch_partial_configuration_fails_closed_without_echoing_secrets(missing):
    env = dict(os.environ)
    env["ASTRAL_BASE_URL"] = SENTINEL_BASE_URL
    env["ASTRAL_TOKEN"] = SENTINEL_TOKEN
    del env[missing]
    process = _launch(env)
    assert process.returncode != 0
    assert process.stdout == ""
    error = _stderr_config_error(process.stderr)
    assert "ASTRAL_BASE_URL" in error["error"]
    assert "ASTRAL_TOKEN" in error["error"]
    assert SENTINEL_TOKEN not in process.stderr
    assert SENTINEL_BASE_URL not in process.stderr


def test_package_root_reexports_bridge_lazily_without_importing_the_module_first():
    sys.modules.pop("astral_sdk.mcp_bridge", None)
    sys.modules.pop("astral_sdk", None)
    package = importlib.import_module("astral_sdk")
    assert "astral_sdk.mcp_bridge" not in sys.modules
    from astral_sdk import Bridge

    import astral_sdk.mcp_bridge

    assert Bridge is astral_sdk.mcp_bridge.Bridge
    assert "Bridge" in dir(package)
    with pytest.raises(AttributeError):
        package.not_an_exported_attribute  # noqa: B018


def test_main_serves_stdio_until_eof_and_reports_success(fake_server, monkeypatch, capsys):
    monkeypatch.setenv("ASTRAL_BASE_URL", fake_server.base_url)
    monkeypatch.setenv("ASTRAL_TOKEN", fake_server.state.valid_token)
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": META}}) + "\n"))
    assert _main() == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out.strip())
    assert parsed["id"] == 1
    assert parsed["result"]["resultType"] == "complete"


@pytest.mark.parametrize("missing", ["ASTRAL_BASE_URL", "ASTRAL_TOKEN"])
def test_main_without_configuration_writes_credential_free_error(monkeypatch, capsys, missing):
    monkeypatch.setenv("ASTRAL_BASE_URL", SENTINEL_BASE_URL)
    monkeypatch.setenv("ASTRAL_TOKEN", SENTINEL_TOKEN)
    monkeypatch.delenv(missing)
    assert _main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert "ASTRAL_BASE_URL" in error["error"]
    assert "ASTRAL_TOKEN" in error["error"]
    assert SENTINEL_TOKEN not in captured.err
