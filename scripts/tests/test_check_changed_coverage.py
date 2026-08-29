"""Feature 075 C# producer tests for the shared changed-line coverage gate."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_changed_coverage.py"


def _load_collector():
    spec = importlib.util.spec_from_file_location("changed_coverage_075", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collector = _load_collector()


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


def _selection(repo: Path, base: str, candidate: str):
    selected = collector.select_revisions(
        event_name="manual",
        event_payload=None,
        base_sha=base,
        candidate_sha=candidate,
    )
    return collector.validate_revisions(repo, selected)


def _cobertura(path: Path, filename: str, hits: list[int]) -> Path:
    lines = "".join(
        f'<line number="{index}" hits="{count}"/>'
        for index, count in enumerate(hits, start=1)
    )
    covered = sum(count > 0 for count in hits)
    total = len(hits)
    rate = covered / total
    path.write_text(
        f'<coverage lines-valid="{total}" lines-covered="{covered}" '
        f'line-rate="{rate}"><sources><source>C:/agent/_work/AstralProjection</source>'
        f'</sources><packages><package><classes><class filename="{filename}" '
        f'line-rate="{rate}"><lines>{lines}</lines></class></classes></package>'
        f"</packages></coverage>\n",
        encoding="utf-8",
    )
    return path


def test_windows_csharp_target_and_projection_profile_are_explicit() -> None:
    target = collector.TARGET_BY_KEY["windows_csharp"]
    producer = collector.PRODUCER_BY_KEY["windows_csharp"]

    assert target.language == "csharp"
    assert target.report_kind == "cobertura"
    assert target.roots == ("components/AstralProjection/windows-client/asr-helper",)
    assert producer.target_key == "windows_csharp"
    assert producer.flag == "windows-csharp"
    assert "windows_csharp" in collector.REPOSITORY_PROFILES["projection"].producer_keys
    assert collector.REPORT_FLAGS["windows_csharp"] == "windows-csharp"


def test_windows_csharp_classification_excludes_tests_and_build_outputs() -> None:
    source = "components/AstralProjection/windows-client/asr-helper/FrameProtocol.cs"
    assert collector.classify_path(source).key == "windows_csharp"
    assert (
        collector.classify_path(
            "components/AstralProjection/windows-client/asr-helper/tests/ProtocolTests.cs"
        )
        is None
    )
    assert (
        collector.classify_path(
            "components/AstralProjection/windows-client/asr-helper/bin/Helper.cs"
        )
        is None
    )


def test_windows_csharp_cobertura_maps_windows_paths_to_composed_source(
    tmp_path: Path,
) -> None:
    report = _cobertura(
        tmp_path / "coverage.cobertura.xml",
        r"windows-client\asr-helper\FrameProtocol.cs",
        [1, 0],
    )

    parsed = collector.parse_coverage_report(report, "windows_csharp")

    path = "components/AstralProjection/windows-client/asr-helper/FrameProtocol.cs"
    assert parsed.files == {path}
    assert parsed.executable == {(path, 1), (path, 2)}
    assert parsed.covered == {(path, 1)}


def test_windows_csharp_changed_line_gate_enforces_ninety_percent(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "projection"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Fixture")
    source = repo / "windows-client" / "asr-helper" / "FrameProtocol.cs"
    source.parent.mkdir(parents=True)
    source.write_text("line0\n", encoding="utf-8")
    base = _commit(repo, "base")
    source.write_text("".join(f"line{i}\n" for i in range(1, 11)), encoding="utf-8")
    candidate = _commit(repo, "candidate")
    selection = _selection(repo, base, candidate)

    report = _cobertura(
        tmp_path / "coverage.cobertura.xml",
        "windows-client/asr-helper/FrameProtocol.cs",
        [1] * 9 + [0],
    )
    decision = collector.evaluate_changed_coverage(
        repo,
        selection,
        {"windows_csharp": [report]},
        fail_under=90,
        producer_slots={"windows_csharp": report},
        strict_producers=False,
        repository_profile="projection",
        source_prefix="components/AstralProjection",
    )
    assert decision["status"] == "pass"
    assert decision["languages"]["csharp"] == {
        "covered_lines": 9,
        "executable_lines": 10,
        "percent": 90.0,
    }

    failing = _cobertura(
        tmp_path / "failing.cobertura.xml",
        "windows-client/asr-helper/FrameProtocol.cs",
        [1] * 8 + [0, 0],
    )
    decision = collector.evaluate_changed_coverage(
        repo,
        selection,
        {"windows_csharp": [failing]},
        fail_under=90,
        producer_slots={"windows_csharp": failing},
        strict_producers=False,
        repository_profile="projection",
        source_prefix="components/AstralProjection",
    )
    assert decision["status"] == "fail"
    assert decision["languages"]["csharp"]["percent"] == 80.0
    assert any(
        failure["scope"] == "csharp" and failure["code"] == "coverage_below_threshold"
        for failure in decision["failures"]
    )


def test_windows_csharp_cli_flag_is_single_and_named(tmp_path: Path) -> None:
    report = _cobertura(
        tmp_path / "coverage.cobertura.xml",
        "windows-client/asr-helper/FrameProtocol.cs",
        [1],
    )
    parsed = collector._parser().parse_args(
        ["--windows-csharp", str(report), "--output", str(tmp_path / "decision.json")]
    )
    assert parsed.windows_csharp == str(report)

    with pytest.raises(SystemExit):
        collector._parser().parse_args(
            [
                "--windows-csharp",
                str(report),
                "--windows-csharp",
                str(report),
                "--output",
                str(tmp_path / "decision.json"),
            ]
        )
