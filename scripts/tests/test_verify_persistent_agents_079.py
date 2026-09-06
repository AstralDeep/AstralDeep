"""Offline driver qualification; fixtures are never represented as live evidence."""
from __future__ import annotations

import base64
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "verify_persistent_agents_079.py"
SPEC = importlib.util.spec_from_file_location("verify_persistent_agents_079", SCRIPT)
driver = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = driver
SPEC.loader.exec_module(driver)
ASSIGNMENT = "b5f64111-b17c-4ab7-8809-ac3b026f0e2b"
SOURCE = "https://www.python.org/downloads/"
BASE = "http://127.0.0.1:8001"
NOW = datetime.now(UTC)


def session_data(**changes):
    payload = base64.urlsafe_b64encode(driver.canonical({"sub": "private-owner", "exp": int((NOW + timedelta(minutes=30)).timestamp())})).decode().rstrip("=")
    return {"schema_version": 1, "base_url": BASE, "owner_sub": "private-owner",
            "access_token": "eyJhbGciOiJSUzI1NiJ9." + payload + ".privateSignatureNeverLogged",
            "expires_at": (NOW + timedelta(minutes=10)).isoformat(), "assignment_id": ASSIGNMENT,
            "allowed_scenarios": list(driver.SCENARIOS), **changes}


def private_json(path, value):
    if path.exists():
        driver.private_file(path)
        path.write_bytes(driver.canonical(value))
    else:
        descriptor = driver.create_private_file(path)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(driver.canonical(value))
    return path


def broaden_permissions(path):
    if os.name != "nt":
        path.chmod(0o644)
        return
    code = """$ErrorActionPreference='Stop'
$acl=[System.IO.File]::GetAccessControl($env:ASTRAL_079_PRIVATE_FILE)
$everyone=[System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
$rule=[System.Security.AccessControl.FileSystemAccessRule]::new(
  $everyone,[System.Security.AccessControl.FileSystemRights]::Read,
  [System.Security.AccessControl.AccessControlType]::Allow)
$acl.AddAccessRule($rule)
[System.IO.File]::SetAccessControl($env:ASTRAL_079_PRIVATE_FILE,$acl)
"""
    driver.process(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                    base64.b64encode(code.encode("utf-16le")).decode()],
                   environment={**os.environ, "ASTRAL_079_PRIVATE_FILE": str(path)})


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return root


def raw_snapshot(generation=1):
    action, event, task, activity = [str(uuid4()) for _ in range(4)]
    limits = {key: 100_000 for key in driver.METRICS if key != "spend_micro_units"}
    limits.update({"daily_" + key: value for key, value in list(limits.items())})
    limits.update(cadence_seconds=3600, max_retries=2, max_concurrent_tasks=1, max_depth=0, max_tasks=16, step_timeout_ms=1000)
    return {"assignment": {"assignment_id": ASSIGNMENT, "definition": {
        "name": "private-assignment-name", "instructions": "private-instructions", "source": {
        "profile": "public_page", "agent_id": "web-research-1", "tool_name": "fetch_page", "arguments": {"url": SOURCE}},
        "allowed_tools": ["web-research-1:fetch_page"], "limits": limits}, "authority": {"grant_bound": True},
        "state_version": 4, "instruction_revision": 1, "control_epoch": 1, "lifecycle": "active", "phase": "waiting",
        "last_completed_generation": generation, "last_check_at": NOW.isoformat(), "latest_result": "private-source-content",
        "next_wake_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(), "usage": {"spent": {"model_calls": 3, "tool_calls": 1, "tokens": 100}},
        "cost_status": "unpriced"},
        "actions": [{"action_id": action, "state": "succeeded", "intent": {"request_digest": "a" * 64, "action_key": "action-key",
                    "request": {"kind": "model", "messages": "private-messages"}, "event_id": event},
                    "attempts": [{"attempt_id": "attempt-one", "state": "completed", "outcome": "succeeded", "result_digest": "b" * 64,
                                  "dispatch_token": "private-dispatch-token"}]}],
        "events": [{"event_id": event, "identity_digest": "c" * 64, "context_digest": "d" * 64,
                    "disposition": "completed", "result_digest": "e" * 64, "context": "private-event"}],
        "tasks": [{"task_id": task, "event_id": event, "state": "completed", "result_digest": "f" * 64,
                   "incorporated_by": {"__assignment__": "f" * 64}, "bounded_result": "private-result"}],
        "activity": [{"activity_id": activity, "sequence": 1, "activity_type": "finding", "references": {"event_id": event},
                      "summary": "private-finding", "notification_state": "delivered"}]}


