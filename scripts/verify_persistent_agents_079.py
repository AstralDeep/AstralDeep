#!/usr/bin/env python3
"""Collect private, diagnostic live evidence for an existing disposable assignment.

The owner supplies a private JSON session outside the repository containing:
schema_version=1, base_url, owner_sub, access_token, expires_at (RFC 3339),
assignment_id, and allowed_scenarios (explicitly selected scenario names).
Optional baseline_file points to an earlier live-monitor report for after-restart.
This script never logs tokens, creates consent, approves actions, restarts a
container, or provisions credentials. Controls ends by permanently stopping the
selected disposable assignment. The bearer token is validated by the product;
its locally decoded subject/expiry are only additional consistency checks.

Local Docker verification compares actual runtime bytes to the working tree,
including dirty component sources. It is diagnostic candidate binding, not a
release attestation. Remote deployments without this binding fail closed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
MAX_JSON = 1_048_576
MAX_ROWS = 1000
SCENARIOS = ("live-monitor", "after-restart", "controls")
METRICS = ("model_calls", "tool_calls", "tokens", "elapsed_ms", "spend_micro_units")
_RUNTIME_SUFFIXES = {".py", ".json", ".js", ".css", ".html", ".svg", ".sh", ".txt", ".woff", ".woff2", ".ttf"}
_EXCLUDED = {"tests", "__pycache__", "data", "tmp", "knowledge", "node_modules"}
_PACKAGES = {
    "components/AstralPlane/src/astralplane": "astralplane",
    "components/AstralProjection/src/astralprojection": "astralprojection",
    "components/AstralProjection/backend/webrender": "webrender",
    "components/AstralProjection/backend/rote": "rote",
    "components/AstralProjection/contracts": "contracts",
    "components/AstralPrimitives/src/astralprims": "astralprims",
    "components/LETS/src/lets": "lets",
}


class EvidenceError(Exception):
    """Only static safe codes, never arbitrary server/process error text."""


def require(condition, code):
    if not condition:
        raise EvidenceError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_json(raw):
    require(len(raw) <= MAX_JSON, "json_size_bound")

    def pairs(entries):
        result = {}
        for key, value in entries:
            require(key not in result, "json_duplicate_key")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: require(False, "json_nonfinite"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceError("json_invalid") from exc


def instant(value):
    try:
        parsed = datetime.fromisoformat(value)
        require(parsed.utcoffset() == timedelta(0), "timestamp_utc_required")
        return parsed
    except (ValueError, TypeError, AttributeError) as exc:
        raise EvidenceError("timestamp_invalid") from exc


def uuid(value):
    try:
        require(str(UUID(value)) == value, "assignment_id_invalid")
    except (ValueError, TypeError, AttributeError) as exc:
        raise EvidenceError("assignment_id_invalid") from exc
    return value


def endpoint(value, *, source=False):
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname or ""
        port = parts.port
    except (ValueError, TypeError) as exc:
        raise EvidenceError("url_invalid") from exc
    require(isinstance(value, str) and len(value) <= 2048 and not any(ord(c) < 33 for c in value), "url_invalid")
    require(host and not parts.username and not parts.password and not parts.fragment
            and not parts.query and "\\" not in value, "url_credentials_or_ambiguity")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    local = address is not None and address.is_loopback
    if source:
        require(parts.scheme == "https" and (address is None or address.is_global)
                and host != "localhost" and "." in host and not host.endswith((".local", ".localhost")),
                "source_public_https_required")
    else:
        require(parts.scheme == "https" or (parts.scheme == "http" and local), "tls_required")
        require(parts.path in ("", "/"), "base_url_origin_required")
    require(port is None or 1 <= port <= 65535, "url_port_invalid")
    return value.rstrip("/") if not source else value


def no_links(path):
    path = path.absolute()
    for candidate in (path, *path.parents):
        if candidate.exists() or candidate.is_symlink():
            info = candidate.lstat()
            require(not stat.S_ISLNK(info.st_mode)
                    and not (getattr(info, "st_file_attributes", 0) & 0x400), "path_link_refused")
    return path


def external_path(path, *, root=ROOT):
    path = no_links(Path(path))
    require(not path.resolve().is_relative_to(root.resolve()), "path_inside_repository")
    require(not any((parent / ".git").exists() for parent in path.parents), "path_inside_repository")
    require(not any(part.lower() in {".git", "backend", "uploads", "knowledge"} for part in path.parts),
            "path_sensitive_store")
    return path


def process(arguments, *, input_bytes=None, environment=None, maximum=MAX_JSON):
    try:
        result = subprocess.run(arguments, input=input_bytes, capture_output=True, timeout=45,
                                check=False, env=environment)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvidenceError("local_verifier_unavailable") from exc
    require(result.returncode == 0 and len(result.stdout) <= maximum, "local_verifier_failed")
    return result.stdout


def private_file(path):
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "session_regular_private_file_required")
    if os.name != "nt":
        require(info.st_uid == os.getuid() and info.st_mode & 0o077 == 0, "session_permissions_not_private")
        return
    # Never interpolate the path into shell source. Only a boolean leaves ACL inspection.
    code = """$ErrorActionPreference='Stop'
