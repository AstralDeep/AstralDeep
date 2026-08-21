"""Fail-closed tests for the default-branch evidence-bootstrap verifier."""

from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify_release_evidence_bootstrap.py"
if not SCRIPT.is_file():  # repo-root tooling is intentionally absent in the image
    pytest.skip("bootstrap verifier is not part of the product image", allow_module_level=True)

SPEC = importlib.util.spec_from_file_location("release_evidence_bootstrap", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)

CANDIDATE = "c" * 40
BASE = "b" * 40
PREVIOUS = "a" * 40
NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
CREATED = "2026-08-02T11:00:00Z"
EXPIRES = "2026-08-09T10:59:59Z"
REPOSITORY = "AstralDeep/AstralDeep"
BRANCH = "065-conversational-voice"


def _state(**updates: Any) -> Any:
    values: dict[str, Any] = {
        "repository": REPOSITORY,
        "default_branch": "main",
        "default_sha": BASE,
        "branch": BRANCH,
        "previous_head": PREVIOUS,
        "candidate_sha": CANDIDATE,
        "changed_paths": ("one.txt", "two.txt"),
        "provider_now": NOW,
        "workflow_files": {
            ".github/workflows/ci.yml": "3" * 64,
        },
        "pull": _pull(),
    }
    values.update(updates)
    return bootstrap.ProviderState(**values)


def _pull(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "state": "open",
        "draft": True,
        "head": {
            "ref": BRANCH,
            "sha": PREVIOUS,
            "repo": {"full_name": REPOSITORY},
        },
        "base": {
            "ref": "main",
            "sha": BASE,
            "repo": {"full_name": REPOSITORY},
        },
    }
    value.update(updates)
    return value


def _args(repo: Path, **updates: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "repo": str(repo),
        "github_repository": REPOSITORY,
        "pr_number": 151,
        "candidate_sha": CANDIDATE,
        "feature": "065-conversational-voice",
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _prepare_local_inputs(
    repo: Path, monkeypatch: pytest.MonkeyPatch, host: str = "Darwin"
) -> None:
    monkeypatch.setattr(bootstrap.platform, "system", lambda: host)
    for name in bootstrap.LOCAL_COVERAGE_BY_HOST[host]:
        path = repo / bootstrap.EVIDENCE_INPUTS[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_coverage_payload(name), encoding="utf-8")


def _coverage_payload(name: str) -> str:
    if bootstrap.EVIDENCE_INPUTS[name].endswith(".xml"):
        return (
            '<coverage lines-valid="1" lines-covered="1">'
            '<packages><line number="1" hits="1"/></packages></coverage>'
        )
    return json.dumps({"coverage": {name: 1}}, sort_keys=True)


def _local_coverage_digests() -> dict[str, str]:
    return {
        name: bootstrap._bytes_sha256(_coverage_payload(name))
        for name in bootstrap.LOCAL_COVERAGE_BY_HOST["Darwin"]
    }


def _failed_evidence(stderr: str = "provider evidence is missing") -> Any:
    return bootstrap.EvidenceRun(
        returncode=2,
        stdout="diagnostic output",
        stderr=stderr,
        failure={
            "document_type": "release_evidence_local_failure",
            "schema_version": 1,
            "error_code": "missing_provider_inputs",
            "error_message_sha256": bootstrap._bytes_sha256(stderr),
            "base_sha": BASE,
            "candidate_sha": CANDIDATE,
            "protected_release_authorization": False,
        },
    )


def _install_inventory_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: Any | None = None,
    evidence: Any | None = None,
) -> None:
    monkeypatch.setattr(bootstrap, "_ensure_clean", lambda *_: None)
    monkeypatch.setattr(bootstrap, "_provider_state", lambda *_args, **_kwargs: state or _state())
    monkeypatch.setattr(bootstrap, "_run_evidence", lambda *_: evidence or _failed_evidence())


def _build_inventory(
    repo: Path, monkeypatch: pytest.MonkeyPatch, **updates: Any
) -> dict[str, Any]:
    _prepare_local_inputs(repo, monkeypatch)
    _install_inventory_fakes(monkeypatch)
    return bootstrap.build_inventory(_args(repo, **updates))


def _approval(inventory_sha256: str, **updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "document_type": "release_evidence_bootstrap_approval",
        "repository": REPOSITORY,
        "pr_number": 151,
        "feature": "065-conversational-voice",
        "branch": BRANCH,
        "base_branch": "main",
        "base_sha": BASE,
        "previous_head": PREVIOUS,
        "candidate_sha": CANDIDATE,
        "approved_paths": ["one.txt", "two.txt"],
        "provider_bound_missing_inputs": sorted(
            set(bootstrap.EVIDENCE_INPUTS)
            - bootstrap.LOCAL_COVERAGE_BY_HOST["Darwin"]
        ),
        "inventory_sha256": inventory_sha256,
        "policy_commit": BASE,
        "local_gate_attestation": {
            "status": "passed",
            "candidate_sha": CANDIDATE,
            "commands": [
                "ruff check .",
                "platform coverage suites and changed-code gate",
            ],
            "evidence_input_sha256": _local_coverage_digests(),
        },
        "structural_blocker": "Provider evidence requires the remote exact SHA.",
        "purpose": "Run diagnostic CI without merge or release authority.",
        "expires_at": EXPIRES,
    }
    value.update(updates)
    return value


def _comment(approval: dict[str, Any], **updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": 99,
        "html_url": "https://github.test/pull/151#issuecomment-99",
        "body": (
            "Lead approval.\n<!-- astraldeep-release-evidence-bootstrap-v1\n"
            + json.dumps(approval, sort_keys=True)
            + "\n-->"
        ),
        "user": {"login": "armstrongsam25"},
        "created_at": CREATED,
        "updated_at": CREATED,
    }
    value.update(updates)
    return value