@pytest.mark.parametrize("raw,code", [(b'{"x":1,"x":2}', "duplicate"), (b'{"x":NaN}', "nonfinite"), (b'broken', "invalid"),
                                     (b'x' * (driver.MAX_JSON + 1), "size_bound")],
                         ids=["duplicate", "nonfinite", "invalid", "oversized"])
def test_strict_json_denials(raw, code):
    with pytest.raises(driver.EvidenceError, match=code):
        driver.strict_json(raw)


@pytest.mark.parametrize("value,source", [("http://remote.example", False), ("https://user:secret@example.com", False),
    ("https://example.com/?token=x", False), ("https://example.com/path", False), ("https://example.com:bad", False),
    ("https://example.com/\n", True), ("http://example.com", True), ("https://127.0.0.1", True),
    ("https://localhost", True), ("https://internal.local", True)])
def test_endpoint_denials(value, source):
    with pytest.raises(driver.EvidenceError):
        driver.endpoint(value, source=source)


def test_endpoint_uuid_and_time_validation():
    assert driver.endpoint(BASE + "/") == BASE
    assert driver.endpoint(SOURCE, source=True) == SOURCE
    assert driver.endpoint("https://api.example") == "https://api.example"
    assert driver.uuid(ASSIGNMENT) == ASSIGNMENT
    for value in ("bad", None):
        with pytest.raises(driver.EvidenceError):
            driver.uuid(value)
        with pytest.raises(driver.EvidenceError):
            driver.instant(value)
    with pytest.raises(driver.EvidenceError, match="utc_required"):
        driver.instant("2026-01-01T00:00:00+01:00")
    assert driver.instant("2026-01-01T00:00:00Z").tzinfo == UTC


def test_private_session_target_expiry_token_and_permission_denials(workspace, tmp_path):
    path = private_json(tmp_path / "session.json", session_data())
    assert driver.load_session(path, BASE, ASSIGNMENT, "live-monitor", root=workspace, now=NOW)["owner_sub"] == "private-owner"
    for changes, code in [({"schema_version": 2}, "schema"), ({"base_url": "https://other.example"}, "target"),
            ({"allowed_scenarios": []}, "owner_selected"), ({"expires_at": (NOW - timedelta(seconds=1)).isoformat()}, "expired"),
            ({"expires_at": (NOW + timedelta(hours=2)).isoformat()}, "short_lived"), ({"access_token": "dev-token"}, "token_invalid"),
            ({"owner_sub": "someone-else"}, "owner_mismatch")]:
        private_json(path, session_data(**changes))
        with pytest.raises(driver.EvidenceError, match=code):
            driver.load_session(path, BASE, ASSIGNMENT, "live-monitor", root=workspace, now=NOW)
    broaden_permissions(path)
    with pytest.raises(driver.EvidenceError, match="not_private|local_verifier_failed"):
        driver.private_file(path)


