"""Windows release-evidence producer (T109, spec 060 US8).

Runs in the ``windows-producer`` job of ``release-readiness.yml`` against
T068's ALREADY-DOWNLOADED archived build-once unsigned EXE — this module never
downloads or rebuilds anything. It re-hashes the executable FIRST, drives the
packaged client plus a direct authenticated WebSocket session against the
trusted staging endpoint, and emits one schema-valid ``platform_evidence``
report (``platform: windows``) that the protected decision job later
re-validates independently. Local success is diagnostic only.

Environment contract (the workflow supplies every value; the module skips
module-wide ONLY when ``ASTRAL_WINDOWS_EXE`` is unset and never skips once it
is present):

- ``ASTRAL_WINDOWS_EXE``             path to the downloaded ``AstralDeep.exe``
- ``ASTRAL_WINDOWS_EXE_SHA256``      expected digest from the candidate job
- ``ASTRAL_STAGING_URL``             trusted staging endpoint (HTTPS)
- ``ASTRAL_RELEASE_EVIDENCE_OUTPUT`` where ``windows.json`` is written
- ``ASTRAL_RELEASE_STAGING_FILE``    trusted stage-deploy outputs JSON
- ``ASTRAL_WINDOWS_CANDIDATE_DIR``   downloaded candidate artifact directory
                                     (``reproducibility.json`` lives here)
- ``ASTRAL_WINDOWS_ARTIFACT_ID``     numeric candidate Actions artifact id
- ``ASTRAL_WINDOWS_SMOKE_TOKEN``     short-lived staging access token
- ``ASTRAL_RELEASE_CANDIDATE_SHA`` / ``ASTRAL_RELEASE_ID`` /
  ``ASTRAL_RELEASE_VERSION``         release identity
- ``ASTRAL_RELEASE_LIFECYCLE_AGENT_ID`` / ``ASTRAL_RELEASE_LIFECYCLE_STATES``
- ``GITHUB_REPOSITORY``, ``GITHUB_WORKFLOW``, ``GITHUB_RUN_ID``,
  ``GITHUB_RUN_ATTEMPT``, ``GITHUB_JOB``, ``RUNNER_OS``, ``RUNNER_ARCH``,
  ``RUNNER_NAME``, ``ASTRAL_RUNNER_ENVIRONMENT`` (+ ``ASTRAL_RUNNER_IMAGE``
  or the hosted ``ImageOS``/``ImageVersion`` pair)
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import importlib.util
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

if not os.getenv("ASTRAL_WINDOWS_EXE"):
    pytest.skip(
        "ASTRAL_WINDOWS_EXE is supplied by the windows-producer release job",
        allow_module_level=True,
    )

from astral_client import integrity  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
VALIDATOR = REPO / "scripts" / "validate_release_evidence.py"
SCHEMA = (
    REPO
    / "specs"
    / "060-runtime-reliability-hardening"
    / "contracts"
    / "release-evidence.schema.json"
)
PROMPT = "Roll exactly six six-sided dice and show the normalized results."
STAGING_FIELDS = (
    "authentication_posture",
    "candidate_image_reference",
    "candidate_image_sha256",
    "database_posture",
    "deployed_at",
    "deployment_run_id",
    "endpoint",
    "environment_id",
    "fixture_manifest_sha256",
    "keycloak_realm_sha256",
    "macos_personal_agent_host",
    "migrated_schema_revision",
    "representative_dataset_sha256",
    "source_schema_revision",
    "topology",
    "voice_runtime",
    "worker_paths",
)
VOICE_RUNTIME_FIELDS = {
    "voice_worker_image_reference",
    "voice_worker_image_sha256",
    "livekit_image_reference",
    "livekit_image_sha256",
    "livekit_config_sha256",
    "livekit_public_url",
    "livekit_turn_domain",
    "speech_profile",
}
SPEECH_PROFILE_FIELDS = {
    "asr_model",
    "tts_model",
    "voice",
    "sample_rate_hz",
    "inventory_sha256",
    "profile_sha256",
}
ASR_MODEL = "Systran/faster-whisper-large-v3"
TTS_MODEL = "speaches-ai/Kokoro-82M-v1.0-ONNX"
TTS_VOICE = "af_heart"
TTS_SAMPLE_RATE_HZ = 24000
REQUIRED_LIFECYCLE_STATES = {"starting", "online", "updating", "failed", "offline"}

_STARTED_AT = datetime.now(UTC).isoformat().replace("+00:00", "Z")
# check_id -> completed check object, filled in file order and assembled last.
_CHECKS: dict[str, dict] = {}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.fail(f"{name} is required once ASTRAL_WINDOWS_EXE is set")
    return value


def _exe() -> Path:
    path = Path(os.environ["ASTRAL_WINDOWS_EXE"])
    if not path.is_file():
        pytest.fail(f"ASTRAL_WINDOWS_EXE is not a file: {path}")
    return path


def _exe_sha256() -> str:
    return hashlib.sha256(_exe().read_bytes()).hexdigest()


def _staging_url() -> str:
    from urllib.parse import urlsplit

    raw = _required_env("ASTRAL_STAGING_URL").rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    ):
        pytest.fail(
            "ASTRAL_STAGING_URL must be credential-free non-loopback HTTPS"
        )
    return raw


def _ws_url() -> str:
    return "wss://" + _staging_url()[len("https://"):] + "/ws"


def _output_path() -> Path:
    return Path(_required_env("ASTRAL_RELEASE_EVIDENCE_OUTPUT")).resolve()


def _atomic_json(path: Path, value: dict) -> str:
    """Write pretty JSON atomically (temp + rename) and return its sha256."""

    data = (json.dumps(value, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4()}.tmp")
    with open(temporary, "xb") as handle:
        handle.write(data)
    os.replace(temporary, path)
    return hashlib.sha256(data).hexdigest()


def _write_raw(name: str, payload: dict) -> dict:
    """Persist one check's raw evidence and return its evidence_artifact row."""

    raw_dir = _output_path().parent / "windows-raw"
    sha256 = _atomic_json(raw_dir / f"{name}.json", payload)
    return {
        "name": f"windows_{name}",
        "kind": "json_metrics",
        "immutable_reference": f"bundle://windows-raw/{name}.json",
        "sha256": sha256,
    }