def _write_inventory(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, Any]]:
    inventory = _build_inventory(repo, monkeypatch)
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inventory, sort_keys=True), encoding="utf-8")
    return path, inventory


def _install_verify_fakes(
    monkeypatch: pytest.MonkeyPatch,
    inventory_path: Path,
    *,
    approval_updates: dict[str, Any] | None = None,
    comment_updates: dict[str, Any] | None = None,
    provider_now: datetime = NOW,
) -> None:
    monkeypatch.setattr(bootstrap, "_ensure_clean", lambda *_: None)
    monkeypatch.setattr(bootstrap, "_provider_state", lambda *_args, **_kwargs: _state())
    monkeypatch.setattr(bootstrap, "_run_evidence", lambda *_: _failed_evidence())
    digest = bootstrap._sha256(inventory_path)
    approval = _approval(digest)
    approval.update(approval_updates or {})
    comment = _comment(approval, **(comment_updates or {}))
    monkeypatch.setattr(
        bootstrap,
        "_all_comments",
        lambda *_: ([comment], provider_now),
    )


def test_inventory_executes_fixed_parser_and_binds_all_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    result = _build_inventory(repo, monkeypatch)

    assert set(result) == bootstrap.INVENTORY_KEYS
    assert result["status"] == "bootstrap_missing_inputs"
    assert result["protected_release_authorization"] is False
    assert result["evidence_command"] == {
        "argv": [
            "python3",
            "scripts/prepare_release_evidence.py",
            "--evidence-dir",
            "build/060/release-evidence",
            "--coverage-dir",
            "build/060/coverage",
            "--base-sha",
            BASE,
            "--candidate-sha",
            CANDIDATE,
            "--failure-output",
            "<external-bootstrap-failure>",
        ],
        "policy_source": "current provider default branch",
    }
    assert result["evidence_command_exit_code"] == 2
    assert result["provider_bound_missing_inputs"] == sorted(
        set(bootstrap.EVIDENCE_INPUTS)
        - bootstrap.LOCAL_COVERAGE_BY_HOST["Darwin"]
    )
    assert result["inputs"]["backend_python"]["classification"] == "locally_required"
    assert result["inputs"]["backend_evidence"] == {
        "path": bootstrap.EVIDENCE_INPUTS["backend_evidence"],
        "classification": "provider_bound",
        "present": False,
    }


def test_inventory_rejects_passing_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    _prepare_local_inputs(repo, monkeypatch)
    _install_inventory_fakes(
        monkeypatch,
        evidence=bootstrap.EvidenceRun(0, "passed", "", None),
    )
    with pytest.raises(bootstrap.BootstrapError, match="handled evidence-policy"):
        bootstrap.build_inventory(_args(repo))


@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        (bootstrap.EvidenceRun(1, "", "traceback", None), "handled evidence-policy"),
        (
            bootstrap.EvidenceRun(
                2,
                "",
                "ordinary policy failure",
                {
                    "document_type": "release_evidence_local_failure",
                    "schema_version": 1,
                    "error_code": "release_evidence_rejected",
                    "error_message_sha256": "d" * 64,
                    "base_sha": BASE,
                    "candidate_sha": CANDIDATE,
                    "protected_release_authorization": False,
                },
            ),
            "not bootstrap-eligible",
        ),
    ],
)
def test_inventory_rejects_crash_and_ordinary_policy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence: Any,
    message: str,
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    _prepare_local_inputs(repo, monkeypatch)
    _install_inventory_fakes(monkeypatch, evidence=evidence)
    with pytest.raises(bootstrap.BootstrapError, match=message):
        bootstrap.build_inventory(_args(repo))


def test_input_snapshot_rejects_local_gap_and_stale_provider_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Darwin")
    with pytest.raises(bootstrap.BootstrapError, match="locally required"):
        bootstrap._snapshot_inputs(repo)

    _prepare_local_inputs(repo, monkeypatch)
    malformed = repo / bootstrap.EVIDENCE_INPUTS["backend_python"]
    malformed.write_text("not coverage", encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="parseable report"):
        bootstrap._snapshot_inputs(repo)
    malformed.write_text(_coverage_payload("backend_python"), encoding="utf-8")

    stale = repo / bootstrap.EVIDENCE_INPUTS["backend_evidence"]
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("old candidate", encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="every provider-bound"):
        bootstrap._snapshot_inputs(repo)


def test_input_classification_rejects_unknown_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Plan9")
    with pytest.raises(bootstrap.BootstrapError, match="unsupported"):
        bootstrap._input_classification()


def _provider_json_fake(endpoint: str, **updates: Any) -> Any:
    if endpoint == f"repos/{REPOSITORY}":
        value: Any = {"full_name": REPOSITORY, "default_branch": "main"}
    elif "/git/ref/heads/" in endpoint:
        value = {"object": {"sha": BASE}}
    elif "/pulls/" in endpoint:
        value = _pull()
    elif "/branches/" in endpoint:
        value = {"protected": False}
    else:
        raise AssertionError(endpoint)
    if isinstance(value, dict):
        value.update(updates.get(endpoint, {}))
    return value, NOW