def test_private_paths_refuse_repository_links_hardlinks_and_sensitive_stores(workspace, tmp_path):
    with pytest.raises(driver.EvidenceError, match="inside_repository"):
        driver.external_path(workspace / "output.json", root=workspace)
    with pytest.raises(driver.EvidenceError, match="sensitive_store"):
        driver.external_path(tmp_path / "uploads" / "report.json", root=workspace)
    target = private_json(tmp_path / "session.json", {})
    linked = tmp_path / "linked.json"
    linked.symlink_to(target)
    with pytest.raises(driver.EvidenceError, match="link_refused"):
        driver.read_json_file(linked, root=workspace)
    hard = tmp_path / "hard.json"
    os.link(target, hard)
    with pytest.raises(driver.EvidenceError, match="regular_private"):
        driver.private_file(hard)


def test_process_is_bounded_and_never_reflects_failure_text(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=1, stdout=b"private-token", stderr=b"private-error"))
    monkeypatch.setattr(driver.subprocess, "run", run)
    with pytest.raises(driver.EvidenceError, match="^local_verifier_failed$"):
        driver.process(["docker", "inspect"])
    run.side_effect = subprocess.TimeoutExpired("private-cmd", 1)
    with pytest.raises(driver.EvidenceError, match="unavailable"):
        driver.process(["docker"])


def test_windows_private_acl_inspection_never_interpolates_path(tmp_path, monkeypatch):
    path = private_json(tmp_path / "special'$(path).json", {})
    monkeypatch.setattr(driver, "os", SimpleNamespace(**(vars(os) | {"name": "nt"})))
    process = Mock(return_value=b"private\r\n")
    monkeypatch.setattr(driver, "process", process)
    driver.private_file(path)
    arguments = process.call_args.args[0]
    source = base64.b64decode(arguments[-1]).decode("utf-16le")
    assert str(path) not in source
    assert "$env:ASTRAL_079_PRIVATE_FILE" in source and "GetAccessRules" in source
    assert process.call_args.kwargs["environment"]["ASTRAL_079_PRIVATE_FILE"] == str(path)
    process.return_value = b"not-private"
    with pytest.raises(driver.EvidenceError, match="not_private"):
        driver.private_file(path)


class Reply(io.BytesIO):
    status = 200


def test_api_tls_no_redirect_secret_transport_and_status_handling(monkeypatch):
    api = driver.ProductAPI(BASE, session_data())
    response = Reply(b'{"assignment":{}}')
    api.opener = SimpleNamespace(open=Mock(return_value=response))
    assert api.request("/api/persistent-agents") == {"assignment": {}}
    request = api.opener.open.call_args.args[0]
    assert request.headers["Authorization"].startswith("Bearer ey")
    assert api.opener.open.call_args.kwargs["timeout"] == 15
    api.opener.open.side_effect = urllib.error.HTTPError("https://private", 409, "private", {}, io.BytesIO(b'{"error":"assignment_stale_control"}'))
    assert api.request("/api/persistent-agents/a", method="PATCH", body={"consent": False}, expected=(409,))["error"] == "assignment_stale_control"
    api.opener.open.side_effect = urllib.error.HTTPError("https://private", 403, "private", {}, io.BytesIO(b'private'))
    with pytest.raises(driver.EvidenceError, match="^api_status_403$"):
        api.request("/api/persistent-agents")
    api.opener.open.side_effect = urllib.error.URLError("private-internal")
    with pytest.raises(driver.EvidenceError, match="^api_transport_failed$"):
        api.request("/api/persistent-agents")
    with pytest.raises(driver.EvidenceError, match="redirect_refused"):
        driver.NoRedirect().redirect_request(None)
    with pytest.raises(driver.EvidenceError, match="path_refused"):
        api.request("//foreign.example")
    api.session["expires_at"] = (NOW - timedelta(hours=1)).isoformat()
    with pytest.raises(driver.EvidenceError, match="session_expired"):
        api.request("/api/persistent-agents")


