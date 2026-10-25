"""Tests for scripts/run_backend_web_qualification.py: deployment isolation, exact
component identity, fail-closed Docker failures, and baseline/recovery binding.
"""

from __future__ import annotations

import importlib.util
import hashlib
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
PRODUCTION_IMAGE = "ghcr.io/astraldeep/astraldeep:sha-ca822e68d5477fb60fa9076107a4dab4138eeda6@sha256:802244a5eb1a715dff4fa587a0e7d447b9eb425f1b674fc48cf0e685ced18aa2"
PRODUCTION_CONFIG = "sha256:c3c72f5685d7d880fc5c84a024a47d43a5760f890fd9bc7ce906b4539dba1d41"
PRODUCTION_COMMIT = "ca822e68d5477fb60fa9076107a4dab4138eeda6"
PRODUCTION_PLANE = "5ecce4cc2bd97ea26e348f2f7c8809985b9713bf"
PRODUCTION_MIGRATION = "08b7d824a62284351d1ce178708bb185d7c81f77c96ab14e890f7e5675a008fc"
PRODUCTION_COMPOSITION = "9ac9e3d362af7800401f499014e75662d24b3303f72cf1696abcfd070bfbd59e"


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
    assert report["synthetic_predecessor_migration"]["target_revision"] == "088.003"
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


@pytest.mark.parametrize("fault", [None, "ownership", "tamper", "state", "exit", "blob", "schema", "append", "discard", "checkpoint",
                                   "output-exists", "output-symlink", "output-directory", "copy-failure",
                                   "archive-exit", "archive-output-exists", "archive-output-symlink",
                                   "archive-output-directory", "archive-copy-failure", "archive-tamper", "archive-late-output"])