def _install_provider_state_fakes(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    policy: Path,
    *,
    pull: dict[str, Any] | None = None,
    protected: bool = False,
    ancestors: list[bool] | None = None,
) -> None:
    monkeypatch.setattr(bootstrap, "_origin_repository", lambda *_: REPOSITORY)

    def provider(_root: Path, endpoint: str) -> tuple[Any, datetime]:
        if "/pulls/" in endpoint:
            return pull or _pull(), NOW
        if "/branches/" in endpoint:
            return {"protected": protected}, NOW
        return _provider_json_fake(endpoint)

    monkeypatch.setattr(bootstrap, "_provider_json", provider)

    def git(root: Path, *args: str) -> str:
        if args[:2] == ("rev-parse", "HEAD"):
            return BASE if root == policy else CANDIDATE
        if args[:2] == ("rev-parse", "origin/main"):
            return BASE
        if args[:2] == ("branch", "--show-current"):
            return BRANCH
        if args and args[0] == "diff":
            return "two.txt\none.txt\n"
        return ""

    monkeypatch.setattr(bootstrap, "_git", git)
    monkeypatch.setattr(bootstrap, "_ensure_clean", lambda *_: None)
    values = iter(ancestors or [True, True])
    monkeypatch.setattr(bootstrap, "_is_ancestor", lambda *_: next(values))
    monkeypatch.setattr(
        bootstrap,
        "_validate_bootstrap_workflows",
        lambda *_, **__: _state().workflow_files,
    )


def test_provider_state_binds_repo_default_pr_branch_and_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    policy = tmp_path / "policy"
    repo.mkdir()
    policy.mkdir()
    _install_provider_state_fakes(monkeypatch, repo, policy)
    state = bootstrap._provider_state(
        repo,
        policy,
        github_repository=REPOSITORY,
        pr_number=151,
        candidate_sha=CANDIDATE,
    )
    assert state.default_sha == BASE
    assert state.previous_head == PREVIOUS
    assert state.changed_paths == ("one.txt", "two.txt")


@pytest.mark.parametrize(
    ("pull", "protected", "ancestors", "message"),
    [
        (_pull(draft=False), False, None, "open and draft"),
        (_pull(state="closed"), False, None, "open and draft"),
        (
            _pull(head={"ref": BRANCH, "sha": PREVIOUS, "repo": {"full_name": "Other/Repo"}}),
            False,
            None,
            "same-repository",
        ),
        (_pull(base={"ref": "other", "sha": BASE, "repo": {"full_name": REPOSITORY}}), False, None, "current provider default"),
        (_pull(), True, None, "non-protected"),
        (_pull(), False, [False], "not an ancestor"),
        (_pull(), False, [True, False], "fast-forward"),
    ],
)
def test_provider_state_rejects_unsafe_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pull: dict[str, Any],
    protected: bool,
    ancestors: list[bool] | None,
    message: str,
) -> None:
    repo = tmp_path / "repo"
    policy = tmp_path / "policy"
    repo.mkdir()
    policy.mkdir()
    _install_provider_state_fakes(
        monkeypatch,
        repo,
        policy,
        pull=pull,
        protected=protected,
        ancestors=ancestors,
    )
    with pytest.raises(bootstrap.BootstrapError, match=message):
        bootstrap._provider_state(
            repo,
            policy,
            github_repository=REPOSITORY,
            pr_number=151,
            candidate_sha=CANDIDATE,
        )


def test_provider_state_rejects_repo_branch_and_sha_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    policy = tmp_path / "policy"
    repo.mkdir()
    policy.mkdir()
    monkeypatch.setattr(bootstrap, "_origin_repository", lambda *_: "Other/Repo")
    with pytest.raises(bootstrap.BootstrapError, match="repository differ"):
        bootstrap._provider_state(
            repo,
            policy,
            github_repository=REPOSITORY,
            pr_number=151,
            candidate_sha=CANDIDATE,
        )

    _install_provider_state_fakes(monkeypatch, repo, policy)
    original_git = bootstrap._git
    monkeypatch.setattr(
        bootstrap,
        "_git",
        lambda root, *args: "other" if args[:2] == ("branch", "--show-current") else original_git(root, *args),
    )
    with pytest.raises(bootstrap.BootstrapError, match="checkout branch"):
        bootstrap._provider_state(
            repo,
            policy,
            github_repository=REPOSITORY,
            pr_number=151,
            candidate_sha=CANDIDATE,
        )


def _workflow(*, dangerous: str = "", event: str = "pull_request") -> str:
    return f"""name: test
on:
  {event}:
permissions:
  contents: read
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - run: echo safe
{dangerous}"""


def _write_required_workflows(root: Path, text: str) -> None:
    for relative in bootstrap.REQUIRED_PR_WORKFLOWS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_workflow_policy_accepts_safe_jobs_and_guarded_privilege(tmp_path: Path) -> None:
    guarded = """  publish:
    if: ${{ github.event_name == 'push' && github.ref == 'refs/heads/main' }}
    permissions:
      packages: write
    secrets: inherit
"""
    _write_required_workflows(tmp_path, _workflow(dangerous=guarded))
    result = bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)
    assert set(result) == bootstrap.REQUIRED_PR_WORKFLOWS
    assert all(bootstrap.DIGEST_RE.fullmatch(value) for value in result.values())


def test_repository_pull_request_workflows_are_draft_bootstrap_safe() -> None:
    assert bootstrap.REQUIRED_PR_WORKFLOWS == {".github/workflows/ci.yml"}
    result = bootstrap._validate_bootstrap_workflows(REPO_ROOT, BRANCH)
    assert bootstrap.REQUIRED_PR_WORKFLOWS.issubset(result)


