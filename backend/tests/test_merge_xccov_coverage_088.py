from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import merge_xccov_line_coverage as merger
from scripts.export_xccov_line_coverage import ExportError
from scripts.merge_xccov_line_coverage import MergeError, merge_xccov_reports


SOURCE = "components/AstralProjection/apple-clients/AstralApp/AstralApp/App.swift"


def _commit(root):
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "add",
            ".",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    source = repo / SOURCE
    source.parent.mkdir(parents=True)
    source.write_text("let one = 1\nlet two = 2\n", encoding="utf-8")
    component = repo / "components/AstralProjection"
    subprocess.run(["git", "init", "-q", str(component)], check=True)
    _commit(component)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _commit(repo)
    (repo / "build").mkdir()
    return repo


def _observations(first: int = 1, second: int = 0) -> list[dict[str, object]]:
    return [
        {"line": 1, "isExecutable": True, "executionCount": first},
        {"line": 2, "isExecutable": True, "executionCount": second},
    ]


def _report(repo: Path, name: str, document: object) -> Path:
    path = repo / "build" / name
    path.write_text(
        json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    return path


def test_unit_and_ui_reports_merge_deterministically_for_one_platform(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    unit = _report(repo, "unit.json", {SOURCE: _observations(2, 0)})
    ui = _report(repo, "ui.json", {SOURCE: _observations(1, 3)})
    output = repo / "build" / "ios.json"

    merged = merge_xccov_reports(
        repo=repo,
        inputs={"unit": unit, "ui": ui},
        output=output,
        platform="macos",
    )

    assert merged == {SOURCE: _observations(3, 3)}
    assert output.read_text(encoding="utf-8") == (
        json.dumps(merged, sort_keys=True, separators=(",", ":")) + "\n"
    )


def test_relative_inputs_and_output_are_resolved_under_explicit_repo(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _report(repo, "unit.json", {SOURCE: _observations(2, 0)})
    _report(repo, "ui.json", {SOURCE: _observations(0, 3)})

    merged = merge_xccov_reports(
        repo=repo,
        inputs={
            "unit": Path("build/unit.json"),
            "ui": Path("build/ui.json"),
        },
        output=Path("build/ios.json"),
        platform="macos",
    )

    assert merged == {SOURCE: _observations(2, 3)}
    assert (repo / "build/ios.json").is_file()


@pytest.mark.parametrize(
    "document",
    (
        {"../App.swift": _observations()},
        {"apple-clients/AstralWatch/Watch.swift": _observations()},
        {"apple-clients/AstralApp/AstralApp/Untracked.swift": _observations()},
        {SOURCE: [{"line": 2, "isExecutable": True, "executionCount": 1}]},
        {SOURCE: [{"line": 1, "isExecutable": True, "executionCount": True}]},
        {SOURCE: [{"line": 1, "isExecutable": True, "executionCount": 1, "extra": 0}]},
    ),
)
def test_path_source_and_observation_shape_fail_closed(
    tmp_path: Path,
    document: object,
) -> None:
    repo = _repo(tmp_path)
    report = _report(repo, "input.json", document)
    ui = _report(repo, "ui.json", {SOURCE: _observations()})

    with pytest.raises(MergeError):
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": report, "ui": ui},
            output=repo / "build" / "output.json",
            platform="macos",
        )


def test_duplicate_json_keys_and_duplicate_input_files_fail_closed(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    duplicate_key = repo / "build" / "duplicate.json"
    encoded = json.dumps(_observations(), separators=(",", ":"))
    duplicate_key.write_text(f'{{"{SOURCE}":{encoded},"{SOURCE}":{encoded}}}\n')
    ui = _report(repo, "ui.json", {SOURCE: _observations()})

    with pytest.raises(MergeError, match="duplicate"):
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": duplicate_key, "ui": ui},
            output=repo / "build" / "output.json",
            platform="macos",
        )

    report = _report(repo, "input.json", {SOURCE: _observations()})
    with pytest.raises(MergeError, match="duplicate"):
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": report, "ui": report},
            output=repo / "build" / "output.json",
            platform="macos",
        )


def test_incompatible_executable_masks_and_count_overflow_fail_closed(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    first = _report(repo, "first.json", {SOURCE: _observations((1 << 63) - 1, 0)})
    incompatible = _report(
        repo,
        "incompatible.json",
        {
            SOURCE: [
                {"line": 1, "isExecutable": False},
                {"line": 2, "isExecutable": True, "executionCount": 0},
            ]
        },
    )
    overflowing = _report(repo, "overflowing.json", {SOURCE: _observations(1, 0)})

    with pytest.raises(MergeError, match="executable"):
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": first, "ui": incompatible},
            output=repo / "build" / "incompatible-output.json",
            platform="macos",
        )
    with pytest.raises(MergeError, match="overflow"):
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": first, "ui": overflowing},
            output=repo / "build" / "overflow-output.json",
            platform="macos",
        )


