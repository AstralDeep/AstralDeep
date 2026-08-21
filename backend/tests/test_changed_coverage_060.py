"""Direct contract tests for the feature-060 changed-code coverage collector."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from coverage import Coverage
from coverage.parser import PythonParser


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_changed_coverage.py"
XCCOV_EXPORT_SCRIPT = REPO_ROOT / "scripts" / "export_xccov_line_coverage.py"

if not (REPO_ROOT / "scripts").is_dir():  # repo root absent inside the product image
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )


def _load_collector() -> ModuleType:
    spec = importlib.util.spec_from_file_location("changed_coverage_060", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collector = _load_collector()


def _native_python_statements(source: str, path: str) -> frozenset[int]:
    parser = PythonParser(
        text=source,
        filename=path,
        exclude="|".join(Coverage(config_file=False).config.exclude_list),
    )
    parser.parse_source()
    return frozenset(parser.statements)


def _load_xccov_exporter() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "export_xccov_line_coverage_060", XCCOV_EXPORT_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


xccov_exporter = _load_xccov_exporter()


def test_javascript_report_identity_matches_projection_v2_union() -> None:
    assert collector.JAVASCRIPT_REPORT_KEYS == {
        "schema_version",
        "producer",
        "producer_version",
        "v8_to_istanbul_version",
        "espree_version",
        "coverage_lane",
        "coverage",
    }
    assert collector.JAVASCRIPT_REPORT_IDENTITY == {
        "schema_version": 1,
        "producer": "astralprojection-node-browser-union",
        "producer_version": 2,
        "v8_to_istanbul_version": "9.3.0",
        "espree_version": "11.2.0",
        "coverage_lane": "node-browser-union",
    }


def test_report_reader_preserves_physical_crlf_bytes(tmp_path: Path) -> None:
    """The bound identity must cover exact bytes on Windows as on POSIX."""

    content = b'{"coverage":"physical"}\r\n{"line":2}\r\n'
    report = tmp_path / "crlf-report.json"
    report.write_bytes(content)

    assert collector._read_report(report) == content


@pytest.fixture(autouse=True)
def _no_ambient_actions_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep event selection deterministic under real GitHub Actions.

    ``main()`` falls back to GITHUB_EVENT_NAME/GITHUB_EVENT_PATH when no
    explicit ``--event-name``/``--event-path`` is given, so an ambient
    ``pull_request`` event would hijack the CLI tests' manual selections.
    """
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)


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


@pytest.fixture
def git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    source = repo / "backend" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text("first = 1\nsecond = 2\n", encoding="utf-8")
    return repo, _commit(repo, "base")


def _selection(repo: Path, base: str, candidate: str):
    selected = collector.select_revisions(
        event_name="manual",
        event_payload=None,
        base_sha=base,
        candidate_sha=candidate,
    )
    return collector.validate_revisions(repo, selected)


def _cobertura(path: Path, filename: str, lines: dict[int, int]) -> Path:
    rendered = "".join(
        f'<line number="{line}" hits="{hits}"/>' for line, hits in lines.items()
    )
    covered = sum(hits > 0 for hits in lines.values())
    total = len(lines)
    rate = covered / total if total else 1
    path.write_text(
        f'<coverage lines-valid="{total}" lines-covered="{covered}" '
        f'line-rate="{rate}"><sources><source>/work</source></sources>'
        "<packages><package><classes>"
        f'<class filename="{filename}" line-rate="{rate}">'
        f"<lines>{rendered}</lines></class>"
        "</classes></package></packages></coverage>\n",
        encoding="utf-8",
    )
    return path


def _cobertura_many(path: Path, files: dict[str, dict[int, int]]) -> Path:
    classes: list[str] = []
    total = 0
    covered = 0
    for filename, lines in files.items():
        rendered = "".join(
            f'<line number="{line}" hits="{hits}"/>' for line, hits in lines.items()
        )
        class_covered = sum(hits > 0 for hits in lines.values())
        class_total = len(lines)
        rate = class_covered / class_total if class_total else 1
        classes.append(
            f'<class filename="{filename}" line-rate="{rate}">'
            f"<lines>{rendered}</lines></class>"
        )
        total += class_total
        covered += class_covered
    rate = covered / total if total else 1
    path.write_text(
        f'<coverage lines-valid="{total}" lines-covered="{covered}" '
        f'line-rate="{rate}"><sources><source>/work</source></sources>'
        "<packages><package><classes>"
        f"{''.join(classes)}"
        "</classes></package></packages></coverage>\n",
        encoding="utf-8",
    )
    return path


def _javascript_envelope(coverage: dict[str, object]) -> dict[str, object]:
    return {
        **collector.JAVASCRIPT_REPORT_IDENTITY,
        "coverage": coverage,
    }


def _kover(path: Path, source_name: str, *, covered: bool = True) -> Path:
    missed = int(not covered)
    hit = int(covered)
    path.write_text(
        '<report><package name="com/example">'
        f'<sourcefile name="{source_name}">'
        f'<line nr="1" mi="{missed}" ci="{hit}" mb="0" cb="0"/>'
        f'<counter type="INSTRUCTION" missed="{missed}" covered="{hit}"/>'
        f'<counter type="LINE" missed="{missed}" covered="{hit}"/>'
        "</sourcefile>"
        f'<counter type="INSTRUCTION" missed="{missed}" covered="{hit}"/>'
        f'<counter type="LINE" missed="{missed}" covered="{hit}"/>'
        "</package>"
        f'<counter type="INSTRUCTION" missed="{missed}" covered="{hit}"/>'
        f'<counter type="LINE" missed="{missed}" covered="{hit}"/>'
        "</report>\n",
        encoding="utf-8",
    )
    return path