def test_workflow_policy_accepts_exact_draft_exclusion(tmp_path: Path) -> None:
    guarded = """  readiness:
    if: ${{ vars.ACTIVE == 'true' && (github.event_name != 'pull_request' || github.event.pull_request.draft == false) }}
    permissions:
      id-token: write
    secrets: inherit
"""
    _write_required_workflows(tmp_path, _workflow(dangerous=guarded))
    bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (_workflow(dangerous="""  unsafe:
    runs-on: ubuntu-latest
    env:
      TOKEN: ${{ secrets.KEY }}
"""), "privileged job"),
        (_workflow(event="pull_request_target"), "pull_request_target"),
        (_workflow().replace("contents: read", "contents: write"), "global secret/write"),
        (_workflow(dangerous="""  unsafe:
    runs-on: [self-hosted, linux]
"""), "provider runner"),
        (_workflow().replace("contents: read", "contents: &grant read"), "anchors"),
    ],
)
def test_workflow_policy_rejects_draft_authority(
    tmp_path: Path, text: str, message: str
) -> None:
    _write_required_workflows(tmp_path, text)
    with pytest.raises(bootstrap.BootstrapError, match=message):
            bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


def test_workflow_policy_requires_the_deep_pull_request_workflow(tmp_path: Path) -> None:
    path = tmp_path / ".github" / "workflows" / "other.yml"
    path.parent.mkdir(parents=True)
    path.write_text(_workflow(), encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="required pull-request"):
        bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


def test_workflow_policy_rejects_symlinked_workflow_files(tmp_path: Path) -> None:
    target = tmp_path / "safe.yml"
    target.write_text(_workflow(), encoding="utf-8")
    for relative in bootstrap.REQUIRED_PR_WORKFLOWS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
    with pytest.raises(bootstrap.BootstrapError, match="regular file"):
        bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


@pytest.mark.parametrize(
    ("dangerous", "message"),
    [
        (
            """  unsafe:
    if: ${{ ! (github.event_name != 'pull_request' || github.event.pull_request.draft == false) }}
    runs-on: ubuntu-latest
    permissions:
      id-token: write
""",
            "privileged job",
        ),
        (
            """  unsafe:
    runs-on: ubuntu-latest
    env:
      ALL_SECRETS: ${{ toJSON(secrets) }}
""",
            "privileged job",
        ),
        (
            """  "unsafe":
    runs-on: production-runner
    permissions:
      id-token: write
""",
            "provider runner",
        ),
    ],
)
def test_workflow_policy_rejects_adversarial_guard_secret_and_runner_forms(
    tmp_path: Path, dangerous: str, message: str
) -> None:
    _write_required_workflows(tmp_path, _workflow(dangerous=dangerous))
    with pytest.raises(bootstrap.BootstrapError, match=message):
        bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            _workflow().replace("permissions:\n  contents: read\n", ""),
            "explicit global permissions",
        ),
        (
            _workflow().replace(
                "permissions:\n  contents: read", "permissions: {contents: read}"
            ),
            "global secret/write",
        ),
        (
            _workflow().replace("contents: read", "contents : write"),
            "global secret/write",
        ),
        (
            _workflow(dangerous="""  unsafe:
    runs-on: ubuntu-latest
    env:
      TOKEN: >-
        ${{ format(
          '{0}',
          secrets.KEY
        ) }}
"""),
            "privileged job",
        ),
    ],
)
def test_workflow_policy_rejects_inherited_or_alternate_authority_syntax(
    tmp_path: Path, text: str, message: str
) -> None:
    _write_required_workflows(tmp_path, text)
    with pytest.raises(bootstrap.BootstrapError, match=message):
        bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


def test_workflow_policy_scans_alternate_valid_event_indentation(
    tmp_path: Path,
) -> None:
    _write_required_workflows(tmp_path, _workflow())
    extra = tmp_path / ".github" / "workflows" / "alternate-indent.yml"
    extra.write_text(
        """name: unsafe
on:
    pull_request:
permissions: {contents: read, id-token: write}
jobs:
  unsafe:
    runs-on: production-runner
    steps:
      - run: echo unsafe
""",
        encoding="utf-8",
    )
    with pytest.raises(bootstrap.BootstrapError, match="global secret/write"):
        bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


def test_workflow_policy_rejects_commented_feature_branch_filter(
    tmp_path: Path,
) -> None:
    _write_required_workflows(tmp_path, _workflow())
    push = tmp_path / ".github" / "workflows" / "feature-push.yml"
    push.write_text(
        _workflow(event="push").replace(
            "  push:\n", f"  push:\n    branches: {BRANCH} # feature\n"
        ),
        encoding="utf-8",
    )
    with pytest.raises(bootstrap.BootstrapError, match="inline comments"):
        bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


def test_workflow_policy_rejects_yaml_encoded_secret_reference(tmp_path: Path) -> None:
    encoded = _workflow().replace(
        "      - run: echo safe",
        r'''      - env:
          TOKEN: "${{ \x73ecrets.REPO_TOKEN }}"
        run: echo unsafe''',
    )
    _write_required_workflows(tmp_path, encoded)
    with pytest.raises(bootstrap.BootstrapError, match="character escapes"):
        bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


def test_workflow_policy_rejects_yaml_encoded_privileged_trigger(
    tmp_path: Path,
) -> None:
    _write_required_workflows(tmp_path, _workflow())
    extra = tmp_path / ".github" / "workflows" / "encoded-trigger.yml"
    extra.write_text(
        r'''name: encoded
"\x6f\x6e":
  "\x70ull_request_target":
permissions:
  contents: write
jobs:
  unsafe:
    runs-on: production-runner
    steps:
      - run: echo unsafe
''',
        encoding="utf-8",
    )
    with pytest.raises(bootstrap.BootstrapError, match="character escapes"):
        bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