def test_manifest_includes_dirty_firstparty_components_excludes_user_generated_stores(workspace, monkeypatch):
    files = {"backend/persistent_agents/runner.py": "runtime", "backend/orchestrator/projection_surfaces/assignments.py": "adapter",
             "backend/agents/user_generated.py": "private-user-code", "backend/data/private.py": "private-data",
             "backend/tests/test_private.py": "test", "config/astral-composition.json": "{}",
             "components/AstralPlane/src/astralplane/assignments.py": "plane",
             "components/AstralProjection/backend/webrender/runtime.js": "render"}
    for relative, value in files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def git(root, *args):
        if args[0] == "rev-parse":
            return b"a" * 40
        if args[0] == "status":
            return b"dirty"
        prefix = root.relative_to(workspace).as_posix() + "/" if root != workspace else ""
        selected = [name[len(prefix):] for name in files if name.startswith(prefix) and
                    (prefix or not name.startswith("components/"))]
        chosen = [name for name in selected if ("config/" not in name if "--others" in args else "config/" in name)]
        return ("\0".join(chosen) + "\0").encode()

    monkeypatch.setattr(driver, "git", git)
    candidate, manifest = driver.runtime_manifest(workspace)
    assert candidate["runtime_file_count"] == 5
    assert manifest["components/AstralPlane/src/astralplane/assignments.py"]["package"] == "astralplane"
    assert "private" not in json.dumps(manifest)
    assert "backend/agents/user_generated.py" not in manifest


def test_actual_deployment_binding_requires_ports_bytes_and_start_identity(monkeypatch):
    candidate = {"runtime_manifest_digest": "a" * 64}
    manifest = {"backend/a.py": {"package": None, "path": "/app/backend/a.py", "sha256": "b" * 64}}
    identity = {"id": "c" * 64, "image": "sha256:" + "d" * 64, "running": True, "started_at": NOW.isoformat(),
                "ports": {"8001/tcp": [{"HostPort": "8001", "HostIp": "127.0.0.1"}]}}
    observed = {"digests": {"backend/a.py": "b" * 64}, "latest_mtime": NOW.timestamp() - 1}
    calls = []

    def process(args, **kwargs):
        calls.append((args, kwargs))
        return driver.canonical(identity if args[1] == "inspect" else observed)

    monkeypatch.setattr(driver, "process", process)
    result = driver.deployment_binding(BASE, "astraldeep-test", candidate, manifest)
    assert result["container_id"] == "c" * 64
    assert "Env" not in calls[0][0][3] and "--format" in calls[0][0]
    assert json.loads(calls[1][1]["input_bytes"]) == manifest
    observed["digests"]["backend/a.py"] = "wrong"
    with pytest.raises(driver.EvidenceError, match="differs"):
        driver.deployment_binding(BASE, "astraldeep-test", candidate, manifest)
    observed["digests"]["backend/a.py"] = "b" * 64
    identity["mounts"] = [{"Destination": "/app/backend/a.py"}]
    with pytest.raises(driver.EvidenceError, match="mutable_runtime_mount"):
        driver.deployment_binding(BASE, "astraldeep-test", candidate, manifest)
    identity["mounts"] = [{"Destination": "/app/backend/data"}]
    assert driver.deployment_binding(BASE, "astraldeep-test", candidate, manifest)["image_id"] == identity["image"]
    observed["latest_mtime"] = NOW.timestamp() + 1
    with pytest.raises(driver.EvidenceError, match="since_container_start"):
        driver.deployment_binding(BASE, "astraldeep-test", candidate, manifest)
    identity["ports"] = {}
    with pytest.raises(driver.EvidenceError, match="endpoint_not_bound"):
        driver.deployment_binding(BASE, "astraldeep-test", candidate, manifest)
    with pytest.raises(driver.EvidenceError, match="missing_input"):
        driver.deployment_binding("https://remote.example", "astraldeep-test", candidate, manifest)
    with pytest.raises(driver.EvidenceError, match="container_name"):
        driver.deployment_binding(BASE, "--privileged", candidate, manifest)


