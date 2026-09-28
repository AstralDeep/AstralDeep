"""Tests for scripts/produce_backend_web_qualification.py: per-job phase selection, isolated
candidate environment, honest phase receipts, and fail-closed receipt assembly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest

from scripts import produce_backend_web_qualification as producer


AUTH_KEYS = ("real_application_keycloak_login_pkce", "login_state_denial", "application_session_renewal",
             "owner_read_denials", "logout_isolation", "chat_history_retention",
             "attachment_upload_metadata_retention", "attachment_owner_denials")
CHECKS = {"real-keycloak-auth", "session-renewal", "owner-authorization-denials", "voice-enabled-browser",
          "backend-complete", "baseline-migration", "retained-data", "backup-restore", "recovery",
          "audit-continuity"}


@pytest.fixture
def args(tmp_path, monkeypatch):
    monkeypatch.setattr(producer, "observer_user", lambda: "1000:1000")
    source = tmp_path / "source"
    (source / "config").mkdir(parents=True)
    (source / "config/astral-composition.json").write_text(json.dumps({"components": {"astral-plane": {"commit": "e" * 40}}}))
    return argparse.Namespace(phase="all", group=None, receipts=None,
                              policy_commit="a" * 40, candidate_sha="b" * 40,
                              candidate_image="sha256:" + "c" * 64, lets_image="sha256:" + "d" * 64,
                              materializer_image="sha256:" + "e" * 64, source=source,
                              baseline_plane_source=tmp_path, private_root=tmp_path / "private",
                              output=tmp_path / "output", port=18091, protected=False,
                              voice_image=None, voice_closure_sha256=None, speech_runtime_env=None)


def driver_stub():
    return SimpleNamespace(verify_source=lambda *_: None, IMAGE=re.compile(r"sha256:[0-9a-f]{64}"))


def protected_environment(monkeypatch, args, job):
    for key, value in {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
                       "GITHUB_JOB": job, "GITHUB_WORKFLOW_SHA": args.policy_commit,
                       "BACKEND_WEB_PRODUCER_SHA": args.policy_commit, "GITHUB_RUN_ID": "12",
                       "GITHUB_RUN_ATTEMPT": "1", "RUNNER_NAME": "runner",
                       "BACKEND_WEB_PRODUCER_RUNNER": "runner"}.items():
        monkeypatch.setenv(key, value)


def stub_phases(args, monkeypatch, failure=None):
    calls = []

    def initialize(**kwargs):
        calls.append(("initialize", kwargs))
        if failure == "services":
            raise RuntimeError("sensitive service error")
        return {"runtime_fragment": str(args.private_root / "runtime.env"),
                "material_root": str(args.private_root / "materials/app"), "service_network": "isolated-services"}

    def deploy(arguments):
        calls.append(("deploy", arguments))
        if failure == "deployment":
            raise RuntimeError("sensitive deployment error")
        return {"status": "started"}

    def load(name):
        if name == "validate_backend_web_evidence":
            return SimpleNamespace(CHECKS=CHECKS)
        if name == "run_backend_web_qualification":
            return SimpleNamespace(deploy=deploy, **vars(driver_stub()))
        return SimpleNamespace(initialize=initialize)

    def execute(arguments, **kwargs):
        calls.append(("execute", arguments, kwargs))
        if arguments[0] == "bash":
            if failure == "gates" or failure == "gate-" + kwargs["environment"]["ASTRAL_GATE_GROUP"]:
                raise RuntimeError("sensitive gates error")
            assert "ASTRAL_GATE_POLICY_ROOT" in kwargs["environment"]
            return SimpleNamespace(returncode=0)
        if failure == "auth":
            raise RuntimeError("sensitive authentication error")
        identifier = arguments[arguments.index("--qualification-id") + 1]
        result = {"qualification_id": identifier, "synthetic_only": True, **dict.fromkeys(AUTH_KEYS, "passed")}
        if failure == "auth-record":
            result["application_session_renewal"] = "skipped"
        evidence = next(part.split("src=", 1)[1].split(",dst=/evidence")[0]
                        for part in arguments if part.endswith("dst=/evidence"))
        (Path(evidence) / "authentication.json").write_text(json.dumps(result))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(producer, "module", load)
    monkeypatch.setattr(producer, "execute", execute)
    return calls


def test_candidate_process_environment_has_no_provider_job_authority(monkeypatch):
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "RUNNER_NAME"):
        monkeypatch.setenv(key, "do-not-propagate")
    monkeypatch.setenv("PATH", "safe-path")
    cleaned = producer.clean_environment()
    assert cleaned["PATH"] == "safe-path"
    assert "do-not-propagate" not in cleaned.values()


def test_execute_uses_fixed_argv_sanitized_environment_and_safe_error(monkeypatch, tmp_path):
    calls = []
    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", run)
    assert producer.execute(["fixed", "command"], cwd=tmp_path).returncode == 0
    assert calls[0][1]["timeout"] == 300
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="fixed qualification"):
        producer.execute(["fixed"], cwd=tmp_path)


def test_phase_jobs_receipts_and_gate_budget_are_fixed():
    assert producer.GATE_GROUPS == ("tests", "persistent_agents", "modules")
    assert producer.GATE_TIMEOUT_SECONDS < 30 * 60
    assert [producer.phase_job(phase) for phase in producer.PHASES] == [
        "qualify-backend-web", "qualify-backend-web-gates", "qualify-backend-web-services", "qualify-backend-web"]
    assert [producer.receipt_name(*item) for item in producer.expected_receipts()] == [
        "phase-gate-tests.json", "phase-gate-persistent_agents.json", "phase-gate-modules.json", "phase-services.json"]


@pytest.mark.parametrize("phase,group,job", [
    ("gate", "persistent_agents", "qualify-backend-web-gates"),
    ("services", None, "qualify-backend-web-services"),
    ("assemble", None, "qualify-backend-web"),
])
def test_context_requires_exact_protected_owner_for_each_phase_job(args, monkeypatch, tmp_path, phase, group, job):
    monkeypatch.setattr(producer, "module", lambda _: driver_stub())
    args.phase, args.group = phase, group
    if phase == "assemble":
        args.receipts = tmp_path / "receipts"
        args.receipts.mkdir()
    assert producer.verify_context(args).IMAGE
    args.protected = True
    with pytest.raises(ValueError, match="protected producer"):
        producer.verify_context(args)
    protected_environment(monkeypatch, args, job)
    assert producer.verify_context(args).IMAGE
    other = "qualify-backend-web-services" if job != "qualify-backend-web-services" else "qualify-backend-web"
    monkeypatch.setenv("GITHUB_JOB", other)
    with pytest.raises(ValueError, match="protected producer"):
        producer.verify_context(args)
    monkeypatch.setenv("GITHUB_JOB", job)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "0")
    with pytest.raises(ValueError, match="protected producer"):
        producer.verify_context(args)


def test_protected_context_refuses_the_bundled_local_pass(args, monkeypatch):
    monkeypatch.setattr(producer, "module", lambda _: driver_stub())
    assert producer.verify_context(args).IMAGE
    args.protected = True
    protected_environment(monkeypatch, args, "qualify-backend-web")
    with pytest.raises(ValueError, match="protected producer"):
        producer.verify_context(args)


@pytest.mark.parametrize("relation", ["same", "nested", "parent"])
def test_context_keeps_private_materials_and_public_evidence_separate(args, monkeypatch, relation):
    monkeypatch.setattr(producer, "module", lambda _: driver_stub())
    if relation == "same":
        args.output = args.private_root
    elif relation == "nested":
        args.output = args.private_root / "evidence"
    else:
        args.private_root = args.output / "private"
    with pytest.raises(ValueError, match="separate trees"):
        producer.verify_context(args)


@pytest.mark.parametrize("field,value", [
    ("candidate_sha", "bad"), ("port", 80), ("port", None), ("candidate_image", "latest"),
    ("candidate_image", None), ("lets_image", "latest"), ("output", Path("relative")),
    ("private_root", Path("relative")), ("group", "tests"), ("receipts", Path("/receipts")),
    ("source", None), ("baseline_plane_source", None), ("phase", "unknown"),
])
def test_invalid_context_refused_before_commands(args, monkeypatch, field, value):
    monkeypatch.setattr(producer, "module", lambda _: driver_stub())
    setattr(args, field, value)
    with pytest.raises(ValueError):
        producer.verify_context(args)


@pytest.mark.parametrize("phase,group,receipts", [
    ("gate", None, None), ("gate", "all", None), ("services", "modules", None), ("assemble", None, None),
    ("assemble", None, "same"), ("assemble", None, "parent"), ("assemble", None, "symlink"),
    ("assemble", None, "absent"), ("assemble", None, "relative"),
])
def test_phase_inputs_are_exclusive(args, monkeypatch, tmp_path, phase, group, receipts):
    monkeypatch.setattr(producer, "module", lambda _: driver_stub())
    args.phase, args.group = phase, group
    if receipts in {"same", "parent"}:
        args.receipts = tmp_path / "receipts"
        args.receipts.mkdir()
        args.output = args.receipts if receipts == "same" else args.receipts / "evidence"
    elif receipts == "relative":
        args.receipts = Path("receipts")
    elif receipts == "symlink":
        (tmp_path / "real").mkdir()
        args.receipts = tmp_path / "linked"
        args.receipts.symlink_to(tmp_path / "real")
    elif receipts == "absent":
        args.receipts = tmp_path / "absent"
    with pytest.raises(ValueError):
        producer.verify_context(args)


def test_qualification_roots_cannot_traverse_a_symlinked_parent(args, monkeypatch, tmp_path):
    monkeypatch.setattr(producer, "module", lambda _: driver_stub())
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    args.output = tmp_path / "link" / "evidence"
    with pytest.raises(ValueError, match="traverse a symlink"):
        producer.verify_context(args)


def test_gate_phase_needs_neither_services_nor_a_port(args, monkeypatch):
    monkeypatch.setattr(producer, "module", lambda _: driver_stub())
    args.phase, args.group, args.port, args.lets_image, args.baseline_plane_source = "gate", "modules", None, None, None
    assert producer.verify_context(args).IMAGE


@pytest.mark.parametrize("group", producer.GATE_GROUPS)
def test_gate_phase_runs_exactly_its_whole_suite_group(args, monkeypatch, group):
    for key, value in {"ASTRAL_GATE_GROUP": "tests", "ASTRAL_GATE_NAMESPACE": "ad-gates-inherited",
                       "ASTRAL_GATE_POLICY_ROOT": "/inherited-policy"}.items():
        monkeypatch.setenv(key, value)
    calls = stub_phases(args, monkeypatch)
    args.phase, args.group = "gate", group
    receipt = producer.produce(args)
    [(kind, arguments, kwargs)] = calls
    assert kind == "execute"
    assert arguments == ["bash", str(producer.ROOT / "scripts/backend_web_image_gate.sh"), args.candidate_image, "tests"]
    assert kwargs["timeout"] == producer.GATE_TIMEOUT_SECONDS and kwargs["cwd"] == args.source
    environment = kwargs["environment"]
    assert environment["ASTRAL_GATE_GROUP"] == group
    assert environment["ASTRAL_GATE_POLICY_ROOT"] == str(producer.ROOT)
    assert re.fullmatch(r"ad-bwq-gates-[0-9a-f]{32}", environment["ASTRAL_GATE_NAMESPACE"])
    assert receipt["phase_observations"] == [{"phase": "complete-backend-unit-gates-" + group, "status": "executed", "exit_code": 0}]
    assert receipt["group"] == group and receipt["release_authorized"] is False
    assert json.loads((args.output / f"phase-gate-{group}.json").read_text()) == receipt
    assert not args.private_root.exists()


@pytest.mark.parametrize("failure", [None, "services", "deployment", "auth", "auth-record"])
def test_service_phase_receipt_records_only_attempted_sequence(args, monkeypatch, failure):
    calls = stub_phases(args, monkeypatch, failure)
    args.phase = "services"
    receipt = producer.produce(args)
    statuses = [row["status"] for row in receipt["phase_observations"]]
    assert statuses == {None: ["executed"] * 3, "services": ["failed"], "deployment": ["executed", "failed"],
                        "auth": ["executed", "executed", "failed"], "auth-record": ["executed", "executed", "failed"]}[failure]
    assert receipt["qualification_namespace_retained"] is (failure != "services")
    assert "sensitive" not in json.dumps(receipt)
    assert producer.validate_receipt(args, receipt, "services", None) == receipt
    if failure is None:
        assert "session-renewal" in receipt["observed_checks"]
        auth = next(row[1] for row in calls if row[0] == "execute" and row[1][0] == "docker")
        assert args.materializer_image in auth and args.candidate_image not in auth
        assert "--read-only" in auth and "/probe.py" in auth
        assert "--user" in auth and "1000:1000" in auth
        assert (args.output / "authentication.json").is_file()
    else:
        assert "session-renewal" not in receipt["observed_checks"]


@pytest.mark.parametrize("failure", [None, "gates", "gate-modules", "services", "deployment", "auth", "auth-record"])
def test_local_pass_records_missing_acceptance_without_fake_success(args, monkeypatch, failure):
    stub_phases(args, monkeypatch, failure)
    report = producer.produce(args)
    assert report["status"] == "incomplete-not-qualified"
    assert report["release_authorized"] is False
    assert report["candidate_image"] == args.candidate_image
    assert "voice-enabled-browser" in report["pending_checks"]
    assert "backend-complete" in report["pending_checks"]
    assert not (args.output / "evidence.json").exists()
    assert "sensitive" not in json.dumps(report)
    assert [row["phase"] for row in report["phase_observations"][:3]] == [
        "complete-backend-unit-gates-" + group for group in producer.GATE_GROUPS]
    gate_statuses = [row["status"] for row in report["phase_observations"][:3]]
    assert gate_statuses == {"gates": ["failed"] * 3, "gate-modules": ["executed", "executed", "failed"]}.get(
        failure, ["executed"] * 3)
    assert json.loads((args.output / "producer-status.json").read_text()) == report
    if failure is None:
        assert "session-renewal" in report["observed_checks"]
        assert (args.output / "authentication.json").is_file()
    if failure in {"auth", "auth-record"}:
        assert "session-renewal" in report["pending_checks"]


def write_receipts(args, directory, *, skip=(), mutate=None, image="sha256:" + "c" * 64):
    directory.mkdir(exist_ok=True)
    for phase, group in producer.expected_receipts():
        name = producer.receipt_name(phase, group)
        if name in skip:
            continue
        if phase == "gate":
            observations = [{"phase": producer.gate_phase_name(group), "status": "executed", "exit_code": 0}]
            checks, identifier, retained = [], None, False
        else:
            observations = [{"phase": item, "status": "executed", "exit_code": 0} for item, _ in producer.SERVICE_PHASES]
            checks = sorted(check for _, phase_checks in producer.SERVICE_PHASES for check in phase_checks)
            identifier, retained = "f" * 32, True
        receipt = {"schema_version": 1, "scope": "backend-web", "phase": phase, "group": group,
                   "candidate_sha": args.candidate_sha, "candidate_image": image,
                   "policy_commit": args.policy_commit, "protected_context": args.protected,
                   "run": producer.run_identity(args.protected), "qualification_id": identifier,
                   "qualification_namespace_retained": retained, "phase_observations": observations,
                   "observed_checks": checks, "release_authorized": False,
                   "script_sha256": producer.script_digest()}
        if mutate and mutate[0] == name:
            mutate[1](receipt)
        (directory / name).write_text(json.dumps(receipt))
    return directory


@pytest.fixture
def assembly(args, monkeypatch, tmp_path):
    stub_phases(args, monkeypatch)
    args.phase, args.receipts = "assemble", tmp_path / "receipts"
    args.candidate_image = args.lets_image = args.materializer_image = None
    args.source = args.baseline_plane_source = args.private_root = args.port = None
    return args


def test_assembly_joins_every_protected_receipt_in_fixed_order(assembly, monkeypatch):
    assembly.protected = True
    protected_environment(monkeypatch, assembly, "qualify-backend-web")
    write_receipts(assembly, assembly.receipts)
    (assembly.receipts / "authentication.json").write_text('{"synthetic_only": true}')
    status = producer.produce(assembly)
    assert status["candidate_image"] == "sha256:" + "c" * 64
    assert [row["phase"] for row in status["phase_observations"]] == [
        *("complete-backend-unit-gates-" + group for group in producer.GATE_GROUPS),
        *(name for name, _ in producer.SERVICE_PHASES)]
    assert {row["status"] for row in status["phase_observations"]} == {"executed"}
    assert status["qualification_id"] == "f" * 32 and status["qualification_namespace_retained"] is True
    assert status["protected_context"] is True and status["release_authorized"] is False
    assert (assembly.output / "authentication.json").read_text() == '{"synthetic_only": true}'
    assert "session-renewal" in status["observed_checks"]


def test_assembly_reports_each_missing_phase_honestly(assembly):
    write_receipts(assembly, assembly.receipts, skip={"phase-gate-modules.json", "phase-services.json"})
    status = producer.produce(assembly)
    missing = [row["phase"] for row in status["phase_observations"] if row["status"] == "missing"]
    assert missing == ["complete-backend-unit-gates-modules", *(name for name, _ in producer.SERVICE_PHASES)]
    assert status["qualification_id"] is None and status["qualification_namespace_retained"] is False
    assert status["observed_checks"] == []
    assert status["status"] == "incomplete-not-qualified"


def test_assembly_of_no_receipts_is_still_an_honest_status(assembly):
    assembly.receipts.mkdir()
    status = producer.produce(assembly)
    assert {row["status"] for row in status["phase_observations"]} == {"missing"}
    assert status["candidate_image"] is None
    assert not (assembly.output / "authentication.json").exists()


def fail_second(receipt):
    receipt["phase_observations"][1] = {"phase": producer.SERVICE_PHASES[1][0], "status": "failed", "exit_code": 1}


@pytest.mark.parametrize("name,mutation", [
    ("phase-gate-tests.json", lambda r: r.update(candidate_sha="0" * 40)),
    ("phase-gate-tests.json", lambda r: r.update(policy_commit="0" * 40)),
    ("phase-gate-tests.json", lambda r: r.update(candidate_image="latest")),
    ("phase-gate-tests.json", lambda r: r.update(group="modules")),
    ("phase-gate-tests.json", lambda r: r.update(phase="services")),
    ("phase-gate-tests.json", lambda r: r.update(release_authorized=True)),
    ("phase-gate-tests.json", lambda r: r.update(protected_context=True)),
    ("phase-gate-tests.json", lambda r: r.update(run={"run_id": "1", "run_attempt": "1"})),
    ("phase-gate-tests.json", lambda r: r.update(script_sha256="0" * 64)),
    ("phase-gate-tests.json", lambda r: r.update(extra=True)),
    ("phase-gate-tests.json", lambda r: r.update(schema_version=2)),
    ("phase-gate-tests.json", lambda r: r.update(observed_checks=["backend-complete"])),
    ("phase-gate-tests.json", lambda r: r.update(qualification_id="f" * 32)),
    ("phase-gate-tests.json", lambda r: r.update(phase_observations=[])),
    ("phase-gate-tests.json", lambda r: r.update(phase_observations="executed")),
    ("phase-gate-tests.json", lambda r: r["phase_observations"][0].update(exit_code=False)),
    ("phase-gate-tests.json", lambda r: r["phase_observations"][0].update(status="skipped")),
    ("phase-gate-tests.json", lambda r: r["phase_observations"][0].update(phase="complete-backend-unit-gates")),
    ("phase-gate-tests.json", lambda r: r["phase_observations"].append(dict(r["phase_observations"][0]))),
    ("phase-services.json", lambda r: r["phase_observations"].pop()),
    ("phase-services.json", fail_second),
    ("phase-services.json", lambda r: r.update(qualification_id="not-hex")),
    ("phase-services.json", lambda r: r.update(qualification_namespace_retained=False)),
    ("phase-services.json", lambda r: r["phase_observations"].__setitem__(0, "executed")),
])
def test_assembly_refuses_rebound_or_overclaiming_receipts(assembly, name, mutation):
    write_receipts(assembly, assembly.receipts, mutate=(name, mutation))
    with pytest.raises(ValueError):
        producer.produce(assembly)
    assert not (assembly.output / "producer-status.json").exists()


def test_assembly_accepts_an_honest_interrupted_service_sequence(assembly):
    def interrupted(receipt):
        receipt["phase_observations"] = [
            {"phase": producer.SERVICE_PHASES[0][0], "status": "executed", "exit_code": 0},
            {"phase": producer.SERVICE_PHASES[1][0], "status": "failed", "exit_code": 1},
        ]
        receipt["observed_checks"] = []
    write_receipts(assembly, assembly.receipts, mutate=("phase-services.json", interrupted))
    status = producer.produce(assembly)
    assert [row["status"] for row in status["phase_observations"]][-2:] == ["executed", "failed"]
    assert status["qualification_namespace_retained"] is True
    assert "baseline-migration" in status["pending_checks"]


@pytest.mark.parametrize("hazard", ["unexpected", "directory", "symlink", "orphan-authentication", "images"])
def test_assembly_refuses_unexpected_members_and_mixed_candidates(assembly, tmp_path, hazard):
    skip = {"phase-services.json"} if hazard == "orphan-authentication" else set()
    mutate = ("phase-gate-modules.json", lambda r: r.update(candidate_image="sha256:" + "9" * 64)) if hazard == "images" else None
    write_receipts(assembly, assembly.receipts, skip=skip, mutate=mutate)
    if hazard == "unexpected":
        (assembly.receipts / "phase-gate-all.json").write_text("{}")
    elif hazard == "directory":
        (assembly.receipts / "nested").mkdir()
    elif hazard == "symlink":
        target = tmp_path / "elsewhere.json"
        target.write_text((assembly.receipts / "phase-gate-tests.json").read_text())
        (assembly.receipts / "phase-gate-tests.json").unlink()
        (assembly.receipts / "phase-gate-tests.json").symlink_to(target)
    elif hazard == "orphan-authentication":
        (assembly.receipts / "authentication.json").write_text("{}")
    with pytest.raises(ValueError):
        producer.produce(assembly)


def cli_values(args):
    values = []
    for name, value in vars(args).items():
        if value is not None and name != "protected":
            values += ["--" + name.replace("_", "-"), str(value)]
    return values


def test_cli_incomplete_is_nonzero_and_does_not_authorize(args, monkeypatch, capsys):
    values = cli_values(args)
    monkeypatch.setattr(producer, "produce", lambda _: {"status": "incomplete-not-qualified", "pending_checks": ["voice"]})
    assert producer.main(values) == 2
    assert json.loads(capsys.readouterr().out)["release_authorized"] is False
    def fail(_):
        raise ValueError("private detail")
    monkeypatch.setattr(producer, "produce", fail)
    assert producer.main(values) == 1
    assert "private detail" not in capsys.readouterr().err


@pytest.mark.parametrize("phase,group,statuses,expected", [
    ("gate", "tests", ["executed"], 0),
    ("gate", "tests", ["failed"], 2),
    ("services", None, ["executed"] * 3, 0),
    ("services", None, ["executed", "failed"], 2),
])
def test_cli_phase_jobs_succeed_only_when_every_attempted_phase_executed(args, monkeypatch, capsys, phase, group, statuses, expected):
    args.phase, args.group = phase, group
    observations = [{"phase": str(index), "status": status, "exit_code": 0} for index, status in enumerate(statuses)]
    monkeypatch.setattr(producer, "produce", lambda _: {"phase_observations": observations})
    assert producer.main(cli_values(args)) == expected
    printed = json.loads(capsys.readouterr().out)
    assert printed["release_authorized"] is False and printed["phase"] == phase
    assert printed["status"] == ("executed" if expected == 0 else "failed")


def test_protected_module_loading_uses_policy_tree():
    assert producer.module("run_backend_web_qualification").BASELINE_PLANE == producer.BASELINE_PLANE