def test_workflow_policy_rejects_yaml_escaped_secret_line_continuation(
    tmp_path: Path,
) -> None:
    encoded = _workflow().replace(
        "      - run: echo safe",
        '''      - env:
          TOKEN: "${{ sec\\
            rets.KEY }}"
        run: echo unsafe''',
    )
    _write_required_workflows(tmp_path, encoded)
    with pytest.raises(bootstrap.BootstrapError, match="line continuation"):
        bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            '--- {"on":{"pull_request_target":null},"jobs":{}}\n',
            "flow-collection",
        ),
        (
            "name: nested-flow-probe\non:\n  {pull_request_target: null}\n",
            "flow-collection",
        ),
        (
            '\ufeff"on":\n  "pull_request_target":\n',
            "byte-order mark",
        ),
        (
            "! on:\n  pull_request_target:\n",
            "YAML tags",
        ),
        (
            "name: tag-probe\non:\n  ! pull_request_target:\n",
            "YAML tags",
        ),
    ],
)
def test_workflow_policy_rejects_alternate_document_encodings(
    text: str, message: str
) -> None:
    with pytest.raises(bootstrap.BootstrapError, match=message):
        bootstrap._reject_ambiguous_workflow_yaml(text)


def test_workflow_policy_rejects_comment_obscured_anchor_key_alias(
    tmp_path: Path,
) -> None:
    _write_required_workflows(tmp_path, _workflow())
    extra = tmp_path / ".github" / "workflows" / "aliased-trigger.yml"
    extra.write_text(
        """name:
  # hide anchor from a line-local mapping check
  &event pull_request_target
on:
  # alias resolves to the privileged trigger key
  *event:
permissions:
  contents: write
jobs:
  unsafe:
    runs-on: ubuntu-latest
    steps:
      - run: echo unsafe
""",
        encoding="utf-8",
    )
    with pytest.raises(bootstrap.BootstrapError, match="anchors or aliases"):
        bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


def test_workflow_policy_rejects_block_event_sequence(tmp_path: Path) -> None:
    _write_required_workflows(
        tmp_path,
        """name: sequence
on:
  - pull_request_target
permissions:
  contents: write
jobs:
  unsafe:
    runs-on: ubuntu-latest
    steps:
      - run: echo unsafe
""",
    )
    with pytest.raises(bootstrap.BootstrapError, match="event-sequence"):
        bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


def test_workflow_policy_rejects_privileged_feature_ref_dispatch(
    tmp_path: Path,
) -> None:
    _write_required_workflows(tmp_path, _workflow())
    dispatch = tmp_path / ".github" / "workflows" / "dispatch.yml"
    dispatch.write_text(
        _workflow(
            event="workflow_dispatch",
            dangerous="""  sign:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
    env:
      SIGNING_KEY: ${{ secrets.SIGNING_KEY }}
""",
        ),
        encoding="utf-8",
    )
    with pytest.raises(bootstrap.BootstrapError, match="privileged job"):
        bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


def test_workflow_policy_accepts_exact_default_ref_dispatch_guard(
    tmp_path: Path,
) -> None:
    _write_required_workflows(tmp_path, _workflow())
    dispatch = tmp_path / ".github" / "workflows" / "dispatch.yml"
    dispatch.write_text(
        _workflow(
            event="workflow_dispatch",
            dangerous="""  sign:
    if: ${{ (github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main') }}
    runs-on: production-runner
    permissions:
      id-token: write
    env:
      SIGNING_KEY: ${{ secrets.SIGNING_KEY }}
""",
        ),
        encoding="utf-8",
    )
    bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


def test_dispatch_guard_must_be_a_required_conjunct() -> None:
    unsafe = """    if: ${{ (github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main') || true }}
"""
    safe = """    if: ${{ (github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main') && (always()) }}
"""
    assert not bootstrap._job_excludes_feature_dispatch(unsafe, "main")
    assert bootstrap._job_excludes_feature_dispatch(safe, "main")


def test_workflow_policy_rejects_privileged_feature_branch_push(tmp_path: Path) -> None:
    _write_required_workflows(tmp_path, _workflow())
    push = tmp_path / ".github" / "workflows" / "feature-push.yml"
    push.write_text(
        _workflow(
            event="push",
            dangerous="""  publish:
    runs-on: ubuntu-latest
    permissions:
      packages: write
""",
        ),
        encoding="utf-8",
    )
    with pytest.raises(bootstrap.BootstrapError, match="privileged job"):
        bootstrap._validate_bootstrap_workflows(tmp_path, BRANCH)


def test_push_branch_filter_is_fail_closed() -> None:
    main_only = "on:\n  push:\n    branches: [main]\n"
    assert not bootstrap._push_reaches_branch(main_only, BRANCH)
    assert bootstrap._push_reaches_branch("on: push\n", BRANCH)
    assert bootstrap._push_reaches_branch(
        "on:\n  push:\n    branches: ['*']\n", BRANCH
    )


def test_pull_event_and_job_helpers_reject_malformed_workflow() -> None:
    assert bootstrap._pull_events("on: [push, pull_request]\njobs: {}") == {"pull_request"}
    assert bootstrap._pull_events("on:\n  push:\n") == set()
    with pytest.raises(bootstrap.BootstrapError, match="does not define jobs"):
        bootstrap._job_blocks("on: pull_request")
    assert bootstrap._job_is_push_only("    if: ${{ github.event_name == 'push' }}\n")
    assert not bootstrap._job_excludes_draft(
        "    if: ${{ ! (github.event_name != 'pull_request' || github.event.pull_request.draft == false) }}\n"
    )
    assert not bootstrap._job_excludes_draft("    runs-on: ubuntu-latest\n")


