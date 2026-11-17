"""Checks pinned, read-only CI qualification and aggregate owner gates.
The PostgreSQL job binds upgrade, fault effectiveness, and changed-line coverage
evidence to the immutable event range through scripts/check_changed_coverage.py.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT_PATH = REPOSITORY_ROOT / "pyproject.toml"
BUILD_REQUIREMENTS_PATH = REPOSITORY_ROOT / "tooling" / "python-ci" / "build-requirements.lock.txt"

CHECKOUT_ACTION = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
WRONG_CHECKOUT_ACTION = "actions/checkout@1111111111111111111111111111111111111111"
UNAPPROVED_ACTION = "example/unapproved-action@1111111111111111111111111111111111111111"
SETUP_UV_ACTION = "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9"
UPLOAD_ARTIFACT_ACTION = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
SETUP_UV_VERSION = "0.11.26"
POSTGRES_IMAGE = (
    "postgres:17-alpine@sha256:dc17045ccfd343b49600570ea734b9c4991cf1c3f3302e67df51e3b402dd55c4"
)
DEEP_COMMIT = "fc113c4f99121b2053bb71523835c5c4743f1f56"
BASE_SHA_ENVIRONMENT = (
    "BASE_SHA: ${{ github.event.pull_request.base.sha || "
    "github.event.merge_group.base_sha || github.event.before }}"
)
BASE_SHA_GUARD = (
    'if [[ ! "$BASE_SHA" =~ ^[0-9a-f]{40}$ || '
    '"$BASE_SHA" == 0000000000000000000000000000000000000000 ]]; then '
    'echo "::error::Changed-line coverage needs a non-zero 40-hex BASE_SHA from the event." '
    "exit 1 fi"
)
MEASURE_CHANGED_LINES = (
    'uv run --frozen --group ci diff-cover coverage.xml --compare-branch "$BASE_SHA" '
    "--diff-range-notation '..' --ignore-staged --ignore-unstaged "
    "--format json:changed-coverage.json"
)
DECIDE_CHANGED_LINES = (
    "uv run --frozen --group ci python scripts/check_changed_coverage.py "
    '--report changed-coverage.json --base-sha "$BASE_SHA" --fail-under 90'
)
OWNER_JOBS = ("quality", "postgresql", "package-compatibility")
SETUPTOOLS_REQUIREMENT = "setuptools==83.0.0"
SETUPTOOLS_HASH = "sha256:29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3"


def _workflow_text() -> str:
    assert WORKFLOW_PATH.is_file(), f"active owner workflow is missing: {WORKFLOW_PATH}"
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _mapping_block(text: str, key: str, *, indent: int = 0) -> str:
    lines = text.splitlines()
    marker = f"{' ' * indent}{key}:"
    try:
        start = lines.index(marker)
    except ValueError:
        raise AssertionError(f"missing {key!r} mapping at indentation {indent}") from None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            end = index
            break
    return "\n".join(lines[start + 1 : end]).rstrip()


def _job_blocks(text: str) -> dict[str, str]:
    jobs = _mapping_block(text, "jobs")
    lines = jobs.splitlines()
    starts = [
        (index, match.group(1))
        for index, line in enumerate(lines)
        if (match := re.fullmatch(r"  ([a-z][a-z0-9-]*):", line))
    ]
    blocks: dict[str, str] = {}
    for position, (start, job_id) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        blocks[job_id] = "\n".join(lines[start + 1 : end])
    return blocks


def _run_commands(job: str) -> tuple[str, ...]:
    commands: list[str] = []
    lines = job.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^\s+(?:-\s+)?run:\s*(.*)$", line)
        if match is None:
            continue
        inline = match.group(1).strip()
        if inline and inline not in {"|", ">"}:
            commands.append(inline)
            continue
        indentation = len(line) - len(line.lstrip())
        body: list[str] = []
        for following in lines[index + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indentation:
                break
            body.append(following.strip())
        commands.append("\n".join(body).strip())
    return tuple(commands)


def _workflow_actions(text: str) -> tuple[str, ...]:
    document = yaml.compose(text)
    assert document is not None, "workflow YAML document is empty"
    actions: list[str] = []

    def visit(node: Node) -> None:
        if isinstance(node, MappingNode):
            for key_node, value_node in node.value:
                if isinstance(key_node, ScalarNode) and key_node.value == "uses":
                    assert isinstance(value_node, ScalarNode), (
                        "workflow action reference must be a scalar"
                    )
                    actions.append(value_node.value)
                visit(key_node)
                visit(value_node)
        elif isinstance(node, SequenceNode):
            for item_node in node.value:
                visit(item_node)

    visit(document)
    return tuple(actions)


def test_owner_workflow_is_active_pinned_and_read_only() -> None:
    text = _workflow_text()
    jobs = _job_blocks(text)

    assert tuple(jobs) == (*OWNER_JOBS, "gates")
    assert _mapping_block(text, "permissions") == "  contents: read"
    assert all("runs-on: ubuntu-24.04" in job for job in jobs.values())
    assert not re.search(r"^\s*if:\s*.*\bfalse\b", text, flags=re.MULTILINE)
    assert "continue-on-error:" not in text
    assert "components/AstralPlane" not in text
    assert text.count("astral-sh/setup-uv@") == len(OWNER_JOBS)

    uses = _workflow_actions(text)
    assert uses
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action) for action in uses)
    assert Counter(uses) == Counter(
        {
            CHECKOUT_ACTION: 4,
            SETUP_UV_ACTION: len(OWNER_JOBS),
            UPLOAD_ARTIFACT_ACTION: 2,
        }
    )
    setup_uv_uses = [action for action in uses if action.startswith("astral-sh/setup-uv@")]
    assert setup_uv_uses == [SETUP_UV_ACTION] * len(OWNER_JOBS)

    for owner_job in OWNER_JOBS:
        setup_uv_steps = re.findall(
            rf"(?m)^      - uses: {re.escape(SETUP_UV_ACTION)}[^\n]*\n"
            r"((?:^        .*\n?)*)",
            jobs[owner_job],
        )
        assert len(setup_uv_steps) == 1
        assert f'          version: "{SETUP_UV_VERSION}"' in setup_uv_steps[0]


def test_owner_workflow_rejects_setup_uv_outside_expected_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _workflow_text()
    gates_step = "    steps:\n      - name: Require every owner gate"
    mutated = text.replace(
        gates_step,
        "    steps:\n"
        f"      - uses: {SETUP_UV_ACTION} # v9.0.0\n"
        "      - name: Require every owner gate",
        1,
    )
    assert mutated != text
    mutated_path = tmp_path / "ci.yml"
    mutated_path.write_text(mutated, encoding="utf-8")
    monkeypatch.setitem(globals(), "WORKFLOW_PATH", mutated_path)

    with pytest.raises(AssertionError):
        test_owner_workflow_is_active_pinned_and_read_only()


def test_owner_workflow_rejects_split_line_setup_uv_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _workflow_text()
    gates_step = "    steps:\n      - name: Require every owner gate"
    mutated = text.replace(
        gates_step,
        "    steps:\n"
        "      -\n"
        f"        uses: {SETUP_UV_ACTION}\n"
        "      - name: Require every owner gate",
        1,
    )
    assert mutated != text
    mutated_path = tmp_path / "ci.yml"
    mutated_path.write_text(mutated, encoding="utf-8")
    monkeypatch.setitem(globals(), "WORKFLOW_PATH", mutated_path)

    with pytest.raises(AssertionError):
        test_owner_workflow_is_active_pinned_and_read_only()


def test_owner_workflow_rejects_wrong_checkout_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _workflow_text()
    mutated = text.replace(CHECKOUT_ACTION, WRONG_CHECKOUT_ACTION, 1)
    assert mutated != text
    assert mutated.count(WRONG_CHECKOUT_ACTION) == 1
    mutated_path = tmp_path / "ci.yml"
    mutated_path.write_text(mutated, encoding="utf-8")
    monkeypatch.setitem(globals(), "WORKFLOW_PATH", mutated_path)

    with pytest.raises(AssertionError):
        test_owner_workflow_is_active_pinned_and_read_only()


def test_owner_workflow_rejects_split_line_unapproved_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _workflow_text()
    gates_step = "    steps:\n      - name: Require every owner gate"
    mutated = text.replace(
        gates_step,
        "    steps:\n"
        "      -\n"
        f"        uses: {UNAPPROVED_ACTION}\n"
        "      - name: Require every owner gate",
        1,
    )
    assert mutated != text
    mutated_path = tmp_path / "ci.yml"
    mutated_path.write_text(mutated, encoding="utf-8")
    monkeypatch.setitem(globals(), "WORKFLOW_PATH", mutated_path)

    with pytest.raises(AssertionError):
        test_owner_workflow_is_active_pinned_and_read_only()


@pytest.mark.parametrize(
    "injected_step",
    (
        f"      -\n        uses : {UNAPPROVED_ACTION}\n",
        f'      - "uses": {UNAPPROVED_ACTION}\n',
        f"      - {{uses: {UNAPPROVED_ACTION}}}\n",
    ),
    ids=("split-line-spaced-colon", "quoted-key", "flow-mapping"),
)
def test_owner_workflow_rejects_unapproved_action_yaml_forms(
    injected_step: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _workflow_text()
    gates_step = "    steps:\n      - name: Require every owner gate"
    mutated = text.replace(
        gates_step,
        f"    steps:\n{injected_step}      - name: Require every owner gate",
        1,
    )
    assert mutated != text
    mutated_path = tmp_path / "ci.yml"
    mutated_path.write_text(mutated, encoding="utf-8")
    monkeypatch.setitem(globals(), "WORKFLOW_PATH", mutated_path)

    with pytest.raises(AssertionError):
        test_owner_workflow_is_active_pinned_and_read_only()


def test_workflow_action_inventory_rejects_non_scalar_values() -> None:
    with pytest.raises(AssertionError):
        _workflow_actions(f"steps: [{{uses: [{UNAPPROVED_ACTION}]}}]")


def test_workflow_action_inventory_preserves_duplicate_uses_keys() -> None:
    text = f"step:\n  uses: {CHECKOUT_ACTION}\n  uses: {SETUP_UV_ACTION}\n"

    assert _workflow_actions(text) == (CHECKOUT_ACTION, SETUP_UV_ACTION)


def test_quality_job_runs_locked_source_and_architecture_gates() -> None:
    job = _job_blocks(_workflow_text())["quality"]
    commands = _run_commands(job)

    assert "fetch-depth: 0" in job
    assert "uv lock --check" in commands
    assert "uv sync --frozen --group ci" in commands
    assert "uv run --frozen --group ci ruff check ." in commands
    assert (
        "uv run --frozen --group ci python tests/architecture/test_dependency_direction.py"
        in commands
    )


def test_postgresql_job_runs_the_complete_suite_with_real_database_and_coverage() -> None:
    job = _job_blocks(_workflow_text())["postgresql"]
    commands = _run_commands(job)

    assert f"image: {POSTGRES_IMAGE}" in job
    assert re.search(
        r"ASTRALPLANE_TEST_POSTGRES_DSN:\s*"
        r"postgresql://astralplane:astralplane_ci@127\.0\.0\.1:5432/astralplane",
        job,
    )
    assert "repository: AstralDeep/AstralDeep" in job
    assert f"ref: {DEEP_COMMIT}" in job
    assert "path: source-deep" in job
    assert "ASTRALDEEP_SOURCE_REPO: ${{ github.workspace }}/source-deep" in job

    pytest_commands = [command for command in commands if re.search(r"\bpytest\b", command)]
    assert len(pytest_commands) == 1
    pytest_command = pytest_commands[0]
    assert "pytest -q -p no:cacheprovider" in pytest_command
    assert "--cov=astralplane" in pytest_command
    assert "--cov=examples" in pytest_command
    assert "--cov=scripts.check_changed_coverage" in pytest_command
    assert "--cov=scripts.qualify_main_upgrade" in pytest_command
    assert "--cov=scripts.check_acceptance_faults" in pytest_command
    assert "--cov=scripts.ci_review_diagnostics" in pytest_command
    assert "--cov-branch" in pytest_command
    assert "--cov-report=xml" in pytest_command
    assert "--cov-fail-under=88.75" in pytest_command
    assert "tests/" not in pytest_command
    assert not re.search(r"--ignore(?:=|\s)", pytest_command)
    upgrades = [command for command in commands if "scripts/qualify_main_upgrade.py" in command]
    assert len(upgrades) == 1 and commands.index(upgrades[0]) < commands.index(pytest_command)
    assert "git fetch --no-tags origin main" in upgrades[0]
    assert "git rev-parse refs/remotes/origin/main" in upgrades[0]
    assert '--baseline-sha "$MAIN_SHA" --report main-upgrade.json' in upgrades[0]
    assert "ASTRALPLANE_FAULT_REPORT: acceptance-faults.json" in job
    assert any(
        "acceptance-faults.json" in command and "all(f['status']=='killed'" in command
        for command in commands
    )

    assert "fetch-depth: 0" in job
    assert BASE_SHA_ENVIRONMENT in job
    decisions = [command for command in commands if "diff-cover" in command]
    assert len(decisions) == 1
    assert commands.index(pytest_command) < commands.index(decisions[0])
    decision = " ".join(decisions[0].replace("\\\n", " ").split())
    assert decision.startswith("set -euo pipefail ")
    assert BASE_SHA_GUARD in decision
    assert MEASURE_CHANGED_LINES in decision
    assert DECIDE_CHANGED_LINES in decision
    assert (
        decision.index(BASE_SHA_GUARD)
        < decision.index(MEASURE_CHANGED_LINES)
        < decision.index(DECIDE_CHANGED_LINES)
    )
    assert decision.count("diff-cover ") == 1
    assert "--fail-under=" not in decision


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        ("--fail-under 90", "--fail-under 80"),
        ("--diff-range-notation '..' ", ""),
        ("--ignore-staged --ignore-unstaged ", ""),
        ("--cov=scripts.check_changed_coverage ", ""),
        ("--cov=examples ", ""),
        ("github.event.before", "github.sha"),
        ("github.event.pull_request.base.sha || ", ""),
        ('|| "$BASE_SHA" == 0000000000000000000000000000000000000000 ', ""),
        ("            exit 1\n", ""),
        ("fetch-depth: 0", "fetch-depth: 1"),
        ("--format json:changed-coverage.json", "--fail-under=90"),
    ),
    ids=(
        "lowered-threshold",
        "three-dot-range",
        "working-tree-diff",
        "unmeasured-decision-script",
        "unmeasured-executable-examples",
        "self-comparison-on-push",
        "no-pull-request-base",
        "zero-base-accepted",
        "guard-does-not-stop",
        "shallow-history",
        "diff-cover-decides",
    ),
)
def test_postgresql_job_rejects_a_weakened_changed_line_decision(
    original: str,
    replacement: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _workflow_text()
    mutated = text.replace(original, replacement)
    assert mutated != text
    mutated_path = tmp_path / "ci.yml"
    mutated_path.write_text(mutated, encoding="utf-8")
    monkeypatch.setitem(globals(), "WORKFLOW_PATH", mutated_path)

    with pytest.raises(AssertionError):
        test_postgresql_job_runs_the_complete_suite_with_real_database_and_coverage()


def test_package_compatibility_builds_and_smokes_a_clean_wheel() -> None:
    job = _job_blocks(_workflow_text())["package-compatibility"]
    commands = _run_commands(job)

    assert re.search(r"python-version:\s*\[\"3\.11\", \"3\.14\"\]", job)
    assert "uv lock --check" in commands
    assert (
        "uv build --build-constraints tooling/python-ci/build-requirements.lock.txt "
        "--require-hashes" in commands
    )
    assert not any(command.startswith("uv build --frozen") for command in commands)
    assert any("uv venv" in command and "--seed" in command for command in commands)
    assert any("pip install --no-deps dist/*.whl" in command for command in commands)
    assert any(
        "import astralplane; assert astralplane.CONTRACT_VERSION == "
        "'astralplane.contract/v1'" in command
        for command in commands
    )
    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
    assert f'requires = ["{SETUPTOOLS_REQUIREMENT}"]' in pyproject

    assert BUILD_REQUIREMENTS_PATH.is_file(), (
        f"hash-locked build constraint is missing: {BUILD_REQUIREMENTS_PATH}"
    )
    assert BUILD_REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines() == [
        f"{SETUPTOOLS_REQUIREMENT} --hash={SETUPTOOLS_HASH}"
    ]


def test_aggregate_fails_closed_over_every_owner_job() -> None:
    job = _job_blocks(_workflow_text())["gates"]

    assert "if: always()" in job
    assert "needs: [quality, postgresql, package-compatibility]" in job
    for owner_job in OWNER_JOBS:
        result_expression = (
            f"needs.{owner_job}.result" if "-" not in owner_job else f"needs['{owner_job}'].result"
        )
        assert result_expression in job
        assert re.search(
            rf"{re.escape(result_expression)}[^\n]+== ['\"]success['\"]",
            job,
        )
