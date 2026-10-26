"""Tests for .github/workflows/release-macos.yml: triggers, guards, permissions, pinned actions, secrets and script argument contracts.
The bootstrap grammar from scripts/verify_release_evidence_bootstrap.py is run over the real workflow and a guard-stripped copy.
"""

from __future__ import annotations

import importlib.util
import re
import shlex
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
    assert flow["concurrency"] == {"group": "release-macos", "cancel-in-progress": False}


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
    assert job["runs-on"] == "macos-26" and job["timeout-minutes"] == 60
    assert job["needs"] == ["decide"] and job["permissions"] == {"contents": "write"}
    expression = condition(job)
    assert expression.startswith(f"{GUARD} && (")
    for clause in (
        "github.event.workflow_run.conclusion == 'success'", "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
        "needs.decide.outputs.skip != 'true'",
    ):
        assert clause in expression


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
    assert gate["if"] == "${{ github.event_name == 'workflow_dispatch' && inputs.dry_run == false }}"
    assert "actions/workflows/ci.yml/runs?head_sha=$head&event=push&branch=main&status=success" in gate["run"]
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