@pytest.mark.parametrize(
    ("workflow", "message"),
    [
        ("on :\n  pull_request:\n", "trigger-key syntax"),
        ("on:\n  push:\non:\n  pull_request:\n", "duplicate trigger"),
        ("on:\n\tpull_request:\n", "indentation cannot use tabs"),
    ],
)
def test_pull_event_helper_rejects_ambiguous_yaml(
    workflow: str, message: str
) -> None:
    with pytest.raises(bootstrap.BootstrapError, match=message):
        bootstrap._pull_events(workflow)


def test_time_parser_rejects_calendar_invalid_timestamp() -> None:
    with pytest.raises(bootstrap.BootstrapError, match="not a valid RFC 3339"):
        bootstrap._parse_time("2026-02-31T00:00:00Z", field="expiry")


def test_verify_accepts_reproducible_inventory_and_exact_lead_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    inventory_path, _ = _write_inventory(repo, tmp_path, monkeypatch)
    _install_verify_fakes(monkeypatch, inventory_path)

    result = bootstrap.verify_bootstrap(
        _args(repo, inventory=str(inventory_path))
    )

    assert result["status"] == "bootstrap_push_verified"
    assert result["protected_release_authorization"] is False
    assert result["branch"] == BRANCH
    assert result["approval_comment_id"] == 99
    assert result["inventory_sha256"] == bootstrap._sha256(inventory_path)


@pytest.mark.parametrize(
    ("approval_updates", "comment_updates", "provider_now", "message"),
    [
        ({"inventory_sha256": "e" * 64}, {}, NOW, "inventory_sha256"),
        ({"approved_paths": ["one.txt"]}, {}, NOW, "approved_paths"),
        ({"base_sha": "e" * 40}, {}, NOW, "base_sha"),
        ({"local_gate_attestation": {}}, {}, NOW, "local gate attestation"),
        ({"expires_at": "2026-08-09T11:00:01Z"}, {}, NOW, "within 168 hours"),
        ({}, {"updated_at": "2026-08-02T11:01:00Z"}, NOW, "edited"),
        ({}, {"user": {"login": "outsider"}}, NOW, "exactly one"),
        ({}, {}, datetime(2026, 8, 9, 11, tzinfo=UTC), "expired"),
    ],
)
def test_verify_rejects_approval_mismatch_or_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approval_updates: dict[str, Any],
    comment_updates: dict[str, Any],
    provider_now: datetime,
    message: str,
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    inventory_path, _ = _write_inventory(repo, tmp_path, monkeypatch)
    _install_verify_fakes(
        monkeypatch,
        inventory_path,
        approval_updates=approval_updates,
        comment_updates=comment_updates,
        provider_now=provider_now,
    )
    with pytest.raises(bootstrap.BootstrapError, match=message):
        bootstrap.verify_bootstrap(_args(repo, inventory=str(inventory_path)))


def test_verify_rejects_tampered_inventory_and_nonreproducible_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    inventory_path, inventory = _write_inventory(repo, tmp_path, monkeypatch)
    inventory["status"] = "passed"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    _install_verify_fakes(monkeypatch, inventory_path)
    with pytest.raises(bootstrap.BootstrapError, match="status differs"):
        bootstrap.verify_bootstrap(_args(repo, inventory=str(inventory_path)))

    inventory["status"] = "bootstrap_missing_inputs"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    _install_verify_fakes(monkeypatch, inventory_path)
    monkeypatch.setattr(
        bootstrap, "_run_evidence", lambda *_: _failed_evidence("different")
    )
    with pytest.raises(bootstrap.BootstrapError, match="not reproducible"):
        bootstrap.verify_bootstrap(_args(repo, inventory=str(inventory_path)))


def test_inventory_contract_rejects_extra_field_and_future_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    inventory_path, inventory = _write_inventory(repo, tmp_path, monkeypatch)
    inventory["extra"] = True
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    _install_verify_fakes(monkeypatch, inventory_path)
    with pytest.raises(bootstrap.BootstrapError, match="fields differ"):
        bootstrap.verify_bootstrap(_args(repo, inventory=str(inventory_path)))

    inventory.pop("extra")
    inventory["generated_at"] = "2026-08-02T12:00:01Z"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    _install_verify_fakes(monkeypatch, inventory_path)
    with pytest.raises(bootstrap.BootstrapError, match="future"):
        bootstrap.verify_bootstrap(_args(repo, inventory=str(inventory_path)))


def test_comment_parser_rejects_noise_invalid_duplicate_and_wrong_author() -> None:
    approval = _approval("d" * 64)
    noise: list[Any] = [None, {"body": 1}, {"body": "ordinary"}]
    with pytest.raises(bootstrap.BootstrapError, match="exactly one"):
        bootstrap._approval_from_comments(
            noise, candidate_sha=CANDIDATE, leads={"armstrongsam25"}
        )
    malformed = _comment(approval)
    malformed["body"] = "<!-- astraldeep-release-evidence-bootstrap-v1\n{bad}\n-->"
    with pytest.raises(bootstrap.BootstrapError, match="invalid JSON"):
        bootstrap._approval_from_comments(
            [malformed], candidate_sha=CANDIDATE, leads={"armstrongsam25"}
        )
    comment = _comment(approval)
    with pytest.raises(bootstrap.BootstrapError, match="exactly one"):
        bootstrap._approval_from_comments(
            [comment, comment], candidate_sha=CANDIDATE, leads={"armstrongsam25"}
        )