@pytest.mark.parametrize(
    "inputs",
    (
        {},
        {"unit": "unit"},
        {"ui": "ui"},
        {"unit": "unit", "first-login": "ui"},
        {"unit": "unit", "ui": "ui", "integration": "other"},
    ),
)
def test_exact_unit_and_ui_producer_labels_are_required(
    tmp_path: Path,
    inputs: dict[str, str],
) -> None:
    repo = _repo(tmp_path)
    paths = {
        name: _report(repo, f"{name}.json", {SOURCE: _observations()})
        for name in {"unit", "ui", "other"}
    }

    with pytest.raises(MergeError, match="producer"):
        merge_xccov_reports(
            repo=repo,
            inputs={label: paths[name] for label, name in inputs.items()},
            output=repo / "build" / "output.json",
            platform="macos",
        )


def test_cli_requires_one_explicit_unit_and_ui_input(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    unit = _report(repo, "unit.json", {SOURCE: _observations(2, 0)})
    ui = _report(repo, "ui.json", {SOURCE: _observations(1, 3)})
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "merge_xccov_line_coverage.py"
    )

    def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *arguments],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )

    base = [
        "--repo",
        str(repo),
        "--platform",
        "macos",
        "--unit-input",
        str(unit),
        "--ui-input",
        str(ui),
        "--output",
        str(repo / "build" / "output.json"),
    ]
    assert run(base).returncode == 0
    (repo / "build" / "output.json").unlink()

    for arguments, expected in (
        (
            [value for index, value in enumerate(base) if index not in {4, 5}],
            "required",
        ),
        ([*base[:6], "--unit-input", str(unit), *base[6:]], "exactly one"),
        ([*base[:4], "--first-login-input", str(unit), *base[6:]], "required"),
    ):
        result = run(arguments)
        assert result.returncode != 0
        assert expected in result.stderr


def test_watchos_is_not_a_unit_ui_union_platform(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    unit = _report(repo, "unit.json", {SOURCE: _observations()})
    ui = _report(repo, "ui.json", {SOURCE: _observations()})

    with pytest.raises(MergeError, match="platform"):
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": unit, "ui": ui},
            output=repo / "build" / "output.json",
            platform="watchos",
        )


def test_hosted_workflow_repo_contained_inputs_are_accepted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    root = repo / "build" / "060" / "coverage" / "union-inputs" / "ios"
    unit = root / "unit" / "apple-ios-unit-xccov.json"
    ui = root / "ui" / "apple-ios-first-login-xccov.json"
    unit.parent.mkdir(parents=True)
    ui.parent.mkdir(parents=True)
    unit.write_text(json.dumps({SOURCE: _observations(2, 0)}) + "\n", encoding="utf-8")
    ui.write_text(json.dumps({SOURCE: _observations(1, 3)}) + "\n", encoding="utf-8")
    output = repo / "build" / "060" / "coverage" / "apple-ios-xccov.json"
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "merge_xccov_line_coverage.py"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--repo",
            str(repo),
            "--platform",
            "macos",
            "--unit-input",
            str(unit),
            "--ui-input",
            str(ui),
            "--output",
            str(output),
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == {
        SOURCE: _observations(3, 3)
    }