$acl=[System.IO.File]::GetAccessControl($env:ASTRAL_079_PRIVATE_FILE)
$me=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
$allowed=@($me,'S-1-5-18','S-1-5-32-544')
$bad=@($acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]) | Where-Object {
  $_.AccessControlType -eq 'Allow' -and $_.IdentityReference.Value -notin $allowed
})
if($owner -ne $me -or $bad.Count -gt 0){exit 1}
Write-Output 'private'
"""
    environment = {**os.environ, "ASTRAL_079_PRIVATE_FILE": str(path)}
    encoded = base64.b64encode(code.encode("utf-16le")).decode()
    require(process(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                    environment=environment).strip() == b"private", "session_permissions_not_private")


def create_private_file(path):
    """Create exclusively with private permissions before writing any bytes.

    Windows mode 0600 does not establish an NTFS DACL. Passing FileSecurity to
    CreateNew applies its protected owner-only ACL atomically, so no reader can
    acquire an inherited broad-read handle before the ACL is tightened.
    """
    if os.name != "nt":
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                       | getattr(os, "O_NOFOLLOW", 0), 0o600)
    code = """$ErrorActionPreference='Stop'
$me=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=[System.Security.AccessControl.FileSecurity]::new()
$acl.SetOwner($me)
$acl.SetAccessRuleProtection($true,$false)
$rule=[System.Security.AccessControl.FileSystemAccessRule]::new(
  $me,[System.Security.AccessControl.FileSystemRights]::FullControl,
  [System.Security.AccessControl.AccessControlType]::Allow)
$acl.AddAccessRule($rule)
$stream=[System.IO.FileStream]::new(
  $env:ASTRAL_079_PRIVATE_FILE,[System.IO.FileMode]::CreateNew,
  [System.Security.AccessControl.FileSystemRights]::Write,
  [System.IO.FileShare]::None,4096,[System.IO.FileOptions]::WriteThrough,$acl)
