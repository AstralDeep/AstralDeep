"""Feature 060 backend release-evidence producer (T108, US8).

Emits ``backend.json`` — one ``platform_evidence`` document with
``platform: "backend"`` — against the exact T107 staging endpoint, then
schema-validates the emitted document in-process with the production policy
engine (``scripts/validate_release_evidence.py``).  The output is DIAGNOSTIC
release-evidence input: protected CI independently re-validates it and no
local run ever carries release authorization.

The producer is gated: every test in this module skips cleanly unless BOTH
``ASTRAL_STAGING_URL`` and ``ASTRAL_RELEASE_EVIDENCE_OUTPUT`` are set (except
the env-independent contract self-tests, which always run).  When the gate is
present the producer NEVER skips — a missing identity variable or a missed
metric floor fails loudly instead.

Metric floors are replayed with the EXISTING reliability machinery:

* ``runtime_admission_stress`` — the SC-001 1,000-frame admission suite
  (``tests/perf/test_runtime_reliability_060.py``, marker ``perf``);
* ``scheduler_exactly_once`` — the SC-002 10,000-interleaving effect-ledger
  trial (``scheduler/tests/test_occurrence_claims_060.py``);
* ``migration_multi_instance`` — the SC-017 50-trial two-starter convergence
  test (``tests/test_migrations_060.py``);
* ``process_supervision_stress`` — 100 fresh SC-020 high-output/descendant/
  cancel/quit/failure trials on the production
  ``shared.process_supervision.ProcessSupervisor``.

Each replayed suite either re-executes here (subprocess pytest with a junit
report) or, when the producer job already ran it, is read from the exact
pre-run junit bytes passed via ``ASTRAL_RELEASE_*_JUNIT``.  The raw junit and
JSON metric bytes are bundled under ``backend-raw/`` and referenced from each
check as ``bundle://`` evidence artifacts with their exact SHA-256 digests.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import re
import ssl
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from xml.etree import ElementTree

import pytest


pytestmark = pytest.mark.perf

REPO_ROOT = Path(__file__).resolve().parents[3]
if not (
    (REPO_ROOT / "scripts").is_dir() and (REPO_ROOT / "specs").is_dir()
):  # repo root absent inside the product image
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )
BACKEND_ROOT = REPO_ROOT / "backend"
VALIDATOR_PATH = REPO_ROOT / "scripts" / "validate_release_evidence.py"
EVIDENCE_SCHEMA_PATH = (
    REPO_ROOT
    / "specs"
    / "060-runtime-reliability-hardening"
    / "contracts"
    / "release-evidence.schema.json"
)

#: Both must be set for the producer to run; either absent means clean skip.
GATE_ENVIRONMENT = ("ASTRAL_STAGING_URL", "ASTRAL_RELEASE_EVIDENCE_OUTPUT")

#: Required once the gate is present; absence is a failure, never a skip.
IDENTITY_ENVIRONMENT = (
    "ASTRAL_RELEASE_CANDIDATE_SHA",
    "ASTRAL_RELEASE_ID",
    "ASTRAL_RELEASE_STAGING_FILE",
    "ASTRAL_RELEASE_VERSION",
    "ASTRAL_RUNNER_ENVIRONMENT",
    "ASTRAL_STAGING_PROBE_TOKEN",
    "GITHUB_JOB",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_RUN_ID",
    "GITHUB_WORKFLOW",
    "RUNNER_ARCH",
    "RUNNER_NAME",
    "RUNNER_OS",
)

#: The exact stage-owned identity every platform report must repeat.
STAGING_ENVIRONMENT_FIELDS = (
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

VOICE_RUNTIME_FIELDS = frozenset(
    {
        "voice_worker_image_reference",
        "voice_worker_image_sha256",
        "livekit_image_reference",
        "livekit_image_sha256",
        "livekit_config_sha256",
        "livekit_public_url",
        "livekit_turn_domain",
        "speech_profile",
    }
)
SPEECH_PROFILE_FIELDS = frozenset(
    {
        "asr_model",
        "tts_model",
        "voice",
        "sample_rate_hz",
        "inventory_sha256",
        "profile_sha256",
    }
)
ASR_MODEL = "Systran/faster-whisper-large-v3"
TTS_MODEL = "speaches-ai/Kokoro-82M-v1.0-ONNX"
TTS_VOICE = "af_heart"
TTS_SAMPLE_RATE_HZ = 24000

GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)

SUPERVISION_TRIAL_COUNT = 100
SUITE_TIMEOUT_SECONDS = 1800
PROBE_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class _ReplayedSuite:
    """One existing reliability suite that pins a backend metric floor."""

    check_id: str
    junit_environment: str
    pytest_arguments: tuple[str, ...]
    required_test: str
    #: Backend-relative suite source plus the exact pinned counter line the
    #: self-test re-reads so the emitted values can never silently drift.
    source: str
    counter_pattern: str
    #: (metric, measured value, sample_count) — semantics come from the
    #: validator's METRIC_REQUIREMENTS so they are canonical by construction.
    measurements: tuple[tuple[str, int, int], ...]
    extra_environment: Mapping[str, str] = field(default_factory=dict)


REPLAYED_SUITES = (
    _ReplayedSuite(
        check_id="runtime_admission_stress",
        junit_environment="ASTRAL_RELEASE_ADMISSION_JUNIT",
        pytest_arguments=("tests/perf/test_runtime_reliability_060.py", "-m", "perf"),
        required_test="test_thousand_read_only_frames_are_bounded_and_fully_accounted",
        source="tests/perf/test_runtime_reliability_060.py",
        counter_pattern=r"^FRAME_COUNT = 1_000$",
        measurements=(
            ("message_count", 1000, 1000),
            ("active_limit_violations", 0, 1000),
            ("unresolved_operations", 0, 1000),
        ),
        extra_environment={"LOOP_GUARD_ENFORCE": "1"},
    ),
    _ReplayedSuite(
        check_id="scheduler_exactly_once",
        junit_environment="ASTRAL_RELEASE_SCHEDULER_JUNIT",
        pytest_arguments=(
            "scheduler/tests/test_occurrence_claims_060.py"
            "::test_deterministic_10000_interleavings_publish_one_visible_effect",
        ),
        required_test=(
            "test_deterministic_10000_interleavings_publish_one_visible_effect"
        ),
        source="scheduler/tests/test_occurrence_claims_060.py",
        counter_pattern=r"^    for index in range\(10_000\):$",
        measurements=(
            ("interleaving_count", 10000, 10000),
            ("duplicate_effects", 0, 10000),
        ),
    ),
    _ReplayedSuite(
        check_id="migration_multi_instance",
        junit_environment="ASTRAL_RELEASE_MIGRATION_JUNIT",
        pytest_arguments=(
            "tests/test_migrations_060.py"
            "::test_fifty_two_starter_schema_and_policy_trials_converge_once",
        ),
        required_test=(
            "test_fifty_two_starter_schema_and_policy_trials_converge_once"
        ),
        source="tests/test_migrations_060.py",
        counter_pattern=r"^    trial_count = 50$",
        measurements=(
            ("trial_count", 50, 50),
            ("migration_owner_violations", 0, 50),
        ),
    ),
)

BACKEND_CHECK_IDS = (
    "candidate_staging",
    "runtime_admission_stress",
    "scheduler_exactly_once",
    "migration_multi_instance",
    "process_supervision_stress",
)


def _load_validator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "release_validator_060_backend_producer", VALIDATOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _gate() -> tuple[str, Path]:
    values = [os.environ.get(name, "").strip() for name in GATE_ENVIRONMENT]
    if not all(values):
        pytest.skip(
            "backend release producer runs only with "
            + " and ".join(GATE_ENVIRONMENT)
        )
    output = Path(values[1])
    assert output.is_absolute(), "ASTRAL_RELEASE_EVIDENCE_OUTPUT must be absolute"
    return values[0], output


def _require_environment(name: str) -> str:
    value = os.environ.get(name, "")
    assert value, f"trusted backend producer environment is incomplete: {name}"
    return value


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _normalized_endpoint(raw: str) -> str:
    parsed = urlsplit(raw)
    hostname = (parsed.hostname or "").lower()
    assert (
        parsed.scheme == "https"
        and hostname
        and hostname not in {"localhost", "127.0.0.1", "::1"}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    ), "ASTRAL_STAGING_URL must be credential-free non-loopback HTTPS"
    return raw.rstrip("/")


def _staging_environment(stage: Mapping[str, Any], endpoint: str) -> dict[str, Any]:
    for name in STAGING_ENVIRONMENT_FIELDS:
        assert stage.get(name) is not None, f"trusted staging output is missing {name}"
    assert str(stage["endpoint"]).rstrip("/") == endpoint, (
        "ASTRAL_STAGING_URL differs from the trusted staged endpoint"
    )
    worker_paths = stage["worker_paths"]
    assert isinstance(worker_paths, list) and "voice" in worker_paths, (
        "trusted staging output does not include the voice worker path"
    )
    _validate_voice_runtime(stage["voice_runtime"])
    return {name: stage[name] for name in STAGING_ENVIRONMENT_FIELDS}


def _validate_voice_runtime(value: Any) -> None:
    """Fail closed without rebuilding the stage-owned, secret-free identity."""

    assert isinstance(value, Mapping) and set(value) == VOICE_RUNTIME_FIELDS, (
        "trusted voice_runtime has a missing, extra, or non-object field"
    )
    profile = value["speech_profile"]
    assert isinstance(profile, Mapping) and set(profile) == SPEECH_PROFILE_FIELDS, (
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


def _runner_identity() -> dict[str, Any]:
    os_name = _require_environment("RUNNER_OS").lower()
    architecture = {"x64": "x86_64", "x86_64": "x86_64", "arm64": "arm64"}.get(
        _require_environment("RUNNER_ARCH").lower()
    )
    environment = _require_environment("ASTRAL_RUNNER_ENVIRONMENT")
    assert os_name == "linux", "the backend producer runs on a Linux runner"
    assert architecture, "RUNNER_ARCH is outside the release schema"
    assert environment in {"github_hosted", "self_hosted"}, (
        "ASTRAL_RUNNER_ENVIRONMENT is outside the release schema"
    )
    runner_image = os.environ.get("ASTRAL_RUNNER_IMAGE", "")
    if not runner_image:
        image_os = os.environ.get("ImageOS", "")
        image_version = os.environ.get("ImageVersion", "")
        if image_os and image_version:
            runner_image = f"{image_os}-{image_version}"
    assert runner_image, (
        "runner image identity requires ASTRAL_RUNNER_IMAGE or ImageOS/ImageVersion"
    )
    return {
        "os": os_name,
        "architecture": architecture,
        "runner_image": runner_image,
        "runner_name": _require_environment("RUNNER_NAME"),
        "runner_environment": environment,
    }


def _workflow_identity() -> dict[str, Any]:
    attempt = int(_require_environment("GITHUB_RUN_ATTEMPT"))
    assert attempt >= 1, "GITHUB_RUN_ATTEMPT is invalid"
    return {
        "name": _require_environment("GITHUB_WORKFLOW"),
        "run_id": _require_environment("GITHUB_RUN_ID"),
        "run_attempt": attempt,
        "job_id": _require_environment("GITHUB_JOB"),
    }


def _atomic_write_bytes(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4()}.tmp")
    with open(temporary, "xb") as handle:
        handle.write(data)
    os.replace(temporary, path)
    return hashlib.sha256(data).hexdigest()


def _raw_artifact(
    raw_root: Path, name: str, kind: str, filename: str, data: bytes
) -> dict[str, Any]:
    sha256 = _atomic_write_bytes(raw_root / filename, data)
    return {
        "name": name,
        "kind": kind,
        "immutable_reference": f"bundle://backend-raw/{filename}",
        "sha256": sha256,
    }


def _raw_json_artifact(
    raw_root: Path, name: str, filename: str, value: Any
) -> dict[str, Any]:
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return _raw_artifact(raw_root, name, "json_metrics", filename, data)


def _measurement_satisfies(value: float, comparator: str, threshold: float) -> bool:
    return {
        "eq": value == threshold,
        "lt": value < threshold,
        "lte": value <= threshold,
        "gt": value > threshold,
        "gte": value >= threshold,
    }[comparator]


def _policy_measurement(
    validator: Any, check_id: str, metric: str, value: int, sample_count: int
) -> dict[str, Any]:
    """Build the canonical measurement straight from the release policy."""

    requirement = validator.METRIC_REQUIREMENTS[check_id][metric]
    assert _measurement_satisfies(
        float(value), requirement.comparator, float(requirement.threshold)
    ), (
        f"measured {check_id}/{metric}={value} misses the release floor "
        f"({requirement.comparator} {requirement.threshold})"
    )
    assert sample_count >= 1
    return {
        "metric": metric,
        "aggregation": requirement.aggregation,
        "value": value,
        "unit": requirement.unit,
        "sample_count": sample_count,
        "comparator": requirement.comparator,
        "threshold": requirement.threshold,
    }


def _passed_check(
    check_id: str,
    duration_ms: int,
    measurements: list[dict[str, Any]],
    evidence_artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": check_id,
        "outcome": "passed",
        "duration_ms": max(0, int(duration_ms)),
        "detail_code": None,
        "applicability_reason": None,
        "measurements": measurements,
        "evidence_artifacts": evidence_artifacts,
    }


def _probe_staging(endpoint: str, token: str) -> dict[str, Any]:
    """Prove the staged endpoint's reachability from this producer itself."""

    parsed = urlsplit(endpoint)
    context = ssl.create_default_context()
    base_path = parsed.path.rstrip("/")
    probes: dict[str, Any] = {}
    for probe_name, path, headers in (
        ("readyz", f"{base_path}/readyz", {}),
        (
            "dashboard",
            f"{base_path}/api/dashboard",
            {"Authorization": f"Bearer {token}"},
        ),
    ):
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            timeout=PROBE_TIMEOUT_SECONDS,
            context=context,
        )
        try:
            started = time.monotonic()
            connection.request("GET", path or "/", headers=headers)
            response = connection.getresponse()
            body = response.read(1024 * 1024 + 1)
            latency_ms = int((time.monotonic() - started) * 1000)
            assert response.status == 200, (
                f"staging {probe_name} probe returned HTTP {response.status}"
            )
            probes[probe_name] = {"status": response.status, "latency_ms": latency_ms}
            if probe_name == "dashboard":
                document = json.loads(body)
                capability = (
                    document.get("capabilities", {})
                    .get("personal_agent_host", {})
                    .get("macos")
                )
                assert isinstance(capability, dict), (
                    "candidate dashboard lacks the macOS host capability map"
                )
                probes[probe_name]["capability_map_present"] = True
        finally:
            connection.close()
    return probes