@pytest.mark.parametrize(
    ("content", "code"),
    (
        (b"", "input_too_large"),
        (b"\xff", "invalid_json"),
        (b"{", "invalid_json"),
        (b"[]", "invalid_document"),
        (b"{}", "invalid_document"),
        (b'{"source":NaN}', "invalid_json"),
    ),
)
def test_malformed_or_unbounded_input_bytes_fail_closed(
    tmp_path: Path,
    content: bytes,
    code: str,
) -> None:
    repo = _repo(tmp_path)
    unit = repo / "build" / "unit.json"
    unit.write_bytes(content)
    ui = _report(repo, "ui.json", {SOURCE: _observations()})

    with pytest.raises(MergeError) as raised:
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": unit, "ui": ui},
            output=repo / "build" / "output.json",
            platform="macos",
        )
    assert raised.value.code == code


@pytest.mark.parametrize(
    "observations",
    (
        [],
        [1, {"line": 2, "isExecutable": True, "executionCount": 0}],
        [
            {"line": 1, "isExecutable": "yes", "executionCount": 0},
            {"line": 2, "isExecutable": True, "executionCount": 0},
        ],
        [
            {"line": 1, "isExecutable": False, "extra": 0},
            {"line": 2, "isExecutable": True, "executionCount": 0},
        ],
    ),
)
def test_additional_observation_shape_failures_are_rejected(
    tmp_path: Path,
    observations: object,
) -> None:
    repo = _repo(tmp_path)
    unit = _report(repo, "unit.json", {SOURCE: observations})
    ui = _report(repo, "ui.json", {SOURCE: _observations()})

    with pytest.raises(MergeError):
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": unit, "ui": ui},
            output=repo / "build" / "output.json",
            platform="macos",
        )


def test_repository_output_and_input_filesystem_boundaries_fail_closed(
    tmp_path: Path,
) -> None:
    missing_repo = tmp_path / "missing"
    with pytest.raises(MergeError) as missing:
        merge_xccov_reports(
            repo=missing_repo,
            inputs={},
            output=Path("output.json"),
            platform="macos",
        )
    assert missing.value.code == "missing_repo"

    regular_file = tmp_path / "regular"
    regular_file.write_text("not a repository", encoding="utf-8")
    with pytest.raises(MergeError) as invalid:
        merge_xccov_reports(
            repo=regular_file,
            inputs={},
            output=Path("output.json"),
            platform="macos",
        )
    assert invalid.value.code == "invalid_repo"

    repo = _repo(tmp_path / "nested")
    unit = _report(repo, "unit.json", {SOURCE: _observations()})
    ui = _report(repo, "ui.json", {SOURCE: _observations()})
    occupied = repo / "build" / "occupied.json"
    occupied.write_text("occupied", encoding="utf-8")
    with pytest.raises(MergeError) as existing:
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": unit, "ui": ui},
            output=occupied,
            platform="macos",
        )
    assert existing.value.code == "output_exists"

    with pytest.raises(MergeError) as wrong_type:
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": unit, "ui": "build/ui.json"},  # type: ignore[dict-item]
            output=repo / "build" / "output.json",
            platform="macos",
        )
    assert wrong_type.value.code == "invalid_producer_input"

    with pytest.raises(MergeError) as missing_input:
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": repo / "build" / "missing.json", "ui": ui},
            output=repo / "build" / "output.json",
            platform="macos",
        )
    assert missing_input.value.code == "missing_input"

    directory_input = repo / "build" / "directory-input"
    directory_input.mkdir()
    with pytest.raises(MergeError) as unsafe_input:
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": directory_input, "ui": ui},
            output=repo / "build" / "output.json",
            platform="macos",
        )
    assert unsafe_input.value.code == "unsafe_input"