def test_deployed_hash_program_reads_real_files_and_reports_missing(tmp_path):
    path = tmp_path / "module.py"
    path.write_bytes(b"runtime")
    manifest = {"present": {"package": None, "path": str(path)}, "missing": {"package": None, "path": str(path) + "missing"},
                "stdlib": {"package": "json", "path": "__init__.py"}}
    result = subprocess.run([sys.executable, "-c", driver._DEPLOYED_HASH_SCRIPT], input=driver.canonical(manifest), capture_output=True, check=True)
    actual = json.loads(result.stdout)
    assert actual["digests"]["present"] == driver.hashlib.sha256(b"runtime").hexdigest()
    assert actual["digests"]["missing"] is None and len(actual["digests"]["stdlib"]) == 64


def api_for(raw):
    def request(path, **_kwargs):
        if "/tasks" in path:
            return {"tasks": raw["tasks"]}
        for name in ("actions", "events", "activity"):
            if "/" + name in path:
                return {name: raw[name], "next_cursor": None}
        return {"assignment": copy.deepcopy(raw["assignment"])}
    return SimpleNamespace(request=Mock(side_effect=request))


def test_owner_snapshot_redacts_content_tokens_and_waiting_usage(monkeypatch):
    raw = raw_snapshot()
    api = api_for(raw)
    observed = driver.snapshot(api, ASSIGNMENT, SOURCE)
    safe = driver.safe_snapshot(observed)
    assert "private" not in json.dumps(safe) and "dispatch_token" not in json.dumps(safe)
    driver.completed(safe)
    result = driver.quiet_observation(api, ASSIGNMENT, SOURCE, 2, sleep=lambda _: None)
    assert result == safe
    with pytest.raises(driver.EvidenceError, match="changed_during_idle"):
        driver.quiet_observation(api, ASSIGNMENT, SOURCE, 2,
            sleep=lambda _: raw["assignment"]["usage"]["spent"].update(model_calls=999))
    raw["assignment"]["assignment_id"] = str(uuid4())
    with pytest.raises(driver.EvidenceError, match="foreign_assignment"):
        driver.snapshot(api, ASSIGNMENT, SOURCE)


def test_snapshot_and_collection_bounds(monkeypatch):
    raw = raw_snapshot()
    api = api_for(raw)
    raw["assignment"]["definition"]["source"]["arguments"]["url"] = "https://elsewhere.example"
    with pytest.raises(driver.EvidenceError, match="source_mismatch"):
        driver.snapshot(api, ASSIGNMENT, SOURCE)
    raw["assignment"]["definition"]["source"]["arguments"]["url"] = SOURCE
    raw["assignment"]["authority"]["grant_bound"] = False
    with pytest.raises(driver.EvidenceError, match="consent_missing"):
        driver.snapshot(api, ASSIGNMENT, SOURCE)
    api.request.side_effect = None
    api.request.return_value = {"actions": [{}], "next_cursor": "same"}
    with pytest.raises(driver.EvidenceError, match="not_advancing"):
        driver.page(api, "/base", "actions")
    api.request.side_effect = [{"activity": [{}], "next_cursor": 1}, {"activity": [], "next_cursor": None}]
    assert driver.page(api, "/base", "activity") == [{}]
    assert "after_sequence=1" in api.request.call_args.args[0]
    api.request.side_effect = [{"actions": [{}], "next_cursor": i} for i in range(10)]
    with pytest.raises(driver.EvidenceError, match="row_bound"):
        driver.page(api, "/base", "actions")