def _record_check(
    check_id: str,
    duration_s: float,
    raw_payload: dict,
    measurements: list[dict] | None = None,
) -> None:
    _CHECKS[check_id] = {
        "id": check_id,
        "outcome": "passed",
        "duration_ms": max(0, round(duration_s * 1000)),
        "detail_code": None,
        "applicability_reason": None,
        "measurements": measurements or [],
        "evidence_artifacts": [_write_raw(check_id, raw_payload)],
    }


def _clean_env(tmp_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "ASTRAL_MANAGED_DEPLOYMENT_PROFILE",
        "ASTRAL_WS_URL",
        "KEYCLOAK_AUTHORITY",
        "ASTRAL_CLIENT_ID",
        "KEYCLOAK_DESKTOP_CLIENT_ID",
        "ASTRAL_AUTH_BFF",
        "ASTRAL_TOKEN",
        "AGENT_API_KEY",
    ):
        environment.pop(name, None)
    roaming = tmp_path / "Roaming"
    local = tmp_path / "Local"
    roaming.mkdir(exist_ok=True)
    local.mkdir(exist_ok=True)
    environment["APPDATA"] = str(roaming)
    environment["LOCALAPPDATA"] = str(local)
    return environment


def _clear_native_windows_settings() -> None:
    """Give each GUI check the same absent-HKCU starting state as a new user."""

    if sys.platform != "win32":
        return
    result = subprocess.run(
        ["reg.exe", "delete", r"HKCU\Software\AstralDeep\WindowsClient", "/f"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr


def _staging_profile(path: Path) -> Path:
    """The bundled release profile, repointed at the trusted staging endpoint."""

    value = json.loads(
        (ROOT / "deployment" / "release-profile.json").read_text(encoding="utf-8")
    )
    value["websocket_endpoint"] = _ws_url()
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _staging_environment() -> dict:
    stage = json.loads(
        Path(_required_env("ASTRAL_RELEASE_STAGING_FILE")).read_text(encoding="utf-8")
    )
    missing = [field for field in STAGING_FIELDS if stage.get(field) is None]
    assert not missing, f"trusted staging output is missing {missing}"
    endpoint = str(stage["endpoint"]).rstrip("/")
    assert endpoint == _staging_url(), (
        "ASTRAL_STAGING_URL differs from the staged endpoint"
    )
    worker_paths = stage["worker_paths"]
    assert isinstance(worker_paths, list) and "voice" in worker_paths, (
        "trusted staging output does not include the voice worker path"
    )
    _validate_voice_runtime(stage["voice_runtime"])
    return {field: stage[field] for field in STAGING_FIELDS}


def _validate_voice_runtime(value: object) -> None:
    """Validate, but never reconstruct, the trusted stage voice identity."""

    assert isinstance(value, dict) and set(value) == VOICE_RUNTIME_FIELDS, (
        "trusted voice_runtime has a missing, extra, or non-object field"
    )
    profile = value["speech_profile"]
    assert isinstance(profile, dict) and set(profile) == SPEECH_PROFILE_FIELDS, (
        "trusted speech_profile has a missing, extra, or non-object field"
    )
    assert profile["asr_model"] == ASR_MODEL
    assert profile["tts_model"] == TTS_MODEL
    assert profile["voice"] == TTS_VOICE
    assert profile["sample_rate_hz"] == TTS_SAMPLE_RATE_HZ
    for digest_field in (
        "voice_worker_image_sha256",
        "livekit_image_sha256",
        "livekit_config_sha256",
    ):
        assert isinstance(value[digest_field], str) and re.fullmatch(
            r"[0-9a-f]{64}", value[digest_field]
        ), f"trusted voice_runtime {digest_field} is not a SHA-256 digest"
    for digest_field in ("inventory_sha256", "profile_sha256"):
        assert isinstance(profile[digest_field], str) and re.fullmatch(
            r"[0-9a-f]{64}", profile[digest_field]
        ), f"trusted speech_profile {digest_field} is not a SHA-256 digest"
    for reference_field in (
        "voice_worker_image_reference",
        "livekit_image_reference",
        "livekit_public_url",
        "livekit_turn_domain",
    ):
        assert (
            isinstance(value[reference_field], str) and value[reference_field].strip()
        ), f"trusted voice_runtime {reference_field} is empty"


def _runner_identity() -> dict:
    os_name = _required_env("RUNNER_OS").lower()
    architecture = {"x64": "x86_64", "x86_64": "x86_64", "arm64": "arm64"}.get(
        _required_env("RUNNER_ARCH").lower()
    )
    environment = _required_env("ASTRAL_RUNNER_ENVIRONMENT")
    if (
        os_name != "windows"
        or architecture is None
        or environment not in {"github_hosted", "self_hosted"}
    ):
        pytest.fail("windows runner identity is outside the release schema")
    image = os.getenv("ASTRAL_RUNNER_IMAGE")
    if not image and os.getenv("ImageOS") and os.getenv("ImageVersion"):
        image = f"{os.environ['ImageOS']}-{os.environ['ImageVersion']}"
    if not image:
        pytest.fail("ASTRAL_RUNNER_IMAGE or ImageOS/ImageVersion must be present")
    return {
        "os": os_name,
        "architecture": architecture,
        "runner_image": image,
        "runner_name": _required_env("RUNNER_NAME"),
        "runner_environment": environment,
    }


def _workflow_identity() -> dict:
    attempt = int(_required_env("GITHUB_RUN_ATTEMPT"))
    assert attempt >= 1
    return {
        "name": _required_env("GITHUB_WORKFLOW"),
        "run_id": _required_env("GITHUB_RUN_ID"),
        "run_attempt": attempt,
        "job_id": _required_env("GITHUB_JOB"),
    }


def _artifact() -> dict:
    """The exact archived candidate EXE identity, re-hashed locally."""

    repository = _required_env("GITHUB_REPOSITORY")
    run_id = _required_env("GITHUB_RUN_ID")
    attempt = _required_env("GITHUB_RUN_ATTEMPT")
    artifact_id = _required_env("ASTRAL_WINDOWS_ARTIFACT_ID")
    assert artifact_id.isdigit() and not artifact_id.startswith("0")
    return {
        "name": "AstralDeep.exe",
        "kind": "windows_exe",
        "immutable_reference": (
            f"gh://{repository}/runs/{run_id}/attempts/{attempt}"
            f"/artifacts/{artifact_id}/members/AstralDeep.exe"
        ),
        "sha256": _exe_sha256(),
        "build_identity": (
            f"windows-candidate:{_required_env('ASTRAL_RELEASE_CANDIDATE_SHA')}"
        ),
    }


# ---------------------------------------------------------------------------
# WebSocket session driving (the e2e_live.py seams, pointed at staging)
# ---------------------------------------------------------------------------


async def _open_session(token: str, session_id: str | None = None):
    import websockets

    ws = await websockets.connect(_ws_url(), max_size=16 * 1024 * 1024, open_timeout=20)
    frame = {
        "type": "register_ui",
        "token": token,
        "capabilities": ["render", "stream"],
        "session_id": session_id,
        "device": {
            "device_type": "windows",
            "screen_width": 1280,
            "screen_height": 860,
            "viewport_width": 1280,
            "viewport_height": 860,
            "pixel_ratio": 1.0,
            "has_touch": False,
        },
        "resumed": session_id is not None,
    }
    await ws.send(json.dumps(frame))
    return ws


async def _recv_json(ws, timeout: float) -> dict | None:
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def _await_frame(ws, predicate, deadline_s: float) -> dict | None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + deadline_s
    while loop.time() < deadline:
        msg = await _recv_json(ws, max(0.1, deadline - loop.time()))
        if msg is None:
            return None
        if msg and predicate(msg):
            return msg
    return None


def _transcript_messages(msg: dict) -> list:
    chat = msg.get("chat") or (msg.get("payload") or {}).get("chat") or {}
    messages = chat.get("messages")
    return messages if isinstance(messages, list) else []


async def _load_chat_snapshot(ws, chat_id: str, deadline_s: float) -> list:
    """Request the chat and return its transcript messages (empty on timeout)."""

    await ws.send(
        json.dumps(
            {
                "type": "ui_event",
                "action": "load_chat",
                "session_id": chat_id,
                "payload": {"chat_id": chat_id},
            }
        )
    )
    msg = await _await_frame(
        ws, lambda m: m.get("type") == "chat_loaded", deadline_s
    )
    return _transcript_messages(msg) if msg else []


# ---------------------------------------------------------------------------
# 0. Identity precondition — re-hash BEFORE executing anything
# ---------------------------------------------------------------------------


def test_downloaded_exe_rehash_matches_candidate_digest_first():
    assert sys.platform == "win32", (
        "the windows evidence producer requires a real Windows runner"
    )
    expected = _required_env("ASTRAL_WINDOWS_EXE_SHA256").lower()
    actual = _exe_sha256()
    assert actual == expected, (
        f"archived EXE bytes drifted: re-hash {actual} != candidate {expected}"
    )


# ---------------------------------------------------------------------------
# Windows extra checks driven from the frozen executable
# ---------------------------------------------------------------------------


def test_windows_deployment_validation_report(tmp_path):
    started = time.monotonic()
    report = tmp_path / "deployment-validation.json"
    result = subprocess.run(
        [str(_exe()), "--validate-deployment", "--report", str(report)],
        env=_clean_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(report.read_text(encoding="utf-8"))
    assert value["status"] == "valid"
    assert value["client_version"] == "0.4.0"
    assert value["source"] == "bundled_release"
    assert value["byo_host_disposition"] == "authenticated_ui_tunnel"
    assert value["legacy_tools_disposition"] == "disabled"
    assert value["requirements_lock_sha256"] == value["required_runtime_lock_sha256"]
    assert "authority" not in value and "websocket_endpoint" not in value
    _record_check(
        "windows_deployment_validation", time.monotonic() - started, value
    )


def test_windows_frozen_worker_completes_benign_round_trip(tmp_path):
    started = time.monotonic()
    agent = tmp_path / "benign-agent"
    agent.mkdir()
    (agent / "agent_main.py").write_text(
        "import json, sys\n"
        "def main():\n"
        "    value = json.loads(sys.stdin.readline())\n"
        "    print(json.dumps({'type':'smoke_result','echo':value}), flush=True)\n"
        "    return 0\n",
        encoding="utf-8",
    )
    request = {"type": "smoke", "value": 6}
    result = subprocess.run(
        [str(_exe()), "--byo-worker", str(agent)],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "type": "smoke_result",
        "echo": request,
    }
    _record_check(
        "windows_frozen_worker",
        time.monotonic() - started,
        {"round_trip": "json_lines_stdio", "echo_verified": True},
    )


def test_windows_upgrade_from_0_3_0_selects_candidate_release(monkeypatch):
    """Drive the shipped v0.3.0 updater parser against an API-shaped fixture."""

    started = time.monotonic()
    version = _required_env("ASTRAL_RELEASE_VERSION")
    fixture = {
        "id": 60001,
        "tag_name": f"v{version}",
        "name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "html_url": "https://releases.invalid/candidate",
        "assets": [
            {
                "name": "AstralDeep.exe",
                "id": 60011,
                "browser_download_url": "https://releases.invalid/AstralDeep.exe",
            },
            {
                "name": "SHA256SUMS",
                "id": 60012,
                "browser_download_url": "https://releases.invalid/SHA256SUMS",
            },
            {
                "name": "cosign.bundle",
                "id": 60013,
                "browser_download_url": "https://releases.invalid/cosign.bundle",
            },
        ],
    }
    expected_path = f"repos/{integrity._REPO}/releases/latest"
    monkeypatch.setattr(
        integrity,
        "_api_get",
        lambda path: fixture if path == expected_path else None,
    )
    assets = integrity.latest_release()
    assert assets is not None, "the shipped parser refused the candidate release"
    assert assets.version == version
    assert assets.tag == f"v{version}"
    assert assets.release_id == 60001
    assert assets.asset_ids == (60011, 60012, 60013)
    assert integrity.is_newer_version(assets.version, "0.3.0") is True
    assert integrity.is_newer_version("0.3.0", assets.version) is False
    assert integrity.parse_semver("0.3.0") < integrity.parse_semver(assets.version)
    with pytest.raises(ValueError):
        integrity.parse_semver(f" {version}")
    with pytest.raises(ValueError):
        integrity.parse_semver(f"v{version}")
    _record_check(
        "windows_upgrade_from_0_3_0",
        time.monotonic() - started,
        {
            "selected_version": assets.version,
            "selected_tag": assets.tag,
            "newer_than_0_3_0": True,
            "strict_semver_whitespace_refused": True,
        },
    )


def test_dependency_lock_reproducibility_manifest_binds_tracked_lock():
    started = time.monotonic()
    candidate_dir = Path(_required_env("ASTRAL_WINDOWS_CANDIDATE_DIR"))
    value = json.loads(
        (candidate_dir / "reproducibility.json").read_text(encoding="utf-8")
    )
    assert value["document_type"] == "windows_release_reproducibility"
    assert value["status"] == "passed"
    lock_sha256 = hashlib.sha256(
        (ROOT / "requirements-release.lock.txt").read_bytes()
    ).hexdigest()
    assert value["requirements_lock_sha256"] == lock_sha256, (
        "candidate reproducibility manifest binds a different release lock"
    )
    assert int(value["package_count"]) >= 1
    _record_check("dependency_lock_reproducibility", time.monotonic() - started, value)


# ---------------------------------------------------------------------------
# Common client checks against the trusted staging endpoint
# ---------------------------------------------------------------------------


def test_sign_in_accepts_staging_token_and_refuses_a_stale_principal():
    started = time.monotonic()
    token = _required_env("ASTRAL_WINDOWS_SMOKE_TOKEN")

    async def _drive() -> dict:
        # Negative control: a garbage principal must be refused up front.
        ws = await _open_session(f"invalid-{uuid.uuid4()}")
        try:
            refused = await _await_frame(
                ws, lambda m: m.get("type") == "auth_required", 20.0
            )
        finally:
            await ws.close()
        assert refused is not None, "an invalid token was not refused"

        ws = await _open_session(token)
        try:
            await ws.send(
                json.dumps({"type": "ui_event", "action": "get_history", "payload": {}})
            )
            frame = await _await_frame(
                ws,
                lambda m: m.get("type") in {"history_list", "auth_required"},
                30.0,
            )
        finally:
            await ws.close()
        assert frame is not None and frame.get("type") == "history_list", (
            f"authenticated registration failed: {frame}"
        )
        return {
            "method": "staging_access_token_register_ui",
            "invalid_token_refused": True,
            "authenticated_history_received": True,
        }

    payload = asyncio.run(_drive())
    _record_check("sign_in", time.monotonic() - started, payload)


@pytest.mark.skipif(sys.platform != "win32", reason="requires the frozen Windows GUI")
def test_rendered_chat_completes_in_the_frozen_gui_against_staging(tmp_path):
    started = time.monotonic()
    token = _required_env("ASTRAL_WINDOWS_SMOKE_TOKEN")
    _clear_native_windows_settings()
    report = tmp_path / "rendered-chat-smoke.json"
    profile = _staging_profile(tmp_path / "staging-profile.json")
    environment = _clean_env(tmp_path)
    environment["ASTRAL_TOKEN"] = token
    result = subprocess.run(
        [
            str(_exe()),
            "--deployment-profile",
            str(profile),
            "--release-smoke-report",
            str(report),
            "--release-smoke-prompt",
            PROMPT,
            "--release-smoke-timeout",
            "90",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=110,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(report.read_text(encoding="utf-8"))
    assert value["status"] == "passed"
    assert value["detail_code"] == "rendered_turn_complete"
    assert value["transcript_turns"] >= 2
    assert value["canvas_components"] >= 1
    assert value["window_profile_match"] is True
    assert value["byo_profile_match"] is True
    assert value["tools_agent_profile_match"] is True
    _record_check("rendered_chat", time.monotonic() - started, value)


def test_reconnect_resume_restores_the_conversation_twenty_times():
    started = time.monotonic()
    token = _required_env("ASTRAL_WINDOWS_SMOKE_TOKEN")

    async def _drive() -> dict:
        ws = await _open_session(token)
        try:
            await ws.send(
                json.dumps({"type": "ui_event", "action": "new_chat", "payload": {}})
            )
            created = await _await_frame(
                ws, lambda m: m.get("type") == "chat_created", 30.0
            )
            assert created is not None, "new_chat drew no chat_created"
            chat_id = (created.get("payload") or {}).get("chat_id") or created.get(
                "chat_id"
            )
            assert chat_id, created
            await ws.send(
                json.dumps(
                    {
                        "type": "ui_event",
                        "action": "chat_message",
                        "session_id": chat_id,
                        "payload": {"message": PROMPT, "chat_id": chat_id},
                    }
                )
            )
            # The turn is complete when the durable transcript holds both sides.
            loop = asyncio.get_event_loop()
            deadline = loop.time() + 240.0
            baseline: list = []
            while loop.time() < deadline:
                baseline = await _load_chat_snapshot(ws, chat_id, 10.0)
                if len(baseline) >= 2:
                    break
                await asyncio.sleep(2.0)
            assert len(baseline) >= 2, "the staged turn never completed"
        finally:
            await ws.close()

        trials = 20
        successes = 0
        latencies_ms: list[int] = []
        for _trial in range(trials):
            trial_started = time.monotonic()
            ws = await _open_session(token, session_id=chat_id)
            try:
                messages = await _load_chat_snapshot(ws, chat_id, 5.0)
                if len(messages) >= len(baseline):
                    successes += 1
            finally:
                await ws.close()
            latencies_ms.append(round((time.monotonic() - trial_started) * 1000))
        return {
            "chat_id_sha256": hashlib.sha256(chat_id.encode("utf-8")).hexdigest(),
            "trial_count": trials,
            "successful_trials": successes,
            "transcript_messages": len(baseline),
            "latencies_ms": latencies_ms,
        }

    payload = asyncio.run(_drive())
    assert payload["successful_trials"] == payload["trial_count"] == 20
    _record_check(
        "reconnect_resume",
        time.monotonic() - started,
        payload,
        measurements=[
            {
                "metric": "trial_count",
                "aggregation": "total",
                "value": 20,
                "unit": "count",
                "sample_count": 20,
                "comparator": "gte",
                "threshold": 20,
            },
            {
                "metric": "resume_success_rate",
                "aggregation": "rate",
                "value": 100,
                "unit": "percent",
                "sample_count": 20,
                "comparator": "gte",
                "threshold": 100,
            },
        ],
    )


def test_agent_lifecycle_states_are_observed_and_generation_fenced():
    started = time.monotonic()
    token = _required_env("ASTRAL_WINDOWS_SMOKE_TOKEN")
    agent_id = _required_env("ASTRAL_RELEASE_LIFECYCLE_AGENT_ID")
    expected = {
        state
        for state in _required_env("ASTRAL_RELEASE_LIFECYCLE_STATES").split(",")
        if state
    }
    assert expected and expected <= REQUIRED_LIFECYCLE_STATES, (
        "ASTRAL_RELEASE_LIFECYCLE_STATES is invalid"
    )
    window_s = float(os.getenv("ASTRAL_RELEASE_LIFECYCLE_TIMEOUT", "120"))

    async def _drive() -> list[dict]:
        events: list[dict] = []
        ws = await _open_session(token)
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + window_s
            while loop.time() < deadline:
                msg = await _recv_json(ws, max(0.1, deadline - loop.time()))
                if msg is None:
                    break
                if (
                    msg.get("type") == "agent_lifecycle"
                    and msg.get("agent_id") == agent_id
                ):
                    events.append(msg)
                    if expected <= {event.get("state") for event in events}:
                        break
        finally:
            await ws.close()
        return events

    events = asyncio.run(_drive())
    observed = {event.get("state") for event in events}
    assert expected <= observed, (
        f"lifecycle states {sorted(expected - observed)} never arrived for {agent_id}"
    )
    for event in events:
        assert event.get("state") in REQUIRED_LIFECYCLE_STATES
        generation = event.get("lifecycle_generation")
        revision = event.get("state_revision")
        assert isinstance(generation, int) and generation >= 0
        assert isinstance(revision, int) and revision >= 0
    _record_check(
        "agent_lifecycle",
        time.monotonic() - started,
        {
            "agent_id_sha256": hashlib.sha256(agent_id.encode("utf-8")).hexdigest(),
            "required_states": sorted(expected),
            "events": [
                {
                    "state": event.get("state"),
                    "generation": event.get("lifecycle_generation"),
                    "revision": event.get("state_revision"),
                }
                for event in events
            ],
        },
    )


def test_personal_agent_authoring_surface_and_benign_host_round_trip(tmp_path):
    started = time.monotonic()
    token = _required_env("ASTRAL_WINDOWS_SMOKE_TOKEN")

    async def _drive() -> dict:
        ws = await _open_session(token)
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "ui_event",
                        "action": "chrome_open",
                        "payload": {"surface": "agent_authoring", "params": {}},
                    }
                )
            )
            frame = await _await_frame(
                ws,
                lambda m: (
                    m.get("type") == "chrome_surface"
                    and m.get("surface_key") == "agent_authoring"
                )
                or (
                    m.get("type") == "chrome_render"
                    and "agents" in str(m.get("html", "")).lower()
                ),
                30.0,
            )
        finally:
            await ws.close()
        assert frame is not None, "the authoring surface never rendered"
        if frame.get("type") == "chrome_surface":
            assert frame.get("components"), "authoring surface arrived empty"
        return {"surface": "agent_authoring", "delivered_as": frame["type"]}

    payload = asyncio.run(_drive())

    # The delivered personal agent runs on the OWNER's desktop: prove the
    # packaged host worker completes a benign hosted round trip.
    agent = tmp_path / "benign-personal-agent"
    agent.mkdir()
    (agent / "agent_main.py").write_text(
        "import json, sys\n"
        "def main():\n"
        "    value = json.loads(sys.stdin.readline())\n"
        "    print(json.dumps({'type':'greeting','for':value.get('owner')}),"
        " flush=True)\n"
        "    return 0\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(_exe()), "--byo-worker", str(agent)],
        input=json.dumps({"type": "greet", "owner": "release-evidence"}) + "\n",
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "type": "greeting",
        "for": "release-evidence",
    }
    payload["benign_host_round_trip"] = True
    _record_check("personal_agent", time.monotonic() - started, payload)


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows HWND inspection")
def test_windows_clean_profile_no_dialog_on_fresh_hkcu(tmp_path):
    started = time.monotonic()
    token = _required_env("ASTRAL_WINDOWS_SMOKE_TOKEN")
    _clear_native_windows_settings()
    profile = _staging_profile(tmp_path / "staging-profile.json")
    environment = _clean_env(tmp_path)
    environment["ASTRAL_TOKEN"] = token
    process = subprocess.Popen(
        [str(_exe()), "--deployment-profile", str(profile)], env=environment
    )
    titles: list[str] = []
    try:
        user32 = ctypes.windll.user32

        def _window_titles() -> list[str]:
            found: list[str] = []
            callback_type = ctypes.WINFUNCTYPE(
                ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
            )

            def _visit(hwnd, _lparam):
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value != process.pid or not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if buffer.value:
                    found.append(buffer.value)
                return True

            user32.EnumWindows(callback_type(_visit), 0)
            return found

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            titles = _window_titles()
            if any(title == "AstralDeep — Windows" for title in titles):
                break
            time.sleep(0.1)
        assert process.poll() is None
        assert "AstralDeep — Windows" in titles
        assert all("Configure AstralDeep" not in title for title in titles)
    finally:
        process.terminate()
        process.wait(timeout=10)
    _record_check(
        "windows_clean_profile_no_dialog",
        time.monotonic() - started,
        {"window_titles": titles, "configure_dialog_seen": False},
    )


def test_accessibility_semantics_of_changed_windows_controls():
    started = time.monotonic()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["ASTRAL_WIN_AGENT"] = "0"
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QAccessible
    from PySide6.QtWidgets import QApplication, QCheckBox, QLabel

    from astral_client.app import AgentsDialog, TopBar

    app = QApplication.instance() or QApplication([])

    def _role(widget):
        interface = QAccessible.queryAccessibleInterface(widget)
        assert interface is not None
        return interface.role()

    topbar = TopBar("user", lambda: None, lambda: None, lambda *_: None, lambda: None)
    assert isinstance(topbar._mark, QLabel)
    assert _role(topbar._mark) == QAccessible.Role.StaticText
    assert topbar._mark.accessibleName() == "Application status"
    topbar.set_status("Saving credentials", "#ffffff")
    assert topbar._mark.accessibleDescription() == "Saving credentials"
    topbar.close()

    emitted: list = []
    dialog = AgentsDialog(None, lambda action, payload: emitted.append((action, payload)))
    dialog.set_agents(
        [
            {
                "id": "windows-tools-1",
                "name": "Windows coding",
                "description": "Local tools",
                "scopes": {"tools:read": True, "tools:write": False},
                "is_public": False,
                "_lifecycle_label": "Agent online",
            }
        ]
    )
    dialog.show()
    app.processEvents()
    scopes = dialog.findChildren(QCheckBox, "agentScopeToggle")
    assert len(scopes) == 3
    for checkbox in scopes:
        assert _role(checkbox) == QAccessible.Role.CheckBox
        assert checkbox.accessibleName()
        assert checkbox.accessibleDescription()
        assert checkbox.focusPolicy() != Qt.FocusPolicy.NoFocus
    lifecycle = dialog.findChild(QLabel, "agentLifecycleStatus")
    assert lifecycle is not None
    assert lifecycle.accessibleName() == "Windows coding lifecycle status"
    assert lifecycle.accessibleDescription() == "Agent online"
    dialog.close()
    _record_check(
        "accessibility_semantics",
        time.monotonic() - started,
        {
            "status_mark_named_and_stateful": True,
            "scope_toggles_named_focusable": len(scopes),
            "lifecycle_label_named": True,
        },
    )


# ---------------------------------------------------------------------------
# Final assembly: emit + in-process schema/policy validation
# ---------------------------------------------------------------------------


def _validator_module():
    spec = importlib.util.spec_from_file_location(
        "windows_release_evidence_validator", VALIDATOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_emit_schema_valid_windows_platform_evidence_report():
    validator = _validator_module()
    required = set(validator.REQUIRED_CHECKS["windows"])
    missing = required - set(_CHECKS)
    assert not missing, f"checks never produced evidence: {sorted(missing)}"
    assert set(_CHECKS) == required

    order = (
        "sign_in",
        "rendered_chat",
        "reconnect_resume",
        "agent_lifecycle",
        "accessibility_semantics",
        "personal_agent",
        "windows_deployment_validation",
        "windows_clean_profile_no_dialog",
        "windows_frozen_worker",
        "windows_upgrade_from_0_3_0",
        "dependency_lock_reproducibility",
    )
    report = {
        "document_type": "platform_evidence",
        "schema_version": 1,
        "evidence_id": str(uuid.uuid4()),
        "candidate_sha": _required_env("ASTRAL_RELEASE_CANDIDATE_SHA"),
        "release_id": _required_env("ASTRAL_RELEASE_ID"),
        "release_version": _required_env("ASTRAL_RELEASE_VERSION"),
        "platform": "windows",
        "target_description": (
            "T068 archived build-once unsigned Windows 0.4.0 executable driven "
            "against the trusted staging endpoint"
        ),
        "artifact": _artifact(),
        "staging_environment": _staging_environment(),
        "runner": _runner_identity(),
        "workflow": _workflow_identity(),
        "started_at": _STARTED_AT,
        "completed_at": _now_iso(),
        "outcome": "passed",
        "unavailable_reason": None,
        "unavailability_observation": None,
        "checks": [_CHECKS[check_id] for check_id in order],
    }

    schema = validator.load_json_document(SCHEMA)
    validator.validate_document(report, schema)
    for check in report["checks"]:
        validator._validate_measurements(check)
    validator._canonical_staging(report["staging_environment"])

    output = _output_path()
    sha256 = _atomic_json(output, report)
    reparsed = validator.load_json_document(output)
    validator.validate_document(reparsed, schema)
    assert hashlib.sha256(output.read_bytes()).hexdigest() == sha256