def test_cumulative_bounds_and_output_write_failure_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    unit = _report(repo, "unit.json", {SOURCE: _observations()})
    ui = _report(repo, "ui.json", {SOURCE: _observations()})

    monkeypatch.setattr(merger, "MAX_TOTAL_INPUT_BYTES", unit.stat().st_size)
    with pytest.raises(MergeError) as input_budget:
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": unit, "ui": ui},
            output=repo / "build" / "input-budget.json",
            platform="macos",
        )
    assert input_budget.value.code == "input_budget_exceeded"

    monkeypatch.setattr(merger, "MAX_TOTAL_INPUT_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(merger, "MAX_TOTAL_OBSERVATIONS", 1)
    with pytest.raises(MergeError) as observation_budget:
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": unit, "ui": ui},
            output=repo / "build" / "observation-budget.json",
            platform="macos",
        )
    assert observation_budget.value.code == "observation_budget_exceeded"

    monkeypatch.setattr(merger, "MAX_TOTAL_OBSERVATIONS", 2_000_000)
    monkeypatch.setattr(merger, "MAX_OUTPUT_BYTES", 1)
    with pytest.raises(MergeError) as output_budget:
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": unit, "ui": ui},
            output=repo / "build" / "output-budget.json",
            platform="macos",
        )
    assert output_budget.value.code == "output_too_large"

    monkeypatch.setattr(merger, "MAX_OUTPUT_BYTES", 64 * 1024 * 1024)

    def reject_write(_output: Path, _content: bytes) -> None:
        raise ExportError("output_write_failed", "refused")

    monkeypatch.setattr(merger, "_write_new_output", reject_write)
    with pytest.raises(MergeError) as write_failure:
        merge_xccov_reports(
            repo=repo,
            inputs={"unit": unit, "ui": ui},
            output=repo / "build" / "write-failure.json",
            platform="macos",
        )
    assert write_failure.value.code == "output_write_failed"


def test_non_executable_lines_remain_non_executable_in_the_union(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    observations = [
        {"line": 1, "isExecutable": False},
        {"line": 2, "isExecutable": True, "executionCount": 1},
    ]
    unit = _report(repo, "unit.json", {SOURCE: observations})
    ui = _report(repo, "ui.json", {SOURCE: observations})

    merged = merge_xccov_reports(
        repo=repo,
        inputs={"unit": unit, "ui": ui},
        output=repo / "build" / "output.json",
        platform="macos",
    )

    assert merged[SOURCE] == [
        {"line": 1, "isExecutable": False},
        {"line": 2, "isExecutable": True, "executionCount": 2},
    ]


def test_main_reports_success_duplicate_producers_and_merge_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    unit = _report(repo, "unit.json", {SOURCE: _observations()})
    ui = _report(repo, "ui.json", {SOURCE: _observations()})

    def arguments(output: str) -> list[str]:
        return [
            "--repo",
            str(repo),
            "--platform",
            "macos",
            "--unit-input",
            str(unit),
            "--ui-input",
            str(ui),
            "--output",
            str(repo / "build" / output),
        ]

    assert merger.main(arguments("success.json")) == 0
    duplicate = arguments("duplicate.json")
    duplicate[6:6] = ["--unit-input", str(unit)]
    assert merger.main(duplicate) == 2
    assert "exactly one unit" in capsys.readouterr().err

    monkeypatch.setattr(
        merger,
        "merge_xccov_reports",
        lambda **_kwargs: (_ for _ in ()).throw(MergeError("forced", "refused")),
    )
    assert merger.main(arguments("merge-error.json")) == 2
    assert "[forced]" in capsys.readouterr().err

    monkeypatch.setattr(
        merger,
        "merge_xccov_reports",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("hidden")),
    )
    assert merger.main(arguments("filesystem-error.json")) == 2
    assert "[filesystem_error]" in capsys.readouterr().err


