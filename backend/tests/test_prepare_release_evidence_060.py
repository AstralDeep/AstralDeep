"""Deterministic local pre-push evidence command contracts (T107)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "prepare_release_evidence.py"
CONTRACT_TEST_PATH = REPO_ROOT / "backend" / "tests" / "test_release_contract_schemas.py"
MATRIX_TEST_PATH = REPO_ROOT / "backend" / "tests" / "test_release_evidence_validator.py"

if not (
    (REPO_ROOT / "scripts").is_dir() and (REPO_ROOT / "specs").is_dir()
):  # repo root absent inside the product image
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )
BASE_SHA = "b" * 40  # Differs from the contract examples' GIT_SHA ("a" * 40).
NOW = "2026-07-16T12:00:00Z"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def prepare() -> Any:
    return _load_module("prepare_release_evidence_060", SCRIPT_PATH)


@pytest.fixture(scope="module")
def contract_examples() -> Any:
    return _load_module("release_contract_examples_060_t107", CONTRACT_TEST_PATH)


@pytest.fixture(scope="module")
def matrix_helpers() -> Any:
    return _load_module("release_evidence_matrix_helpers_060", MATRIX_TEST_PATH)


def _write_matrix(
    evidence_dir: Path,
    contract_examples: Any,
    matrix_helpers: Any,
    *,
    candidate_sha: str | None = None,
) -> list[dict[str, Any]]:
    """Write one passing per-platform report file per required target."""

    evidence_dir.mkdir(parents=True, exist_ok=True)
    targets = ["backend", "web", "windows", "android", "macos", "ios", "watchos", "docs"]
    reports = []
    for index, target in enumerate(targets, 1):
        report = contract_examples._platform_evidence(target)
        if candidate_sha is not None:
            report["candidate_sha"] = candidate_sha
        report["evidence_id"] = f"00000000-0000-4000-8000-{index:012d}"
        matrix_helpers._add_required_measurements(report)
        (evidence_dir / f"{target}.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        reports.append(report)
    return reports


def _exception_request(contract_examples: Any) -> dict[str, Any]:
    return {
        "document_type": "evidence_exception_request",
        "schema_version": 1,
        "exception_id": "33333333-3333-4333-8333-333333333333",
        "candidate_sha": contract_examples.GIT_SHA,
        "release_id": "release-060-1",
        "platform": "windows",
        "missing_checks": ["windows_deployment_validation"],
        "reason": "windows runner pool offline for this candidate",
        "requester_login": "fixture-author",
        "requested_at": "2026-07-16T11:00:00Z",
        "maximum_valid_days": 7,
        "blocks_next_release": True,
    }


def _run(
    prepare: Any,
    contract_examples: Any,
    evidence_dir: Path,
    output: Path,
    *extra: str,
) -> int:
    return prepare.main(
        [
            "--evidence-dir",
            str(evidence_dir),
            "--base-sha",
            BASE_SHA,
            "--candidate-sha",
            contract_examples.GIT_SHA,
            "--output",
            str(output),
            "--now",
            NOW,
            "--coverage-mode",
            "partial",
            *extra,
        ]
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _cobertura(path: Path, filename: str, *, hits: int, line: int = 1) -> Path:
    covered = 1 if hits else 0
    path.write_text(
        f'<coverage lines-valid="1" lines-covered="{covered}" '
        f'line-rate="{covered}"><sources><source>/work</source></sources>'
        "<packages><package><classes>"
        f'<class filename="{filename}" line-rate="{covered}"><lines>'
        f'<line number="{line}" hits="{hits}"/></lines></class>'
        "</classes></package></packages></coverage>\n",
        encoding="utf-8",
    )
    return path


def _cobertura_many(
    path: Path, sources: dict[str, dict[int, int]]
) -> Path:
    total = sum(len(lines) for lines in sources.values())
    covered = sum(
        hits > 0 for lines in sources.values() for hits in lines.values()
    )
    rate = covered / total if total else 1
    classes = "".join(
        f'<class filename="{filename}" line-rate="'
        f'{sum(hit > 0 for hit in lines.values()) / len(lines)}"><lines>'
        + "".join(
            f'<line number="{line}" hits="{hits}"/>'
            for line, hits in lines.items()
        )
        + "</lines></class>"
        for filename, lines in sources.items()
    )
    path.write_text(
        f'<coverage lines-valid="{total}" lines-covered="{covered}" '
        f'line-rate="{rate}"><sources><source>/work</source></sources>'
        f"<packages><package><classes>{classes}</classes></package></packages>"
        "</coverage>\n",
        encoding="utf-8",
    )
    return path


def _native_coverage_report(prepare: Any, root: Path, slot: str) -> Path:
    """Write one minimal parseable report with a useful slot-scoped observation."""

    producer = prepare.COVERAGE_INPUT_SLOTS[slot]
    if slot == "backend":
        return _cobertura(root / "backend.xml", "backend/service.py", hits=1)
    if slot == "voice_worker":
        return _cobertura(
            root / "voice-worker.xml", "backend/voice_agent/main.py", hits=1
        )
    if slot == "tooling":
        return _cobertura(root / "tooling.xml", "scripts/release.py", hits=1)
    if slot == "windows":
        return _cobertura(root / "windows.xml", "components/AstralProjection/windows-client/app.py", hits=1)
    if slot == "javascript":
        path = root / "javascript.json"
        source = "components/AstralProjection/backend/webrender/static/client.js"
        path.write_text(
            json.dumps(
                {
                    **prepare.COVERAGE.JAVASCRIPT_REPORT_IDENTITY,
                    "coverage": {
                        source: {
                            "path": source,
                            "statementMap": {
                                "0": {
                                    "start": {"line": 1, "column": 0},
                                    "end": {"line": 1, "column": 1},
                                }
                            },
                            "s": {"0": 1},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return path
    if producer.target_key in {"android_app", "android_core"}:
        path = root / f"{slot}.xml"
        source_name = "App.kt" if slot == "android_app" else "Core.kt"
        path.write_text(
            '<report><package name="com/example"><sourcefile '
            f'name="{source_name}"><line nr="1" mi="0" ci="1" mb="0" cb="0"/>'
            '<counter type="INSTRUCTION" missed="0" covered="1"/>'
            '<counter type="LINE" missed="0" covered="1"/></sourcefile>'
            '<counter type="INSTRUCTION" missed="0" covered="1"/>'
            '<counter type="LINE" missed="0" covered="1"/></package>'
            '<counter type="INSTRUCTION" missed="0" covered="1"/>'
            '<counter type="LINE" missed="0" covered="1"/></report>',
            encoding="utf-8",
        )
        return path
    apple_sources = {
        "ios": ("components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift",),
        "macos": ("components/AstralProjection/apple-clients/AstralApp/AstralApp/AstralAppMain.swift",),
        "watchos": (
            "components/AstralProjection/apple-clients/AstralWatch/WatchModel.swift",
            "components/AstralProjection/apple-clients/AstralCore/Sources/AstralCore/API/Rest.swift",
        ),
    }
    path = root / f"{slot}.json"
    path.write_text(
        json.dumps(
            {
                source: [
                    {"line": 1, "isExecutable": True, "executionCount": 1}
                ]
                for source in apple_sources[slot]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_cli_parse_errors_and_absent_decision_output_mode(prepare: Any) -> None:
    with pytest.raises(SystemExit) as missing:
        prepare.main([])
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as unknown:
        prepare.main(
            [
                "--base-sha",
                BASE_SHA,
                "--candidate-sha",
                "a" * 40,
                "--decision-output",
                "decision.json",
            ]
        )
    assert unknown.value.code == 2
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "--decision-output" not in source
    assert "uuid4" not in source
    parsed = prepare._parser().parse_args(
        [
            "--base-sha",
            BASE_SHA,
            "--candidate-sha",
            "a" * 40,
            "--backend-python",
            "backend.xml",
            "--voice-worker-python",
            "voice.xml",
            "--tooling-python",
            "tooling.xml",
            "--windows-python",
            "windows.xml",
            "--javascript",
            "web.json",
            "--android-app",
            "app.xml",
            "--android-core",
            "core.xml",
            "--ios",
            "ios.json",
            "--macos",
            "macos.json",
            "--watchos",
            "watchos.json",
        ]
    )
    assert parsed.backend == "backend.xml"
    assert parsed.voice_worker == "voice.xml"
    assert parsed.ios == "ios.json"
    assert parsed.macos == "macos.json"
    assert parsed.watchos == "watchos.json"


def test_malformed_or_equal_shas_are_rejected(
    prepare: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "diagnostic.json"
    assert (
        prepare.main(
            [
                "--evidence-dir",
                str(tmp_path),
                "--base-sha",
                BASE_SHA,
                "--candidate-sha",
                "not-a-sha",
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert "release evidence rejected:" in capsys.readouterr().err
    assert (
        prepare.main(
            [
                "--evidence-dir",
                str(tmp_path),
                "--base-sha",
                BASE_SHA,
                "--candidate-sha",
                BASE_SHA,
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert "must differ" in capsys.readouterr().err
    assert not output.exists()


def test_missing_empty_or_unrecognized_evidence_directory_is_rejected(
    prepare: Any,
    contract_examples: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "diagnostic.json"
    assert _run(prepare, contract_examples, tmp_path / "absent", output) == 2
    assert "does not exist" in capsys.readouterr().err

    empty = tmp_path / "empty"
    empty.mkdir()
    assert _run(prepare, contract_examples, empty, output) == 2
    assert "no release evidence documents" in capsys.readouterr().err

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "notes.json").write_text('{"decision": "x"}', encoding="utf-8")
    assert _run(prepare, contract_examples, unrelated, output) == 2
    assert "no release evidence documents" in capsys.readouterr().err
    assert not output.exists()


def test_failure_receipt_classifies_only_structural_missing_evidence(
    prepare: Any,
    contract_examples: Any,
    matrix_helpers: Any,
    tmp_path: Path,
) -> None:
    output = tmp_path / "diagnostic.json"
    failure = tmp_path / "missing-failure.json"
    assert (
        _run(
            prepare,
            contract_examples,
            tmp_path / "absent",
            output,
            "--failure-output",
            str(failure),
        )
        == 2
    )
    missing = json.loads(failure.read_text(encoding="utf-8"))
    assert missing == {
        "document_type": "release_evidence_local_failure",
        "schema_version": 1,
        "error_code": "missing_provider_inputs",
        "error_message_sha256": missing["error_message_sha256"],
        "base_sha": BASE_SHA,
        "candidate_sha": contract_examples.GIT_SHA,
        "protected_release_authorization": False,
    }
    assert len(missing["error_message_sha256"]) == 64

    incomplete = tmp_path / "incomplete"
    _write_matrix(incomplete, contract_examples, matrix_helpers)
    (incomplete / "docs.json").unlink()
    rejected_failure = tmp_path / "rejected-failure.json"
    assert (
        _run(
            prepare,
            contract_examples,
            incomplete,
            output,
            "--failure-output",
            str(rejected_failure),
        )
        == 2
    )
    rejected = json.loads(rejected_failure.read_text(encoding="utf-8"))
    assert rejected["error_code"] == "release_evidence_rejected"
    assert rejected["protected_release_authorization"] is False


def test_passing_matrix_emits_the_diagnostic_contract(
    prepare: Any,
    contract_examples: Any,
    matrix_helpers: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_dir = tmp_path / "evidence"
    reports = _write_matrix(evidence_dir, contract_examples, matrix_helpers)
    # A stray non-evidence JSON file (e.g. an earlier diagnostic) is ignored.
    (evidence_dir / "local-diagnostic.json").write_text(
        '{"decision": "stale"}', encoding="utf-8"
    )
    output = tmp_path / "out" / "diagnostic.json"
    assert _run(prepare, contract_examples, evidence_dir, output) == 0

    diagnostic = json.loads(output.read_text(encoding="utf-8"))
    assert diagnostic["decision"] == "diagnostic_policy_passed"
    assert diagnostic["protected_release_authorization"] is False
    assert diagnostic["base_sha"] == BASE_SHA
    assert diagnostic["candidate_sha"] == contract_examples.GIT_SHA
    assert diagnostic["generated_at"] == NOW
    assert diagnostic["evidence_set_assembled"] is True
    assert uuid.UUID(diagnostic["evidence_set_id"]).version == 5
    assert diagnostic["staging_environment_id"] == "stage-060-request-1"
    assert diagnostic["used_exception_ids"] == []
    assert diagnostic["staging_outputs"] is None
    assert diagnostic["required_targets"] == list(prepare.VALIDATOR.REQUIRED_TARGETS)
    release_identity = diagnostic["release_identity"]
    stage = reports[0]["staging_environment"]
    assert release_identity["voice_runtime"] == stage["voice_runtime"]
    assert release_identity["voice_runtime_sha256"] == (
        prepare.VALIDATOR.canonical_json_sha256(stage["voice_runtime"])
    )
    assert release_identity["staging_environment_sha256"] == (
        prepare.VALIDATOR.canonical_json_sha256(stage)
    )
    assert [entry["platform"] for entry in release_identity["client_artifacts"]] == (
        sorted(report["platform"] for report in reports)
    )
    assert diagnostic["coverage_inputs"]["complete"] is False
    assert diagnostic["coverage_inputs"]["missing_slots"] == list(
        prepare.COVERAGE_INPUT_SLOTS
    )

    by_path = {entry["path"]: entry for entry in diagnostic["documents"]}
    assert len(diagnostic["documents"]) == len(reports)
    for report in reports:
        entry = by_path[f"{report['platform']}.json"]
        assert entry["document_type"] == "platform_evidence"
        assert entry["sha256"] == prepare.VALIDATOR.canonical_json_sha256(report)

    stdout = capsys.readouterr().out.strip()
    assert json.loads(stdout) == diagnostic


def test_assembly_is_deterministic_for_the_same_now(
    prepare: Any, contract_examples: Any, matrix_helpers: Any, tmp_path: Path
) -> None:
    evidence_dir = tmp_path / "evidence"
    _write_matrix(evidence_dir, contract_examples, matrix_helpers)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    assert _run(prepare, contract_examples, evidence_dir, first) == 0
    assert _run(prepare, contract_examples, evidence_dir, second) == 0
    assert first.read_bytes() == second.read_bytes()

    # The assembled identity is content-derived, not time-derived.
    third = tmp_path / "third.json"
    assert (
        _run(
            prepare,
            contract_examples,
            evidence_dir,
            third,
            "--now",
            "2026-07-16T13:00:00Z",
        )
        == 0
    )
    identity = json.loads(first.read_text(encoding="utf-8"))["evidence_set_id"]
    assert json.loads(third.read_text(encoding="utf-8"))["evidence_set_id"] == identity


def test_single_existing_evidence_set_passes_through_without_assembly(
    prepare: Any,
    contract_examples: Any,
    matrix_helpers: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_dir = tmp_path / "evidence"
    _write_matrix(evidence_dir, contract_examples, matrix_helpers)
    evidence_set = matrix_helpers._passing_set(contract_examples)
    (evidence_dir / "evidence-set.json").write_text(
        json.dumps(evidence_set), encoding="utf-8"
    )
    output = tmp_path / "diagnostic.json"
    assert _run(prepare, contract_examples, evidence_dir, output) == 0
    diagnostic = json.loads(output.read_text(encoding="utf-8"))
    assert diagnostic["evidence_set_assembled"] is False
    assert diagnostic["evidence_set_id"] == evidence_set["evidence_set_id"]
    assert len(diagnostic["documents"]) == 9
    capsys.readouterr()

    (evidence_dir / "second-set.json").write_text(
        json.dumps(evidence_set), encoding="utf-8"
    )
    assert _run(prepare, contract_examples, evidence_dir, output) == 2
    assert "exactly one release_evidence_set" in capsys.readouterr().err


def test_policy_and_assembly_rejections_pass_through_as_exit_two(
    prepare: Any,
    contract_examples: Any,
    matrix_helpers: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "diagnostic.json"

    incomplete = tmp_path / "incomplete"
    _write_matrix(incomplete, contract_examples, matrix_helpers)
    (incomplete / "docs.json").unlink()
    assert _run(prepare, contract_examples, incomplete, output) == 2
    assert "required targets" in capsys.readouterr().err

    unused = tmp_path / "unused-request"
    _write_matrix(unused, contract_examples, matrix_helpers)
    (unused / "request.json").write_text(
        json.dumps(_exception_request(contract_examples)), encoding="utf-8"
    )
    assert _run(prepare, contract_examples, unused, output) == 2
    assert "unused exception request" in capsys.readouterr().err

    drift = tmp_path / "drift"
    reports = _write_matrix(drift, contract_examples, matrix_helpers)
    reports[0]["release_version"] = "0.4.1"
    (drift / "backend.json").write_text(json.dumps(reports[0]), encoding="utf-8")
    assert _run(prepare, contract_examples, drift, output) == 2
    assert "disagree" in capsys.readouterr().err

    complete = tmp_path / "complete"
    _write_matrix(complete, contract_examples, matrix_helpers)
    assert (
        prepare.main(
            [
                "--evidence-dir",
                str(complete),
                "--base-sha",
                BASE_SHA,
                "--candidate-sha",
                "c" * 40,
                "--output",
                str(output),
                "--now",
                NOW,
            ]
        )
        == 2
    )
    assert "differs from CLI candidate" in capsys.readouterr().err
    assert not output.exists()


def test_staging_outputs_must_bind_the_matrix_environment(
    prepare: Any,
    contract_examples: Any,
    matrix_helpers: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_dir = tmp_path / "evidence"
    _write_matrix(evidence_dir, contract_examples, matrix_helpers)
    staging_outputs = tmp_path / "staging-outputs.json"
    staging_outputs.write_text(
        json.dumps({"environment_id": "stage-060-request-1", "endpoint": "x"}),
        encoding="utf-8",
    )
    output = tmp_path / "diagnostic.json"
    assert (
        _run(
            prepare,
            contract_examples,
            evidence_dir,
            output,
            "--staging-outputs",
            str(staging_outputs),
        )
        == 0
    )
    diagnostic = json.loads(output.read_text(encoding="utf-8"))
    assert diagnostic["staging_outputs"]["environment_id"] == "stage-060-request-1"
    assert diagnostic["staging_outputs"]["sha256"] == (
        hashlib.sha256(staging_outputs.read_bytes()).hexdigest()
    )
    capsys.readouterr()

    staging_outputs.write_text(
        json.dumps({"environment_id": "some-other-environment"}), encoding="utf-8"
    )
    assert (
        _run(
            prepare,
            contract_examples,
            evidence_dir,
            output,
            "--staging-outputs",
            str(staging_outputs),
        )
        == 2
    )
    assert "differs from evidence matrix" in capsys.readouterr().err


def test_coverage_reports_are_inventoried_with_raw_digests(
    prepare: Any, contract_examples: Any, matrix_helpers: Any, tmp_path: Path
) -> None:
    evidence_dir = tmp_path / "evidence"
    _write_matrix(evidence_dir, contract_examples, matrix_helpers)
    coverage_dir = tmp_path / "coverage"
    (coverage_dir / "node-v8").mkdir(parents=True)
    (coverage_dir / "tooling-python.xml").write_bytes(b"<coverage/>")
    (coverage_dir / "node-v8" / "web.json").write_bytes(b"{}")
    (coverage_dir / "notes.txt").write_bytes(b"ignored")
    output = tmp_path / "diagnostic.json"
    assert (
        _run(
            prepare,
            contract_examples,
            evidence_dir,
            output,
            "--coverage-dir",
            str(coverage_dir),
        )
        == 0
    )
    diagnostic = json.loads(output.read_text(encoding="utf-8"))
    assert diagnostic["coverage_reports"] == [
        {
            "path": "node-v8/web.json",
            "sha256": hashlib.sha256(b"{}").hexdigest(),
        },
        {
            "path": "tooling-python.xml",
            "sha256": hashlib.sha256(b"<coverage/>").hexdigest(),
        },
    ]

    absent = tmp_path / "no-coverage"
    assert (
        _run(
            prepare,
            contract_examples,
            evidence_dir,
            output,
            "--coverage-dir",
            str(absent),
        )
        == 0
    )
    diagnostic = json.loads(output.read_text(encoding="utf-8"))
    assert diagnostic["coverage_reports"] == []


def test_supplied_coverage_inputs_run_the_diagnostic_combined_gate(
    prepare: Any,
    contract_examples: Any,
    matrix_helpers: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    source = repo / "backend" / "shared" / "voice_transcript.py"
    source.parent.mkdir(parents=True)
    source.write_text("first = 1\nready = False\n", encoding="utf-8")
    for tracked_path in (
        "backend/service.py",
        "backend/voice_agent/main.py",
        "backend/voice_agent/voice_transcript.py",
        "scripts/release.py",
        "components/AstralProjection/windows-client/app.py",
        "components/AstralProjection/backend/webrender/static/client.js",
        "components/AstralProjection/android-client/app/src/main/kotlin/com/example/App.kt",
        "components/AstralProjection/android-client/core/src/main/kotlin/com/example/Core.kt",
        "components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift",
        "components/AstralProjection/apple-clients/AstralApp/AstralApp/AstralAppMain.swift",
        "components/AstralProjection/apple-clients/AstralCore/Sources/AstralCore/API/Rest.swift",
        "components/AstralProjection/apple-clients/AstralWatch/WatchModel.swift",
    ):
        tracked = repo / tracked_path
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text("fixture\n", encoding="utf-8")
    shim = repo / "backend" / "voice_agent" / "voice_transcript.py"
    shim.write_text("first = 1\nshim = False\n", encoding="utf-8")
    worker_main = repo / "backend" / "voice_agent" / "main.py"
    worker_main.write_text("ready = 1\nstate = False\n", encoding="utf-8")
    base = _commit(repo, "base")
    source.write_text("first = 1\nready = True\n", encoding="utf-8")
    shim.write_text("first = 1\nshim = True\n", encoding="utf-8")
    worker_main.write_text("ready = 1\nstate = True\n", encoding="utf-8")
    candidate = _commit(repo, "candidate")

    evidence_dir = tmp_path / "evidence"
    _write_matrix(
        evidence_dir,
        contract_examples,
        matrix_helpers,
        candidate_sha=candidate,
    )
    coverage_args: list[str] = []
    for slot, producer in prepare.COVERAGE_INPUT_SLOTS.items():
        report = _native_coverage_report(prepare, tmp_path, slot)
        coverage_args.extend((f"--{producer.flag}", str(report)))
    _cobertura_many(
        tmp_path / "backend.xml",
        {
            "backend/shared/voice_transcript.py": {2: 1},
            "backend/service.py": {1: 1},
            "backend/voice_agent/voice_transcript.py": {2: 1},
            "backend/voice_agent/main.py": {2: 1},
        },
    )
    _cobertura_many(
        tmp_path / "voice-worker.xml",
        {
            "backend/voice_agent/voice_transcript.py": {2: 1},
            "backend/voice_agent/main.py": {1: 1},
        },
    )
    coverage = tmp_path / "voice-worker.xml"
    coverage_decision = tmp_path / "coverage" / "changed-code.json"
    output = tmp_path / "diagnostic.json"
    assert (
        prepare.main(
            [
                "--repo",
                str(repo),
                "--evidence-dir",
                str(evidence_dir),
                "--base-sha",
                base,
                "--candidate-sha",
                candidate,
                *coverage_args,
                "--coverage-decision-output",
                str(coverage_decision),
                "--output",
                str(output),
                "--now",
                NOW,
            ]
        )
        == 0
    )

    decision_bytes = coverage_decision.read_bytes()
    decision = json.loads(decision_bytes)
    assert decision["status"] == "pass"
    assert decision["combined"]["percent"] == 100.0
    diagnostic = json.loads(output.read_text(encoding="utf-8"))
    assert diagnostic["protected_release_authorization"] is False
    assert diagnostic["changed_coverage"] == {
        "combined_percent": 100.0,
        "path": str(coverage_decision).replace("\\", "/"),
        "sha256": hashlib.sha256(decision_bytes).hexdigest(),
        "status": "pass",
    }
    assert diagnostic["coverage_inputs"]["inputs"]["voice_worker"] == {
        "path": coverage.as_posix(),
        **prepare.COVERAGE.coverage_report_identity(
            coverage.read_bytes(),
            "backend_python",
            producer_key="voice_worker",
        ),
    }
    assert diagnostic["coverage_inputs"]["complete"] is True
    assert set(decision["producer_slots"]) == set(prepare.COVERAGE_INPUT_SLOTS)
    assert set(decision["producer_contributions"]) == set(
        prepare.COVERAGE_INPUT_SLOTS
    )
    assert all(decision["producer_contributions"].values())
    assert not any(
        line["path"] == "backend/voice_agent/main.py" for line in decision["lines"]
    )
    capsys.readouterr()

    # The tracked host shim is owned by the generic backend producer. The
    # worker report's same runtime filename is attributed to backend/shared and
    # therefore cannot mask an omitted changed shim.
    _cobertura_many(
        tmp_path / "backend.xml",
        {
            "backend/shared/voice_transcript.py": {2: 1},
            "backend/service.py": {1: 1},
        },
    )
    assert (
        prepare.main(
            [
                "--repo",
                str(repo),
                "--evidence-dir",
                str(evidence_dir),
                "--base-sha",
                base,
                "--candidate-sha",
                candidate,
                *coverage_args,
                "--coverage-decision-output",
                str(coverage_decision),
                "--output",
                str(output),
                "--now",
                NOW,
            ]
        )
        == 2
    )
    assert "producer_unmapped_changed_file" in capsys.readouterr().err
    _cobertura_many(
        tmp_path / "backend.xml",
        {
            "backend/shared/voice_transcript.py": {2: 1},
            "backend/service.py": {1: 1},
            "backend/voice_agent/voice_transcript.py": {2: 1},
            "backend/voice_agent/main.py": {2: 1},
        },
    )

    # The isolated worker renames the shared source at image build time. Its
    # runtime-path line 1 must not let backend's line 2 mask the worker's missing
    # observation after producer-specific source attribution.
    _cobertura_many(
        tmp_path / "voice-worker.xml",
        {
            "backend/voice_agent/voice_transcript.py": {1: 1},
            "backend/voice_agent/main.py": {1: 1},
        },
    )
    assert (
        prepare.main(
            [
                "--repo",
                str(repo),
                "--evidence-dir",
                str(evidence_dir),
                "--base-sha",
                base,
                "--candidate-sha",
                candidate,
                *coverage_args,
                "--coverage-decision-output",
                str(coverage_decision),
                "--output",
                str(output),
                "--now",
                NOW,
            ]
        )
        == 2
    )
    assert "producer_unmapped_changed_line" in capsys.readouterr().err
    _cobertura_many(
        tmp_path / "voice-worker.xml",
        {
            "backend/voice_agent/voice_transcript.py": {2: 1},
            "backend/voice_agent/main.py": {1: 1},
        },
    )

    (tmp_path / "watchos.json").write_text(
        json.dumps(
            {
                "components/AstralProjection/apple-clients/AstralWatch/WatchModel.swift": [
                    {"line": 1, "isExecutable": True, "executionCount": 1}
                ],
                "components/AstralProjection/apple-clients/AstralApp/AstralApp/AstralAppMain.swift": [
                    {"line": 1, "isExecutable": True, "executionCount": 1}
                ],
            }
        ),
        encoding="utf-8",
    )
    assert (
        prepare.main(
            [
                "--repo",
                str(repo),
                "--evidence-dir",
                str(evidence_dir),
                "--base-sha",
                base,
                "--candidate-sha",
                candidate,
                *coverage_args,
                "--coverage-decision-output",
                str(coverage_decision),
                "--output",
                str(output),
                "--now",
                NOW,
            ]
        )
        == 2
    )
    assert "producer_scope_mismatch" in capsys.readouterr().err
    _native_coverage_report(prepare, tmp_path, "watchos")

    (tmp_path / "ios.json").write_text(
        json.dumps(
            {
                "components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift": [
                    {"line": 1, "isExecutable": True, "executionCount": 1}
                ],
                "components/AstralProjection/apple-clients/AstralWatch/WatchModel.swift": [
                    {"line": 1, "isExecutable": True, "executionCount": 1}
                ],
            }
        ),
        encoding="utf-8",
    )
    assert (
        prepare.main(
            [
                "--repo",
                str(repo),
                "--evidence-dir",
                str(evidence_dir),
                "--base-sha",
                base,
                "--candidate-sha",
                candidate,
                *coverage_args,
                "--coverage-decision-output",
                str(coverage_decision),
                "--output",
                str(output),
                "--now",
                NOW,
            ]
        )
        == 2
    )
    assert "producer_scope_mismatch" in capsys.readouterr().err
    _native_coverage_report(prepare, tmp_path, "ios")

    # A tracked path alone is not a candidate witness: its executable line must
    # exist in the immutable candidate blob rather than only in report metadata.
    _cobertura(
        tmp_path / "tooling.xml",
        "scripts/release.py",
        hits=1,
        line=999_999,
    )
    assert (
        prepare.main(
            [
                "--repo",
                str(repo),
                "--evidence-dir",
                str(evidence_dir),
                "--base-sha",
                base,
                "--candidate-sha",
                candidate,
                *coverage_args,
                "--coverage-decision-output",
                str(coverage_decision),
                "--output",
                str(output),
                "--now",
                NOW,
            ]
        )
        == 2
    )
    assert "unproductive_report" in capsys.readouterr().err

    # Even though the immutable diff only changes backend voice code, strict mode
    # parses and requires a useful tooling contribution rather than accepting a
    # syntactically valid dummy report for the unchanged target.
    _cobertura(tmp_path / "tooling.xml", "scripts/generated.py", hits=1)
    assert (
        prepare.main(
            [
                "--repo",
                str(repo),
                "--evidence-dir",
                str(evidence_dir),
                "--base-sha",
                base,
                "--candidate-sha",
                candidate,
                *coverage_args,
                "--coverage-decision-output",
                str(coverage_decision),
                "--output",
                str(output),
                "--now",
                NOW,
            ]
        )
        == 2
    )
    assert "unproductive_report" in capsys.readouterr().err


def test_standard_mode_rejects_an_empty_coverage_matrix(
    prepare: Any,
    contract_examples: Any,
    matrix_helpers: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_dir = tmp_path / "evidence"
    _write_matrix(evidence_dir, contract_examples, matrix_helpers)
    output = tmp_path / "diagnostic.json"

    assert (
        prepare.main(
            [
                "--evidence-dir",
                str(evidence_dir),
                "--base-sha",
                BASE_SHA,
                "--candidate-sha",
                contract_examples.GIT_SHA,
                "--output",
                str(output),
                "--now",
                NOW,
            ]
        )
        == 2
    )
    error = capsys.readouterr().err
    assert "strict coverage mode requires all ten producer slots" in error
    assert "--voice-worker-python" in error
    assert "--watchos" in error
    assert not output.exists()

    backend = _cobertura(tmp_path / "backend.xml", "backend/service.py", hits=1)
    assert (
        prepare.main(
            [
                "--evidence-dir",
                str(evidence_dir),
                "--base-sha",
                BASE_SHA,
                "--candidate-sha",
                contract_examples.GIT_SHA,
                "--backend-python",
                str(backend),
                "--output",
                str(output),
                "--now",
                NOW,
            ]
        )
        == 2
    )
    incomplete_error = capsys.readouterr().err
    assert "--voice-worker-python" in incomplete_error
    assert "--backend-python" not in incomplete_error


def test_strict_apple_producers_cannot_mask_changed_physical_lines(
    prepare: Any,
    contract_examples: Any,
    matrix_helpers: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    tracked_paths = (
        "backend/service.py",
        "backend/voice_agent/main.py",
        "scripts/release.py",
        "components/AstralProjection/windows-client/app.py",
        "components/AstralProjection/backend/webrender/static/client.js",
        "components/AstralProjection/android-client/app/src/main/kotlin/com/example/App.kt",
        "components/AstralProjection/android-client/core/src/main/kotlin/com/example/Core.kt",
        "components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift",
        "components/AstralProjection/apple-clients/AstralApp/AstralApp/AstralAppMain.swift",
        "components/AstralProjection/apple-clients/AstralCore/Sources/AstralCore/API/Rest.swift",
        "components/AstralProjection/apple-clients/AstralWatch/WatchModel.swift",
    )
    for tracked_path in tracked_paths:
        tracked = repo / tracked_path
        tracked.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "first\nsecond\n"
            if tracked_path.endswith(("AppModel.swift", "Rest.swift"))
            else "fixture\n"
        )
        tracked.write_text(content, encoding="utf-8")
    base = _commit(repo, "base")
    app_model = repo / "components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift"
    app_model.write_text("first\nchanged\n", encoding="utf-8")
    core_source = (
        repo / "components/AstralProjection/apple-clients/AstralCore/Sources/AstralCore/API/Rest.swift"
    )
    core_source.write_text("first\nchanged\n", encoding="utf-8")
    candidate = _commit(repo, "candidate")
    evidence_dir = tmp_path / "evidence"
    _write_matrix(
        evidence_dir,
        contract_examples,
        matrix_helpers,
        candidate_sha=candidate,
    )
    coverage_args: list[str] = []
    for slot, producer in prepare.COVERAGE_INPUT_SLOTS.items():
        report = _native_coverage_report(prepare, tmp_path, slot)
        coverage_args.extend((f"--{producer.flag}", str(report)))

    ios = {
        "components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift": [
            {"line": 1, "isExecutable": False},
            {"line": 2, "isExecutable": True, "executionCount": 1},
        ]
    }
    macos = {
        "components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift": [
            {"line": 1, "isExecutable": False},
            {"line": 2, "isExecutable": True, "executionCount": 0},
        ],
        "components/AstralProjection/apple-clients/AstralCore/Sources/AstralCore/API/Rest.swift": [
            {"line": 1, "isExecutable": False},
            {"line": 2, "isExecutable": True, "executionCount": 1}
        ],
    }
    (tmp_path / "ios.json").write_text(json.dumps(ios), encoding="utf-8")
    (tmp_path / "macos.json").write_text(json.dumps(macos), encoding="utf-8")
    output = tmp_path / "diagnostic.json"
    common = [
        "--repo",
        str(repo),
        "--evidence-dir",
        str(evidence_dir),
        "--base-sha",
        base,
        "--candidate-sha",
        candidate,
        *coverage_args,
        "--output",
        str(output),
        "--now",
        NOW,
    ]
    assert prepare.main(common) == 0
    capsys.readouterr()

    # The iOS archive may legitimately omit AstralCore. The macOS archive must
    # then map the changed Core file and all of its changed physical lines.
    (tmp_path / "macos.json").write_text(
        json.dumps(
                {
                    "components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift": [
                        {"line": 1, "isExecutable": False},
                        {"line": 2, "isExecutable": True, "executionCount": 0}
                    ],
                "components/AstralProjection/apple-clients/AstralCore/Sources/AstralCore/API/Rest.swift": [
                    {"line": 1, "isExecutable": True, "executionCount": 0}
                ],
            }
        ),
        encoding="utf-8",
    )
    assert prepare.main(common) == 2
    assert "producer_unmapped_changed_line" in capsys.readouterr().err

    (tmp_path / "macos.json").write_text(
        json.dumps(
            {
                "components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift": [
                    {"line": 1, "isExecutable": False},
                    {"line": 2, "isExecutable": True, "executionCount": 0}
                ]
            }
        ),
        encoding="utf-8",
    )
    assert prepare.main(common) == 2
    assert "producer_unmapped_changed_file" in capsys.readouterr().err

    (tmp_path / "macos.json").write_text(json.dumps(macos), encoding="utf-8")

    # macOS still has a tracked executable App contribution, but omits the
    # changed physical line that iOS observes and covers. Strict per-slot Apple
    # mapping must reject it without requiring the macOS line to have any hits.
    (tmp_path / "macos.json").write_text(
        json.dumps(
            {
                "components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift": [
                    {"line": 1, "isExecutable": True, "executionCount": 0}
                ]
            }
        ),
        encoding="utf-8",
    )
    assert prepare.main(common) == 2
    assert "producer_unmapped_changed_line" in capsys.readouterr().err


def test_mutation_between_binding_and_collector_is_rejected(
    prepare: Any,
    contract_examples: Any,
    matrix_helpers: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    source = repo / "backend" / "voice_agent" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("ready = False\n", encoding="utf-8")
    base = _commit(repo, "base")
    source.write_text("ready = True\n", encoding="utf-8")
    candidate = _commit(repo, "candidate")
    evidence_dir = tmp_path / "evidence"
    _write_matrix(
        evidence_dir,
        contract_examples,
        matrix_helpers,
        candidate_sha=candidate,
    )
    report = _cobertura(
        tmp_path / "voice-worker.xml", "backend/voice_agent/main.py", hits=1
    )
    original_main = prepare.COVERAGE.main

    def mutate_then_collect(argv: list[str]) -> int:
        report.write_text(
            report.read_text(encoding="utf-8").replace("><", ">\n<"),
            encoding="utf-8",
        )
        return original_main(argv)

    monkeypatch.setattr(prepare.COVERAGE, "main", mutate_then_collect)
    output = tmp_path / "diagnostic.json"
    assert (
        prepare.main(
            [
                "--repo",
                str(repo),
                "--evidence-dir",
                str(evidence_dir),
                "--base-sha",
                base,
                "--candidate-sha",
                candidate,
                "--voice-worker-python",
                str(report),
                "--coverage-mode",
                "partial",
                "--output",
                str(output),
                "--now",
                NOW,
            ]
        )
        == 2
    )
    assert "identity changed between input binding" in capsys.readouterr().err
    assert not output.exists()


def test_coverage_input_binding_requires_every_native_partition(
    prepare: Any, tmp_path: Path
) -> None:
    paths: dict[str, str] = {}
    for slot, producer in prepare.COVERAGE_INPUT_SLOTS.items():
        report = _native_coverage_report(prepare, tmp_path, slot)
        paths[slot] = str(report)
    args = prepare._parser().parse_args(
        [
            "--base-sha",
            BASE_SHA,
            "--candidate-sha",
            "a" * 40,
            *[
                item
                for slot, report in paths.items()
                for item in (
                    f"--{prepare.COVERAGE_INPUT_SLOTS[slot].flag}",
                    report,
                )
            ],
        ]
    )

    summary = prepare._coverage_input_summary(args)

    assert summary["complete"] is True
    assert summary["missing_slots"] == []
    assert all(identity is not None for identity in summary["inputs"].values())
    assert summary["required_slots"] == list(prepare.COVERAGE_INPUT_SLOTS)


@pytest.mark.parametrize(
    "alias_kind",
    (
        "same_path",
        "hardlink",
        "copy",
        "whitespace_copy",
        "metadata_copy",
        "cross_partition",
    ),
)
def test_coverage_input_minimums_require_globally_distinct_artifacts(
    prepare: Any, tmp_path: Path, alias_kind: str
) -> None:
    report = tmp_path / "backend.xml"
    _cobertura(report, "backend/service.py", hits=1)
    alias = tmp_path / "alias.xml"
    if alias_kind == "same_path":
        alias = report
    elif alias_kind == "hardlink":
        alias.hardlink_to(report)
    elif alias_kind == "whitespace_copy":
        alias.write_text(
            report.read_text(encoding="utf-8").replace("><", ">\n  <"),
            encoding="utf-8",
        )
    elif alias_kind == "metadata_copy":
        alias.write_text(
            report.read_text(encoding="utf-8").replace(
                "<coverage ", '<coverage timestamp="1780000000" '
            ),
            encoding="utf-8",
        )
    else:
        alias.write_bytes(report.read_bytes())
    second_flag = (
        "--tooling-python"
        if alias_kind == "cross_partition"
        else "--voice-worker-python"
    )
    args = prepare._parser().parse_args(
        [
            "--base-sha",
            BASE_SHA,
            "--candidate-sha",
            "a" * 40,
            "--backend-python",
            str(report),
            second_flag,
            str(alias),
        ]
    )

    with pytest.raises(prepare.VALIDATOR.PolicyError, match="duplicate coverage input"):
        prepare._coverage_input_summary(args)


def test_make_wrapper_passes_every_required_coverage_partition() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("prepare-release-evidence:", 1)[1].split(
        "## ---------- Lint", 1
    )[0]

    assert "$(RELEASE_COVERAGE_FLAGS)" in target
    assert "--coverage-mode strict" in target
    assert target.index("$(RELEASE_COVERAGE_FLAGS)") < target.index(
        "--coverage-mode strict"
    )
    flags = makefile.split("RELEASE_COVERAGE_FLAGS ?=", 1)[1].split(
        "## ---------- Lifecycle", 1
    )[0]
    assert flags.count("--backend-python") == 1
    assert flags.count("--voice-worker-python") == 1
    assert flags.count("--tooling-python") == 1
    assert flags.count("--windows-python") == 1
    assert flags.count("--javascript") == 1
    assert flags.count("--android-app") == 1
    assert flags.count("--android-core") == 1
    assert flags.count("--ios") == 1
    assert flags.count("--macos") == 1
    assert flags.count("--watchos") == 1