def test_exact_baseline_state_and_paired_recovery_never_rewind_lets(args, monkeypatch, fault):
    args.baseline_deep_image = PRODUCTION_IMAGE
    evidence = args.output.parent / "state"
    calls = []
    prefix = "ad-bwq-" + args.qualification_id
    checkpoint = {"owner": "synthetic-recovery-owner", "audit_event_ids": ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"]}
    candidate_event = "00000000-0000-4000-8000-000000000003"
    container_outputs = {}
    def command(command, *, check=True):
        calls.append(command)
        stdout = ""
        if command[:3] == ["docker", "start", "--attach"] and command[-1] == prefix + "-baseline-blob-archive":
            container_outputs[command[-1] + ":/tmp/baseline-blobs.tar"] = b"synthetic paired blob archive"
            if fault == "archive-late-output":
                (evidence / "baseline-blobs.tar").write_bytes(b"preserve original")
        elif command[:3] == ["docker", "start", "--attach"]:
            phase = command[-1].removeprefix(prefix + "-")
            report = {"checkpoint": checkpoint, "synthetic_only": True, "release_authorized": False,
                      "credential_decryption": "pass", "conversation_retention": "pass",
                      "owner_denials": "pass", "authenticated_audit_continuity": "pass", "blob_retention": "pass",
                      "schema": {"revision": "089.004", "migration_sha256": PRODUCTION_MIGRATION},
                      "appended_event_id": candidate_event if phase == "candidate-state" else "00000000-0000-4000-8000-000000000004",
                      "pre_append_audit_event_ids": list(checkpoint["audit_event_ids"])}
            if fault == "state":
                report["credential_decryption"] = "failed"
            if fault == "blob":
                report["blob_retention"] = "failed"
            if fault == "schema":
                report["schema"]["revision"] = "088.003"
            if fault == "append" and phase == "candidate-state":
                report["appended_event_id"] = checkpoint["audit_event_ids"][0]
            if fault == "discard" and phase == "recovered-state":
                report["pre_append_audit_event_ids"].append(candidate_event)
            if fault == "checkpoint" and phase == "recovered-state":
                report["checkpoint"] = {"owner": "changed-owner"}
            container_outputs[f"{command[-1]}:/tmp/{phase}.json"] = json.dumps(report)
            if fault == "output-exists":
                (evidence / f"{phase}.json").write_text("preserve existing report")
            if fault == "output-symlink":
                (evidence / f"{phase}.json").symlink_to(evidence / "baseline-image.json")
            if fault == "output-directory":
                (evidence / f"{phase}.json").mkdir()
            if phase == "candidate-state" and fault == "tamper":
                (evidence / "baseline.dump").write_bytes(b"changed")
            if phase == "candidate-state" and fault == "archive-tamper":
                (evidence / "baseline-blobs.tar").write_bytes(b"changed")
            if phase == "baseline-state":
                if fault == "archive-output-exists":
                    (evidence / "baseline-blobs.tar").write_bytes(b"preserve original")
                elif fault == "archive-output-symlink":
                    (evidence / "baseline-blobs.tar").symlink_to(evidence / "baseline-image.json")
                elif fault == "archive-output-directory":
                    (evidence / "baseline-blobs.tar").mkdir()
        elif command[:3] == ["docker", "inspect", "--format"]:
            stdout = "1" if fault == "exit" or (fault == "archive-exit" and command[-1].endswith("-baseline-blob-archive")) else "0"
        elif command[:3] == ["docker", "container", "inspect"]:
            stdout = json.dumps([{"Config": {"Labels": {driver.LABEL: "wrong" if fault == "ownership" else args.qualification_id}}}])
        elif command[:2] == ["docker", "cp"] and command[-1] == str(evidence / "baseline.dump"):
            (evidence / "baseline.dump").write_bytes(b"synthetic complete backup")
        elif command[:2] == ["docker", "cp"] and command[2] in container_outputs:
            if fault == "copy-failure" or (fault == "archive-copy-failure" and command[2].endswith("/baseline-blobs.tar")):
                raise driver.QualificationError("copy failed")
            value = container_outputs[command[2]]
            Path(command[-1]).write_bytes(value if isinstance(value, bytes) else value.encode())
        return subprocess.CompletedProcess(command, 0, stdout, "")
    monkeypatch.setattr(driver, "run", command)
    kwargs = dict(prefix=prefix, network=prefix + "-network", app_env=args.runtime_env, pg_env=args.runtime_env,
                  material=args.material_root, evidence=evidence, database="astralplane_qualification_" + args.qualification_id,
                  baseline_profile=driver.load_recovery_baseline())
    if fault:
        with pytest.raises(driver.QualificationError):
            driver._state_rehearsal(args, **kwargs)
        if fault not in {"discard", "checkpoint"}:
            assert not any("pg_restore" in call for call in calls)
        if fault in {"exit", "output-exists", "output-symlink", "output-directory"}:
            assert not any(call[:2] == ["docker", "cp"] and call[2] in container_outputs for call in calls)
        if fault == "output-exists":
            assert (evidence / "baseline-state.json").read_text() == "preserve existing report"
        if fault.startswith("archive-output-") or fault in {"archive-exit", "archive-late-output"}:
            assert not any(call[:2] == ["docker", "cp"] and call[2].endswith(":/tmp/baseline-blobs.tar") for call in calls)
        if fault.startswith("archive-output-"):
            assert not any(call[:2] == ["docker", "create"] and prefix + "-baseline-blob-archive" in call for call in calls)
        if fault in {"archive-output-exists", "archive-late-output"}:
            assert (evidence / "baseline-blobs.tar").read_bytes() == b"preserve original"
        return
    report, roots = driver._state_rehearsal(args, **kwargs)
    assert report["status"] == "synthetic-state-and-paired-recovery-passed"
    assert report["lets_replay_authority_rewound"] is False
    assert set(report["backup_sha256"]) == {"baseline.dump", "baseline-blobs.tar"}
    assert report["backup_sha256"]["baseline-blobs.tar"] == hashlib.sha256(b"synthetic paired blob archive").hexdigest()
    assert roots == {name: prefix + "-recovered-" + name for name in ("data", "tmp", "knowledge", "agents")}
    restore = next(call for call in calls if "pg_restore" in call)
    assert "--single-transaction" in restore and "--exit-on-error" in restore
    assert "--clean" not in restore and restore[2] == prefix + "-recovered-pg"
    fresh = next(call for call in calls if "-d" in call and "--name" in call and prefix + "-recovered-pg" in call)
    assert "--network-alias" in fresh and prefix + "-pg" in fresh
    assert report["restore_target"] == "new-empty-postgres-cluster"
    assert report["candidate_schema_changed"] is False
    archived = next(call for call in calls if call[:2] == ["docker", "create"] and prefix + "-baseline-blob-archive" in call)
    assert "--network" in archived and archived[archived.index("--network") + 1] == "none"
    assert "--user" not in archived and driver.LABEL + "=" + args.qualification_id in archived
    assert "--file=/tmp/baseline-blobs.tar" in archived
    archive_mounts = [archived[index + 1] for index, value in enumerate(archived) if value == "--mount"]
    assert len(archive_mounts) == 4 and all(mount.startswith("type=volume,") and mount.endswith(",readonly") for mount in archive_mounts)
    archive_copy = ["docker", "cp", prefix + "-baseline-blob-archive:/tmp/baseline-blobs.tar", str(evidence / "baseline-blobs.tar")]
    archive_exit = ["docker", "inspect", "--format", "{{.State.ExitCode}}", prefix + "-baseline-blob-archive"]
    assert calls.index(archive_exit) < calls.index(archive_copy)
    assert (evidence / "baseline-blobs.tar").stat().st_uid == os.getuid()
    assert (evidence / "baseline-blobs.tar").stat().st_mode & 0o777 == 0o600
    for phase in ("baseline-state", "candidate-state", "recovered-state"):
        created = next(call for call in calls if "create" in call and prefix + "-" + phase in call)
        assert f"type=bind,src={evidence},dst=/qualification-evidence,readonly" in created
        assert created[created.index("--output") + 1] == f"/tmp/{phase}.json"
        assert "--user" not in created
        copied = ["docker", "cp", f"{prefix}-{phase}:/tmp/{phase}.json", str(evidence / f"{phase}.json")]
        inspected = ["docker", "inspect", "--format", "{{.State.ExitCode}}", prefix + "-" + phase]
        assert calls.index(inspected) < calls.index(copied)
        assert (evidence / f"{phase}.json").stat().st_mode & 0o777 == 0o600
        assert (evidence / f"{phase}.json").stat().st_uid == os.getuid()
    recovered = next(call for call in calls if "create" in call and prefix + "-recovered-state" in call)
    assert recovered[recovered.index("--forbid-audit-event") + 1] == candidate_event
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


def test_preexisting_state_output_refuses_before_creating_a_container(args, monkeypatch):
    evidence = args.output.parent / "state"
    original = Path.mkdir
    def mkdir(path, *values, **kwargs):
        original(path, *values, **kwargs)
        if path == evidence:
            (path / "baseline-state.json").write_text("preserve original")
    monkeypatch.setattr(Path, "mkdir", mkdir)
    calls = []
    monkeypatch.setattr(driver, "run", lambda *args, **kwargs: calls.append(args))
    args.baseline_deep_image = PRODUCTION_IMAGE
    with pytest.raises(driver.QualificationError, match="destination must be new"):
        driver._state_rehearsal(args, prefix="ad-bwq-" + args.qualification_id, network="private-network",
            app_env=args.runtime_env, pg_env=args.runtime_env, material=args.material_root,
            evidence=evidence, database="astralplane_qualification_" + args.qualification_id,
            baseline_profile=driver.load_recovery_baseline())
    assert calls == []
    assert (evidence / "baseline-state.json").read_text() == "preserve original"


def test_blob_archive_container_collision_refuses_before_resource_creation(args, monkeypatch):
    calls = fake_docker(args, monkeypatch)
    original = driver.run
    def command(command, **kwargs):
        if command[:3] == ["docker", "container", "inspect"] and command[-1].endswith("-baseline-blob-archive"):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        return original(command, **kwargs)
    monkeypatch.setattr(driver, "run", command)
    with pytest.raises(driver.QualificationError, match="already exists"):
        driver.deploy(args)
    assert not any(call[:3] in (["docker", "network", "create"], ["docker", "volume", "create"])
                   or call[:2] == ["docker", "create"] for call in calls)


@pytest.mark.parametrize("fault", ["empty", "oversized", "symlink", "directory", "wrong-owner", "changed-size", "malformed", "encoding", "non-object", "missing"])
def test_copied_state_output_rejects_unsafe_ownership_shape_or_bytes(tmp_path, monkeypatch, fault):
    path = tmp_path / "state.json"
    path.write_text('{"synthetic_only":true}')
    if fault == "empty":
        path.write_bytes(b"")
    elif fault == "oversized":
        with path.open("wb") as stream:
            stream.truncate(65537)
    elif fault == "symlink":
        target = tmp_path / "original.json"
        path.rename(target)
        path.symlink_to(target)
    elif fault == "directory":
        path.unlink()
        path.mkdir()
    elif fault in {"wrong-owner", "changed-size"}:
        original = os.fstat
        def metadata(descriptor):
            value = original(descriptor)
            return SimpleNamespace(st_mode=value.st_mode,
                st_uid=value.st_uid + 1 if fault == "wrong-owner" else value.st_uid,
                st_size=value.st_size - 1 if fault == "changed-size" else value.st_size)
        monkeypatch.setattr(os, "fstat", metadata)
    elif fault == "malformed":
        path.write_bytes(b"{invalid")
    elif fault == "encoding":
        path.write_bytes(b"\xff")
    elif fault == "non-object":
        path.write_text("[]")
    else:
        path.unlink()
    with pytest.raises(driver.QualificationError, match="copied state report"):
        driver._read_state_output(path)


def test_copied_state_output_is_read_from_owned_descriptor_and_made_private(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"synthetic_only":true}')
    path.chmod(0o644)
    assert driver._read_state_output(path) == {"synthetic_only": True}
    assert path.stat().st_uid == os.getuid()
    assert path.stat().st_mode & 0o777 == 0o600


def test_archive_hash_streams_more_than_one_json_report_or_chunk(tmp_path):
    path = tmp_path / "baseline-blobs.tar"
    payload = b"synthetic archive block" * 65536
    path.write_bytes(payload)
    path.chmod(0o644)
    assert driver._copied_file_digest(path, maximum=driver.MAX_BLOB_ARCHIVE_BYTES) == hashlib.sha256(payload).hexdigest()
    assert path.stat().st_uid == os.getuid() and path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("fault", ["empty", "oversized", "symlink", "directory", "wrong-owner", "grew", "shrunk"])
def test_copied_archive_refuses_unsafe_owner_path_size_or_mutation(tmp_path, monkeypatch, fault):
    path = tmp_path / "baseline-blobs.tar"
    path.write_bytes(b"archive")
    maximum = None
    if fault == "empty":
        path.write_bytes(b"")
    elif fault == "oversized":
        maximum = 6
    elif fault == "symlink":
        original = tmp_path / "original.tar"
        path.rename(original)
        path.symlink_to(original)
    elif fault == "directory":
        path.unlink()
        path.mkdir()
    else:
        original = os.fstat
        def metadata(descriptor):
            value = original(descriptor)
            return SimpleNamespace(st_mode=value.st_mode,
                st_uid=value.st_uid + 1 if fault == "wrong-owner" else value.st_uid,
                st_size=value.st_size + (1 if fault == "shrunk" else -1) if fault in {"grew", "shrunk"} else value.st_size)
        monkeypatch.setattr(os, "fstat", metadata)
    with pytest.raises(driver.QualificationError, match="copied paired backup"):
        driver._copied_file_digest(path, maximum=maximum)


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


def production_docker(args, monkeypatch, failure=None):
    calls = fake_docker(args, monkeypatch)
    original = driver.run
    args.baseline_deep_image = PRODUCTION_IMAGE
    def command(command, **kwargs):
        if command[:3] == ["docker", "image", "inspect"] and command[-1] == PRODUCTION_IMAGE:
            calls.append(command)
            image = {"Id": PRODUCTION_CONFIG,
                "Os": "linux", "Architecture": "amd64", "Config": {"Labels": {"org.opencontainers.image.revision": PRODUCTION_COMMIT,
                    "org.opencontainers.image.source": "https://github.com/AstralDeep/AstralDeep"}},
                "RepoDigests": ["ghcr.io/astraldeep/astraldeep@" + PRODUCTION_IMAGE.split("@", 1)[1]]}
            if failure in {"Id", "Os", "Architecture", "RepoDigests"}:
                image[failure] = [] if failure == "RepoDigests" else "changed"
            if failure in {"revision", "source"}:
                image["Config"]["Labels"]["org.opencontainers.image." + failure] = "changed"
            return subprocess.CompletedProcess(command, 0, json.dumps([image]), "")
        if "-c" in command and PRODUCTION_IMAGE in command:
            calls.append(command)
            manifest = {"manifest_sha256": PRODUCTION_COMPOSITION,
                "composition": {"components": {"astral-plane": {"commit": PRODUCTION_PLANE}},
                    "compatibility": {"data_plane": {"schema_revision": "089.004", "migration_sha256": PRODUCTION_MIGRATION}}}}
            if failure == "composition":
                manifest["manifest_sha256"] = "f" * 64
            if failure == "plane":
                manifest["composition"]["components"]["astral-plane"]["commit"] = "f" * 40
            if failure in {"schema_revision", "migration_sha256"}:
                manifest["composition"]["compatibility"]["data_plane"][failure] = "changed"
            return subprocess.CompletedProcess(command, 0, json.dumps(manifest), "")
        if PRODUCTION_IMAGE in command and "/app/scripts/install_local_components.py" in command and failure == "components":
            raise driver.QualificationError("baseline installed component verification failed")
        return original(command, **kwargs)
    monkeypatch.setattr(driver, "run", command)
    return calls


@pytest.mark.parametrize("failure", ["Id", "Os", "Architecture", "RepoDigests", "revision", "source", "composition", "plane", "schema_revision", "migration_sha256", "components", "substitution"])
def test_production_baseline_refuses_every_identity_mismatch_before_app_resources(args, monkeypatch, failure):
    calls = production_docker(args, monkeypatch, failure)
    if failure == "substitution":
        args.baseline_deep_image = "sha256:" + "f" * 64
    with pytest.raises(driver.QualificationError):
        driver.deploy(args)
    assert not any(call[:3] in (["docker", "network", "create"], ["docker", "volume", "create"])
                   or call[:2] == ["docker", "create"] for call in calls)
    assert not args.output.exists()


def test_baseline_image_is_exact_and_recovered_roots_feed_final_application(args, monkeypatch):
    calls = production_docker(args, monkeypatch)
    monkeypatch.setattr(driver, "_state_rehearsal", lambda *a, **kw: ({"status": "verified", "application_network": "new-private-recovery-network"}, {"data": "new-recovered-data"}))
    report = driver.deploy(args)
    assert report["state_rehearsal"]["status"] == "verified"
    assert report["production_baseline"]["deep_commit"] == PRODUCTION_COMMIT
    assert report["production_baseline"]["image_ref"] == PRODUCTION_IMAGE
    assert report["synthetic_predecessor"]["plane_commit"] == "4a07d59a448c1960ce2ae3f35e605f4d78c4a3f9"
    assert report["synthetic_predecessor"]["schema_revision"] == "088.003"
    app = next(call for call in calls if "create" in call and "ad-bwq-" + args.qualification_id + "-app" in call)
    assert "type=volume,src=new-recovered-data,dst=/app/backend/data" in app
    assert "new-private-recovery-network" in app


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("deep_commit", "bad"), ("plane_commit", "bad"),
    ("image_config", "latest"), ("composition_sha256", "bad"), ("migration_sha256", "bad"),
    ("platform", "linux/arm64"), ("schema_revision", "bad"), ("image_ref", "sha256:" + "f" * 64),
    ("deep_commit", None), ("unexpected", "extra")])
def test_recovery_profile_is_closed_and_loaded_only_from_policy_root(tmp_path, monkeypatch, field, value):
    profile = driver.load_recovery_baseline()
    assert profile["image_ref"] == PRODUCTION_IMAGE and profile["image_config"] == PRODUCTION_CONFIG
    policy = tmp_path / "policy"
    (policy / "config").mkdir(parents=True)
    candidate = tmp_path / "candidate"
    (candidate / "config").mkdir(parents=True)
    (candidate / "config/backend-web-recovery-baseline.json").write_text('{"image_ref":"forged"}')
    path = policy / "config/backend-web-recovery-baseline.json"
    path.write_text(json.dumps(profile))
    monkeypatch.setattr(driver, "POLICY_ROOT", policy)
    assert driver.load_recovery_baseline() == profile
    path.write_text(json.dumps(profile | {field: value}))
    with pytest.raises(driver.QualificationError, match="trusted recovery"):
        driver.load_recovery_baseline()
