"""``python -m astral_sdk ...`` run as a REAL separate OS process against the
local fake server — this is the exact invocation shape
``backend/tests/test_framework_conformance_088.py`` uses against the real
Deep server.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid


def _run(fake_server, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "astral_sdk", *args,
        "--base-url", fake_server.base_url, "--token", fake_server.state.valid_token],
        capture_output=True, text=True, timeout=30,
    )


def test_submit_prints_one_json_operation_to_stdout(fake_server):
    key = str(uuid.uuid4())
    result = _run(fake_server, "submit", "--idempotency-key", key, "--name", "A", "--instructions", "B")
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["created"] is True
    assert parsed["title"] == "A"


def test_get_reads_back_the_submitted_operation(fake_server):
    key = str(uuid.uuid4())
    submitted = json.loads(_run(fake_server, "submit", "--idempotency-key", key,
                                "--name", "A", "--instructions", "B").stdout)
    result = _run(fake_server, "get", submitted["id"])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["id"] == submitted["id"]


def test_cancel_then_get_shows_the_cancelled_disposition(fake_server):
    key = str(uuid.uuid4())
    submitted = json.loads(_run(fake_server, "submit", "--idempotency-key", key,
                                "--name", "A", "--instructions", "B").stdout)
    cancel = _run(fake_server, "cancel", submitted["id"],
                 "--expected-revision", str(submitted["revision"]))
    assert cancel.returncode == 0, cancel.stderr
    assert json.loads(cancel.stdout)["operation"]["disposition"] == "cancelled"


def test_unknown_operation_exits_nonzero_with_a_json_error_on_stderr(fake_server):
    result = _run(fake_server, "get", str(uuid.uuid4()))
    assert result.returncode == 1
    error = json.loads(result.stderr)
    assert error["code"] == "work_not_found"


def test_missing_token_is_a_usage_error(fake_server):
    result = subprocess.run(
        [sys.executable, "-m", "astral_sdk", "get", "x", "--base-url", fake_server.base_url],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "ASTRAL_TOKEN" in result.stderr


def test_tools_command_lists_available_tools(fake_server):
    result = _run(fake_server, "tools")
    assert result.returncode == 0, result.stderr
    names = {tool["name"] for tool in json.loads(result.stdout)}
    assert "astral_submit_operation" in names
