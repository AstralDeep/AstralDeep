"""Tests for astral_sdk.__main__: python -m astral_sdk run as a real subprocess against
the local fake server, the same invocation shape
backend/tests/test_framework_conformance_088.py uses against the real server.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid


def _run(fake_server, *args: str, owner=False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "astral_sdk", *args,
        "--base-url", fake_server.base_url, "--token",
        fake_server.state.owner_token if owner else fake_server.state.valid_token],
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


def test_emergency_status_reports_running_before_a_stop(fake_server):
    result = _run(fake_server, "emergency-status")
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status["engaged"] is False
    assert status["state"] == "running"


def test_emergency_stop_engages_and_status_shows_stopped(fake_server):
    stopped = _run(fake_server, "emergency-stop", "--reason", "drill")
    assert stopped.returncode == 0, stopped.stderr
    engaged = json.loads(stopped.stdout)
    assert engaged["engaged"] is True
    assert engaged["state"] == "stopped"
    revision = engaged["revision"]
    status = json.loads(_run(fake_server, "emergency-status").stdout)
    assert status["engaged"] is True and status["revision"] == revision


def test_emergency_resume_rejects_a_stale_revision_then_accepts_the_current_one(fake_server):
    engaged = json.loads(_run(fake_server, "emergency-stop").stdout)
    stale = _run(fake_server, "emergency-resume", "--expected-revision", str(engaged["revision"] + 5), owner=True)
    assert stale.returncode == 1
    assert json.loads(stale.stderr)["code"] == "emergency_stop_stale_revision"
    still = json.loads(_run(fake_server, "emergency-status").stdout)
    assert still["engaged"] is True
    resumed = _run(fake_server, "emergency-resume", "--expected-revision", str(engaged["revision"]), owner=True)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["engaged"] is False


def test_emergency_resume_without_a_stop_is_refused(fake_server):
    result = _run(fake_server, "emergency-resume", "--expected-revision", "1", owner=True)
    assert result.returncode == 1
    assert json.loads(result.stderr)["code"] == "emergency_stop_not_engaged"


def test_framework_control_cannot_resume_owner_stop(fake_server):
    engaged = json.loads(_run(fake_server, "emergency-stop").stdout)
    result = _run(fake_server, "emergency-resume", "--expected-revision", str(engaged["revision"]))
    assert result.returncode == 1
    assert json.loads(result.stderr)["code"] == "emergency_stop_owner_authentication_required"
    assert json.loads(_run(fake_server, "emergency-status").stdout)["engaged"]