def _parse_junit(data: bytes) -> dict[str, Any]:
    """Fail-closed pytest junit summary: totals plus per-case pass names."""

    root = ElementTree.fromstring(data)
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    assert suites, "junit report contains no testsuite element"
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    passed_cases: list[str] = []
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.get(key, "0"))
        for case in suite.iter("testcase"):
            outcomes = {child.tag for child in case}
            if not outcomes & {"failure", "error", "skipped"}:
                passed_cases.append(case.get("name", ""))
    assert totals["tests"] >= 1, "junit report contains no test cases"
    return {**totals, "passed_cases": sorted(passed_cases)}


def _case_passed(summary: Mapping[str, Any], required_test: str) -> bool:
    return any(
        name == required_test or name.startswith(f"{required_test}[")
        for name in summary["passed_cases"]
    )


def _replay_suite(suite: _ReplayedSuite, junit_directory: Path) -> tuple[bytes, dict[str, Any]]:
    """Run the existing suite (or read its pre-run junit) and prove it green."""

    pre_run = os.environ.get(suite.junit_environment, "")
    if pre_run:
        junit_path = Path(pre_run)
        assert junit_path.is_file(), (
            f"{suite.junit_environment} names a missing junit report: {junit_path}"
        )
        executed = "pre_run_junit"
    else:
        junit_path = junit_directory / f"{suite.check_id}.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            *suite.pytest_arguments,
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junit-xml={junit_path}",
        ]
        completed = subprocess.run(
            command,
            cwd=BACKEND_ROOT,
            env={**os.environ, **suite.extra_environment},
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=SUITE_TIMEOUT_SECONDS,
        )
        assert completed.returncode == 0, (
            f"{suite.check_id} replay failed:\n{completed.stdout[-8000:]}"
        )
        executed = "executed_here"
    junit_bytes = junit_path.read_bytes()
    summary = _parse_junit(junit_bytes)
    assert summary["failures"] == 0 and summary["errors"] == 0, (
        f"{suite.check_id} junit records failures: {summary}"
    )
    assert summary["skipped"] == 0, (
        f"{suite.check_id} junit records skips — the floor was not replayed"
    )
    assert _case_passed(summary, suite.required_test), (
        f"{suite.check_id} junit lacks a passing {suite.required_test}"
    )
    summary["replay_mode"] = executed
    summary["suite"] = suite.source
    summary["required_test"] = suite.required_test
    return junit_bytes, summary