def _xccov(path: Path, source: str, *, execution_count: int) -> Path:
    path.write_text(
        json.dumps(
            {
                source: [
                    {
                        "line": 1,
                        "isExecutable": True,
                        "executionCount": execution_count,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _projection_strict_case(
    tmp_path: Path,
) -> tuple[
    Path,
    object,
    dict[str, list[Path]],
    dict[str, Path],
]:
    """Build one real child-repository candidate and its eight native reports."""

    repo = tmp_path / "projection"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    sources = {
        "src/astralprojection/runtime.py": "first = 0\nsecond = 0\n",
        "backend/rote/adaptation.py": "adapted = 0\n",
        "src/astralprojection/declarations.py": (
            "# old comment\n    \ndef declaration(\n"
            "    value: int,\n) -> int:\n    return value\n"
        ),
        "windows-client/runtime.py": "value = 0\n",
        "backend/webrender/static/client.js": "const value = 0;\n",
        "android-client/app/src/main/kotlin/com/example/App.kt": "val value = 0\n",
        "android-client/core/src/main/kotlin/com/example/Core.kt": "val value = 0\n",
        "apple-clients/AstralApp/AstralApp/App.swift": "let value = 0\n",
        "apple-clients/AstralWatch/Watch.swift": "let value = 0\n",
    }
    for relative, content in sources.items():
        source = repo / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(content, encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "src/astralprojection/runtime.py").write_text(
        "first = 1\nsecond = 1\n", encoding="utf-8"
    )
    (repo / "backend/rote/adaptation.py").write_text("adapted = 1\n", encoding="utf-8")
    (repo / "src/astralprojection/declarations.py").write_text(
        "# new comment\n\ndef declaration(\n"
        "    value: str,\n) -> int:\n    return value\n",
        encoding="utf-8",
    )
    candidate = _commit(repo, "candidate")

    projection = _cobertura_many(
        tmp_path / "projection-python.xml",
        {
            "src/astralprojection/runtime.py": {1: 1, 2: 1},
            "backend/rote/adaptation.py": {1: 1},
            "src/astralprojection/declarations.py": {3: 1, 6: 1},
        },
    )
    windows = _cobertura(tmp_path / "windows.xml", "windows-client/runtime.py", {1: 1})
    javascript = tmp_path / "javascript.json"
    javascript.write_text(
        json.dumps(
            _javascript_envelope(
                {
                    "backend/webrender/static/client.js": {
                        "path": "backend/webrender/static/client.js",
                        "statementMap": {
                            "0": {
                                "start": {"line": 1, "column": 0},
                                "end": {"line": 1, "column": 16},
                            }
                        },
                        "s": {"0": 1},
                    }
                }
            )
        ),
        encoding="utf-8",
    )
    android_app = _kover(tmp_path / "android-app.xml", "App.kt")
    android_core = _kover(tmp_path / "android-core.xml", "Core.kt")
    ios = _xccov(
        tmp_path / "ios.json",
        "apple-clients/AstralApp/AstralApp/App.swift",
        execution_count=1,
    )
    macos = _xccov(
        tmp_path / "macos.json",
        "apple-clients/AstralApp/AstralApp/App.swift",
        execution_count=0,
    )
    watchos = _xccov(
        tmp_path / "watchos.json",
        "apple-clients/AstralWatch/Watch.swift",
        execution_count=1,
    )
    slots = {
        "projection_python": projection,
        "windows": windows,
        "javascript": javascript,
        "android_app": android_app,
        "android_core": android_core,
        "ios": ios,
        "macos": macos,
        "watchos": watchos,
    }
    reports = {
        "projection_python": [projection],
        "windows_python": [windows],
        "javascript": [javascript],
        "android_app": [android_app],
        "android_core": [android_core],
        "apple": [ios, macos, watchos],
    }
    return repo, _selection(repo, base, candidate), reports, slots


def _evaluate_projection_strict(
    repo: Path,
    selection: object,
    reports: dict[str, list[Path]],
    slots: dict[str, Path],
) -> dict[str, object]:
    profile = collector.REPOSITORY_PROFILES["projection"]
    return collector.evaluate_changed_coverage(
        repo,
        selection,
        reports,
        producer_slots=slots,
        strict_producers=True,
        repository_profile="projection",
        required_producer_keys=profile.producer_keys,
        source_prefix=profile.source_prefix,
    )


def test_repository_profiles_partition_owned_producers() -> None:
    assert collector.REPOSITORY_PROFILES["deep"].producer_keys == (
        "backend",
        "voice_worker",
        "tooling",
    )
    assert collector.REPOSITORY_PROFILES["projection"].producer_keys == (
        "projection_python",
        "windows",
        "javascript",
        "android_app",
        "android_core",
        "ios",
        "macos",
        "watchos",
    )
    assert set(collector.REPOSITORY_PROFILES["monorepo"].producer_keys) == set(
        collector.PRODUCER_BY_KEY
    )
    assert collector.REPORT_FLAGS["projection_python"] == "projection-python"


def test_projection_profile_maps_child_git_paths_to_composed_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "projection"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    source = repo / "windows-client" / "runtime.py"
    projection_source = repo / "src" / "astralprojection" / "runtime.py"
    source.parent.mkdir(parents=True)
    projection_source.parent.mkdir(parents=True)
    source.write_text("first = 1\n", encoding="utf-8")
    projection_source.write_text("first = 1\n", encoding="utf-8")
    base = _commit(repo, "base")
    source.write_text("first = 1\nsecond = 2\n", encoding="utf-8")
    projection_source.write_text("first = 1\nsecond = 2\n", encoding="utf-8")
    candidate = _commit(repo, "candidate")

    prefix = collector.REPOSITORY_PROFILES["projection"].source_prefix
    changed = collector.read_changed_lines(
        repo,
        base,
        candidate,
        source_prefix=prefix,
    )
    blobs = collector._candidate_source_blobs(
        repo,
        candidate,
        source_prefix=prefix,
    )

    composed_path = "components/AstralProjection/windows-client/runtime.py"
    projection_path = "components/AstralProjection/src/astralprojection/runtime.py"
    assert changed == {composed_path: {2}, projection_path: {2}}
    assert composed_path in blobs
    assert projection_path in blobs


@pytest.mark.parametrize(
    "relative",
    (
        "src/astralprojection/runtime.py",
        "backend/rote/adaptation.py",
        "backend/webrender/renderer.py",
    ),
)
def test_projection_python_cobertura_maps_each_composed_owner_root(
    tmp_path: Path, relative: str
) -> None:
    report = _cobertura(tmp_path / "projection.xml", relative, {1: 1, 2: 0})

    parsed = collector.parse_coverage_report(report, "projection_python")

    composed = f"components/AstralProjection/{relative}"
    assert parsed.files == {composed}
    assert parsed.executable == {(composed, 1), (composed, 2)}
    assert parsed.covered == {(composed, 1)}


def test_projection_profile_requires_projection_python_slot(tmp_path: Path) -> None:
    repo, selection, reports, slots = _projection_strict_case(tmp_path)
    reports.pop("projection_python")
    slots.pop("projection_python")

    with pytest.raises(collector.CoveragePolicyError) as failure:
        _evaluate_projection_strict(repo, selection, reports, slots)

    assert failure.value.code == "incomplete_report_matrix"
    assert "projection_python" in failure.value.message


def test_projection_python_cli_rejects_duplicate_slot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _cobertura(
        tmp_path / "projection.xml", "src/astralprojection/runtime.py", {1: 1}
    )

    with pytest.raises(SystemExit) as failure:
        collector._parser().parse_args(
            [
                "--projection-python",
                str(report),
                "--projection-python",
                str(report),
                "--output",
                str(tmp_path / "decision.json"),
            ]
        )

    assert failure.value.code == 2
    assert "--projection-python may be supplied exactly once" in capsys.readouterr().err


def test_projection_python_slot_rejects_wrong_owner_report(tmp_path: Path) -> None:
    repo, selection, reports, slots = _projection_strict_case(tmp_path)
    wrong_owner = _cobertura(
        tmp_path / "wrong-owner.xml", "windows-client/runtime.py", {1: 2}
    )
    reports["projection_python"] = [wrong_owner]
    slots["projection_python"] = wrong_owner

    with pytest.raises(collector.CoveragePolicyError) as failure:
        _evaluate_projection_strict(repo, selection, reports, slots)

    assert failure.value.code == "unproductive_report"
    assert "projection_python" in failure.value.message


def test_projection_python_slot_rejects_non_candidate_source(tmp_path: Path) -> None:
    repo, selection, reports, slots = _projection_strict_case(tmp_path)
    non_candidate = _cobertura(
        tmp_path / "non-candidate.xml",
        "src/astralprojection/not-in-candidate.py",
        {1: 1},
    )
    reports["projection_python"] = [non_candidate]
    slots["projection_python"] = non_candidate

    with pytest.raises(collector.CoveragePolicyError) as failure:
        _evaluate_projection_strict(repo, selection, reports, slots)

    assert failure.value.code == "unproductive_report"
    assert "projection_python" in failure.value.message


def test_projection_python_report_cannot_omit_one_of_multiple_executable_changes(
    tmp_path: Path,
) -> None:
    repo, selection, reports, slots = _projection_strict_case(tmp_path)
    projection = _cobertura_many(
        tmp_path / "projection-unobserved.xml",
        {
            "src/astralprojection/runtime.py": {1: 1},
            "backend/rote/adaptation.py": {1: 1},
            "src/astralprojection/declarations.py": {3: 1, 6: 1},
        },
    )
    reports["projection_python"] = [projection]
    slots["projection_python"] = projection

    with pytest.raises(collector.CoveragePolicyError) as failure:
        _evaluate_projection_strict(repo, selection, reports, slots)

    assert failure.value.code == "producer_unmapped_changed_line"
    assert "runtime.py':2" in failure.value.message


def test_python_candidate_executable_witness_excludes_non_statements() -> None:
    source = b'''# comment
"""module doc"""

@decorate(
    1,
)
def declared(
    value: int,
) -> int:
    """function doc"""
    try:
        first = value + 1
        return first
    except ValueError:
        return 0

if TYPE_CHECKING:
    from missing import thing

blank = 1

if unavailable:  # pragma: no cover - platform declaration
    hidden = 1
'''

    assert collector._python_candidate_executable_lines(
        source, "components/AstralProjection/src/astralprojection/runtime.py"
    ) == frozenset({4, 7, 11, 12, 13, 14, 15, 20})


def test_python_candidate_executable_witness_includes_match_case_headers() -> None:
    source = b"""match value:
    case 1:
        result = 1
    case _:
        result = 0
"""

    assert collector._python_candidate_executable_lines(
        source, "components/AstralProjection/backend/rote/match.py"
    ) == frozenset({1, 2, 3, 4, 5})


def test_python_candidate_executable_witness_keeps_runtime_else_clauses() -> None:
    source = b"""if TYPE_CHECKING:
    from missing import thing
else:
    runtime = 1

if unavailable:  # pragma no cover
    hidden = 1
else:
    visible = 1

"runtime marker"
"""

    assert collector._python_candidate_executable_lines(
        source, "components/AstralProjection/backend/rote/runtime.py"
    ) == frozenset({4, 9, 11})


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        (
            "if (TYPE_CHECKING):\n    hidden = 1\nvisible = 2\n",
            frozenset({1, 2, 3}),
        ),
        (
            "if (\n    TYPE_CHECKING\n):\n    hidden = 1\nvisible = 2\n",
            frozenset({1, 4, 5}),
        ),
        (
            "if enabled:  # do not add pragma: no cover here\n"
            "    runtime = 1\nvisible = 2\n",
            frozenset({1, 2, 3}),
        ),
        (
            "match value:\n    case (\n        1\n    ):\n        result = 1\n",
            frozenset({1, 2, 5}),
        ),
        (
            "def generate():\n    while False:\n        yield None\n    return 1\n",
            frozenset({1, 2, 4}),
        ),
    ),
)
def test_python_candidate_witness_matches_locked_parser_edge_fixtures(
    source: str, expected: frozenset[int]
) -> None:
    path = "backend/edge_fixture.py"

    assert _native_python_statements(source, path) == expected
    assert (
        collector._python_candidate_executable_lines(source.encode("utf-8"), path)
        == expected
    )


def test_python_candidate_witness_has_no_coverage_parser_underapproximation() -> None:
    missing_by_path: dict[str, list[int]] = {}
    extra_by_path: dict[str, list[int]] = {}
    statement_count = 0
    source_paths = (
        REPO_ROOT / relative
        for relative in _git(
            REPO_ROOT, "ls-files", "--", "backend", "scripts"
        ).splitlines()
        if relative.endswith(".py")
    )
    for source_path in source_paths:
        relative = source_path.relative_to(REPO_ROOT).as_posix()
        target = collector.classify_path(relative)
        if target is None or target.language != "python":
            continue
        source = source_path.read_text(encoding="utf-8")
        native = _native_python_statements(source, relative)
        statement_count += len(native)
        witness = collector._python_candidate_executable_lines(
            source.encode("utf-8"), relative
        )
        missing = sorted(native - witness)
        if missing:
            missing_by_path[relative] = missing
        extra = sorted(witness - native)
        if extra:
            extra_by_path[relative] = extra

    assert statement_count > 400
    assert missing_by_path == {}
    assert extra_by_path == {}


def test_python_314_candidate_witness_matches_locked_coverage_parser() -> None:
    python = shutil.which("python3")
    assert python is not None
    version = subprocess.run(
        [
            python,
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    if tuple(int(part) for part in version.split(".")) < (3, 14):
        pytest.skip("system Python 3.14 is not available on this runner")

    relative_paths = [
        relative
        for relative in _git(
            REPO_ROOT, "ls-files", "--", "backend", "scripts"
        ).splitlines()
        if relative.endswith(".py")
        and (target := collector.classify_path(relative)) is not None
        and target.language == "python"
    ]
    program = r"""
import importlib.util
import json
import pathlib
import sys

root = pathlib.Path.cwd()
spec = importlib.util.spec_from_file_location(
    "changed_coverage_python_314", root / "scripts/check_changed_coverage.py"
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
paths = json.loads(sys.stdin.read())
print(json.dumps({
    path: sorted(module._python_candidate_executable_lines(
        (root / path).read_bytes(), path
    ))
    for path in paths
}, sort_keys=True))
"""
    process = subprocess.run(
        [python, "-c", program],
        cwd=REPO_ROOT,
        input=json.dumps(relative_paths),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    system_witnesses = json.loads(process.stdout)
    mismatches: dict[str, dict[str, list[int]]] = {}
    for relative in relative_paths:
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        native = _native_python_statements(source, relative)
        witness = frozenset(system_witnesses[relative])
        if native != witness:
            mismatches[relative] = {
                "missing": sorted(native - witness),
                "extra": sorted(witness - native),
            }

    assert mismatches == {}


def test_python_candidate_witness_covers_real_multiline_probe_statement() -> None:
    relative = "backend/llm_config/probe.py"
    source = (REPO_ROOT / relative).read_text(encoding="utf-8")
    native = _native_python_statements(source, relative)

    assert 32 in native
    assert 32 in collector._python_candidate_executable_lines(
        source.encode("utf-8"), relative
    )


def test_python_candidate_witness_does_not_use_nullable_dis_line_tables() -> None:
    assert "dis.findlinestarts" not in SCRIPT.read_text(encoding="utf-8")


def test_projection_python_slot_binds_raw_and_semantic_report_digests(
    tmp_path: Path,
) -> None:
    repo, selection, reports, slots = _projection_strict_case(tmp_path)

    decision = _evaluate_projection_strict(repo, selection, reports, slots)

    projection = slots["projection_python"]
    expected = {
        "path": projection.as_posix(),
        **collector.coverage_report_identity(
            projection.read_bytes(),
            "projection_python",
            producer_key="projection_python",
        ),
        "producer_slot": "projection_python",
    }
    assert decision["status"] == "pass"
    assert decision["producer_slots"]["projection_python"] == expected
    assert decision["reports"]["projection_python"]["artifact_identities"] == [expected]


def test_deep_repository_profile_accepts_exact_three_slots_with_gitlink(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "deep"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    sources = {
        "backend/service.py": "service",
        "backend/voice_agent/worker.py": "worker",
        "scripts/tool.py": "tool",
    }
    for relative in sources:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("first = 1\n", encoding="utf-8")
    base = _commit(repo, "base")
    for relative in sources:
        (repo / relative).write_text("first = 1\nsecond = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{base},components/AstralPrimitives",
    )
    _git(repo, "commit", "-m", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")

    reports = {
        relative: _cobertura(
            tmp_path / f"{label}.xml",
            relative,
            {1: 1, 2: 1},
        )
        for relative, label in zip(
            sources,
            ("backend", "voice", "tooling"),
        )
    }
    output = tmp_path / "decision.json"
    assert (
        collector.main(
            [
                "--repo",
                str(repo),
                "--event-name",
                "manual",
                "--base-sha",
                base,
                "--candidate-sha",
                candidate,
                "--backend-python",
                str(reports["backend/service.py"]),
                "--voice-worker-python",
                str(reports["backend/voice_agent/worker.py"]),
                "--tooling-python",
                str(reports["scripts/tool.py"]),
                "--repository-profile",
                "deep",
                "--coverage-mode",
                "strict",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    decision = json.loads(output.read_text(encoding="utf-8"))
    assert decision["status"] == "pass"
    assert decision["repository_profile"] == "deep"
    assert set(decision["producer_slots"]) == {"backend", "voice_worker", "tooling"}


def test_candidate_source_inventory_ignores_gitlinks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    source = repo / "backend" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 0\n", encoding="utf-8")
    base = _commit(repo, "base")
    source.write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{base},components/AstralProjection",
    )
    _git(repo, "commit", "-m", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")

    blobs = collector._candidate_source_blobs(repo, candidate)

    assert "backend/service.py" in blobs
    assert "components/AstralProjection" not in blobs


def test_candidate_source_inventory_rejects_malformed_regular_blob_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        collector,
        "_git",
        lambda *_args, **_kwargs: (
            b"100644 blob 1111111111111111111111111111111111111111 -"
            b"\tbackend/service.py\0"
        ),
    )

    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector._candidate_source_blobs(tmp_path, "2" * 40)

    assert failure.value.code == "invalid_candidate_tree"


def test_candidate_source_inventory_rejects_regular_mode_non_blob_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        collector,
        "_git",
        lambda *_args, **_kwargs: (
            b"100644 commit 1111111111111111111111111111111111111111 1"
            b"\tbackend/service.py\0"
        ),
    )

    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector._candidate_source_blobs(tmp_path, "2" * 40)

    assert failure.value.code == "invalid_candidate_tree"


def test_candidate_source_inventory_rejects_negative_regular_blob_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        collector,
        "_git",
        lambda *_args, **_kwargs: (
            b"100755 blob 1111111111111111111111111111111111111111 -1"
            b"\tscripts/tool.py\0"
        ),
    )

    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector._candidate_source_blobs(tmp_path, "2" * 40)

    assert failure.value.code == "invalid_candidate_tree"


def test_required_python_witness_rejects_nonregular_candidate_path(
    tmp_path: Path,
) -> None:
    path = "backend/linked.py"

    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector._candidate_source_witnesses(
            tmp_path,
            {},
            {path},
            required_python_paths={path},
        )

    assert failure.value.code == "candidate_witness_unavailable"
    assert "regular candidate blob" in failure.value.message


def test_required_python_witness_rejects_oversized_candidate_blob(
    tmp_path: Path,
) -> None:
    path = "backend/oversized.py"
    blobs = {
        path: collector.CandidateBlob(
            "1" * 40,
            collector.MAX_CANDIDATE_WITNESS_BLOB_BYTES + 1,
        )
    }

    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector._candidate_source_witnesses(
            tmp_path,
            blobs,
            {path},
            required_python_paths={path},
        )

    assert failure.value.code == "candidate_witness_limit"
    assert "per-blob" in failure.value.message


def test_required_python_witness_rejects_nul_candidate_blob(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    path = "backend/nul.py"
    source = repo / path
    source.parent.mkdir(parents=True)
    source.write_bytes(b"first = 1\n\0second = 2\n")
    candidate = _commit(repo, "candidate")
    blobs = collector._candidate_source_blobs(repo, candidate)

    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector._candidate_source_witnesses(
            repo,
            blobs,
            {path},
            required_python_paths={path},
        )

    assert failure.value.code == "invalid_candidate_source"
    assert "NUL" in failure.value.message


def test_event_selection_is_authoritative_for_pr_main_and_manual() -> None:
    base = "1" * 40
    candidate = "2" * 40
    pull_request = {"pull_request": {"base": {"sha": base}, "head": {"sha": candidate}}}
    selected = collector.select_revisions(
        event_name="pull_request",
        event_payload=pull_request,
        base_sha=base,
        candidate_sha=candidate,
    )
    assert selected.base_source == "pull_request.base.sha"
    assert selected.candidate_source == "pull_request.head.sha"

    with pytest.raises(collector.CoveragePolicyError) as mismatch:
        collector.select_revisions(
            event_name="pull_request",
            event_payload=pull_request,
            base_sha="3" * 40,
            candidate_sha=candidate,
        )
    assert mismatch.value.code == "event_identity_mismatch"

    push = collector.select_revisions(
        event_name="push",
        event_payload={"ref": "refs/heads/main", "before": base, "after": candidate},
        base_sha=None,
        candidate_sha=None,
    )
    assert (push.base_sha, push.candidate_sha) == (base, candidate)
    with pytest.raises(collector.CoveragePolicyError, match="refs/heads/main"):
        collector.select_revisions(
            event_name="push",
            event_payload={
                "ref": "refs/heads/topic",
                "before": base,
                "after": candidate,
            },
            base_sha=None,
            candidate_sha=None,
        )

    manual = collector.select_revisions(
        event_name="workflow_dispatch",
        event_payload={"inputs": {"base_sha": base, "candidate_sha": candidate}},
        base_sha=None,
        candidate_sha=None,
    )
    assert manual.base_source == "manual.base_sha"


def test_event_selection_rejects_missing_unknown_and_conflicting_manual_inputs() -> (
    None
):
    base = "1" * 40
    candidate = "2" * 40
    cases = [
        {
            "event_name": "pull_request",
            "event_payload": {},
            "base_sha": None,
            "candidate_sha": None,
        },
        {
            "event_name": "schedule",
            "event_payload": {},
            "base_sha": base,
            "candidate_sha": candidate,
        },
        {
            "event_name": "workflow_dispatch",
            "event_payload": {"inputs": {"base_sha": base, "candidate_sha": candidate}},
            "base_sha": "3" * 40,
            "candidate_sha": candidate,
        },
        {
            "event_name": "workflow_dispatch",
            "event_payload": {"inputs": {"base_sha": base, "candidate_sha": candidate}},
            "base_sha": base,
            "candidate_sha": "3" * 40,
        },
        {
            "event_name": "manual",
            "event_payload": None,
            "base_sha": None,
            "candidate_sha": None,
        },
    ]
    for case in cases:
        with pytest.raises(collector.CoveragePolicyError):
            collector.select_revisions(**case)


def test_revision_validation_rejects_zero_equal_and_nonancestor(
    git_repo: tuple[Path, str],
) -> None:
    repo, base = git_repo
    (repo / "backend" / "service.py").write_text("first = 3\n", encoding="utf-8")
    candidate = _commit(repo, "candidate")
    assert _selection(repo, base, candidate).candidate_sha == candidate

    with pytest.raises(collector.CoveragePolicyError) as zero:
        collector.validate_revisions(
            repo,
            collector.RevisionSelection(
                "manual", "0" * 40, candidate, "manual", "manual"
            ),
        )
    assert zero.value.code == "zero_revision"
    with pytest.raises(collector.CoveragePolicyError) as equal:
        _selection(repo, candidate, candidate)
    assert equal.value.code == "empty_revision_range"

    _git(repo, "checkout", "--orphan", "unrelated")
    (repo / "backend" / "service.py").write_text("other = 1\n", encoding="utf-8")
    unrelated = _commit(repo, "unrelated")
    with pytest.raises(collector.CoveragePolicyError) as nonancestor:
        _selection(repo, base, unrelated)
    assert nonancestor.value.code == "non_ancestor_base"


def test_null_delimited_diff_and_explicit_path_mapping(
    git_repo: tuple[Path, str],
) -> None:
    repo, base = git_repo
    unusual = repo / "backend" / "space name.py"
    unusual.write_text("value = 1\n", encoding="utf-8")
    candidate = _commit(repo, "space path")
    changed = collector.read_changed_lines(repo, base, candidate)
    assert changed["backend/space name.py"] == {1}

    expected = {
        "backend/orchestrator/a.py": "backend_python",
        "backend/voice_agent/main.py": "backend_python",
        "scripts/release.py": "tooling_python",
        "components/AstralProjection/src/astralprojection/runtime.py": "projection_python",
        "components/AstralProjection/backend/rote/adaptation.py": "projection_python",
        "components/AstralProjection/backend/webrender/renderer.py": "projection_python",
        "components/AstralProjection/windows-client/win_agent/host.py": "windows_python",
        "components/AstralProjection/backend/webrender/static/client.js": "javascript",
        "components/AstralProjection/tooling/web-ci/eslint.config.mjs": "javascript",
        "components/AstralProjection/android-client/app/src/main/kotlin/x/App.kt": "android_app",
        "components/AstralProjection/android-client/app/src/main/java/x/Compat.kt": "android_app",
        "components/AstralProjection/android-client/core/src/main/kotlin/x/Core.kt": "android_core",
        "components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift": "apple",
        "components/AstralProjection/apple-clients/AstralCore/Sources/AstralCore/API/Rest.swift": "apple",
        "components/AstralProjection/apple-clients/AstralWatch/WatchModel.swift": "apple",
    }
    assert {path: collector.classify_path(path).key for path in expected} == expected
    for excluded in (
        "backend/tests/test_a.py",
        "components/AstralProjection/backend/webrender/static/vendor/plotly.min.js",
        "components/AstralProjection/tooling/web-ci/tests/release.spec.js",
        "components/AstralProjection/android-client/app/src/test/kotlin/x/AppTest.kt",
        "components/AstralProjection/apple-clients/AstralCore/Tests/AstralCoreTests/CoreTests.swift",
        "components/AstralProjection/android-client/build.gradle.kts",
    ):
        assert collector.classify_path(excluded) is None


def test_distinct_cobertura_reports_union_and_dedupe_changed_lines(
    git_repo: tuple[Path, str], tmp_path: Path
) -> None:
    repo, base = git_repo
    (repo / "backend" / "service.py").write_text(
        "first = 10\nsecond = 20\n", encoding="utf-8"
    )
    candidate = _commit(repo, "both lines")
    first = _cobertura(tmp_path / "first.xml", "backend/service.py", {1: 1, 2: 0})
    second = _cobertura(
        tmp_path / "second.xml", "/app/backend/service.py", {1: 1, 2: 1}
    )
    decision = collector.evaluate_changed_coverage(
        repo,
        _selection(repo, base, candidate),
        {"backend_python": [first, second]},
    )
    assert decision["status"] == "pass"
    assert decision["languages"]["python"] == {
        "covered_lines": 2,
        "executable_lines": 2,
        "percent": 100.0,
    }
    assert len(decision["reports"]["backend_python"]["artifacts"]) == 2
    first_identity = collector.coverage_report_identity(
        first.read_bytes(), "backend_python"
    )
    second_identity = collector.coverage_report_identity(
        second.read_bytes(), "backend_python"
    )
    assert decision["reports"]["backend_python"]["artifact_identities"] == [
        {
            "path": str(first).replace("\\", "/"),
            **first_identity,
        },
        {
            "path": str(second).replace("\\", "/"),
            **second_identity,
        },
    ]
    assert len(decision["lines"]) == 2


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
def test_report_inputs_reject_global_path_inode_and_content_aliases(
    git_repo: tuple[Path, str], tmp_path: Path, alias_kind: str
) -> None:
    repo, base = git_repo
    (repo / "backend" / "service.py").write_text("first = 9\n", encoding="utf-8")
    candidate = _commit(repo, "changed")
    report = _cobertura(tmp_path / "coverage.xml", "backend/service.py", {1: 1})
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
    reports = {"backend_python": [report, alias]}
    if alias_kind == "cross_partition":
        reports = {
            "backend_python": [report],
            "windows_python": [alias],
        }

    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector.evaluate_changed_coverage(
            repo,
            _selection(repo, base, candidate),
            reports,
        )

    assert failure.value.code == "duplicate_report"


def test_semantic_identity_ignores_irrelevant_json_metadata(tmp_path: Path) -> None:
    swift_path = (
        "components/AstralProjection/apple-clients/AstralWatch/WatchModel.swift"
    )
    observations = [{"line": 1, "isExecutable": True, "executionCount": 1}]
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(
        json.dumps({swift_path: observations, "generatedAt": "2026-07-31T00:00:00Z"}),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps({swift_path: observations, "generatedAt": "2026-08-01T00:00:00Z"}),
        encoding="utf-8",
    )
    first_identity = collector.coverage_report_identity(first.read_bytes(), "apple")
    second_identity = collector.coverage_report_identity(second.read_bytes(), "apple")
    assert first_identity["sha256"] != second_identity["sha256"]
    assert first_identity["semantic_sha256"] == second_identity["semantic_sha256"]
    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector._unique_report_inputs({"apple": [first, second]})
    assert failure.value.code == "duplicate_report"


def test_native_identity_rejects_metadata_copy_across_target_filters(
    tmp_path: Path,
) -> None:
    first = _cobertura(tmp_path / "backend.xml", "backend/service.py", {1: 1})
    second = tmp_path / "windows.xml"
    second.write_text(
        first.read_text(encoding="utf-8").replace(
            "<coverage ", '<coverage timestamp="1780000000" '
        ),
        encoding="utf-8",
    )
    backend = collector.coverage_report_identity(first.read_bytes(), "backend_python")
    windows = collector.coverage_report_identity(second.read_bytes(), "windows_python")
    assert backend["semantic_sha256"] != windows["semantic_sha256"]
    assert backend["native_semantic_sha256"] == windows["native_semantic_sha256"]
    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector._unique_report_inputs(
            {"backend_python": [first], "windows_python": [second]}
        )
    assert failure.value.code == "duplicate_report"


@pytest.mark.parametrize(
    ("runtime_name", "candidate_path"),
    tuple(collector.VOICE_WORKER_SOURCE_ALIASES.items()),
)
def test_voice_worker_runtime_sources_map_to_candidate_shared_sources(
    tmp_path: Path, runtime_name: str, candidate_path: str
) -> None:
    report = _cobertura(tmp_path / "voice.xml", runtime_name, {7: 1})
    _identity, coverage = collector._coverage_report_binding(
        report.read_bytes(),
        "backend_python",
        source=report,
        producer_key="voice_worker",
    )
    assert coverage.files == {candidate_path}
    assert coverage.executable == {(candidate_path, 7)}
    generic = collector.parse_coverage_report(report, "backend_python")
    assert generic.files == {runtime_name}


def test_overwritten_voice_shims_have_explicit_backend_ownership() -> None:
    for shim in collector.VOICE_WORKER_OVERWRITTEN_SHIMS:
        assert collector._producer_applies_to_path("backend", shim) is True
        assert collector._producer_applies_to_path("voice_worker", shim) is False
    for shared_source in collector.VOICE_WORKER_SOURCE_ALIASES.values():
        assert collector._producer_applies_to_path("backend", shared_source) is True
        assert (
            collector._producer_applies_to_path("voice_worker", shared_source) is True
        )
    assert (
        collector._producer_applies_to_path(
            "voice_worker", "backend/voice_agent/main.py"
        )
        is True
    )
    assert (
        collector._producer_applies_to_path("backend", "backend/voice_agent/main.py")
        is False
    )


def test_apple_core_uses_ios_or_macos_ownership_not_watchos() -> None:
    core_path = "components/AstralProjection/apple-clients/AstralCore/Sources/AstralCore/API/Rest.swift"
    assert collector._producer_applies_to_path("ios", core_path) is True
    assert collector._producer_applies_to_path("macos", core_path) is True
    assert collector._producer_applies_to_path("watchos", core_path) is False


@pytest.mark.parametrize(
    ("reports", "expected"),
    [
        ({}, "missing_report"),
        ({"backend_python": ["broken"]}, "unparseable_report"),
        ({"backend_python": ["unmapped"]}, "unmapped_changed_file"),
    ],
)
def test_missing_unparseable_and_unmapped_reports_fail_closed(
    git_repo: tuple[Path, str],
    tmp_path: Path,
    reports: dict[str, list[str]],
    expected: str,
) -> None:
    repo, base = git_repo
    (repo / "backend" / "service.py").write_text("first = 9\n", encoding="utf-8")
    candidate = _commit(repo, "changed")
    broken = tmp_path / "broken.xml"
    broken.write_text("not xml", encoding="utf-8")
    unmapped = _cobertura(tmp_path / "unmapped.xml", "backend/other.py", {1: 1})
    resolved = {
        key: [broken if item == "broken" else unmapped for item in values]
        for key, values in reports.items()
    }
    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector.evaluate_changed_coverage(
            repo, _selection(repo, base, candidate), resolved
        )
    assert failure.value.code == expected


def test_unexpected_empty_executable_selection_fails(
    git_repo: tuple[Path, str], tmp_path: Path
) -> None:
    repo, base = git_repo
    (repo / "backend" / "service.py").write_text(
        "# changed comment\nfirst = 1\nsecond = 2\n", encoding="utf-8"
    )
    candidate = _commit(repo, "comment")
    report = _cobertura(tmp_path / "coverage.xml", "backend/service.py", {2: 1, 3: 1})
    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector.evaluate_changed_coverage(
            repo,
            _selection(repo, base, candidate),
            {"backend_python": [report]},
        )
    assert failure.value.code == "unexpected_empty_executable_diff"


def test_per_language_gate_cannot_be_hidden_by_combined_coverage(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    python_path = repo / "backend" / "service.py"
    js_path = (
        repo
        / "components"
        / "AstralProjection"
        / "backend"
        / "webrender"
        / "static"
        / "client.js"
    )
    python_path.parent.mkdir(parents=True)
    js_path.parent.mkdir(parents=True)
    python_path.write_text(
        "".join(f"value_{n} = 0\n" for n in range(9)), encoding="utf-8"
    )
    js_path.write_text("const value = 0;\n", encoding="utf-8")
    base = _commit(repo, "base")
    python_path.write_text(
        "".join(f"value_{n} = 1\n" for n in range(9)), encoding="utf-8"
    )
    js_path.write_text("const value = 1;\n", encoding="utf-8")
    candidate = _commit(repo, "candidate")
    py_report = _cobertura(
        tmp_path / "python.xml", "backend/service.py", dict.fromkeys(range(1, 10), 1)
    )
    js_report = tmp_path / "javascript.json"
    js_report.write_text(
        json.dumps(
            _javascript_envelope(
                {
                    "components/AstralProjection/backend/webrender/static/client.js": {
                        "path": "components/AstralProjection/backend/webrender/static/client.js",
                        "statementMap": {
                            "0": {
                                "start": {"line": 1, "column": 0},
                                "end": {"line": 1, "column": 16},
                            }
                        },
                        "s": {"0": 0},
                    }
                }
            )
        ),
        encoding="utf-8",
    )
    decision = collector.evaluate_changed_coverage(
        repo,
        _selection(repo, base, candidate),
        {"backend_python": [py_report], "javascript": [js_report]},
    )
    assert decision["combined"]["percent"] == 90.0
    assert decision["status"] == "fail"
    assert [(item["scope"], item["code"]) for item in decision["failures"]] == [
        ("javascript", "coverage_below_threshold")
    ]


def test_kover_istanbul_and_xccov_line_observations_parse(tmp_path: Path) -> None:
    kover = tmp_path / "app.xml"
    kover.write_text(
        '<report><package name="com/example"><sourcefile name="App.kt">'
        '<line nr="3" mi="0" ci="2" mb="0" cb="0"/>'
        '<line nr="4" mi="2" ci="0" mb="0" cb="0"/>'
        '<line nr="5" mi="0" ci="0" mb="0" cb="0"/>'
        '<counter type="INSTRUCTION" missed="2" covered="2"/>'
        '<counter type="LINE" missed="1" covered="1"/>'
        '</sourcefile><counter type="INSTRUCTION" missed="2" covered="2"/>'
        '<counter type="LINE" missed="1" covered="1"/>'
        '</package><counter type="INSTRUCTION" missed="2" covered="2"/>'
        '<counter type="LINE" missed="1" covered="1"/></report>',
        encoding="utf-8",
    )
    kotlin = collector.parse_coverage_report(kover, "android_app")
    kotlin_path = "components/AstralProjection/android-client/app/src/main/kotlin/com/example/App.kt"
    assert kotlin.observed == {
        (kotlin_path, 3),
        (kotlin_path, 4),
        (kotlin_path, 5),
    }
    assert kotlin.executable == {(kotlin_path, 3), (kotlin_path, 4)}
    assert kotlin.covered == {(kotlin_path, 3)}

    js_path = "components/AstralProjection/backend/webrender/static/client.js"
    istanbul = tmp_path / "istanbul.json"
    istanbul.write_text(
        json.dumps(
            _javascript_envelope(
                {
                    js_path: {
                        "path": js_path,
                        "statementMap": {
                            "0": {
                                "start": {"line": 1, "column": 0},
                                "end": {"line": 1, "column": 10},
                            },
                            "1": {
                                "start": {"line": 2, "column": 0},
                                "end": {"line": 2, "column": 10},
                            },
                        },
                        "s": {"0": 0, "1": 3},
                    }
                }
            )
        ),
        encoding="utf-8",
    )
    javascript = collector.parse_coverage_report(istanbul, "javascript")
    assert javascript.executable == {(js_path, 1), (js_path, 2)}
    assert javascript.covered == {(js_path, 2)}

    xccov = tmp_path / "apple.json"
    swift_path = (
        "components/AstralProjection/apple-clients/AstralWatch/WatchModel.swift"
    )
    xccov.write_text(
        json.dumps(
            {
                f"/work/{swift_path}": [
                    *[{"line": line, "isExecutable": False} for line in range(1, 7)],
                    {"line": 7, "isExecutable": True, "executionCount": 0},
                    {"line": 8, "isExecutable": True, "executionCount": 1},
                ]
            }
        ),
        encoding="utf-8",
    )
    apple = collector.parse_coverage_report(xccov, "apple")
    assert apple.observed == {(swift_path, line) for line in range(1, 9)}
    assert apple.executable == {(swift_path, 7), (swift_path, 8)}
    assert apple.covered == {(swift_path, 8)}


def test_istanbul_statement_ranges_are_supported(
    tmp_path: Path,
) -> None:
    js_path = "components/AstralProjection/tooling/web-ci/eslint.config.mjs"
    istanbul = tmp_path / "statements.json"
    istanbul.write_text(
        json.dumps(
            _javascript_envelope(
                {
                    js_path: {
                        "path": js_path,
                        "statementMap": {
                            "0": {
                                "start": {"line": 2, "column": 0},
                                "end": {"line": 3, "column": 1},
                            }
                        },
                        "s": {"0": 1},
                    }
                }
            )
        ),
        encoding="utf-8",
    )
    parsed = collector.parse_coverage_report(istanbul, "javascript")
    assert parsed.executable == {(js_path, 2)}
    assert parsed.covered == parsed.executable


def test_realistic_xccov_report_summary_is_not_misused_as_line_proof(
    tmp_path: Path,
) -> None:
    """`xccov view --report --json` exposes aggregates, not raw line counts."""

    report = tmp_path / "xccov-report.json"
    report.write_text(
        json.dumps(
            {
                "coveredLines": 8,
                "executableLines": 10,
                "lineCoverage": 0.8,
                "targets": [
                    {
                        "name": "AstralWatch",
                        "coveredLines": 8,
                        "executableLines": 10,
                        "lineCoverage": 0.8,
                        "files": [
                            {
                                "path": "/work/components/AstralProjection/apple-clients/AstralWatch/WatchModel.swift",
                                "coveredLines": 8,
                                "executableLines": 10,
                                "lineCoverage": 0.8,
                                "functions": [
                                    {
                                        "name": "WatchModel.refresh()",
                                        "lineNumber": 20,
                                        "executionCount": 1,
                                        "coveredLines": 8,
                                        "executableLines": 10,
                                        "lineCoverage": 0.8,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector.parse_coverage_report(report, "apple")
    assert failure.value.code == "unsupported_xccov_report"
    assert "export_xccov_line_coverage.py" in failure.value.message


def _apple_export_repo(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    sources = {
        "app": "components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift",
        "core": "components/AstralProjection/apple-clients/AstralCore/Sources/AstralCore/API/Rest.swift",
        "watch": "components/AstralProjection/apple-clients/AstralWatch/WatchModel.swift",
    }
    for relative_path in sources.values():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("let first = 1\nlet second = 2\n", encoding="utf-8")
    _commit(repo, "tracked Apple sources")
    bundle = repo / "build" / "fixture.xcresult"
    bundle.mkdir(parents=True)
    return repo, bundle, sources


def _local_archive_source(repo: Path, relative_path: str) -> str:
    return f"{xccov_exporter._local_archive_repo_root(repo)}/{relative_path}"


def _install_fake_xcrun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    file_list: list[str],
    files: dict[str, object],
) -> Path:
    fixture = tmp_path / "fake-xccov.json"
    fixture.write_text(
        json.dumps({"file_list": file_list, "files": files}), encoding="utf-8"
    )
    calls = tmp_path / "fake-xccov-calls.jsonl"
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir(exist_ok=True)
    # POSIX can execute the shebang fixture directly. On Windows, place the
    # script at the first argument (``xccov``) and expose a hard-linked Python
    # launcher as xcrun.exe; CreateProcess does not execute extensionless
    # shebang files.
    xcrun = tmp_path / "repo" / "xccov" if os.name == "nt" else binary_dir / "xcrun"
    xcrun.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

fixture = json.load(open(os.environ["FAKE_XCCOV_FIXTURE"], encoding="utf-8"))
args = sys.argv[1:]
if not args or args[0] != "xccov":
    args = ["xccov", *args]
with open(os.environ["FAKE_XCCOV_CALLS"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:4] == ["xccov", "view", "--archive", "--file-list"]:
    payload = "\\n".join(fixture["file_list"]) + "\\n"
    sys.stdout.buffer.write(payload.encode("utf-8"))
elif args[:4] == ["xccov", "view", "--archive", "--file"] and args[5] == "--json":
    value = fixture["files"][args[4]]
    payload = value if isinstance(value, str) else json.dumps(value)
    sys.stdout.buffer.write(payload.encode("utf-8"))
else:
    raise SystemExit(7)
""",
        encoding="utf-8",
    )
    if os.name == "nt":
        os.link(sys.executable, binary_dir / "xcrun.exe")
        shutil.copyfile(
            Path(sys.executable).parents[1] / "pyvenv.cfg",
            tmp_path / "pyvenv.cfg",
        )
    else:
        xcrun.chmod(0o755)
    monkeypatch.setenv("FAKE_XCCOV_FIXTURE", str(fixture))
    monkeypatch.setenv("FAKE_XCCOV_CALLS", str(calls))
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}{os.environ['PATH']}")
    return calls


def _xccov_lines(*, covered: bool = True) -> list[dict[str, object]]:
    return [
        {"line": 2, "isExecutable": True, "executionCount": int(covered)},
        {
            "line": 1,
            "isExecutable": True,
            "executionCount": 1,
            "subranges": [{"column": 1, "executionCount": 1, "length": 3}],
        },
    ]


def test_xccov_exporter_uses_real_per_file_subprocess_contract_and_platform_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, bundle, sources = _apple_export_repo(tmp_path)
    raw = {name: _local_archive_source(repo, path) for name, path in sources.items()}
    dependency = "/tmp/checkouts/LiveKit/Sources/LiveKit/Room.swift"
    calls_path = _install_fake_xcrun(
        tmp_path,
        monkeypatch,
        file_list=[raw["watch"], dependency, raw["core"], raw["app"]],
        files={path: {path: _xccov_lines()} for path in raw.values()},
    )

    output = repo / "build" / "ios.json"
    report = xccov_exporter.export_xccov(
        repo=repo, xcresult=bundle, output=output, platform="ios"
    )
    assert list(report) == [sources["app"], sources["core"]]
    assert report[sources["app"]][0] == {
        "line": 1,
        "isExecutable": True,
        "executionCount": 1,
    }
    assert sources["watch"] not in output.read_text(encoding="utf-8")
    assert collector.parse_coverage_report(output, "apple").files == {
        sources["app"],
        sources["core"],
    }

    second = repo / "build" / "ios-second.json"
    assert (
        xccov_exporter.main(
            [
                "--repo",
                str(repo),
                "--xcresult",
                str(bundle),
                "--platform",
                "ios",
                "--output",
                str(second),
            ]
        )
        == 0
    )
    assert output.read_bytes() == second.read_bytes()

    watch_output = repo / "build" / "watchos.json"
    watch_report = xccov_exporter.export_xccov(
        repo=repo, xcresult=bundle, output=watch_output, platform="watchos"
    )
    assert list(watch_report) == [sources["core"], sources["watch"]]
    assert sources["app"] not in watch_output.read_text(encoding="utf-8")

    calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    assert ["xccov", "view", "--archive", "--file-list", str(bundle)] in calls
    assert not any(raw["watch"] in call for call in calls[:3])
    assert any(raw["watch"] in call for call in calls)
    assert not any(dependency in call for call in calls)


def test_xccov_exporter_rejects_out_of_checkout_source_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, bundle, sources = _apple_export_repo(tmp_path)
    alias = f"/tmp/easier-covered/{sources['app']}"
    _install_fake_xcrun(
        tmp_path,
        monkeypatch,
        file_list=[alias],
        files={},
    )
    with pytest.raises(xccov_exporter.ExportError) as failure:
        xccov_exporter.export_xccov(
            repo=repo,
            xcresult=bundle,
            output=repo / "build" / "duplicate.json",
            platform="ios",
        )
    assert failure.value.code == "unsafe_archive_source"


def test_xccov_exporter_binds_a_historical_raw_job_checkout_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, bundle, sources = _apple_export_repo(tmp_path)
    archive_root = "/Users/runner/work/AstralDeep/AstralDeep"
    raw = {name: f"{archive_root}/{relative}" for name, relative in sources.items()}
    _install_fake_xcrun(
        tmp_path,
        monkeypatch,
        file_list=[raw["core"], raw["app"]],
        files={path: {path: _xccov_lines()} for path in raw.values()},
    )
    output = repo / "build" / "historical-root.json"
    report = xccov_exporter.export_xccov(
        repo=repo,
        xcresult=bundle,
        output=output,
        platform="ios",
        archive_repo_root=archive_root,
    )
    assert set(report) == {sources["app"], sources["core"]}

    mismatch = repo / "build" / "mismatched-root.json"
    with pytest.raises(xccov_exporter.ExportError) as failure:
        xccov_exporter.export_xccov(
            repo=repo,
            xcresult=bundle,
            output=mismatch,
            platform="ios",
            archive_repo_root="/Users/runner/work/Other/Other",
        )
    assert failure.value.code == "unsafe_archive_source"
    assert not mismatch.exists()


@pytest.mark.parametrize(
    "archive_root",
    ["relative/repo", "/tmp/repo/", "/tmp/../repo", "/tmp//repo", "/tmp/repo\nnext"],
)
def test_xccov_exporter_rejects_noncanonical_historical_roots(
    archive_root: str,
) -> None:
    with pytest.raises(xccov_exporter.ExportError) as failure:
        xccov_exporter._canonical_archive_repo_root(archive_root)
    assert failure.value.code == "invalid_archive_repo_root"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"not-json", "invalid_xccov_json"),
        (b'{"same":[],"same":[]}', "invalid_xccov_json"),
        (json.dumps({"wrong": _xccov_lines()}).encode(), "invalid_observation"),
        (
            json.dumps(
                {
                    "source": [
                        {"line": 1, "isExecutable": True, "executionCount": 1},
                        {"line": 1, "isExecutable": False},
                    ]
                }
            ).encode(),
            "invalid_observation",
        ),
        (
            json.dumps(
                {"source": [{"line": 1, "isExecutable": False, "unknown": 1}]}
            ).encode(),
            "invalid_observation",
        ),
        (
            json.dumps(
                {"source": [{"line": 1, "isExecutable": False, "executionCount": 0}]}
            ).encode(),
            "invalid_observation",
        ),
        (
            json.dumps(
                {
                    "source": [
                        {
                            "line": 1,
                            "isExecutable": True,
                            "executionCount": 1,
                            "subranges": [{"column": 1}],
                        }
                    ]
                }
            ).encode(),
            "invalid_observation",
        ),
        (
            json.dumps(
                {"source": [{"line": 3, "isExecutable": True, "executionCount": 1}]}
            ).encode(),
            "source_line_mismatch",
        ),
    ],
)
def test_xccov_exporter_rejects_malformed_or_partial_per_file_json(
    payload: bytes, expected: str
) -> None:
    with pytest.raises(xccov_exporter.ExportError) as failure:
        xccov_exporter._normalize_observations(
            payload, queried_path="source", maximum_lines=2
        )
    assert failure.value.code == expected


@pytest.mark.parametrize(
    "file_list",
    [
        ["/tmp/App.swift", "/tmp/App.swift"],
        ["/tmp/App.swift", "relative.swift"],
        ["/tmp/App.swift", "/tmp/Bad\rName.swift"],
    ],
)
def test_xccov_exporter_rejects_invalid_file_list_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_list: list[str],
) -> None:
    repo, bundle, _sources = _apple_export_repo(tmp_path)
    _install_fake_xcrun(tmp_path, monkeypatch, file_list=file_list, files={})
    with pytest.raises(xccov_exporter.ExportError) as failure:
        xccov_exporter._archive_file_list(repo, bundle)
    assert failure.value.code == "invalid_file_list"


def test_xccov_exporter_enforces_file_count_and_output_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, bundle, sources = _apple_export_repo(tmp_path)
    raw = _local_archive_source(repo, sources["app"])
    raw_core = _local_archive_source(repo, sources["core"])
    _install_fake_xcrun(
        tmp_path,
        monkeypatch,
        file_list=[raw, raw_core],
        files={
            raw: {raw: _xccov_lines()},
            raw_core: {raw_core: _xccov_lines()},
        },
    )
    monkeypatch.setattr(xccov_exporter, "MAX_ARCHIVE_FILES", 1)
    with pytest.raises(xccov_exporter.ExportError) as too_many:
        xccov_exporter._archive_file_list(repo, bundle)
    assert too_many.value.code == "invalid_file_list"

    monkeypatch.setattr(xccov_exporter, "MAX_ARCHIVE_FILES", 10_000)
    monkeypatch.setattr(xccov_exporter, "MAX_OUTPUT_BYTES", 1)
    output = repo / "build" / "oversize.json"
    with pytest.raises(xccov_exporter.ExportError) as oversized:
        xccov_exporter.export_xccov(
            repo=repo, xcresult=bundle, output=output, platform="ios"
        )
    assert oversized.value.code == "output_too_large"
    assert not output.exists()


def test_xccov_exporter_observation_budget_aborts_before_all_selected_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, bundle, sources = _apple_export_repo(tmp_path)
    raw = {name: _local_archive_source(repo, path) for name, path in sources.items()}
    calls_path = _install_fake_xcrun(
        tmp_path,
        monkeypatch,
        file_list=[raw["app"], raw["core"]],
        files={path: {path: _xccov_lines()} for path in raw.values()},
    )
    monkeypatch.setattr(xccov_exporter, "MAX_TOTAL_OBSERVATIONS", 2)
    output = repo / "build" / "bounded-observations.json"
    with pytest.raises(xccov_exporter.ExportError) as failure:
        xccov_exporter.export_xccov(
            repo=repo, xcresult=bundle, output=output, platform="ios"
        )
    assert failure.value.code == "observation_budget_exceeded"
    calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    per_file_calls = [call for call in calls if "--file" in call]
    assert len(per_file_calls) <= 1
    assert not output.exists()


def test_xccov_exporter_cumulative_input_budget_stops_before_second_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, bundle, sources = _apple_export_repo(tmp_path)
    raw = {name: _local_archive_source(repo, path) for name, path in sources.items()}
    files = {path: {path: _xccov_lines()} for path in raw.values()}
    calls_path = _install_fake_xcrun(
        tmp_path,
        monkeypatch,
        file_list=[raw["app"], raw["core"]],
        files=files,
    )
    first_payload_bytes = len(json.dumps(files[raw["app"]]).encode("utf-8"))
    monkeypatch.setattr(xccov_exporter, "MAX_TOTAL_XCCOV_BYTES", first_payload_bytes)
    output = repo / "build" / "bounded-input.json"
    with pytest.raises(xccov_exporter.ExportError) as failure:
        xccov_exporter.export_xccov(
            repo=repo, xcresult=bundle, output=output, platform="ios"
        )
    assert failure.value.code == "input_budget_exceeded"
    calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len([call for call in calls if "--file" in call]) == 1
    assert not output.exists()


def test_xccov_exporter_overall_deadline_covers_inventory_and_all_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, bundle, sources = _apple_export_repo(tmp_path)
    raw = _local_archive_source(repo, sources["app"])
    _install_fake_xcrun(
        tmp_path,
        monkeypatch,
        file_list=[raw],
        files={raw: {raw: _xccov_lines()}},
    )
    monkeypatch.setattr(xccov_exporter, "EXPORT_TIMEOUT_SECONDS", 0)
    output = repo / "build" / "deadline.json"
    with pytest.raises(xccov_exporter.ExportError) as failure:
        xccov_exporter.export_xccov(
            repo=repo, xcresult=bundle, output=output, platform="ios"
        )
    assert failure.value.code == "export_timeout"
    assert not output.exists()


def test_xccov_exporter_rejects_output_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    repo, bundle, _sources = _apple_export_repo(tmp_path)
    victim = repo / "victim.txt"
    victim.write_text("preserve me", encoding="utf-8")
    output = repo / "build" / "coverage.json"
    output.symlink_to(victim)
    with pytest.raises(xccov_exporter.ExportError) as failure:
        xccov_exporter.export_xccov(
            repo=repo, xcresult=bundle, output=output, platform="ios"
        )
    assert failure.value.code == "output_exists"
    assert victim.read_text(encoding="utf-8") == "preserve me"


def test_xccov_exporter_command_and_json_bounds_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(xccov_exporter.ExportError) as failed:
        xccov_exporter._bounded_command(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x'); raise SystemExit(7)",
            ],
            cwd=tmp_path,
            max_stdout_bytes=2,
        )
    assert failed.value.code == "producer_failed"

    with pytest.raises(xccov_exporter.ExportError) as empty:
        xccov_exporter._bounded_command(
            [sys.executable, "-c", "pass"], cwd=tmp_path, max_stdout_bytes=2
        )
    assert empty.value.code == "producer_output_too_large"

    monkeypatch.setattr(xccov_exporter, "MAX_FILE_LIST_BYTES", 1)
    with pytest.raises(xccov_exporter.ExportError) as stderr_bound:
        xccov_exporter._bounded_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('long'); print('x')"],
            cwd=tmp_path,
            max_stdout_bytes=2,
        )
    assert stderr_bound.value.code == "producer_output_too_large"

    with pytest.raises(xccov_exporter.ExportError) as stdout_bound:
        xccov_exporter._bounded_command(
            [sys.executable, "-c", "print('long')"], cwd=tmp_path, max_stdout_bytes=2
        )
    assert stdout_bound.value.code == "producer_output_too_large"

    with pytest.raises(xccov_exporter.ExportError) as unavailable_error:
        xccov_exporter._bounded_command(
            [str(tmp_path / "missing-command")], cwd=tmp_path, max_stdout_bytes=2
        )
    assert unavailable_error.value.code == "producer_unavailable"

    monkeypatch.setattr(xccov_exporter, "COMMAND_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(xccov_exporter.ExportError) as timeout:
        xccov_exporter._bounded_command(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            cwd=tmp_path,
            max_stdout_bytes=2,
        )
    assert timeout.value.code == "producer_timeout"

    for content in (b"NaN", b"\xff"):
        with pytest.raises(xccov_exporter.ExportError) as malformed:
            xccov_exporter._strict_json(content)
        assert malformed.value.code == "invalid_xccov_json"


def test_xccov_exporter_inventory_path_and_observation_edge_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bundle = repo / "fixture.xcresult"
    bundle.mkdir()

    for value in ("", "bad\npath.swift", "../outside.swift", "/absolute.swift"):
        with pytest.raises(xccov_exporter.ExportError):
            xccov_exporter._safe_repo_path(value)
    assert (
        xccov_exporter._normalize_archive_path(
            "components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift"
        )
        == "components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift"
    )
    assert xccov_exporter._normalize_archive_path("/tmp/dependency.swift") is None

    inventory_outputs = iter(
        (
            b"truncated",
            b"\xff\x00",
            b"README.md\x00",
        )
    )
    monkeypatch.setattr(
        xccov_exporter,
        "_bounded_command",
        lambda *_args, **_kwargs: next(inventory_outputs),
    )
    for expected in (
        "invalid_git_inventory",
        "invalid_git_inventory",
        "empty_source_inventory",
    ):
        with pytest.raises(xccov_exporter.ExportError) as failure:
            xccov_exporter._tracked_swift_sources(repo, "ios")
        assert failure.value.code == expected

    file_list_outputs = iter((b"/tmp/App.swift", b"\xff\n", b"\n"))
    monkeypatch.setattr(
        xccov_exporter,
        "_bounded_command",
        lambda *_args, **_kwargs: next(file_list_outputs),
    )
    for _index in range(3):
        with pytest.raises(xccov_exporter.ExportError) as failure:
            xccov_exporter._archive_file_list(repo, bundle)
        assert failure.value.code == "invalid_file_list"

    for value in (True, -1, "1"):
        with pytest.raises(xccov_exporter.ExportError):
            xccov_exporter._integer(value, label="fixture")
    for value in ("not-a-list", [{}] * (xccov_exporter.MAX_SUBRANGES_PER_LINE + 1)):
        with pytest.raises(xccov_exporter.ExportError):
            xccov_exporter._validate_subranges(value)
    with pytest.raises(xccov_exporter.ExportError):
        xccov_exporter._validate_subranges(
            [
                {
                    "column": 1,
                    "executionCount": xccov_exporter.MAX_EXECUTION_COUNT + 1,
                    "length": 1,
                }
            ]
        )
    with pytest.raises(xccov_exporter.ExportError):
        xccov_exporter._normalize_observations(
            json.dumps(
                {
                    "source": [
                        {
                            "line": 1,
                            "isExecutable": True,
                            "executionCount": xccov_exporter.MAX_EXECUTION_COUNT + 1,
                        }
                    ]
                }
            ).encode(),
            queried_path="source",
            maximum_lines=1,
        )
    with pytest.raises(xccov_exporter.ExportError):
        xccov_exporter._normalize_observations(
            json.dumps({"source": []}).encode(),
            queried_path="source",
            maximum_lines=1,
        )


def test_xccov_exporter_filesystem_and_cli_failures_are_non_destructive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    existing = repo / "existing"
    existing.mkdir()
    symlink = repo / "linked"
    symlink.symlink_to(existing, target_is_directory=True)

    for path in (Path("../escape"), Path("missing"), Path("linked")):
        with pytest.raises(xccov_exporter.ExportError):
            xccov_exporter._validate_path(path, repo=repo, kind="fixture")
    with pytest.raises(xccov_exporter.ExportError):
        xccov_exporter._validate_path(Path("/tmp/outside"), repo=repo, kind="fixture")

    for output in (
        Path("../escape.json"),
        Path("missing/report.json"),
        Path("linked/report.json"),
        Path("/tmp/outside.json"),
    ):
        with pytest.raises(xccov_exporter.ExportError):
            xccov_exporter._validate_output(output, repo=repo)

    source = repo / "empty.swift"
    source.write_bytes(b"")
    with pytest.raises(xccov_exporter.ExportError) as empty_source:
        xccov_exporter._read_source_line_count(repo, "empty.swift")
    assert empty_source.value.code == "invalid_source_size"
    source.write_text("let value = 1\n", encoding="utf-8")
    monkeypatch.setattr(xccov_exporter, "MAX_FILE_JSON_BYTES", 1)
    with pytest.raises(xccov_exporter.ExportError) as source_bound:
        xccov_exporter._read_source_line_count(repo, "empty.swift")
    assert source_bound.value.code == "source_changed"

    output = repo / "write.json"
    original_fsync = xccov_exporter.os.fsync

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(xccov_exporter.os, "fsync", fail_fsync)
    with pytest.raises(xccov_exporter.ExportError) as write_failure:
        xccov_exporter._write_new_output(output, b"{}\n")
    assert write_failure.value.code == "output_write_failed"
    assert not output.exists()
    monkeypatch.setattr(xccov_exporter.os, "fsync", original_fsync)

    not_bundle = repo / "not-a-result"
    not_bundle.mkdir()
    with pytest.raises(xccov_exporter.ExportError) as invalid_bundle:
        xccov_exporter.export_xccov(
            repo=repo,
            xcresult=not_bundle,
            output=repo / "never.json",
            platform="ios",
        )
    assert invalid_bundle.value.code == "invalid_xcresult"
    with pytest.raises(xccov_exporter.ExportError) as invalid_platform:
        xccov_exporter.export_xccov(
            repo=repo,
            xcresult=not_bundle,
            output=repo / "never.json",
            platform="tvos",
        )
    assert invalid_platform.value.code == "invalid_platform"

    assert (
        xccov_exporter.main(
            [
                "--repo",
                str(repo / "absent"),
                "--xcresult",
                "missing.xcresult",
                "--platform",
                "ios",
                "--output",
                "missing.json",
            ]
        )
        == 2
    )
    assert "filesystem_error" in capsys.readouterr().err


def test_compact_fail_closed_parser_edge_contracts(tmp_path: Path) -> None:
    with pytest.raises(collector.CoveragePolicyError):
        collector.CoverageData().add("backend/a.py", 0, False)
    assert collector.classify_path("./backend/a.py").key == "backend_python"
    with pytest.raises(collector.CoveragePolicyError):
        collector.classify_path("../backend/a.py")

    for value in (True, "not-an-integer", -1):
        with pytest.raises(collector.CoveragePolicyError):
            collector._integer(value, label="fixture")
    for payload in (b'{"x":1,"x":2}', b'{"x":NaN}', b"not-json"):
        with pytest.raises(collector.CoveragePolicyError):
            collector._strict_json(payload)
    for threshold in ("not-a-number", -1, 101):
        with pytest.raises(collector.CoveragePolicyError):
            collector._threshold(threshold)

    empty = tmp_path / "empty.xml"
    empty.write_bytes(b"")
    with pytest.raises(collector.CoveragePolicyError):
        collector.parse_coverage_report(empty, "backend_python")
    with pytest.raises(collector.CoveragePolicyError):
        collector.parse_coverage_report(tmp_path / "missing.xml", "backend_python")
    valid = _cobertura(tmp_path / "valid.xml", "backend/a.py", {1: 1})
    with pytest.raises(collector.CoveragePolicyError):
        collector.parse_coverage_report(valid, "unknown")


def test_real_playwright_v8_comment_vector_is_rejected(tmp_path: Path) -> None:
    source = (
        "\n// pure comment\nconst hit = 1;\n\nfunction never() {\n"
        "  return 2;\n}\n//# sourceURL=https://candidate.invalid/static/client.js\n"
    )
    report = tmp_path / "raw-playwright-v8.json"
    report.write_text(
        json.dumps(
            [
                {
                    "url": "https://candidate.invalid/static/client.js",
                    "source": source,
                    "functions": [
                        {
                            "ranges": [
                                {"startOffset": 0, "endOffset": 123, "count": 1},
                                {"startOffset": 33, "endOffset": 65, "count": 0},
                            ]
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector.parse_coverage_report(report, "javascript")
    assert failure.value.code == "unparseable_report"
    assert "node/browser union envelope" in failure.value.message


def test_istanbul_comment_padding_cannot_mask_uncovered_statement(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    source_path = (
        repo
        / "components"
        / "AstralProjection"
        / "backend"
        / "webrender"
        / "static"
        / "client.js"
    )
    source_path.parent.mkdir(parents=True)
    source_path.write_text("", encoding="utf-8")
    base = _commit(repo, "base")
    source_path.write_text(
        "".join(f"// padding {index}\n" for index in range(1, 10)) + "neverCalled();\n",
        encoding="utf-8",
    )
    candidate = _commit(repo, "candidate")
    report = tmp_path / "istanbul.json"
    report.write_text(
        json.dumps(
            _javascript_envelope(
                {
                    "components/AstralProjection/backend/webrender/static/client.js": {
                        "path": "components/AstralProjection/backend/webrender/static/client.js",
                        "statementMap": {
                            "0": {
                                "start": {"line": 10, "column": 0},
                                "end": {"line": 10, "column": 13},
                            }
                        },
                        "s": {"0": 0},
                    }
                }
            )
        ),
        encoding="utf-8",
    )
    decision = collector.evaluate_changed_coverage(
        repo,
        _selection(repo, base, candidate),
        {"javascript": [report]},
    )
    assert decision["status"] == "fail"
    assert decision["languages"]["javascript"] == {
        "covered_lines": 0,
        "executable_lines": 1,
        "percent": 0.0,
    }
    assert [(line["line"], line["covered"]) for line in decision["lines"]] == [
        (10, False)
    ]


@pytest.mark.parametrize(
    "hidden_record",
    [
        {
            "path": "components/AstralProjection/backend/webrender/static/hidden.js",
            "l": {},
            "statementMap": {
                "0": {
                    "start": {"line": 1, "column": 0},
                    "end": {"line": 1, "column": 11},
                }
            },
            "s": {"0": 0},
        },
        {
            "path": "components/AstralProjection/backend/webrender/static/hidden.js",
            "l": {"1": 1},
            "statementMap": {
                "0": {
                    "start": {"line": 1, "column": 0},
                    "end": {"line": 1, "column": 11},
                }
            },
            "s": {"0": 0},
        },
        {
            "path": "components/AstralProjection/backend/webrender/static/hidden.js",
            "statementMap": {},
            "s": {},
        },
        {
            "path": "components/AstralProjection/backend/webrender/static/hidden.js",
            "statementMap": {
                "0": {
                    "start": {"line": 1, "column": 0},
                    "end": {"line": 1, "column": 11},
                }
            },
            "s": {"1": 0},
        },
    ],
)
def test_malformed_istanbul_cannot_hide_uncovered_file_behind_covered_peer(
    tmp_path: Path, hidden_record: dict[str, object]
) -> None:
    peer_path = "components/AstralProjection/backend/webrender/static/peer.js"
    report = tmp_path / "malformed-istanbul.json"
    report.write_text(
        json.dumps(
            _javascript_envelope(
                {
                    "components/AstralProjection/backend/webrender/static/hidden.js": hidden_record,
                    peer_path: {
                        "path": peer_path,
                        "statementMap": {
                            "0": {
                                "start": {"line": 1, "column": 0},
                                "end": {"line": 1, "column": 9},
                            }
                        },
                        "s": {"0": 1},
                    },
                }
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector.parse_coverage_report(report, "javascript")
    assert failure.value.code == "unparseable_report"


def test_gitattributes_cannot_hide_uncovered_maintained_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    backend = repo / "backend"
    backend.mkdir()
    hidden = backend / "hidden.py"
    peer = backend / "peer.py"
    hidden.write_text("hidden = 0\n", encoding="utf-8")
    peer.write_text("peer = 0\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / ".gitattributes").write_text("backend/hidden.py -diff\n", encoding="utf-8")
    hidden.write_text("hidden = 1\n", encoding="utf-8")
    peer.write_text("peer = 1\n", encoding="utf-8")
    candidate = _commit(repo, "candidate")

    changed = collector.read_changed_lines(repo, base, candidate)
    assert changed["backend/hidden.py"] == {1}
    assert changed["backend/peer.py"] == {1}
    report = tmp_path / "python.xml"
    report.write_text(
        '<coverage lines-valid="2" lines-covered="1" line-rate="0.5">'
        "<sources><source>/work</source></sources><packages><package><classes>"
        '<class filename="backend/hidden.py" line-rate="0"><lines>'
        '<line number="1" hits="0"/></lines></class>'
        '<class filename="backend/peer.py" line-rate="1"><lines>'
        '<line number="1" hits="1"/></lines></class>'
        "</classes></package></packages></coverage>\n",
        encoding="utf-8",
    )
    decision = collector.evaluate_changed_coverage(
        repo,
        _selection(repo, base, candidate),
        {"backend_python": [report]},
    )
    assert decision["status"] == "fail"
    assert decision["languages"]["python"]["percent"] == 50.0


def test_cobertura_resolves_relative_filenames_against_declared_sources(
    tmp_path: Path,
) -> None:
    backend = tmp_path / "backend.xml"
    backend.write_text(
        '<coverage lines-valid="1" lines-covered="0">'
        "<sources><source>/work/backend</source></sources>"
        '<packages><package><classes><class filename="scripts/prod.py" '
        'line-rate="0"><lines><line number="4" hits="0"/></lines></class>'
        "</classes></package></packages></coverage>",
        encoding="utf-8",
    )
    tooling = tmp_path / "tooling.xml"
    tooling.write_text(
        '<coverage lines-valid="1" lines-covered="1">'
        "<sources><source>/work</source></sources>"
        '<packages><package><classes><class filename="scripts/prod.py" '
        'line-rate="1"><lines><line number="4" hits="1"/></lines></class>'
        "</classes></package></packages></coverage>",
        encoding="utf-8",
    )

    parsed_backend = collector.parse_coverage_report(backend, "backend_python")
    parsed_tooling = collector.parse_coverage_report(tooling, "tooling_python")
    assert parsed_backend.executable == {("backend/scripts/prod.py", 4)}
    assert parsed_backend.covered == set()
    assert parsed_tooling.covered == {("scripts/prod.py", 4)}


@pytest.mark.parametrize(
    "contents",
    [
        (
            '<coverage lines-valid="2" lines-covered="1"><sources>'
            "<source>/work</source></sources><packages><package><classes>"
            '<class filename="backend/hidden.py" line-rate="1"><lines/></class>'
            '<class filename="backend/peer.py" line-rate="1"><lines>'
            '<line number="1" hits="1"/></lines></class>'
            "</classes></package></packages></coverage>"
        ),
        (
            '<coverage lines-valid="1" lines-covered="1"><sources>'
            "<source>/work</source></sources><packages><package><classes>"
            '<class filename="backend/hidden.py" line-rate="1"><lines>'
            '<line number="1" hits="0"/></lines></class>'
            "</classes></package></packages></coverage>"
        ),
        (
            '<coverage lines-valid="2" lines-covered="1"><sources>'
            "<source>/work</source></sources><packages><package><classes>"
            '<class filename="backend/hidden.py"><lines>'
            '<line number="1" hits="0"/></lines></class>'
            '<class filename="/app/backend/hidden.py"><lines>'
            '<line number="1" hits="1"/></lines></class>'
            "</classes></package></packages></coverage>"
        ),
    ],
)
def test_cobertura_omissions_rates_and_normalized_aliases_fail_closed(
    tmp_path: Path, contents: str
) -> None:
    report = tmp_path / "invalid.xml"
    report.write_text(contents, encoding="utf-8")
    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector.parse_coverage_report(report, "backend_python")
    assert failure.value.code == "unparseable_report"


def test_first_repository_anchor_prevents_cross_target_and_repeated_aliases(
    tmp_path: Path,
) -> None:
    tooling = collector.TARGET_BY_KEY["tooling_python"]
    backend = collector.TARGET_BY_KEY["backend_python"]
    assert (
        collector._normalized_report_path("/app/backend/scripts/release.py", tooling)
        is None
    )
    assert (
        collector._normalized_report_path("/app/backend/scripts/release.py", backend)
        == "backend/scripts/release.py"
    )
    assert (
        collector._normalized_report_path("/app/backend/backend/foo.py", backend)
        == "backend/backend/foo.py"
    )

    report = tmp_path / "aliases.xml"
    report.write_text(
        '<coverage lines-valid="3" lines-covered="2"><sources>'
        "<source>/work</source></sources><packages><package><classes>"
        '<class filename="scripts/release.py"><lines>'
        '<line number="1" hits="0"/></lines></class>'
        '<class filename="/app/backend/scripts/release.py"><lines>'
        '<line number="1" hits="1"/></lines></class>'
        '<class filename="/app/backend/backend/foo.py"><lines>'
        '<line number="1" hits="1"/></lines></class>'
        "</classes></package></packages></coverage>",
        encoding="utf-8",
    )
    parsed = collector.parse_coverage_report(report, "tooling_python")
    assert parsed.executable == {("scripts/release.py", 1)}
    assert parsed.covered == set()


@pytest.mark.parametrize(
    "contents",
    [
        (
            '<report><package name="com/example">'
            '<sourcefile name="Hidden.kt">'
            '<counter type="INSTRUCTION" missed="1" covered="0"/>'
            '<counter type="LINE" missed="1" covered="0"/></sourcefile>'
            '<sourcefile name="Peer.kt"><line nr="1" mi="0" ci="1"/>'
            '<counter type="INSTRUCTION" missed="0" covered="1"/>'
            '<counter type="LINE" missed="0" covered="1"/></sourcefile>'
            '<counter type="INSTRUCTION" missed="1" covered="1"/>'
            '<counter type="LINE" missed="1" covered="1"/></package>'
            '<counter type="INSTRUCTION" missed="1" covered="1"/>'
            '<counter type="LINE" missed="1" covered="1"/></report>'
        ),
        (
            '<report><package name="com/example"><sourcefile name="Hidden.kt">'
            '<line nr="1" mi="1" ci="0"/><line nr="1" mi="0" ci="1"/>'
            '<counter type="INSTRUCTION" missed="1" covered="1"/>'
            '<counter type="LINE" missed="1" covered="1"/></sourcefile>'
            '<counter type="INSTRUCTION" missed="1" covered="1"/>'
            '<counter type="LINE" missed="1" covered="1"/></package>'
            '<counter type="INSTRUCTION" missed="1" covered="1"/>'
            '<counter type="LINE" missed="1" covered="1"/></report>'
        ),
        (
            '<report><package name="com/example"><sourcefile name="Hidden.kt">'
            '<line nr="1" mi="1" ci="0"/>'
            '<counter type="INSTRUCTION" missed="1" covered="0"/>'
            '<counter type="LINE" missed="1" covered="0"/></sourcefile>'
            '<counter type="INSTRUCTION" missed="1" covered="0"/>'
            '<counter type="LINE" missed="0" covered="1"/></package>'
            '<counter type="INSTRUCTION" missed="1" covered="0"/>'
            '<counter type="LINE" missed="0" covered="1"/></report>'
        ),
    ],
)
def test_kover_omissions_duplicates_and_counter_mismatches_fail_closed(
    tmp_path: Path, contents: str
) -> None:
    report = tmp_path / "invalid-kover.xml"
    report.write_text(contents, encoding="utf-8")
    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector.parse_coverage_report(report, "android_app")
    assert failure.value.code == "unparseable_report"


@pytest.mark.parametrize(
    "observations",
    [
        [],
        [
            {"line": 1, "isExecutable": False},
            {"line": 3, "isExecutable": True, "executionCount": 0},
        ],
        [
            {"line": 1, "isExecutable": False},
            {"line": 1, "isExecutable": True, "executionCount": 0},
        ],
    ],
)
def test_xccov_empty_partial_and_duplicate_physical_lines_fail_closed(
    tmp_path: Path, observations: list[dict[str, object]]
) -> None:
    report = tmp_path / "invalid-archive.json"
    report.write_text(
        json.dumps(
            {
                "/work/components/AstralProjection/apple-clients/AstralWatch/WatchModel.swift": observations
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector.parse_coverage_report(report, "apple")
    assert failure.value.code == "unparseable_report"


def test_xccov_non_executable_changed_line_is_observed_but_not_counted(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    apple = repo / "components" / "AstralProjection" / "apple-clients"
    hidden = apple / "AstralWatch" / "Hidden.swift"
    peer = apple / "AstralWatch" / "Peer.swift"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("// old\n", encoding="utf-8")
    peer.write_text("let peer = 0\n", encoding="utf-8")
    base = _commit(repo, "base")
    hidden.write_text("// changed\n", encoding="utf-8")
    peer.write_text("let peer = 1\n", encoding="utf-8")
    candidate = _commit(repo, "candidate")
    report = tmp_path / "archive.json"
    report.write_text(
        json.dumps(
            {
                str(hidden): [{"line": 1, "isExecutable": False}],
                str(peer): [{"line": 1, "isExecutable": True, "executionCount": 1}],
            }
        ),
        encoding="utf-8",
    )
    decision = collector.evaluate_changed_coverage(
        repo,
        _selection(repo, base, candidate),
        {"apple": [report]},
    )
    assert decision["status"] == "pass"
    assert decision["languages"]["swift"]["executable_lines"] == 1
    assert decision["lines"] == [
        {
            "path": "components/AstralProjection/apple-clients/AstralWatch/Peer.swift",
            "line": 1,
            "target": "apple",
            "language": "swift",
            "covered": True,
        }
    ]


def test_xccov_must_observe_each_changed_physical_line(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    source = (
        repo
        / "components"
        / "AstralProjection"
        / "apple-clients"
        / "AstralWatch"
        / "WatchModel.swift"
    )
    source.parent.mkdir(parents=True)
    source.write_text("// first\n// old\n", encoding="utf-8")
    base = _commit(repo, "base")
    source.write_text("// first\n// changed\n", encoding="utf-8")
    candidate = _commit(repo, "candidate")
    report = tmp_path / "archive.json"
    report.write_text(
        json.dumps({str(source): [{"line": 1, "isExecutable": False}]}),
        encoding="utf-8",
    )
    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector.evaluate_changed_coverage(
            repo,
            _selection(repo, base, candidate),
            {"apple": [report]},
        )
    assert failure.value.code == "unmapped_changed_line"


def test_bare_unfiltered_istanbul_output_is_rejected(tmp_path: Path) -> None:
    path = "components/AstralProjection/backend/webrender/static/client.js"
    report = tmp_path / "unfiltered-v8-to-istanbul.json"
    report.write_text(
        json.dumps(
            {
                path: {
                    "path": path,
                    "statementMap": {
                        "0": {
                            "start": {"line": 1, "column": 0},
                            "end": {"line": 1, "column": 10},
                        }
                    },
                    "s": {"0": 1},
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(collector.CoveragePolicyError) as failure:
        collector.parse_coverage_report(report, "javascript")
    assert failure.value.code == "unparseable_report"
    assert "node/browser union envelope" in failure.value.message


def test_cli_writes_repeatable_exact_identity_json(
    git_repo: tuple[Path, str], tmp_path: Path
) -> None:
    repo, base = git_repo
    (repo / "backend" / "service.py").write_text(
        "first = 10\nsecond = 2\n", encoding="utf-8"
    )
    candidate = _commit(repo, "candidate")
    report = _cobertura(tmp_path / "coverage.xml", "backend/service.py", {1: 1, 2: 1})
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    common = [
        "--repo",
        str(repo),
        "--base-sha",
        base,
        "--candidate-sha",
        candidate,
        "--backend-python",
        str(report),
        "--coverage-mode",
        "partial",
    ]
    assert collector.main([*common, "--output", str(first)]) == 0
    assert collector.main([*common, "--output", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    document = json.loads(first.read_text(encoding="utf-8"))
    assert document["base_sha"] == base
    assert document["candidate_sha"] == candidate
    assert document["revisions_validated"] is True
    assert document["status"] == "pass"
    expected_identity = {
        "path": report.as_posix(),
        **collector.coverage_report_identity(report.read_bytes(), "backend_python"),
        "producer_slot": "backend",
    }
    assert document["producer_slots"] == {"backend": expected_identity}


def test_cli_rejects_repeated_producer_slots(tmp_path: Path) -> None:
    report = tmp_path / "coverage.xml"
    report.write_text("<coverage/>", encoding="utf-8")
    with pytest.raises(SystemExit) as failure:
        collector._parser().parse_args(
            [
                "--backend-python",
                str(report),
                "--backend-python",
                str(report),
                "--output",
                str(tmp_path / "decision.json"),
            ]
        )
    assert failure.value.code == 2


def test_cli_missing_report_error_retains_validated_revision_audit_fields(
    git_repo: tuple[Path, str], tmp_path: Path
) -> None:
    repo, base = git_repo
    (repo / "backend" / "service.py").write_text("first = 10\n", encoding="utf-8")
    candidate = _commit(repo, "candidate")
    output = tmp_path / "error.json"
    assert (
        collector.main(
            [
                "--repo",
                str(repo),
                "--base-sha",
                base,
                "--candidate-sha",
                candidate,
                "--fail-under",
                "91",
                "--coverage-mode",
                "partial",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["error"]["code"] == "missing_report"
    assert document["base_sha"] == base
    assert document["candidate_sha"] == candidate
    assert document["revisions_validated"] is True
    assert document["selection"] == {
        "event_name": "manual",
        "base_source": "manual.base_sha",
        "candidate_source": "manual.candidate_sha",
    }
    assert document["fail_under"] == 91.0


def test_cli_strict_mode_requires_the_exact_eleven_slot_matrix(
    git_repo: tuple[Path, str], tmp_path: Path
) -> None:
    repo, base = git_repo
    (repo / "backend" / "service.py").write_text("first = 10\n", encoding="utf-8")
    candidate = _commit(repo, "candidate")
    report = _cobertura(tmp_path / "backend.xml", "backend/service.py", {1: 1})
    output = tmp_path / "strict-error.json"
    assert (
        collector.main(
            [
                "--repo",
                str(repo),
                "--base-sha",
                base,
                "--candidate-sha",
                candidate,
                "--backend-python",
                str(report),
                "--coverage-mode",
                "strict",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["error"]["code"] == "incomplete_report_matrix"
    assert "voice_worker" in document["error"]["message"]
    assert "watchos" in document["error"]["message"]