def test_completed_and_recovery_requires_real_prior_receipts():
    old = driver.safe_snapshot(raw_snapshot())
    current = copy.deepcopy(old)
    current["last_completed_generation"] = 2
    report = {"owner_digest": "owner", "assignment_id": ASSIGNMENT, "source_digest": "source", "candidate": {"head": "x"},
              "deployment": {"container_id": "container", "image_id": "image", "started_at": "later"}}
    baseline = {**report, "status": "passed", "scenario": "live-monitor", "snapshot": old,
                "deployment": {**report["deployment"], "started_at": "earlier"}}
    driver.compare_restart(baseline, current, report)
    bad = copy.deepcopy(current)
    bad["actions"][0]["attempts"].append({"attempt_id": "duplicate"})
    with pytest.raises(driver.EvidenceError, match="identity_changed_actions"):
        driver.compare_restart(baseline, bad, report)
    bad = copy.deepcopy(current)
    bad["actions"].append({**copy.deepcopy(bad["actions"][0]), "id": str(uuid4()), "action_key_digest": "new-key"})
    with pytest.raises(driver.EvidenceError, match="event_reprocessed"):
        driver.compare_restart(baseline, bad, report)
    bad = copy.deepcopy(current)
    bad["usage_spent"]["model_calls"] += 1
    with pytest.raises(driver.EvidenceError, match="unchanged_source_reprocessed"):
        driver.compare_restart(baseline, bad, report)
    bad = raw_snapshot()
    bad["actions"].append(copy.deepcopy(bad["actions"][0]))
    with pytest.raises(driver.EvidenceError, match="duplicate_evidence_identity"):
        driver.safe_snapshot(bad)
    bad = copy.deepcopy(current)
    bad["activity"].append(copy.deepcopy(bad["activity"][0]))
    with pytest.raises(driver.EvidenceError, match="duplicate_finding"):
        driver.completed(bad)
    baseline["deployment"]["started_at"] = "later"
    with pytest.raises(driver.EvidenceError, match="observed_container_restart"):
        driver.compare_restart(baseline, current, report)
    current["tasks"][0]["incorporated_by"] = {}
    with pytest.raises(driver.EvidenceError, match="child_results"):
        driver.completed(current)


def test_controls_never_invent_consent_and_require_terminal_refusal(monkeypatch):
    raw = raw_snapshot()
    api = api_for(raw)
    calls = []
    original = api.request.side_effect

    def request(path, **kwargs):
        if kwargs.get("method") not in ("POST", "PATCH"):
            return original(path)
        calls.append((path, copy.deepcopy(kwargs)))
        record = raw["assignment"]
        if kwargs["method"] == "PATCH":
            assert kwargs["body"]["consent"] is False
            assert kwargs["body"]["expected_control_epoch"] != record["control_epoch"]
            return {"error": "assignment_revision_conflict"}
        command = path.rsplit("/", 1)[1]
        if record["lifecycle"] == "stopped":
            assert kwargs["expected"] == (409,)
            return {"error": "assignment_terminal"}
        record["control_epoch"] += 1
        record["lifecycle"] = {"pause": "paused", "resume": "active", "stop": "stopped"}[command]
        return {"assignment": copy.deepcopy(record)}

    api.request.side_effect = request
    result = driver.controls_scenario(api, ASSIGNMENT, SOURCE)
    assert result["lifecycle"] == "stopped"
    assert [path.rsplit("/", 1)[1] for path, _ in calls[1:]] == ["pause", "resume", "stop", "resume"]
    assert all("consent" not in payload["body"] for _, payload in calls[1:])


def test_await_check_times_out_and_refuses_owner_holds(monkeypatch):
    raw = raw_snapshot()
    monkeypatch.setattr(driver, "snapshot", lambda *_: raw)
    assert driver.await_check(None, ASSIGNMENT, SOURCE, 0, 10, sleep=lambda _: None) == raw
    for phase in ("reconciliation", "waiting_approval", "waiting_authorization", "budget_exhausted", "failed"):
        raw["assignment"]["phase"] = phase
        sleep = Mock(side_effect=AssertionError("A held assignment must refuse immediately"))
        with pytest.raises(driver.EvidenceError, match="owner_attention"):
            driver.await_check(None, ASSIGNMENT, SOURCE, 1, 10, sleep=sleep)
        sleep.assert_not_called()
    raw["assignment"]["phase"] = "running"
    clock = iter([0, 0, 1, 2])
    monkeypatch.setattr(driver.time, "monotonic", lambda: next(clock))
    with pytest.raises(driver.EvidenceError, match="timeout"):
        driver.await_check(None, ASSIGNMENT, SOURCE, 1, 1, sleep=lambda _: None)