def test_all_comments_paginates_provider_state(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def provider(_root: Path, endpoint: str) -> tuple[list[dict[str, int]], datetime]:
        calls.append(endpoint)
        page = 1 if endpoint.endswith("&page=1") else 2
        size = 100 if page == 1 else 1
        return [{"id": index} for index in range(size)], NOW

    monkeypatch.setattr(bootstrap, "_provider_json", provider)
    comments, provider_now = bootstrap._all_comments(Path("."), REPOSITORY, 151)
    assert len(comments) == 101
    assert provider_now == NOW
    assert len(calls) == 2


@pytest.mark.parametrize(
    "value",
    ["2026-08-02 12:00:00Z", "2026-08-02T12:00:00+00:00", "bad", 1],
)
def test_time_parser_requires_exact_rfc3339_utc(value: Any) -> None:
    with pytest.raises(bootstrap.BootstrapError, match="RFC 3339"):
        bootstrap._parse_time(value, field="time")


def test_provider_json_uses_provider_date_not_local_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = (
        "HTTP/2.0 200 OK\r\nDate: Sun, 02 Aug 2026 12:00:00 GMT\r\n\r\n"
        '{"ok":true}'
    )
    captured: dict[str, Any] = {}

    def run(args: Any, **kwargs: Any) -> Any:
        captured.update(args=args, **kwargs)
        return subprocess.CompletedProcess([], 0, response, "")

    monkeypatch.setattr(bootstrap, "_run", run)
    value, provider_now = bootstrap._provider_json(tmp_path, "endpoint")
    assert value == {"ok": True}
    assert provider_now == NOW
    assert captured["args"][:4] == ("gh", "api", "--hostname", "github.com")


@pytest.mark.parametrize(
    "response",
    [
        "{}",
        "HTTP/2 200\nDate: Sun, 02 Aug 2026 12:00:00 GMT\n\nnot-json",
    ],
)
def test_provider_json_rejects_missing_headers_or_invalid_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    monkeypatch.setattr(
        bootstrap,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, response, ""),
    )
    with pytest.raises(bootstrap.BootstrapError, match="headers|provider time|invalid data"):
        bootstrap._provider_json(tmp_path, "endpoint")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/AstralDeep/AstralDeep.git", REPOSITORY),
        ("https://user:secret@github.com/AstralDeep/AstralDeep.git", REPOSITORY),
        ("git@github.com:AstralDeep/AstralDeep.git", REPOSITORY),
        ("ssh://git@github.com/AstralDeep/AstralDeep", REPOSITORY),
    ],
)
def test_origin_repository_accepts_canonical_github_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, expected: str
) -> None:
    monkeypatch.setattr(bootstrap, "_git", lambda *_: url)
    assert bootstrap._origin_repository(tmp_path) == expected


def test_origin_repository_rejects_other_hosts_and_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for url in ("https://example.com/owner/repo", "https://github.com/only-owner"):
        monkeypatch.setattr(bootstrap, "_git", lambda *_args, value=url: value)
        with pytest.raises(bootstrap.BootstrapError, match="github.com|owner/repository"):
            bootstrap._origin_repository(tmp_path)


def test_candidate_clean_ancestor_and_changed_path_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bootstrap,
        "_git",
        lambda _repo, *args: BASE if args[0] == "rev-parse" else "",
    )
    with pytest.raises(bootstrap.BootstrapError, match="differs"):
        bootstrap._ensure_clean(tmp_path, CANDIDATE)

    monkeypatch.setattr(
        bootstrap,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", ""),
    )
    assert not bootstrap._is_ancestor(tmp_path, BASE, CANDIDATE)
    monkeypatch.setattr(bootstrap, "_git", lambda *_: "")
    with pytest.raises(bootstrap.BootstrapError, match="no changed paths"):
        bootstrap._changed_paths(tmp_path, PREVIOUS, CANDIDATE)


def test_process_git_digest_cleanliness_and_output_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed = bootstrap._run((sys.executable, "-c", "print('ok')"), cwd=tmp_path)
    assert completed.stdout.strip() == "ok"
    with pytest.raises(bootstrap.BootstrapError, match="failed"):
        bootstrap._run((sys.executable, "-c", "raise SystemExit(3)"), cwd=tmp_path)

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert bootstrap._git(tmp_path, "rev-parse", "--is-inside-work-tree") == "true"
    with pytest.raises(bootstrap.BootstrapError, match="exact lowercase Git SHA"):
        bootstrap._require_sha("bad", field="sha")
    with pytest.raises(bootstrap.BootstrapError, match="SHA-256"):
        bootstrap._require_digest("bad", field="digest")

    monkeypatch.setattr(
        bootstrap,
        "_git",
        lambda _repo, *args: CANDIDATE if args[0] == "rev-parse" else " M changed",
    )
    with pytest.raises(bootstrap.BootstrapError, match="not clean"):
        bootstrap._ensure_clean(tmp_path, CANDIDATE)
    with pytest.raises(bootstrap.BootstrapError, match="outside the candidate"):
        bootstrap._outside_candidate(tmp_path / "record.json", tmp_path)


