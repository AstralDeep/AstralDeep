"""Tests for scripts/run_backend_web_qualification.py: deployment isolation, exact
component identity, fail-closed Docker failures, and baseline/recovery binding.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "run_backend_web_qualification.py"
SPEC = importlib.util.spec_from_file_location("backend_web_qualification", SCRIPT)
assert SPEC and SPEC.loader
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


@pytest.fixture
def args(tmp_path):
    environment = tmp_path / "runtime.env"
    environment.write_text(
        "# isolated test profile\nASTRAL_ENV=production\nUSE_MOCK_AUTH=false\n"
        "LETS_MODE=enforce\nFF_LETS_EXTERNAL_WARDEN=true\n", encoding="utf-8"
    )
    environment.chmod(0o600)
    material = tmp_path / "materials"
    material.mkdir(mode=0o700)
    marker = material / "qualification.json"
    marker.write_text(json.dumps({
        "schema_version": 1, "qualification_id": "a" * 32, "classification": "synthetic",
    }), encoding="utf-8")
    marker.chmod(0o600)
    return SimpleNamespace(
        qualification_id="a" * 32, candidate_sha="b" * 40,
        candidate_image="sha256:" + "c" * 64, postgres_image="registry.test/pg@sha256:" + "d" * 64,
        plane_source=tmp_path, plane_commit="e" * 40, baseline_plane_source=tmp_path,
        runtime_env=environment, fixture_sha256="f" * 64, port=18091, output=tmp_path / "evidence.json",
        material_root=material, service_network="ad-bwq-services-" + "a" * 32,
    )


@pytest.mark.parametrize("content,match", [
    ("ASTRAL_ENV=development", "production"),
    ("ASTRAL_ENV=production\nUSE_MOCK_AUTH=true", "mock"),
    ("ASTRAL_ENV=production\nLETS_MODE=off", "LETS enforce"),
    ("ASTRAL_ENV=production\nLETS_MODE=enforce", "external LETS"),
    ("BADLINE", "malformed"),
    ("BAD=one\nBAD=two", "duplicate"),
    ("BAD='quoted'", "literal"),
    ("BAD=\x00", "literal"),
])
def test_runtime_posture_and_syntax_are_explicit(args, content, match):
    args.runtime_env.write_text(content, encoding="utf-8")
    with pytest.raises(driver.QualificationError, match=match):
        driver.read_runtime(args.runtime_env)


def test_runtime_path_and_permissions(args, monkeypatch):
    with pytest.raises(driver.QualificationError, match="protected file"):
        driver.read_runtime(Path("relative"))
    if os.name != "nt":
        args.runtime_env.chmod(0o644)
        with pytest.raises(driver.QualificationError, match="only by its owner"):
            driver.read_runtime(args.runtime_env)


def test_subprocess_failure_does_not_expose_sensitive_output(monkeypatch):
    monkeypatch.setattr(driver.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 7, "secret", "secret"))
    with pytest.raises(driver.QualificationError, match="exit 7") as caught:
        driver.run(["docker", "inspect"])
    assert "secret" not in str(caught.value)
    assert driver.run(["docker"], check=False).returncode == 7


def test_component_source_refuses_wrong_identity_and_dirt(tmp_path, monkeypatch):
    with pytest.raises(driver.QualificationError, match="exact Git SHA"):
        driver.verify_source(tmp_path, "bad")
    monkeypatch.setattr(driver, "run", lambda *a, **k: SimpleNamespace(stdout="a" * 40))
    with pytest.raises(driver.QualificationError, match="clean exact"):
        driver.verify_source(tmp_path, "a" * 40)
    monkeypatch.setattr(driver, "run", lambda a, **k: SimpleNamespace(stdout="a" * 40 if "rev-parse" in a else ""))
    assert driver.verify_source(tmp_path, "a" * 40) == tmp_path


def test_private_environment_files_refuse_overwrite_and_control_bytes(tmp_path):
    target = tmp_path / "private.env"
    driver.write_env(target, {"KEY": "value"})
    assert target.read_text() == "KEY=value\n"
    with pytest.raises(FileExistsError):
        driver.write_env(target, {"KEY": "replaced"})
    with pytest.raises(driver.QualificationError, match="control"):
        driver.write_env(tmp_path / "bad.env", {"KEY": "value\nINJECTED=yes"})


def fake_docker(args, monkeypatch, *, fail=None):
    calls = []
    def command(command, *, check=True):
        calls.append(command)
        output = ""
        code = 0
        if command[:3] == ["docker", "image", "inspect"]:
            output = json.dumps([{"Id": args.candidate_image, "Config": {"Labels": {
                "org.opencontainers.image.revision": "0" * 40 if fail == "label" else args.candidate_sha,
            }}}])
        elif command[:3] == ["docker", "network", "inspect"] and command[-1] == args.service_network:
            output = json.dumps([{"Labels": {
                driver.SERVICES_LABEL: "wrong" if fail == "service-owner" else args.qualification_id,
            }}])
        elif "-c" in command:
            output = "0" * 40 if fail == "pin" else args.plane_commit
        elif "ls" in command:
            output = "exists" if fail == "label-collision" else ""
        elif "inspect" in command:
            code = 0 if fail == "name-collision" else 1
        elif "pg_isready" in command:
            code = 1 if fail == "database" else 0
        elif "/qualification-plane/scripts/import_staging_fixture.py" in command:
            output = json.dumps({"source_schema_revision": "066.001"})
        elif "/qualification-plane/scripts/migrate_qualification_database.py" in command:
            output = json.dumps({"target_revision": "088.003"})
        return subprocess.CompletedProcess(command, code, output, "")
    monkeypatch.setattr(driver, "run", command)
    monkeypatch.setattr(driver, "verify_source", lambda p, s: p)
    monkeypatch.setattr(driver.time, "sleep", lambda _: None)
    monkeypatch.setattr(driver, "wait_application", lambda port: {"status": "ready", "acceptance_qualified": False})
    return calls


@pytest.mark.parametrize("field,value,match", [
    ("qualification_id", "bad", "exact"),
    ("candidate_sha", "bad", "exact"),
    ("candidate_image", "latest", "immutable"),
    ("postgres_image", "latest", "immutable"),
    ("port", 80, "host port"),
    ("fixture_sha256", "bad", "fixture identity"),
    ("service_network", "invalid/network", "bounded explicit"),
])
def test_deploy_rejects_unbound_inputs(args, monkeypatch, field, value, match):
    fake_docker(args, monkeypatch)
    setattr(args, field, value)
    with pytest.raises(driver.QualificationError, match=match):
        driver.deploy(args)


@pytest.mark.parametrize("failure,match", [
    ("label", "revision label"), ("pin", "Plane pin"),
    ("label-collision", "already owns"), ("name-collision", "already exists"),
    ("database", "did not become ready"),
    ("service-owner", "service network is not owned"),
])
def test_existing_resources_and_failed_database_never_launch_application(args, monkeypatch, failure, match):
    calls = fake_docker(args, monkeypatch, fail=failure)
    with pytest.raises(driver.QualificationError, match=match):
        driver.deploy(args)
    assert not any(f"ad-bwq-{args.qualification_id}-app" in c and "create" in c for c in calls)
    assert not args.output.exists()
    assert not any("rm" in c or "down" in c for c in calls)


def test_deployment_binds_baseline_and_isolated_durable_roots(args, monkeypatch):
    calls = fake_docker(args, monkeypatch)
    report = driver.deploy(args)
    assert report["status"] == "started-acceptance-required"
    assert report["release_authorized"] is False
    assert report["scope"] == "backend-web"
    assert report["baseline"]["target_revision"] == "088.003"
    assert report == json.loads(args.output.read_text())
    app = next(c for c in calls if "create" in c and f"ad-bwq-{args.qualification_id}-app" in c)
    assert f"127.0.0.1:{args.port}:8001" in app
    assert all("astraldeep-postgres" not in str(c) for c in calls)
    assert all("--privileged" not in c for c in calls)
    assert len([c for c in calls if c[:3] == ["docker", "volume", "create"]]) == 7
    connect = ["docker", "network", "connect", args.service_network, f"ad-bwq-{args.qualification_id}-app"]
    start = ["docker", "start", f"ad-bwq-{args.qualification_id}-app"]
    assert calls.index(app) < calls.index(connect) < calls.index(start)
    assert any("dst=/run/astral-bwq,readonly" in part for part in app)
    assert_resource_limits(calls)
    with pytest.raises(driver.QualificationError, match="new file"):
        driver.deploy(args)


def test_cli_reports_only_diagnostic_status(args, monkeypatch, capsys):
    argv = [str(SCRIPT)]
    for name, value in vars(args).items():
        argv += ["--" + name.replace("_", "-"), str(value)]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(driver, "deploy", lambda _: {
        "qualification_id": args.qualification_id,
        "status": "started-acceptance-required", "release_authorized": False,
    })
    assert driver.main() == 0
    assert json.loads(capsys.readouterr().out)["release_authorized"] is False

    def fail(_):
        raise driver.QualificationError("safe contract refusal")

    monkeypatch.setattr(driver, "deploy", fail)
    assert driver.main() == 2
    assert "safe contract refusal" in capsys.readouterr().out

    def unexpected(_):
        raise OSError("sensitive runtime diagnostic")

    monkeypatch.setattr(driver, "deploy", unexpected)
    assert driver.main() == 2
    assert "sensitive" not in capsys.readouterr().out


def test_material_binding_and_paths(args):
    with pytest.raises(driver.QualificationError, match="absolute real"):
        driver.validate_material(Path("relative"), args.qualification_id)
    marker = args.material_root / "qualification.json"
    marker.write_text("{}", encoding="utf-8")
    with pytest.raises(driver.QualificationError, match="bound to this synthetic"):
        driver.validate_material(args.material_root, args.qualification_id)


@pytest.mark.parametrize("fault", [None, "ownership", "tamper", "state", "exit"])
def test_exact_baseline_state_and_paired_recovery_never_rewind_lets(args, monkeypatch, fault):
    args.baseline_deep_image = driver.BASELINE_IMAGE
    evidence = args.output.parent / "state"
    calls = []
    prefix = "ad-bwq-" + args.qualification_id
    checkpoint = {"owner": "synthetic-recovery-owner"}
    def command(command, *, check=True):
        calls.append(command)
        stdout = ""
        if command[:3] == ["docker", "start", "--attach"]:
            phase = command[-1].removeprefix(prefix + "-")
            report = {"checkpoint": checkpoint, "synthetic_only": True, "release_authorized": False,
                      "credential_decryption": "pass", "conversation_retention": "pass",
                      "owner_denials": "pass", "authenticated_audit_continuity": "pass", "appended_event_id": phase}
            if fault == "state":
                report["credential_decryption"] = "failed"
            (evidence / f"{phase}.json").write_text(json.dumps(report))
            if phase == "candidate-state" and fault == "tamper":
                (evidence / "baseline.dump").write_bytes(b"changed")
        elif command[:3] == ["docker", "inspect", "--format"]:
            stdout = "1" if fault == "exit" else "0"
        elif command[:3] == ["docker", "container", "inspect"]:
            stdout = json.dumps([{"Config": {"Labels": {driver.LABEL: "wrong" if fault == "ownership" else args.qualification_id}}}])
        elif command[:2] == ["docker", "cp"] and command[-1] == str(evidence / "baseline.dump"):
            (evidence / "baseline.dump").write_bytes(b"synthetic complete backup")
        elif "--create" in command:
            (evidence / "baseline-blobs.tar").write_bytes(b"synthetic paired blob archive")
        return subprocess.CompletedProcess(command, 0, stdout, "")
    monkeypatch.setattr(driver, "run", command)
    kwargs = dict(prefix=prefix, network=prefix + "-network", app_env=args.runtime_env, pg_env=args.runtime_env,
                  material=args.material_root, evidence=evidence, database="astralplane_qualification_" + args.qualification_id)
    if fault:
        with pytest.raises(driver.QualificationError):
            driver._state_rehearsal(args, **kwargs)
        assert not any("pg_restore" in call for call in calls)
        return
    report, roots = driver._state_rehearsal(args, **kwargs)
    assert report["status"] == "synthetic-state-and-paired-recovery-passed"
    assert report["lets_replay_authority_rewound"] is False
    assert set(report["backup_sha256"]) == {"baseline.dump", "baseline-blobs.tar"}
    assert roots == {name: prefix + "-recovered-" + name for name in ("data", "tmp", "knowledge", "agents")}
    restore = next(call for call in calls if "pg_restore" in call)
    assert "--single-transaction" in restore and "--exit-on-error" in restore
    assert "--clean" not in restore and restore[2] == prefix + "-recovered-pg"
    fresh = next(call for call in calls if "-d" in call and "--name" in call and prefix + "-recovered-pg" in call)
    assert "--network-alias" in fresh and prefix + "-pg" in fresh
    assert report["restore_target"] == "new-empty-postgres-cluster"
    baseline = next(call for call in calls if "create" in call and prefix + "-baseline-state" in call)
    assert not any("dst=/app/backend/agents" in part for part in baseline)
    assert report["legacy_mutable_generated_agent_directories_rehearsed"] is False
    assert any("--entrypoint" in call and "true" in call and args.candidate_image in call
               and any("dst=/app/backend/agents" in part for part in call) for call in calls)
    assert all("psql" not in call and "--privileged" not in call for call in calls)
    before_restore = calls[:calls.index(restore)]
    assert any(prefix + "-candidate-state" in call and "--attach" in call for call in before_restore)
    assert not any("lets-replay" in part or "lets-authority" in part for call in calls if "tar" in call for part in call)
    assert_resource_limits(calls)


def assert_resource_limits(calls):
    for command in calls:
        if command[:2] not in (["docker", "run"], ["docker", "create"]):
            continue
        limits = driver.POSTGRES_LIMITS if "-d" in command and "--name" in command else driver.APP_LIMITS
        assert command[2:8] == limits


@pytest.mark.parametrize("failure", [None, "http", "oversized", "malformed", "wrong-shape", "lets-off", "lets-blocked"])
def test_application_readiness_is_bounded_enforce_only(monkeypatch, failure):
    connections = []
    class Connection:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("127.0.0.1", 18091, 3)
            self.status = 503 if failure == "http" else 200
            self.closed = False
            connections.append(self)
        def request(self, method, path):
            assert (method, path) == ("GET", "/readyz")
        def getresponse(self):
            return self
        def read(self, size):
            assert size == 65537
            if failure == "oversized":
                return b"x" * size
            if failure == "malformed":
                return b"bad"
            if failure == "wrong-shape":
                return b"[]"
            return json.dumps({"status": "ok", "db": "ok", "lets": {
                "mode": "off" if failure == "lets-off" else "enforce",
                "status": "healthy", "governed_dispatch_ready": failure != "lets-blocked",
            }}).encode()
        def close(self):
            self.closed = True
    monkeypatch.setattr(driver.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(driver.time, "sleep", lambda _: None)
    if failure:
        with pytest.raises(driver.QualificationError, match="LETS-enforce readiness"):
            driver.wait_application(18091, attempts=2)
        assert len(connections) == 2
    else:
        assert driver.wait_application(18091)["acceptance_qualified"] is False
        assert len(connections) == 1
    assert all(connection.closed for connection in connections)


def test_application_connection_failure_is_bounded(monkeypatch):
    class Connection:
        def request(self, *args):
            raise OSError("private transport details")
        def close(self):
            pass
    monkeypatch.setattr(driver.http.client, "HTTPConnection", lambda *a, **k: Connection())
    monkeypatch.setattr(driver.time, "sleep", lambda _: None)
    with pytest.raises(driver.QualificationError, match="LETS-enforce readiness"):
        driver.wait_application(18091, attempts=1)


def test_baseline_image_is_exact_and_recovered_roots_feed_final_application(args, monkeypatch):
    calls = fake_docker(args, monkeypatch)
    original = driver.run
    args.baseline_deep_image = driver.BASELINE_IMAGE
    def command(command, **kwargs):
        if command[:3] == ["docker", "image", "inspect"] and command[-1] == driver.BASELINE_IMAGE:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, json.dumps([{"Id": driver.BASELINE_IMAGE}]), "")
        if "-c" in command and driver.BASELINE_IMAGE in command:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, driver.BASELINE_PLANE, "")
        return original(command, **kwargs)
    monkeypatch.setattr(driver, "run", command)
    monkeypatch.setattr(driver, "_state_rehearsal", lambda *a, **kw: ({"status": "verified", "application_network": "new-private-recovery-network"}, {"data": "new-recovered-data"}))
    report = driver.deploy(args)
    assert report["state_rehearsal"]["status"] == "verified"
    app = next(call for call in calls if "create" in call and "ad-bwq-" + args.qualification_id + "-app" in call)
    assert "type=volume,src=new-recovered-data,dst=/app/backend/data" in app
    assert "new-private-recovery-network" in app