def test_write_report_is_private_exclusive_and_bounded(workspace, tmp_path):
    path = tmp_path / "report.json"
    driver.write_report(path, {"status": "failed"}, root=workspace)
    driver.private_file(path)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(driver.EvidenceError, match="exists"):
        driver.write_report(path, {}, root=workspace)
    with pytest.raises(driver.EvidenceError, match="size_bound"):
        driver.write_report(tmp_path / "large.json", {"x": "x" * driver.MAX_JSON}, root=workspace)


@pytest.mark.skipif(os.name != "nt", reason="Exercises actual NTFS security descriptors")
def test_native_creation_has_protected_acl_before_first_write(tmp_path):
    parent = tmp_path / "inherited-public"
    parent.mkdir()
    code = """$ErrorActionPreference='Stop'
$acl=[System.IO.Directory]::GetAccessControl($env:ASTRAL_079_PRIVATE_FILE)
$everyone=[System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
$rule=[System.Security.AccessControl.FileSystemAccessRule]::new(
  $everyone,[System.Security.AccessControl.FileSystemRights]::Read,
  [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit',
  [System.Security.AccessControl.PropagationFlags]::None,
  [System.Security.AccessControl.AccessControlType]::Allow)
$acl.AddAccessRule($rule)
[System.IO.Directory]::SetAccessControl($env:ASTRAL_079_PRIVATE_FILE,$acl)
"""
    driver.process(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                    base64.b64encode(code.encode("utf-16le")).decode()],
                   environment={**os.environ, "ASTRAL_079_PRIVATE_FILE": str(parent)})
    inherited = parent / "ordinary.json"
    inherited.write_text("non-sensitive fixture")
    with pytest.raises(driver.EvidenceError):
        driver.private_file(inherited)
    path = parent / "private'$(literal).json"
    descriptor = driver.create_private_file(path)
    try:
        assert path.read_bytes() == b""
        driver.private_file(path)
        inspection = """$ErrorActionPreference='Stop'
$acl=[System.IO.File]::GetAccessControl($env:ASTRAL_079_PRIVATE_FILE)
$rules=$acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])
if(-not $acl.AreAccessRulesProtected -or @($rules | Where-Object IsInherited).Count -ne 0){exit 1}
Write-Output 'protected'
"""
        assert driver.process(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
            base64.b64encode(inspection.encode("utf-16le")).decode()],
            environment={**os.environ, "ASTRAL_079_PRIVATE_FILE": str(path)}).strip() == b"protected"
        os.write(descriptor, b"private fixture")
    finally:
        os.close(descriptor)
    with pytest.raises(driver.EvidenceError):
        driver.create_private_file(path)
    assert path.read_bytes() == b"private fixture"
    broaden_permissions(path)
    with pytest.raises(driver.EvidenceError):
        driver.private_file(path)


def test_private_creation_refuses_failed_command_and_replaced_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "empty.json"
    path.write_bytes(b"")
    actual_fstat = os.fstat
    actual_open = os.open
    opened = []

    def capture_open(*args):
        descriptor = actual_open(*args)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(driver, "os", SimpleNamespace(**(vars(os) | {
        "name": "nt", "O_BINARY": getattr(os, "O_BINARY", 0), "open": capture_open,
        "fstat": lambda _: SimpleNamespace(st_dev=-1, st_ino=-1, st_size=0)})))
    monkeypatch.setattr(driver, "private_file", Mock())
    process = Mock(return_value=b"not-created")
    monkeypatch.setattr(driver, "process", process)
    with pytest.raises(driver.EvidenceError, match="creation_failed"):
        driver.create_private_file(path)
    assert not opened and path.read_bytes() == b""
    process.return_value = b"created"
    with pytest.raises(driver.EvidenceError, match="output_file_changed"):
        driver.create_private_file(path)
    with pytest.raises(OSError):
        actual_fstat(opened[0])
    assert path.read_bytes() == b""