def test_json_leads_and_create_only_records_fail_closed(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not-json", encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="cannot read"):
        bootstrap._load_json(invalid, label="record")
    invalid.write_text("[]", encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="JSON object"):
        bootstrap._load_json(invalid, label="record")

    leads = tmp_path / "leads.json"
    leads.write_text('{"schema_version":1,"lead_logins":["same","same"]}', encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="duplicate"):
        bootstrap._load_leads(leads)
    leads.write_text('{"schema_version":2,"lead_logins":["lead"]}', encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="v1 contract"):
        bootstrap._load_leads(leads)

    record = tmp_path / "record.json"
    bootstrap._atomic_json(record, {"ok": True})
    with pytest.raises(bootstrap.BootstrapError, match="already exists"):
        bootstrap._atomic_json(record, {"ok": False})


def test_fixed_evidence_command_injects_only_base_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "candidate"
    policy = tmp_path / "policy"
    repo.mkdir()
    tool = policy / bootstrap.EVIDENCE_TOOL
    tool.parent.mkdir(parents=True)
    tool.write_text("# trusted parser", encoding="utf-8")
    captured: dict[str, Any] = {}

    def run(args: Any, **kwargs: Any) -> Any:
        captured.update(args=args, **kwargs)
        return _failed_evidence()

    monkeypatch.setattr(bootstrap, "_run", run)
    bootstrap._run_evidence(repo, policy, BASE, CANDIDATE)
    assert captured["args"][0] == sys.executable
    assert captured["args"][1] == str(tool)
    assert captured["args"][2:4] == (
        "--evidence-dir",
        str(repo / "build/060/release-evidence"),
    )
    assert BASE in captured["args"]
    assert CANDIDATE in captured["args"]
    assert captured["cwd"] == policy


def test_push_is_exact_fast_forward_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def run(args: Any, **kwargs: Any) -> Any:
        captured.update(args=args, **kwargs)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(bootstrap, "_run", run)
    bootstrap._push_candidate(
        tmp_path,
        candidate_sha=CANDIDATE,
        branch=BRANCH,
        previous_head=PREVIOUS,
    )
    assert f"--force-with-lease=refs/heads/{BRANCH}:{PREVIOUS}" in captured["args"]
    assert f"{CANDIDATE}:refs/heads/{BRANCH}" in captured["args"]


def test_post_push_receipt_rechecks_open_draft_exact_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pull = _pull(head={"ref": BRANCH, "sha": CANDIDATE, "repo": {"full_name": REPOSITORY}})
    monkeypatch.setattr(bootstrap, "_provider_json", lambda *_: (pull, NOW))
    monkeypatch.setattr(bootstrap, "_git", lambda *_: "https://github.com/AstralDeep/AstralDeep")
    result = bootstrap._post_push_receipt(
        _args(tmp_path), {"previous_head": PREVIOUS, "ok": True}
    )
    assert result["status"] == "bootstrap_push_completed"
    assert result["candidate_sha"] == CANDIDATE
    assert result["remote_repository"] == REPOSITORY

    pull["draft"] = False
    with pytest.raises(bootstrap.BootstrapError, match="open draft"):
        bootstrap._post_push_receipt(
            _args(tmp_path), {"previous_head": PREVIOUS, "ok": True}
        )


def test_main_writes_inventory_verify_and_push_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    monkeypatch.setattr(bootstrap, "build_inventory", lambda _args: {"kind": "inventory"})
    inventory_output = tmp_path / "inventory.json"
    assert bootstrap.main([
        "inventory", "--repo", str(repo), "--github-repository", REPOSITORY,
        "--pr-number", "151", "--candidate-sha", CANDIDATE,
        "--feature", "065-conversational-voice", "--output", str(inventory_output),
    ]) == 0
    assert json.loads(inventory_output.read_text())["kind"] == "inventory"

    verification = {
        "branch": BRANCH,
        "previous_head": PREVIOUS,
        "changed_paths": ["one.txt"],
    }
    monkeypatch.setattr(bootstrap, "verify_bootstrap", lambda _args: verification)
    verify_output = tmp_path / "verify.json"
    assert bootstrap.main([
        "verify", "--repo", str(repo), "--github-repository", REPOSITORY,
        "--pr-number", "151", "--candidate-sha", CANDIDATE,
        "--inventory", str(inventory_output), "--output", str(verify_output),
    ]) == 0

    pushed: list[dict[str, Any]] = []
    monkeypatch.setattr(bootstrap, "_push_candidate", lambda _repo, **kwargs: pushed.append(kwargs))
    monkeypatch.setattr(
        bootstrap,
        "_post_push_receipt",
        lambda _args, _verification: {"status": "bootstrap_push_completed"},
    )
    preflight = tmp_path / "preflight.json"
    receipt = tmp_path / "receipt.json"
    assert bootstrap.main([
        "push", "--repo", str(repo), "--github-repository", REPOSITORY,
        "--pr-number", "151", "--candidate-sha", CANDIDATE,
        "--inventory", str(inventory_output), "--preflight-output", str(preflight),
        "--receipt-output", str(receipt),
    ]) == 0
    assert pushed == [{"candidate_sha": CANDIDATE, "branch": BRANCH, "previous_head": PREVIOUS}]
    assert json.loads(receipt.read_text())["status"] == "bootstrap_push_completed"


def test_main_reports_fail_closed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    monkeypatch.setattr(
        bootstrap,
        "build_inventory",
        lambda _args: (_ for _ in ()).throw(bootstrap.BootstrapError("denied")),
    )
    result = bootstrap.main([
        "inventory", "--repo", str(repo), "--github-repository", REPOSITORY,
        "--pr-number", "151", "--candidate-sha", CANDIDATE,
        "--feature", "065-conversational-voice", "--output", str(tmp_path / "out.json"),
    ])
    assert result == 2
    assert "bootstrap rejected: denied" in capsys.readouterr().err


def test_main_rejects_identical_push_record_paths_before_remote_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    pushed: list[bool] = []
    monkeypatch.setattr(bootstrap, "_push_candidate", lambda *_args, **_kwargs: pushed.append(True))
    output = tmp_path / "same.json"
    result = bootstrap.main([
        "push", "--repo", str(repo), "--github-repository", REPOSITORY,
        "--pr-number", "151", "--candidate-sha", CANDIDATE,
        "--inventory", str(tmp_path / "inventory.json"),
        "--preflight-output", str(output), "--receipt-output", str(output),
    ])
    assert result == 2
    assert pushed == []


def test_parser_has_no_caller_controlled_clock() -> None:
    help_text = bootstrap._parser().format_help()
    assert "--now" not in help_text