$stream.Dispose()
Write-Output 'created'
"""
    encoded = base64.b64encode(code.encode("utf-16le")).decode()
    require(process(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                    environment={**os.environ, "ASTRAL_079_PRIVATE_FILE": str(path)}).strip()
            == b"created", "private_file_creation_failed")
    no_links(path)
    private_file(path)
    before = path.stat()
    descriptor = os.open(path, os.O_WRONLY | os.O_BINARY)
    after = os.fstat(descriptor)
    if before.st_size != 0 or (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev, after.st_ino, after.st_size
    ):
        os.close(descriptor)
        raise EvidenceError("output_file_changed")
    return descriptor


def read_json_file(path, *, private=False, root=ROOT):
    path = external_path(path, root=root)
    if private:
        private_file(path)
    require(path.is_file() and path.stat().st_size <= MAX_JSON, "input_file_missing_or_oversized")
    before = path.stat()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        after = os.fstat(stream.fileno())
        require((before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size), "input_file_changed")
        return strict_json(stream.read(MAX_JSON + 1))


def load_session(path, base_url, assignment_id, scenario, *, root=ROOT, now=None):
    session = read_json_file(path, private=True, root=root)
    require(isinstance(session, dict) and session.get("schema_version") == 1, "session_schema_invalid")
    require(session.get("base_url") == base_url and session.get("assignment_id") == assignment_id,
            "session_target_mismatch")
    require(isinstance(session.get("allowed_scenarios"), list)
            and scenario in session["allowed_scenarios"], "scenario_not_owner_selected")
    now = now or datetime.now(UTC)
    expiry = instant(session.get("expires_at"))
    require(now + timedelta(seconds=30) < expiry <= now + timedelta(hours=1), "session_expired_or_not_short_lived")
    token = session.get("access_token")
    require(isinstance(token, str) and 32 <= len(token) <= 16384
            and re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token), "session_token_invalid")
    try:
        payload = token.split(".")[1]
        claims = strict_json(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, TypeError) as exc:
        raise EvidenceError("session_token_invalid") from exc
    require(isinstance(claims, dict) and isinstance(session.get("owner_sub"), str)
            and claims.get("sub") == session["owner_sub"] and 1 <= len(session["owner_sub"]) <= 512,
            "session_owner_mismatch")
    require(type(claims.get("exp")) is int and claims["exp"] >= expiry.timestamp(), "session_token_expired")
    require(not claims.get("act") and not claims.get("machine_turn_class"), "session_human_required")
    return session


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise EvidenceError("api_redirect_refused")


class ProductAPI:
    def __init__(self, base_url, session):
        self.base_url = base_url
        self.session = session
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def request(self, path, *, method="GET", body=None, expected=(200,)):
        require(path.startswith("/api/persistent-agents") and not path.startswith("//"), "api_path_refused")
        require(datetime.now(UTC) < instant(self.session["expires_at"]), "session_expired")
        data = None if body is None else canonical(body)
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self.session["access_token"],
                     "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=15) as response:
                status, raw = response.status, response.read(MAX_JSON + 1)
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read(MAX_JSON + 1)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise EvidenceError("api_transport_failed") from exc
        require(status in expected, "api_status_" + str(status))
        payload = strict_json(raw)
        require(isinstance(payload, dict), "api_response_invalid")
        return payload


def git(root, *arguments):
    return process(["git", "-C", str(root), *arguments], maximum=4 * MAX_JSON)


def runtime_manifest(root=ROOT):
    """Hash reviewed runtime paths only; never inspect generated user stores."""
    repos = [root, *(root / "components" / name for name in ("AstralPlane", "AstralProjection", "AstralPrimitives", "LETS"))]
    identities = {}
    selected = set()
    for repo in repos:
        name = repo.relative_to(root).as_posix() if repo != root else "."
        head = git(repo, "rev-parse", "HEAD").decode().strip()
        require(bool(re.fullmatch("[a-f0-9]{40}", head)), "candidate_git_identity_invalid")
        status = git(repo, "status", "--porcelain=v1", "-z")
        identities[name] = {"head": head, "working_tree_status_digest": hashlib.sha256(status).hexdigest()}
        paths = git(repo, "ls-files", "--cached", "-z").decode().split("\0")
        # New authored feature modules are part of the uncommitted candidate.
        new = git(repo, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
        for relative in [*paths, *new]:
            if not relative:
                continue
            path = repo / relative
            local = path.relative_to(root).as_posix()
            if relative in new and repo == root and not local.startswith(("backend/persistent_agents/",
                    "backend/orchestrator/", "backend/personalization/", "backend/shared/")):
                continue
            if path.suffix not in _RUNTIME_SUFFIXES or _EXCLUDED.intersection(path.relative_to(root).parts):
                continue
            if local.startswith("backend/") or local == "config/astral-composition.json" or any(
                    local.startswith(prefix + "/") for prefix in _PACKAGES):
                selected.add(local)
    require(0 < len(selected) <= 20_000, "candidate_manifest_bound")
    manifest = {}
    for relative in sorted(selected):
        path = no_links(root / relative)
        require(path.is_file() and path.stat().st_size <= 20 * MAX_JSON, "candidate_runtime_file_unavailable")
        target = {"package": None, "path": "/app/" + relative}
        for prefix, package in _PACKAGES.items():
            if relative.startswith(prefix + "/"):
                target = {"package": package, "path": relative[len(prefix) + 1:]}
                break
        manifest[relative] = {**target, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return {"repositories": identities, "runtime_manifest_digest": digest(manifest),
            "runtime_file_count": len(manifest)}, manifest


_DEPLOYED_HASH_SCRIPT = """import hashlib, importlib.util, json, pathlib, sys
manifest=json.load(sys.stdin)
out={}
roots={}
latest=0
for key, entry in manifest.items():
    package=entry['package']
    if package not in roots and package is not None:
        spec=importlib.util.find_spec(package)
        roots[package]=pathlib.Path(spec.origin).parent if spec and spec.origin else None
    root=roots.get(package)
    path=(root / entry['path']) if root is not None else pathlib.Path(entry['path'])
    out[key]=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and path.stat().st_size<=20971520 else None
    if path.is_file(): latest=max(latest,path.stat().st_mtime)
