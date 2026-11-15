"""Tests for .github/workflows/release-macos.yml: triggers, guards, permissions, pinned actions, secrets and script argument contracts.
The bootstrap grammar from scripts/verify_release_evidence_bootstrap.py is run over the real workflow and a guard-stripped copy.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release-macos.yml"
GUARD = "(github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main')"
SECRETS = {
    "APPLE_DEVELOPER_ID_APP_CERT_P12_BASE64", "APPLE_DEVELOPER_ID_CERT_PASSWORD", "ASC_KEY_ID", "ASC_ISSUER_ID",
    "ASC_KEY_P8_BASE64", "SPARKLE_ED25519_PRIVATE_KEY",
}
PBXPROJ = "components/AstralProjection/apple-clients/AstralApp/AstralApp.xcodeproj/project.pbxproj"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bootstrap = load("verify_release_evidence_bootstrap")
release = load("release_macos")
nxt = load("next_macos_release")
notes = load("macos_release_notes")


@pytest.fixture(scope="module")
def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def flow(text) -> dict:
    return yaml.safe_load(text)


def triggers(flow: dict) -> dict:
    return flow.get("on", flow.get(True))


def step(job: dict, name: str) -> dict:
    return next(entry for entry in job["steps"] if entry.get("name") == name)


def condition(job: dict) -> str:
    expression = " ".join(job["if"].split())
    assert expression.startswith("${{ ") and expression.endswith(" }}")
    return expression[4:-3]


def test_runs_after_green_ci_on_main_or_by_dispatch_that_defaults_to_a_dry_run(flow):
    on = triggers(flow)
    assert set(on) == {"workflow_run", "workflow_dispatch"}
    assert on["workflow_run"] == {"workflows": ["CI"], "types": ["completed"], "branches": ["main"]}
    dry_run = on["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["type"] == "boolean" and dry_run["default"] is True
    assert flow["permissions"] == {"contents": "read"}
    assert flow["concurrency"] == {
        "group": "release-macos-${{ github.event_name == 'workflow_dispatch' && inputs.dry_run != false && 'dry-run' || 'publish' }}",
        "cancel-in-progress": False,
    }


def test_decide_is_cheap_unprivileged_and_guarded(flow):
    decide = flow["jobs"]["decide"]
    assert decide["runs-on"] == "ubuntu-24.04" and decide["timeout-minutes"] == 5
    assert decide["permissions"] == {"contents": "read", "actions": "read"}
    expression = condition(decide)
    assert expression.startswith(f"{GUARD} && (")
    for clause in (
        "github.event.workflow_run.conclusion == 'success'", "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    ):
        assert clause in expression
    assert set(decide["outputs"]) == {"head", "version", "build", "skip", "previous_target"}
    assert "secrets." not in yaml.safe_dump(decide)


def test_release_job_runs_only_for_a_green_main_commit_that_is_not_released(flow):
    job = flow["jobs"]["release"]
    assert job["runs-on"] == "macos-26" and job["timeout-minutes"] == 30
    assert job["needs"] == ["decide", "native-check", "apple-artifacts", "node-pack"]
    assert job["permissions"] == {"contents": "write"}
    assert job["environment"] == "release-macos"
    expression = condition(job)
    assert expression.startswith(f"{GUARD} && (")
    for clause in (
        "github.event.workflow_run.conclusion == 'success'", "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
        "needs.decide.outputs.skip != 'true'",
        "needs.native-check.result == 'success'",
    ):
        assert clause in expression


def test_pinned_native_check_is_read_only_bounded_and_precedes_signing(flow):
    job = flow["jobs"]["native-check"]
    assert job["needs"] == ["decide"]
    assert job["runs-on"] == "ubuntu-24.04" and job["timeout-minutes"] == 27
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    assert condition(job).startswith(f"{GUARD} && ")
    assert "needs.decide.outputs.skip != 'true'" in condition(job)
    assert "emergency_skip_ci" not in yaml.safe_dump(job)
    assert "secrets." not in yaml.safe_dump(job)
    checkout = job["steps"][0]
    assert checkout["with"] == {"ref": "${{ needs.decide.outputs.head }}", "persist-credentials": False}
    gate = step(job, "Wait for pinned Projection Apple CI")
    assert gate["env"] == {"DECIDED_HEAD": "${{ needs.decide.outputs.head }}"}
    assert "https://api.github.com/repos/$repo/actions/workflows/apple-ci.yml/runs?" in gate["run"]
    assert "git ls-tree HEAD -- components/AstralProjection" in gate["run"]
    assert "for attempt in {1..26}" in gate["run"] and "sleep 60" in gate["run"]
    assert list(flow["jobs"]).index("native-check") < list(flow["jobs"]).index("release")


def native_run(*, conclusion="success", **changes):
    run = {
        "head_sha": "b" * 40, "head_branch": "main", "event": "push",
        "path": ".github/workflows/apple-ci.yml", "repository": {"full_name": "AstralDeep/AstralProjection"},
        "head_repository": {"full_name": "AstralDeep/AstralProjection"},
        "status": "completed", "conclusion": conclusion,
    }
    run.update(changes)
    return run


def execute_native_check(flow, tmp_path, runs, *, projection="b" * 40, checked_head="a" * 40):
    commands = tmp_path / "bin"
    commands.mkdir(parents=True)
    git = commands / "git"
    git.write_text(
        "#!/bin/sh\ncase \"$*\" in\n"
        "  'rev-parse HEAD') printf '%s\\n' \"$TEST_HEAD\";;\n"
        "  'ls-tree HEAD -- components/AstralProjection') "
        "printf '160000 commit %s\\tcomponents/AstralProjection\\n' \"$TEST_PROJECTION\";;\n"
        "  *) exit 2;;\nesac\n", encoding="utf-8")
    curl = commands / "curl"
    curl.write_text(
        "#!/bin/sh\nfor arg do url=\"$arg\"; done\n"
        "printf '%s\\n' \"$url\" >> \"$API_QUERY_LOG\"\n"
        "printf '%s' \"$FAKE_RESPONSE\"\n", encoding="utf-8")
    sleeper = commands / "sleep"
    sleeper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for command in (git, curl, sleeper):
        command.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": str(commands) + os.pathsep + env["PATH"],
        "DECIDED_HEAD": "a" * 40, "TEST_HEAD": checked_head, "TEST_PROJECTION": projection,
        "FAKE_RESPONSE": json.dumps({"workflow_runs": runs}), "API_QUERY_LOG": str(tmp_path / "queries"),
    })
    result = subprocess.run(
        ["bash", "-c", step(flow["jobs"]["native-check"], "Wait for pinned Projection Apple CI")["run"]],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10, check=False)
    queries = (tmp_path / "queries").read_text(encoding="utf-8").splitlines() if (tmp_path / "queries").exists() else []
    return result, queries


def test_native_check_accepts_only_exact_pinned_main_push_and_queries_that_source(flow, tmp_path):
    result, queries = execute_native_check(flow, tmp_path, [native_run()])
    assert result.returncode == 0, result.stderr
    assert queries == [
        "https://api.github.com/repos/AstralDeep/AstralProjection/actions/workflows/apple-ci.yml/runs?"
        f"head_sha={'b' * 40}&event=push&branch=main&per_page=100"
    ]


@pytest.mark.parametrize("change", [
    {"head_sha": "c" * 40}, {"head_branch": "feature"}, {"event": "pull_request"},
    {"path": ".github/workflows/other.yml"},
    {"repository": {"full_name": "Other/AstralProjection"}},
    {"head_repository": {"full_name": "Other/AstralProjection"}},
])
def test_native_check_rejects_wrong_source_identity(flow, tmp_path, change):
    result, queries = execute_native_check(flow, tmp_path, [native_run(**change)])
    assert result.returncode != 0
    assert len(queries) == 26


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_native_check_stops_immediately_on_completed_failure(flow, tmp_path, conclusion):
    result, queries = execute_native_check(flow, tmp_path, [native_run(conclusion=conclusion)])
    assert result.returncode != 0
    assert len(queries) == 1


def test_native_check_rejects_pending_or_missing_green_run(flow, tmp_path):
    result, queries = execute_native_check(
        flow, tmp_path, [native_run(status="in_progress", conclusion=None)])
    assert result.returncode != 0
    assert len(queries) == 26


def test_native_check_requires_decided_head_and_gitlink(flow, tmp_path):
    result, queries = execute_native_check(flow, tmp_path, [native_run()], checked_head="c" * 40)
    assert result.returncode != 0 and queries == []
    result, queries = execute_native_check(flow, tmp_path / "missing-link", [native_run()], projection="missing")
    assert result.returncode != 0 and queries == []


def test_node_pack_and_framework_producers_are_read_only_and_bounded(flow):
    for name in ("apple-artifacts", "node-pack"):
        job = flow["jobs"][name]
        assert job["runs-on"] == "macos-26" and job["timeout-minutes"] == 30
        assert job["permissions"] == {"contents": "read"}
        assert condition(job).startswith(f"{GUARD} && ")
        assert "secrets." not in yaml.safe_dump(job)
    assert flow["jobs"]["node-pack"]["needs"] == ["decide", "apple-artifacts"]


def test_no_job_or_step_uses_always(text):
    assert "always()" not in text


def test_every_action_is_pinned_to_a_full_commit(flow):
    uses = [entry["uses"] for job in flow["jobs"].values() for entry in job["steps"] if "uses" in entry]
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", value) for value in uses)


def test_required_secrets_step_names_exactly_the_six_secrets(flow):
    job = flow["jobs"]["release"]
    required = step(job, "Require release secrets")
    assert set(required["env"]) == SECRETS
    assert all(value == f"${{{{ secrets.{name} }}}}" for name, value in required["env"].items())
    for name in SECRETS:
        assert name in required["run"]
    referenced = set(re.findall(r"secrets\.([A-Z0-9_]+)", yaml.safe_dump(job)))
    assert referenced == SECRETS


def test_xcode_is_selected_exactly(flow):
    job = flow["jobs"]["release"]
    assert job["env"]["XCODE_VERSION"] == "26.6" and job["env"]["XCODE_BUILD"] == "17F113"
    selected = step(job, "Select exact Xcode")["run"]
    assert 'sudo xcode-select --switch "$selected/Contents/Developer"' in selected


def test_signing_material_lives_in_an_ephemeral_keychain_and_is_removed(flow):
    job = flow["jobs"]["release"]
    imported = step(job, "Import the Developer ID identity into an ephemeral keychain")["run"]
    for fragment in ("security create-keychain", "-T /usr/bin/codesign", "set-key-partition-list", "list-keychains",
                     "find-identity -v -p codesigning", "Developer ID Application: Personalized AI Labs LLC (593LGH35GS)"):
        assert fragment in imported
    keys = step(job, "Write the notary and Sparkle keys")["run"]
    assert "umask 077" in keys and '"$RUNNER_TEMP/AuthKey.p8"' in keys and '"$RUNNER_TEMP/sparkle.key"' in keys
    cleanup = step(job, "Remove signing material")
    assert cleanup["if"] == "${{ !cancelled() }}"
    assert 'rm -f "$RUNNER_TEMP/AuthKey.p8" "$RUNNER_TEMP/sparkle.key"' in cleanup["run"]
    assert "security delete-keychain" in cleanup["run"]
    names = [entry.get("name") for entry in job["steps"]]
    assert names.index("Remove signing material") == len(names) - 1


def test_stage_is_kept_as_an_artifact(flow):
    job = flow["jobs"]["release"]
    upload = next(entry for entry in job["steps"] if entry.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["with"]["name"] == "macos-release-${{ needs.decide.outputs.version }}-${{ needs.decide.outputs.build }}"
    assert upload["with"]["path"] == "${{ runner.temp }}/stage" and upload["with"]["retention-days"] == 14


def placeholders(command: str) -> list[str]:
    return shlex.split(command.replace("\\\n", " "))


def test_decide_invokes_the_version_script_with_its_real_arguments(flow, monkeypatch):
    run = step(flow["jobs"]["decide"], "Decide the next release")["run"]
    line = next(line for line in run.splitlines() if "scripts/next_macos_release.py" in line)
    tokens = placeholders(line)
    seen = {}

    def capture(args):
        seen["args"] = args
        raise nxt.NextReleaseError("captured")

    monkeypatch.setattr(nxt, "run", capture)
    assert nxt.main(tokens[tokens.index("scripts/next_macos_release.py") + 1:]) == 1
    args = seen["args"]
    assert args.repo == "$GITHUB_REPOSITORY" and args.head == "$head" and args.pbxproj == PBXPROJ
    assert args.github_output == "$GITHUB_OUTPUT"


def test_dispatch_publication_requires_a_green_ci_run_for_the_head(flow):
    decide = flow["jobs"]["decide"]
    gate = step(decide, "Require green CI before a dispatched publication")
    assert gate["if"] == (
        "${{ github.event_name == 'workflow_dispatch' && inputs.dry_run == false"
        " && inputs.emergency_skip_ci != true }}"
    )
    assert "actions/workflows/ci.yml/runs?head_sha=$head&event=push&branch=main&status=success" in gate["run"]
    override = step(decide, "Record manual emergency CI override")
    assert override["if"] == (
        "${{ github.event_name == 'workflow_dispatch' && inputs.dry_run == false"
        " && inputs.emergency_skip_ci == true }}"
    )
    assert "::warning::" in override["run"]
    assert decide["steps"].index(gate) < decide["steps"].index(step(decide, "Decide the next release"))


def test_release_invokes_notes_and_release_scripts_with_their_real_arguments(flow):
    job = flow["jobs"]["release"]
    notes_run = step(job, "Write release notes")["run"]
    line = next(line for line in notes_run.splitlines() if "scripts/macos_release_notes.py" in line)
    tokens = placeholders(line.replace("$BUILD", "72"))
    parsed = notes.build_parser().parse_args(tokens[tokens.index("scripts/macos_release_notes.py") + 1:])
    assert (parsed.base, parsed.head, parsed.version, parsed.build) == (
        "$PREVIOUS_TARGET", "$(git rev-parse HEAD)", "$VERSION", 72)
    assert str(parsed.output) == "$RUNNER_TEMP/notes.md"
    build = step(job, "Build, notarize and publish")
    script = build["run"]
    array = script.partition("args=(")[2].partition("\n)")[0]
    parsed = release.build_parser().parse_args(placeholders(array.replace("$BUILD", "71")))
    assert parsed.channel == "stable" and parsed.publish is False and parsed.dry_run is False
    assert str(parsed.notes) == "$RUNNER_TEMP/notes.md" and str(parsed.ed_key_file) == "$RUNNER_TEMP/sparkle.key"
    assert str(parsed.notary_key) == "$RUNNER_TEMP/AuthKey.p8"
    assert (parsed.notary_key_id, parsed.notary_issuer) == ("$ASC_KEY_ID", "$ASC_ISSUER_ID")
    assert parsed.projection_commit == "$(git -C components/AstralProjection rev-parse HEAD)"
    assert str(parsed.staging_dir) == "$RUNNER_TEMP/stage" and str(parsed.work_dir) == "$RUNNER_TEMP/work"
    assert 'if [ "$DRY_RUN" != "true" ]; then\n  args+=(--publish)\nfi' in script
    assert build["env"]["DRY_RUN"] == "${{ github.event_name == 'workflow_dispatch' && inputs.dry_run != false }}"
    assert build["env"]["ASC_KEY_ID"] == "${{ secrets.ASC_KEY_ID }}"


def install(root: Path, release_text: str) -> None:
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_bytes((ROOT / ".github" / "workflows" / "ci.yml").read_bytes())
    (workflows / "release-macos.yml").write_text(release_text, encoding="utf-8")


def test_bootstrap_grammar_accepts_the_workflow(tmp_path, text):
    install(tmp_path, text)
    assert ".github/workflows/release-macos.yml" in bootstrap._validate_bootstrap_workflows(tmp_path, "feature")


@pytest.mark.parametrize("job", ["decide", "release"])
def test_a_dispatch_from_another_ref_cannot_reach_a_privileged_job(tmp_path, text, job):
    lines = text.splitlines(keepends=True)
    start = lines.index(f"  {job}:\n")
    guard = next(index for index in range(start, len(lines)) if lines[index].startswith("    if: "))
    lines[guard] = lines[guard].replace(f"{GUARD} && ", "")
    install(tmp_path, "".join(lines))
    with pytest.raises(bootstrap.BootstrapError, match=f"privileged job .github/workflows/release-macos.yml:{job}"):
        bootstrap._validate_bootstrap_workflows(tmp_path, "feature")