def _run_supervision_trials(trial_count: int) -> dict[str, Any]:
    """SC-020: fresh trials on the production process-supervision machinery."""

    backend_root = os.fspath(BACKEND_ROOT)
    if backend_root not in sys.path:
        sys.path.insert(0, backend_root)
    # Imported inside the gated path so collection stays stdlib-only.
    from shared.process_supervision import (
        OutputStream,
        ProcessOwner,
        ProcessSupervisor,
        TerminationReason,
    )

    behaviors = (
        (
            "high_output",
            "import sys\n"
            "print('ready', flush=True)\n"
            "for _ in range(1200):\n"
            "    sys.stdout.write('x' * 220 + '\\n')\n"
            "sys.stdout.flush()\n"
            "import time; time.sleep(30)\n",
        ),
        (
            # Mirrors test_process_supervision._tree_script: the parent reaps
            # its descendant on shutdown so reaper-less hosts leave no zombie.
            "descendant",
            "import signal, subprocess, sys, time\n"
            "grandchild = subprocess.Popen("
            "[sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "def shutdown(*_):\n"
            "    try:\n"
            "        grandchild.wait(timeout=1.0)\n"
            "    except Exception:\n"
            "        pass\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, shutdown)\n"
            "signal.signal(signal.SIGINT, shutdown)\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n",
        ),
        (
            "plain",
            "print('ready', flush=True)\nimport time; time.sleep(30)\n",
        ),
        (
            "failing_child",
            "print('ready', flush=True)\nraise SystemExit(3)\n",
        ),
    )
    reasons = (
        TerminationReason.CANCEL,
        TerminationReason.QUIT,
        TerminationReason.STOP,
        TerminationReason.FAILURE,
    )
    supervisor = ProcessSupervisor()
    residual_processes = 0
    trials: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        for trial in range(trial_count):
            behavior, script = behaviors[trial % len(behaviors)]
            reason = reasons[(trial // len(behaviors)) % len(reasons)]
            child = supervisor.spawn(
                process_id=uuid.uuid4(),
                owner=ProcessOwner(
                    owner_kind="release_evidence_producer",
                    owner_id=f"trial-{trial}",
                ),
                argv=(sys.executable, "-u", "-c", script),
            )
            child.wait_for_line(OutputStream.STDOUT, prefix=b"ready", timeout=30)
            snapshot = child.terminate(reason=reason)
            clean = (
                snapshot.process_tree_terminated
                and snapshot.readers_joined
                and snapshot.pipes_closed
                and snapshot.cleanup_error is None
            )
            within_limits = (
                snapshot.stdout.retained_bytes
                <= supervisor.limits.ring_capacity_bytes_per_stream
                and snapshot.stdout.maximum_retained_line_bytes
                <= supervisor.limits.maximum_logical_line_bytes
            )
            if not (clean and within_limits):
                residual_processes += 1
            trials.append(
                {
                    "trial": trial,
                    "behavior": behavior,
                    "reason": reason.value,
                    "clean": clean,
                    "within_limits": within_limits,
                    "cleanup_seconds": round(snapshot.cleanup_duration_seconds, 3),
                }
            )
    finally:
        supervisor.terminate_all(reason=TerminationReason.QUIT)
    return {
        "trial_count": trial_count,
        "residual_processes": residual_processes,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "limits": {
            "ring_capacity_bytes_per_stream": (
                supervisor.limits.ring_capacity_bytes_per_stream
            ),
            "maximum_logical_line_bytes": (
                supervisor.limits.maximum_logical_line_bytes
            ),
            "termination_deadline_seconds": (
                supervisor.limits.termination_deadline_seconds
            ),
        },
        "trials": trials,
    }


# ---------------------------------------------------------------------------
# Env-independent contract self-tests (always run; keep CI parity honest).
# ---------------------------------------------------------------------------


def test_backend_check_set_and_floors_match_release_policy() -> None:
    validator = _load_validator()
    produced = {suite.check_id for suite in REPLAYED_SUITES} | {
        "candidate_staging",
        "process_supervision_stress",
    }
    assert produced == validator.REQUIRED_CHECKS["backend"]
    assert tuple(sorted(BACKEND_CHECK_IDS)) == tuple(sorted(produced))
    for suite in REPLAYED_SUITES:
        requirements = validator.METRIC_REQUIREMENTS[suite.check_id]
        assert {metric for metric, _, _ in suite.measurements} == set(requirements)
        for metric, value, sample_count in suite.measurements:
            document = _policy_measurement(
                validator, suite.check_id, metric, value, sample_count
            )
            assert set(document) == {
                "metric",
                "aggregation",
                "value",
                "unit",
                "sample_count",
                "comparator",
                "threshold",
            }
    supervision = validator.METRIC_REQUIREMENTS["process_supervision_stress"]
    assert SUPERVISION_TRIAL_COUNT >= supervision["trial_count"].threshold


def test_reused_suite_counters_still_pin_metric_floors() -> None:
    """The emitted values must match the exact counters the suites execute."""

    for suite in REPLAYED_SUITES:
        source = (BACKEND_ROOT / suite.source).read_text(encoding="utf-8")
        assert re.search(suite.counter_pattern, source, flags=re.MULTILINE), (
            f"{suite.source} no longer pins the counter {suite.counter_pattern!r};"
            " update the producer's emitted measurements together with the suite"
        )


def test_junit_parsing_is_fail_closed(tmp_path: Path) -> None:
    passing = (
        '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0">'
        '<testcase name="test_thousand_read_only_frames_are_bounded_and_fully_accounted[legacy]"/>'
        '<testcase name="test_other"/>'
        "</testsuite></testsuites>"
    ).encode("utf-8")
    summary = _parse_junit(passing)
    assert summary["tests"] == 2 and summary["failures"] == 0
    assert _case_passed(
        summary, "test_thousand_read_only_frames_are_bounded_and_fully_accounted"
    )
    assert not _case_passed(summary, "test_absent")

    failing = (
        '<testsuite tests="1" failures="1" errors="0" skipped="0">'
        '<testcase name="test_broken"><failure message="boom"/></testcase>'
        "</testsuite>"
    ).encode("utf-8")
    failed = _parse_junit(failing)
    assert failed["failures"] == 1
    assert not _case_passed(failed, "test_broken")
    with pytest.raises(AssertionError):
        _parse_junit(b"<testsuites></testsuites>")


# ---------------------------------------------------------------------------
# The gated producer itself.
# ---------------------------------------------------------------------------


def test_backend_release_evidence_binds_staging_and_metric_floors(
    tmp_path: Path,
) -> None:
    endpoint_raw, output = _gate()
    for name in IDENTITY_ENVIRONMENT:
        _require_environment(name)
    endpoint = _normalized_endpoint(endpoint_raw)
    candidate_sha = _require_environment("ASTRAL_RELEASE_CANDIDATE_SHA")
    assert GIT_SHA_RE.fullmatch(candidate_sha), "candidate SHA is malformed"
    release_id = _require_environment("ASTRAL_RELEASE_ID")
    assert RELEASE_ID_RE.fullmatch(release_id), "release id is malformed"
    release_version = _require_environment("ASTRAL_RELEASE_VERSION")
    assert SEMVER_RE.fullmatch(release_version) and not re.search(
        r"\s", release_version
    ), "release version is malformed"

    validator = _load_validator()
    schema = validator.load_json_document(EVIDENCE_SCHEMA_PATH)
    stage = validator.load_json_document(
        Path(_require_environment("ASTRAL_RELEASE_STAGING_FILE"))
    )
    staging = _staging_environment(stage, endpoint)
    raw_root = output.parent / "backend-raw"
    started_at = _now_iso()
    checks: list[dict[str, Any]] = []

    # candidate_staging — reachability of the exact staged endpoint, proven
    # from this producer, plus the authenticated candidate dashboard.
    probe_started = time.monotonic()
    probes = _probe_staging(endpoint, _require_environment("ASTRAL_STAGING_PROBE_TOKEN"))
    probe_duration_ms = int((time.monotonic() - probe_started) * 1000)
    staging_artifact = _raw_json_artifact(
        raw_root,
        "backend_candidate_staging",
        "candidate_staging.json",
        {
            "endpoint": endpoint,
            "probes": probes,
            "migrated_schema_revision": staging["migrated_schema_revision"],
            "source_schema_revision": staging["source_schema_revision"],
            "environment_id": staging["environment_id"],
        },
    )
    checks.append(
        _passed_check(
            "candidate_staging",
            probe_duration_ms,
            [
                {
                    "metric": "endpoint_probes_succeeded",
                    "aggregation": "total",
                    "value": len(probes),
                    "unit": "count",
                    "sample_count": len(probes),
                    "comparator": "gte",
                    "threshold": 2,
                },
                {
                    "metric": "readyz_latency_ms",
                    "aggregation": "maximum",
                    "value": probes["readyz"]["latency_ms"],
                    "unit": "milliseconds",
                    "sample_count": 1,
                    "comparator": "lte",
                    "threshold": 60000,
                },
                {
                    "metric": "dashboard_latency_ms",
                    "aggregation": "maximum",
                    "value": probes["dashboard"]["latency_ms"],
                    "unit": "milliseconds",
                    "sample_count": 1,
                    "comparator": "lte",
                    "threshold": 60000,
                },
            ],
            [staging_artifact],
        )
    )

    # The three replayed reliability suites — junit-bound metric floors.
    for suite in REPLAYED_SUITES:
        replay_started = time.monotonic()
        junit_bytes, summary = _replay_suite(suite, tmp_path)
        duration_ms = int((time.monotonic() - replay_started) * 1000)
        junit_artifact = _raw_artifact(
            raw_root,
            f"backend_{suite.check_id}_junit",
            "junit",
            f"{suite.check_id}-junit.xml",
            junit_bytes,
        )
        metrics_artifact = _raw_json_artifact(
            raw_root,
            f"backend_{suite.check_id}",
            f"{suite.check_id}.json",
            summary,
        )
        checks.append(
            _passed_check(
                suite.check_id,
                duration_ms,
                [
                    _policy_measurement(
                        validator, suite.check_id, metric, value, sample_count
                    )
                    for metric, value, sample_count in suite.measurements
                ],
                [metrics_artifact, junit_artifact],
            )
        )

    # process_supervision_stress — 100 fresh SC-020 trials, zero residue.
    supervision = _run_supervision_trials(SUPERVISION_TRIAL_COUNT)
    assert supervision["residual_processes"] == 0, (
        f"supervision trials left residue: {supervision}"
    )
    supervision_artifact = _raw_json_artifact(
        raw_root,
        "backend_process_supervision_stress",
        "process_supervision_stress.json",
        supervision,
    )
    checks.append(
        _passed_check(
            "process_supervision_stress",
            supervision["duration_ms"],
            [
                _policy_measurement(
                    validator,
                    "process_supervision_stress",
                    "trial_count",
                    supervision["trial_count"],
                    supervision["trial_count"],
                ),
                _policy_measurement(
                    validator,
                    "process_supervision_stress",
                    "residual_processes",
                    supervision["residual_processes"],
                    supervision["trial_count"],
                ),
            ],
            [supervision_artifact],
        )
    )

    report = {
        "document_type": "platform_evidence",
        "schema_version": 1,
        "evidence_id": str(uuid.uuid4()),
        "candidate_sha": candidate_sha,
        "release_id": release_id,
        "release_version": release_version,
        "platform": "backend",
        "target_description": (
            "Candidate backend container proven against the trusted TLS "
            "staging endpoint with replayed reliability floors"
        ),
        "artifact": {
            "name": "astraldeep-backend-candidate",
            "kind": "container",
            "immutable_reference": f"oci://{staging['candidate_image_reference']}",
            "sha256": staging["candidate_image_sha256"],
            "build_identity": f"candidate-container:{candidate_sha}",
        },
        "staging_environment": staging,
        "runner": _runner_identity(),
        "workflow": _workflow_identity(),
        "started_at": started_at,
        "completed_at": _now_iso(),
        "outcome": "passed",
        "unavailable_reason": None,
        "unavailability_observation": None,
        "checks": checks,
    }

    assert [check["id"] for check in report["checks"]] == list(BACKEND_CHECK_IDS)
    validator.validate_document(report, schema)
    # Same helpers the production policy engine applies to submitted evidence;
    # emitting an under-floor or noncanonical measurement must be impossible.
    for check in report["checks"]:
        validator._validate_measurements(check)
    validator._canonical_staging(report["staging_environment"])

    _atomic_write_bytes(
        output, (json.dumps(report, indent=2) + "\n").encode("utf-8")
    )
    reloaded = validator.load_json_document(output)
    validator.validate_document(reloaded, schema)
    assert reloaded["candidate_sha"] == candidate_sha
    assert reloaded["outcome"] == "passed"