def arguments(tmp_path, scenario="live-monitor"):
    return driver.parser().parse_args(["--base-url", BASE, "--session-file", str(tmp_path / "session.json"),
        "--source-url", SOURCE, "--assignment-id", ASSIGNMENT, "--scenario", scenario,
        "--output", str(tmp_path / (scenario + ".json")), "--observe-seconds", "2"])


@pytest.mark.parametrize("scenario", ["live-monitor", "after-restart", "controls"])
def test_scenario_driver_reports_safe_success_with_explicit_live_boundaries(workspace, tmp_path, monkeypatch, capsys, scenario):
    args = arguments(tmp_path, scenario)
    private_json(args.session_file, session_data())
    candidate = {"repositories": {}, "runtime_manifest_digest": "candidate", "runtime_file_count": 1}
    deployed = {"container_id": "container", "image_id": "image", "started_at": "later"}
    monkeypatch.setattr(driver, "runtime_manifest", lambda *_: (candidate, {}))
    monkeypatch.setattr(driver, "deployment_binding", lambda *_: deployed)
    monkeypatch.setattr(driver, "ProductAPI", lambda *_: None)
    raw = raw_snapshot(2)
    safe = driver.safe_snapshot(raw)
    monkeypatch.setattr(driver, "snapshot", lambda *_: raw)
    monkeypatch.setattr(driver, "control", Mock())
    monkeypatch.setattr(driver, "await_check", Mock())
    monkeypatch.setattr(driver, "quiet_observation", lambda *_: safe)
    monkeypatch.setattr(driver, "controls_scenario", lambda *_: {**safe, "lifecycle": "stopped"})
    if scenario == "after-restart":
        old = {**safe, "last_completed_generation": 1}
        args.baseline = private_json(tmp_path / "baseline.json", {"status": "passed", "scenario": "live-monitor",
            "owner_digest": driver.digest("private-owner"), "assignment_id": ASSIGNMENT, "source_digest": driver.digest(SOURCE),
            "candidate": candidate, "deployment": {**deployed, "started_at": "earlier"}, "snapshot": old})
    assert driver.run(args, root=workspace) == 0
    report = json.loads(args.output.read_bytes())
    assert report["status"] == "passed" and report["not_exercised"]
    assert "private" not in args.output.read_text()
    assert "private-owner" not in capsys.readouterr().out


def test_driver_missing_session_candidate_or_output_never_claims_success(workspace, tmp_path, monkeypatch, capsys):
    args = arguments(tmp_path)
    assert driver.run(args, root=workspace) == 1
    report = json.loads(args.output.read_bytes())
    assert report["status"] == "failed" and not report["checks"]
    args.output = tmp_path / "next.json"
    private_json(args.session_file, session_data())
    monkeypatch.setattr(driver, "runtime_manifest", lambda *_: ({}, {}))
    monkeypatch.setattr(driver, "deployment_binding", Mock(side_effect=driver.EvidenceError("missing_input_deployment")))
    assert driver.run(args, root=workspace) == 1
    assert json.loads(args.output.read_bytes())["error_code"] == "missing_input_deployment"
    args.output = workspace / "unsafe.json"
    assert driver.run(args, root=workspace) == 1 and not args.output.exists()
    assert "private-owner" not in capsys.readouterr().out


def test_help_requires_no_session_or_network():
    result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, check=False)
    assert result.returncode == 0 and b"--assignment-id" in result.stdout and b"--session-file" in result.stdout