@pytest.mark.parametrize(
    "profile,lanes",
    [("ci", ("core", "unit", "ui")), ("release", ("core", "unit", "ui", "staging"))],
)
def test_ios_closed_profiles_keep_actual_counts_and_require_every_lane(
    tmp_path, profile, lanes
):
    repo = _repo(tmp_path)
    core = _core_source(repo)
    inputs = {
        lane: _report(
            repo,
            lane + ".json",
            {core if lane == "core" else SOURCE: _observations(index + 1, 0)},
        )
        for index, lane in enumerate(lanes)
    }
    result = merge_xccov_reports(
        repo=repo,
        platform="ios",
        profile=profile,
        inputs=inputs,
        output=repo / "build/complete.json",
    )
    assert result[SOURCE] == _observations(sum(range(2, len(lanes) + 1)), 0)
    assert result[core] == _observations(1, 0)
    for lane in lanes:
        with pytest.raises(MergeError, match="mandatory"):
            merge_xccov_reports(
                repo=repo,
                platform="ios",
                profile=profile,
                inputs={key: value for key, value in inputs.items() if key != lane},
                output=repo / "build/missing.json",
            )
    with pytest.raises(MergeError, match="mandatory"):
        merge_xccov_reports(
            repo=repo,
            platform="ios",
            profile=profile,
            inputs={**inputs, "optional": inputs["ui"]},
            output=repo / "build/extra.json",
        )


def test_ios_cannot_fall_back_to_old_two_lane_inputs_and_macos_cannot_claim_release(
    tmp_path,
):
    repo = _repo(tmp_path)
    inputs = {
        lane: _report(repo, lane + ".json", {SOURCE: _observations()})
        for lane in ("unit", "ui")
    }
    with pytest.raises(MergeError, match="mandatory"):
        merge_xccov_reports(
            repo=repo, platform="ios", inputs=inputs, output=repo / "build/ios.json"
        )
    with pytest.raises(MergeError, match="unsupported platform coverage profile"):
        merge_xccov_reports(
            repo=repo,
            platform="macos",
            profile="release",
            inputs=inputs,
            output=repo / "build/macos.json",
        )