print(json.dumps({'digests':out,'latest_mtime':latest,'package_roots':{name:str(root.resolve()) for name,root in roots.items() if root is not None}},sort_keys=True,separators=(',',':')))
"""


def deployment_binding(base_url, container, candidate, manifest):
    parts = urllib.parse.urlsplit(base_url)
    try:
        local = ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        local = False
    require(local and parts.scheme == "http", "missing_input_local_deployment_binding")
    require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container)), "container_name_invalid")
    # Select only public identity fields. Full inspect includes credential env vars.
    template = '{"id":{{json .Id}},"image":{{json .Image}},"started_at":{{json .State.StartedAt}},"running":{{json .State.Running}},"ports":{{json .NetworkSettings.Ports}},"mounts":{{json .Mounts}}}'
    identity = strict_json(process(["docker", "inspect", "--format", template, container]))
    require(identity.get("running") is True, "deployment_not_running")
    ports = (identity.get("ports") or {}).get("8001/tcp") or []
    port = str(parts.port or 80)
    require(any(p.get("HostPort") == port and p.get("HostIp") in ("127.0.0.1", "0.0.0.0", "::", "::1")
                for p in ports), "deployment_endpoint_not_bound")
    observed = strict_json(process(["docker", "exec", "-i", container, "python", "-c", _DEPLOYED_HASH_SCRIPT],
                                  input_bytes=canonical(manifest), maximum=4 * MAX_JSON))
    expected = {key: value["sha256"] for key, value in manifest.items()}
    require(observed.get("digests") == expected, "deployed_runtime_differs_from_candidate")
    require(type(observed.get("latest_mtime")) in (float, int)
            and observed["latest_mtime"] <= instant(identity["started_at"]).timestamp(),
            "runtime_bytes_changed_since_container_start")
    package_roots = observed.get("package_roots", {})
    paths = [(package_roots.get(entry["package"], "") + "/" + entry["path"])
             if entry["package"] else entry["path"] for entry in manifest.values()]
    mounts = identity.get("mounts", [])
    require(isinstance(mounts, list) and all(isinstance(mount.get("Destination"), str) for mount in mounts),
            "deployment_mount_identity_missing")
    for mount in mounts:
        destination = mount["Destination"].rstrip("/")
        require(not any(path == destination or path.startswith(destination + "/") for path in paths),
                "mutable_runtime_mount_refused")
    return {"container_id": identity["id"], "image_id": identity["image"],
            "started_at": identity["started_at"], "runtime_manifest_digest": candidate["runtime_manifest_digest"],
            "scope": "reviewed backend and packaged component runtime files plus composition; diagnostic only"}


def page(api, base, collection):
    records, cursor = [], None
    for _ in range(MAX_ROWS // 100):
        key = "after_sequence" if collection == "activity" else "after_id"
        query = "?limit=100" + ("&" + key + "=" + urllib.parse.quote(str(cursor), safe="") if cursor is not None else "")
        response = api.request(base + "/" + collection + query)
        entries = response.get(collection)
        require(isinstance(entries, list) and len(entries) <= 100
                and all(isinstance(row, dict) for row in entries), "api_collection_invalid")
        records.extend(entries)
        following = response.get("next_cursor")
        if following is None:
            return records
        require(following != cursor and entries, "api_cursor_not_advancing")
        cursor = following
    raise EvidenceError("evidence_row_bound")


def snapshot(api, assignment_id, source_url):
    base = "/api/persistent-agents/" + assignment_id
    for _ in range(3):
        record = api.request(base).get("assignment", {})
        require(record.get("assignment_id") == assignment_id, "foreign_assignment_response")
        source = record.get("definition", {}).get("source", {})
        require(source.get("profile") == "public_page" and source.get("agent_id") == "web-research-1"
                and source.get("tool_name") == "fetch_page" and source.get("arguments", {}).get("url") == source_url,
                "assignment_source_mismatch")
        require(record.get("authority", {}).get("grant_bound") is True, "assignment_owner_consent_missing")
        result = {"assignment": record, **{name: page(api, base, name) for name in ("actions", "events", "activity")}}
        result["tasks"] = api.request(base + "/tasks").get("tasks")
        require(isinstance(result["tasks"], list) and len(result["tasks"]) <= 32, "api_tasks_invalid")
        again = api.request(base).get("assignment", {})
        if again.get("state_version") == record.get("state_version"):
            return result
    raise EvidenceError("assignment_snapshot_unstable")


def safe_snapshot(value):
    record = value["assignment"]
    usage = record.get("usage", {}).get("spent", {})
    require(all(type(usage.get(metric, 0)) is int and usage.get(metric, 0) >= 0 for metric in METRICS), "usage_invalid")
    require(type(record.get("last_completed_generation")) is int, "missing_input_completed_generation")
    actions = [{"id": row["action_id"], "state": row["state"], "request_digest": row["intent"]["request_digest"],
                "action_key_digest": digest(row["intent"]["action_key"]),
                "kind": row["intent"]["request"]["kind"], "event_id": row["intent"].get("event_id"),
                "attempts": [{key: item.get(key) for key in ("attempt_id", "state", "outcome", "result_digest")}
                             for item in row.get("attempts", [])]}
               for row in value["actions"]]
    events = [{key: row.get(key) for key in ("event_id", "identity_digest", "context_digest", "disposition", "result_digest")}
              for row in value["events"]]
    tasks = [{key: row.get(key) for key in ("task_id", "event_id", "state", "result_digest", "incorporated_by")}
             for row in value["tasks"]]
    activity = [{"id": row["activity_id"], "sequence": row["sequence"], "type": row["activity_type"],
                 "event_id": row.get("references", {}).get("event_id"), "notification_state": row.get("notification_state")}
                for row in value["activity"]]
    for rows, key in ((actions, "id"), (actions, "action_key_digest"), (events, "event_id"),
                      (events, "identity_digest"), (tasks, "task_id"), (activity, "id"), (activity, "sequence")):
        identities = [row[key] for row in rows]
        require(len(identities) == len(set(identities)), "duplicate_evidence_identity")
    return {"assignment_id": record["assignment_id"], "instruction_revision": record["instruction_revision"],
            "control_epoch": record["control_epoch"], "lifecycle": record["lifecycle"], "phase": record["phase"],
            "last_completed_generation": record["last_completed_generation"], "last_check_at": record.get("last_check_at"),
            "latest_result_digest": digest(record.get("latest_result")), "usage_spent": {key: usage.get(key, 0) for key in METRICS
                                                                                      if key in usage or key != "spend_micro_units"},
            "cost_status": record.get("cost_status"), "actions": actions, "events": events, "tasks": tasks, "activity": activity}


def control(api, record, command, *, expected=(200,)):
    return api.request("/api/persistent-agents/" + record["assignment_id"] + "/" + command, method="POST",
        body={"submission_id": str(uuid4()), "expected_instruction_revision": record["instruction_revision"],
              "expected_control_epoch": record["control_epoch"]}, expected=expected)


def completed(value):
    require(value["lifecycle"] == "active" and value["phase"] == "waiting", "assignment_not_waiting")
    require(value["last_check_at"] and value["last_completed_generation"] > 0, "missing_input_completed_live_check")
    require(any(row["disposition"] == "completed" for row in value["events"]), "missing_input_completed_event")
    require(any(row["state"] == "succeeded" for row in value["actions"]), "missing_input_completed_action")
    require(value["tasks"] and all(row["state"] == "completed" and row["result_digest"]
            and row["incorporated_by"].get("__assignment__") == row["result_digest"] for row in value["tasks"]),
            "missing_input_incorporated_child_results")
    findings = [row["event_id"] for row in value["activity"] if row["type"] == "finding"]
    require(all(findings) and len(findings) == len(set(findings)), "duplicate_finding")


def quiet_observation(api, assignment_id, source_url, seconds, *, sleep=time.sleep):
    first = snapshot(api, assignment_id, source_url)
    before = safe_snapshot(first)
    completed(before)
    wake = first["assignment"].get("next_wake_at")
    require(wake and instant(wake) > datetime.now(UTC) + timedelta(seconds=seconds + 2), "missing_input_idle_observation_window")
    sleep(seconds)
    after = safe_snapshot(snapshot(api, assignment_id, source_url))
    # Delivery acknowledgement may advance independently while the agent waits.
    def comparable(item):
        return {**item, "activity": [{key: value for key, value in row.items()
                                      if key != "notification_state"} for row in item["activity"]]}
    require(comparable(before) == comparable(after), "waiting_assignment_changed_during_idle_window")
    return after


def await_check(api, assignment_id, source_url, previous_generation, timeout, *, sleep=time.sleep):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = snapshot(api, assignment_id, source_url)
        record = value["assignment"]
        if record.get("last_completed_generation", 0) > previous_generation and record.get("phase") == "waiting":
            return value
        require(record.get("lifecycle") == "active" and record.get("phase") not in
                ("reconciliation", "waiting_approval", "waiting_authorization", "budget_exhausted", "failed"),
                "assignment_requires_owner_attention")
        sleep(1)
    raise EvidenceError("live_check_timeout")


def compare_restart(baseline, current, report):
    require(baseline.get("status") == "passed" and baseline.get("scenario") == "live-monitor", "baseline_not_successful_live_monitor")
    for key in ("owner_digest", "assignment_id", "source_digest", "candidate"):
        require(baseline.get(key) == report.get(key), "baseline_candidate_or_owner_mismatch")
    old_deployment = baseline.get("deployment", {})
    require(old_deployment.get("container_id") == report["deployment"]["container_id"]
            and old_deployment.get("image_id") == report["deployment"]["image_id"]
            and old_deployment.get("started_at") != report["deployment"]["started_at"], "missing_input_observed_container_restart")
    old = baseline.get("snapshot", {})
    require(all(current.get(key) == old.get(key) for key in ("instruction_revision", "control_epoch")),
            "assignment_revised_between_observations")
    require(current["last_completed_generation"] > old.get("last_completed_generation", 0), "missing_input_new_fenced_episode")
    for collection, key, state_key, terminal in (
            ("actions", "id", "state", "succeeded"), ("events", "event_id", "disposition", "completed"),
            ("tasks", "task_id", "state", "completed")):
        prior = {row[key]: row for row in old.get(collection, []) if row.get(state_key) == terminal}
        now = {row[key]: row for row in current[collection]}
        require(prior and all(now.get(identity) == row for identity, row in prior.items()), "completed_identity_changed_" + collection)
    completed_events = {row["event_id"] for row in old["events"] if row["disposition"] == "completed"}
    for collection, identity in (("actions", "id"), ("tasks", "task_id")):
        prior = {row[identity]: row for row in old[collection] if row.get("event_id") in completed_events}
        now = {row[identity]: row for row in current[collection] if row.get("event_id") in completed_events}
        require(prior == now, "completed_event_reprocessed_" + collection)
    old_findings = {row["id"]: row for row in old.get("activity", []) if row["type"] == "finding"}
    now_findings = {row["id"]: row for row in current["activity"] if row["type"] == "finding"}
    require(all(identity in now_findings and now_findings[identity]["event_id"] == row["event_id"]
                for identity, row in old_findings.items()), "prior_publication_changed")
    if {row["event_id"] for row in old["events"]} == {row["event_id"] for row in current["events"]}:
        require(all(current["usage_spent"].get(key) == old["usage_spent"].get(key) for key in ("model_calls", "tokens"))
                and set(now_findings) == set(old_findings), "unchanged_source_reprocessed")


def stale_revision_body(record):
    definition = record["definition"]
    limits = definition["limits"]
    lifetime = {key: limits[key] for key in (*METRICS, "currency") if key in limits}
    daily = {key: limits["daily_" + key] for key in METRICS if "daily_" + key in limits}
    if "currency" in lifetime:
        daily["currency"] = lifetime["currency"]
    return {"submission_id": str(uuid4()), "name": definition["name"], "instructions": definition["instructions"],
            "source": definition["source"], "allowed_tools": [{"agent_id": item.split(":", 1)[0],
            "tool_name": item.split(":", 1)[1]} for item in definition["allowed_tools"]],
            "completion_condition": definition.get("completion_condition"), "conversation_id": definition.get("conversation_id"),
            "limits": {**{key: limits[key] for key in ("cadence_seconds", "max_retries", "max_concurrent_tasks", "max_depth", "max_tasks", "step_timeout_ms")
                          if key in limits}, "daily": daily, "lifetime": lifetime}, "consent": False,
            "expected_instruction_revision": record["instruction_revision"], "expected_control_epoch": record["control_epoch"] + 1}


def controls_scenario(api, assignment_id, source_url):
    original = snapshot(api, assignment_id, source_url)["assignment"]
    require(original["lifecycle"] == "active", "controls_requires_active_disposable_assignment")
    base = "/api/persistent-agents/" + assignment_id
    conflict = api.request(base, method="PATCH", body=stale_revision_body(original), expected=(409,))
    require(conflict.get("error") in ("assignment_stale_control", "assignment_revision_conflict"), "revision_conflict_not_proven")
    paused = control(api, original, "pause")["assignment"]
    require(paused["lifecycle"] == "paused" and paused["control_epoch"] > original["control_epoch"], "pause_not_applied")
    resumed = control(api, paused, "resume")["assignment"]
    require(resumed["lifecycle"] == "active" and resumed["control_epoch"] > paused["control_epoch"], "resume_not_applied")
    stopped = control(api, resumed, "stop")["assignment"]
    require(stopped["lifecycle"] == "stopped" and stopped["control_epoch"] > resumed["control_epoch"], "stop_not_applied")
    control(api, stopped, "resume", expected=(409,))
    final = safe_snapshot(snapshot(api, assignment_id, source_url))
    require(final["lifecycle"] == "stopped", "terminal_stop_not_retained")
    return final


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--base-url", required=True)
    result.add_argument("--session-file", required=True, type=Path)
    result.add_argument("--source-url", required=True)
    result.add_argument("--assignment-id", required=True)
    result.add_argument("--scenario", required=True, choices=SCENARIOS)
    result.add_argument("--output", required=True, type=Path, help="New private diagnostic JSON outside repository; never overwritten")
    result.add_argument("--baseline", type=Path, help="Earlier live-monitor JSON; required for after-restart unless session has baseline_file")
    result.add_argument("--container", default="astraldeep", help="Existing local Docker container; inspected read-only")
    result.add_argument("--timeout", type=int, default=90, help="Maximum seconds to await a requested check (1..300)")
    result.add_argument("--observe-seconds", type=int, default=5, help="Quiet waiting observation window (2..30 seconds)")
    return result


def write_report(path, report, *, root=ROOT):
    path = external_path(path, root=root)
    require(path.parent.is_dir() and not path.exists(), "output_missing_parent_or_exists")
    raw = canonical(report) + b"\n"
    require(len(raw) <= MAX_JSON, "report_size_bound")
    descriptor = create_private_file(path)
    try:
        if os.name == "nt":
            private_file(path)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def run(args, *, root=ROOT):
    report = {"schema_version": 1, "feature": "079-persistent-agents", "scenario": args.scenario,
              "started_at": datetime.now(UTC).isoformat(), "status": "failed", "checks": [],
              "evidence_class": "live authenticated product API; local diagnostic candidate binding",
              "not_exercised": ["controlled upstream revisions", "two-worker exact crash boundaries", "cross-owner denial",
                                "spent-budget refusal", "uncertain external effect", "sensitive approval", "native client behavior"],
              "fixture_evidence": "Separate deterministic integration reports are required; this driver does not label them live."}
    try:
        output = external_path(args.output, root=root)
        require(output != Path(args.session_file).absolute() and not output.exists() and output.parent.is_dir(), "output_unsafe")
        require(1 <= args.timeout <= 300 and 2 <= args.observe_seconds <= 30, "observation_bounds_invalid")
        base_url = endpoint(args.base_url)
        source_url = endpoint(args.source_url, source=True)
        assignment_id = uuid(args.assignment_id)
        session = load_session(args.session_file, base_url, assignment_id, args.scenario, root=root)
        report.update(owner_digest=digest(session["owner_sub"]), assignment_id=assignment_id, source_digest=digest(source_url))
        report["candidate"], manifest = runtime_manifest(root)
        report["deployment"] = deployment_binding(base_url, args.container, report["candidate"], manifest)
        report["checks"].append("candidate_runtime_bytes_match_selected_running_container")
        api = ProductAPI(base_url, session)
        if args.scenario == "controls":
            report["snapshot"] = controls_scenario(api, assignment_id, source_url)
            report["checks"].extend(["stale_revision_refused_without_consent", "pause_resume_stop", "terminal_resume_refused"])
        else:
            baseline = None
            if args.scenario == "after-restart":
                baseline_path = args.baseline or session.get("baseline_file")
                require(baseline_path, "missing_input_baseline_report")
                baseline = read_json_file(baseline_path, root=root)
            before = snapshot(api, assignment_id, source_url)["assignment"]
            control(api, before, "run-now", expected=(202,))
            await_check(api, assignment_id, source_url, before.get("last_completed_generation", 0), args.timeout)
            report["snapshot"] = quiet_observation(api, assignment_id, source_url, args.observe_seconds)
            report["idle_observation_seconds"] = args.observe_seconds
            report["checks"].extend(["authenticated_owner_source_and_grant", "real_tool_dispatch_completed",
                "completed_event_action_and_child_receipts", "no_usage_or_durable_activity_while_waiting", "no_duplicate_finding"])
            if baseline is not None:
                compare_restart(baseline, report["snapshot"], report)
                report["checks"].append("observed_restart_new_fenced_episode_preserves_completed_identities")
            report["upstream_observation"] = "Observed public page baseline/check; no controlled release change asserted."
        # Re-read actual deployment bytes and candidate state after observation.
        current, current_manifest = runtime_manifest(root)
        require(current == report["candidate"], "candidate_changed_during_observation")
        require(deployment_binding(base_url, args.container, current, current_manifest) == report["deployment"],
                "deployment_changed_during_observation")
        report["status"] = "passed"
    except EvidenceError as exc:
        report["error_code"] = str(exc)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        report["error_code"] = "verification_input_or_response_invalid"
    report["finished_at"] = datetime.now(UTC).isoformat()
    try:
        write_report(args.output, report, root=root)
    except (EvidenceError, OSError):
        print("Verification failed: diagnostic output path unavailable.", file=sys.stderr)
        return 1
    print("Persistent-agent verification " + report["status"] + "; private diagnostic report written.")
    return 0 if report["status"] == "passed" else 1


def main(argv=None):
    return run(parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