def test_release_cli_uses_pinned_sibling_under_isolation_and_refuses_missing_staging(
    tmp_path,
):
    repo = _repo(tmp_path)
    marker = tmp_path / "candidate-imported"
    (repo / "scripts").mkdir()
    (repo / "scripts/__init__.py").write_text(f"raise AssertionError({str(marker)!r})")
    (repo / "sitecustomize.py").write_text(
        f"open({str(marker)!r}, 'w').write('unsafe')"
    )
    core = _core_source(repo)
    command = [
        sys.executable,
        "-I",
        str(Path(merger.__file__).resolve()),
        "--repo",
        str(repo),
        "--platform",
        "ios",
        "--profile",
        "release",
        "--output",
        str(repo / "build/union.json"),
    ]
    for lane in ("core", "unit", "ui"):
        path = _report(
            repo,
            lane + ".json",
            {core if lane == "core" else SOURCE: _observations(1, 0)},
        )
        command += [f"--{lane}-input", str(path)]
    result = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    assert result.returncode == 2 and "invalid_producer_set" in result.stderr
    staging = _report(repo, "staging.json", {SOURCE: _observations(0, 1)})
    result = subprocess.run(
        command + ["--staging-input", str(staging)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads((repo / "build/union.json").read_text())[SOURCE] == _observations(
        2, 1
    )
    assert not marker.exists()


def _core_source(repo):
    path = "components/AstralProjection/apple-clients/AstralCore/Sources/AstralCore/Core.swift"
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text("let one = 1\nlet two = 2\n")
    _commit(repo / "components/AstralProjection")
    _commit(repo)
    return path


@pytest.mark.parametrize("wrong_lane", ["core", "unit", "ui", "staging"])
def test_ios_lanes_require_their_real_core_or_app_source_domain(tmp_path, wrong_lane):
    repo = _repo(tmp_path)
    core = _core_source(repo)
    inputs = {}
    for lane in ("core", "unit", "ui", "staging"):
        path = core if (lane == "core") != (lane == wrong_lane) else SOURCE
        inputs[lane] = _report(repo, lane + ".json", {path: _observations()})
    with pytest.raises(MergeError, match="source domain"):
        merge_xccov_reports(
            repo=repo,
            platform="ios",
            profile="release",
            inputs=inputs,
            output=repo / "build/refused.json",
        )


def test_protected_merger_copy_matches_initialized_projection_exact_bytes():
    repo = Path(__file__).resolve().parents[2]
    projection = (
        repo / "components/AstralProjection/scripts/merge_xccov_line_coverage.py"
    )
    if not projection.is_file():
        pytest.skip(
            "component absent in standalone secret-free tooling; protected normalizer enforces byte parity"
        )
    assert Path(merger.__file__).read_bytes() == projection.read_bytes()
    assert Path(merger.__file__).with_name("native_xccov_domain.py").read_bytes() == (
        projection.with_name("native_xccov_domain.py").read_bytes()
    )


def test_isolated_import_uses_only_the_policy_sibling_exporter(tmp_path, monkeypatch):
    """Exercise the production -I fallback while measuring its actual source."""
    import builtins
    import runpy

    original_import = builtins.__import__

    def isolated_import(name, *args, **kwargs):
        if name == "scripts.export_xccov_line_coverage":
            raise ModuleNotFoundError(
                "package deliberately unavailable in isolated execution"
            )
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", isolated_import)
    policy = runpy.run_path(
        str(Path(merger.__file__).resolve()), run_name="_isolated_policy"
    )
    expected = (
        Path(merger.__file__).resolve().with_name("export_xccov_line_coverage.py")
    )
    assert Path(policy["exporter"].__file__).resolve() == expected
    repo = _repo(tmp_path)
    report = policy["merge_xccov_reports"](
        repo=repo,
        platform="macos",
        inputs={
            "unit": _report(repo, "unit.json", {SOURCE: _observations(1, 0)}),
            "ui": _report(repo, "ui.json", {SOURCE: _observations(0, 1)}),
        },
        output=repo / "build/isolated.json",
    )
    assert report[SOURCE] == _observations(1, 1)


def _domain_report(repo, lane, coverage, all_sources):
    from scripts import native_xccov_domain as policy

    geometry = {
        path: {"native_last_line": 2, "geometry_sha256": policy.sha256(path.encode())}
        for path in all_sources
    }
    domain = policy.make_domain(
        geometry=geometry,
        source_bytes=lambda path: (repo / path).read_bytes(),
        binary={
            "member": policy.BINARY_MEMBERS["core" if lane == "core" else "app"],
            "sha256": "a" * 64,
            "artifact_member": f"coverage/native-binaries/apple-ios-{lane}.zip",
            "artifact_sha256": "b" * 64,
        },
        observed_sources=sorted(coverage),
        prefix="components/AstralProjection/",
        lane=lane,
    )
    return policy.native_report(coverage, {lane: domain})


def test_native_domains_preserve_distinct_lane_inventory_and_real_counts(tmp_path):
    repo = _repo(tmp_path)
    core = _core_source(repo)
    (repo / SOURCE).write_text("enum Example {\n let value = 1\n}\n")
    _commit(repo / "components/AstralProjection")
    _commit(repo)
    inputs = {}
    for lane in ("core", "unit", "ui", "staging"):
        paths = [core] if lane == "core" else [core, SOURCE]
        coverage = {core if lane == "core" else SOURCE: _observations(1, 2)}
        inputs[lane] = _report(
            repo, lane + ".json", _domain_report(repo, lane, coverage, paths)
        )
    result = merge_xccov_reports(
        repo=repo,
        platform="ios",
        profile="release",
        inputs=inputs,
        output=repo / "build/native.json",
    )
    assert set(result["domains"]) == {"core", "unit", "ui", "staging"}
    assert result["domains"]["unit"]["observed_sources"] == [SOURCE]
    assert set(result["domains"]["unit"]["sources"]) == {SOURCE, core}
    assert result["coverage"] == {
        core: _observations(1, 2),
        SOURCE: _observations(3, 6),
    }
    assert all(len(rows) == 2 for rows in result["coverage"].values())
    assert result["domains"]["unit"]["sources"][SOURCE]["physical_lines"] == 3


@pytest.mark.parametrize(
    "mutation",
    ["mixed", "lane", "source", "geometry", "truncated", "counter", "platform"],
)
def test_native_merge_refuses_conflicting_or_relabelled_lane_evidence(
    tmp_path, mutation
):
    repo = _repo(tmp_path)
    core = _core_source(repo)
    documents = {
        lane: _domain_report(
            repo,
            lane,
            {core if lane == "core" else SOURCE: _observations()},
            [core] if lane == "core" else [core, SOURCE],
        )
        for lane in ("core", "unit", "ui")
    }
    if mutation == "mixed":
        documents["unit"] = documents["unit"]["coverage"]
    elif mutation == "lane":
        documents["unit"] = documents["ui"]
    elif mutation == "source":
        documents["ui"]["domains"]["ui"]["sources"][SOURCE]["source_sha256"] = "c" * 64
    elif mutation == "geometry":
        documents["ui"]["domains"]["ui"]["sources"][core]["geometry_sha256"] = "c" * 64
    elif mutation == "truncated":
        documents["ui"]["coverage"][SOURCE].pop()
    elif mutation == "counter":
        documents["ui"]["coverage"][SOURCE][0]["executionCount"] = True
    elif mutation == "platform":
        documents["ui"]["platform"] = "macos"
    inputs = {
        lane: _report(repo, lane + ".json", value) for lane, value in documents.items()
    }
    with pytest.raises((MergeError, ExportError)):
        merge_xccov_reports(
            repo=repo, platform="ios", inputs=inputs, output=repo / "build/refused.json"
        )
    assert not (repo / "build/refused.json").exists()


def test_native_domain_cannot_enter_legacy_macos_profile(tmp_path):
    repo = _repo(tmp_path)
    inputs = {
        lane: _report(
            repo,
            lane + ".json",
            _domain_report(repo, lane, {SOURCE: _observations()}, [SOURCE]),
        )
        for lane in ("unit", "ui")
    }
    with pytest.raises(MergeError):
        merge_xccov_reports(
            repo=repo,
            platform="macos",
            inputs=inputs,
            output=repo / "build/refused.json",
        )


@pytest.mark.parametrize("failure", ["policy_missing", "mapped_source_missing"])
def test_native_merge_refuses_missing_policy_or_unreadable_mapped_source(
    tmp_path, monkeypatch, failure
):
    repo = _repo(tmp_path)
    core = _core_source(repo)
    documents = {
        lane: _domain_report(
            repo,
            lane,
            {core if lane == "core" else SOURCE: _observations()},
            [core] if lane == "core" else [core, SOURCE],
        )
        for lane in ("core", "unit", "ui")
    }
    if failure == "policy_missing":
        monkeypatch.setitem(
            merger._native_domain_policy.__globals__,
            "__file__",
            str(tmp_path / "missing-policy/exporter.py"),
        )
    else:
        documents["unit"]["domains"]["unit"]["sources"][
            core.replace("Core.swift", "Absent.swift")
        ] = dict(documents["unit"]["domains"]["unit"]["sources"][core])
    inputs = {
        lane: _report(repo, lane + ".json", value) for lane, value in documents.items()
    }
    with pytest.raises(MergeError):
        merge_xccov_reports(
            repo=repo, platform="ios", inputs=inputs, output=repo / "build/refused.json"
        )
    assert not (repo / "build/refused.json").exists()
