"""Tests for scripts/backend_web_service_materials.py and
initialize_backend_web_services.py: synthetic material generation, private-directory
ownership, and LiveKit/Postgres network isolation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization

from scripts import backend_web_service_materials as materials
from scripts import initialize_backend_web_services as services

ROOT = Path(__file__).resolve().parents[2]
QID = "a" * 32
SHA = "b" * 40
IMAGE = "sha256:" + "c" * 64


@pytest.fixture
def ownership(monkeypatch):
    changes = []
    monkeypatch.setattr(materials, "_require_posix", lambda: None)
    monkeypatch.setattr(services, "_require_posix", lambda: None)
    monkeypatch.setattr(
        os,
        "chown",
        lambda path, uid, gid: changes.append((Path(path), uid, gid)),
        raising=False,
    )
    monkeypatch.setattr(os, "getuid", lambda: 1234, raising=False)
    return changes


@pytest.fixture
def generated(tmp_path, ownership):
    root = tmp_path / "materials"
    root.mkdir()
    result = materials.generate_materials(
        root, QID, ROOT / "components/LETS", operator_uid=1234
    )
    return root, result, ownership


def test_materials_bind_real_tls_owner_subjects_scopes_and_fresh_keys(generated):
    root, report, owners = generated
    assert report["qualification_id"] == QID
    assert report["voice_status"] == "operator-input-required"
    assert set(report["public_certificate_sha256"]) == {
        "app/iam-ca.pem",
        "app/lets-ca.pem",
        "proxy/server-cert.pem",
        "proxy/livekit-cert.pem",
        "livekit/turn-cert.pem",
    }
    realm = json.loads(
        (
            root / "keycloak/import" / (report["keycloak_realm"] + "-realm.json")
        ).read_text()
    )
    assert {user["id"] for user in realm["users"]} == {
        "fixture-owner-a",
        "fixture-owner-b",
        "fixture-administrator",
    }
    assert not realm["registrationAllowed"] and realm["sslRequired"] == "all"
    for client in realm["clients"]:
        assert not client["directAccessGrantsEnabled"]
        assert not client["publicClient"]
        assert "tools:read" in client["optionalClientScopes"]
        assert all(
            not scope.startswith("tools:") for scope in client["defaultClientScopes"]
        )
    env = dict(
        line.split("=", 1)
        for line in (root / "runtime-fragment.env").read_text().splitlines()
    )
    assert env["ASTRAL_ENV"] == "production" and env["LETS_MODE"] == "enforce"
    assert env["USE_MOCK_AUTH"] == "false" and env["FF_LETS_EXTERNAL_WARDEN"] == "true"
    assert env["VOICE_SPEECH_BACKEND"] == "llm_factory"
    assert env["FF_CONVERSATIONAL_VOICE"] == "true"
    assert env["VOICE_COORDINATOR_REPLICA_ID"] == "bwq-coordinator-" + QID
    assert (
        env["VOICE_WATCH_BRIDGE_PUBLIC_URL"]
        == "wss://ad-bwq-web:9443/api/voice/watch-bridge"
    )
    from orchestrator.livekit_service import LiveKitSettings

    settings = LiveKitSettings.from_environ(env)
    assert settings.public_url == "wss://ad-bwq-livekit:9444"
    assert settings.internal_url == "https://ad-bwq-livekit:9444"
    assert env["LIVEKIT_API_SECRET"] not in json.dumps(report)
    assert (root / "livekit/runtime.env").read_text().strip() == "LIVEKIT_KEYS=" + env[
        "LIVEKIT_API_KEY"
    ] + ": " + env["LIVEKIT_API_SECRET"]
    assert "VOICE_SPEECH_API_KEY" not in env
    for name in (
        "CREDENTIAL_ENCRYPTION_KEY",
        "WEB_SESSION_ENC_KEY",
        "OFFLINE_GRANT_ENC_KEY",
    ):
        cipher = Fernet(env[name])
        assert (
            cipher.decrypt(cipher.encrypt(b"retained-synthetic-record"))
            == b"retained-synthetic-record"
        )
        assert env[name] not in json.dumps(report)
    assert (
        len(
            {
                env[name]
                for name in (
                    "AUDIT_HMAC_SECRET",
                    "MEMORY_HMAC_KEY",
                    "VOICE_CONTROL_SECRET",
                )
            }
        )
        == 3
    )
    ca = x509.load_pem_x509_certificate((root / "app/iam-ca.pem").read_bytes())
    cert = x509.load_pem_x509_certificate((root / "proxy/server-cert.pem").read_bytes())
    cert.verify_directly_issued_by(ca)
    assert cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName) == ["ad-bwq-web"]
    key = serialization.load_pem_private_key(
        (root / "proxy/server-key.pem").read_bytes(), password=None
    )
    assert key.public_key().public_numbers() == cert.public_key().public_numbers()
    for path, hostname in (
        ("proxy/livekit-cert.pem", "ad-bwq-livekit"),
        ("livekit/turn-cert.pem", "ad-bwq-turn"),
    ):
        media_cert = x509.load_pem_x509_certificate((root / path).read_bytes())
        media_cert.verify_directly_issued_by(ca)
        assert media_cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName) == [hostname]
    marker = json.loads((root / "app/qualification.json").read_text())
    assert marker == {
        "schema_version": 1,
        "qualification_id": QID,
        "classification": "synthetic",
    }
    assert (root / "init_warden.py", 10001, 10001) in owners
    assert (root / "keycloak", 1000, 1000) in owners
    assert (root / "app", 1234, 1234) in owners
    initializer = (root / "init_warden.py").read_text()
    assert 'staged.setdefault("peer_endpoints", {})' in initializer
    assert "lets-provider" in initializer and '"--production"' in initializer
    assert "https://identity.production-acceptance" not in initializer
    assert (
        "proxy_set_header Upgrade $http_upgrade"
        in (root / "proxy/nginx.conf").read_text()
    )
    assert "proxy_request_buffering off" in (root / "proxy/nginx.conf").read_text()
    assert "proxy_buffering off" in (root / "proxy/nginx.conf").read_text()


@pytest.mark.parametrize(
    "qid,uid,message",
    [
        ("bad", 0, "qualification ID"),
        (QID, -1, "operator UID"),
        (QID, True, "operator UID"),
    ],
)
def test_materials_reject_invalid_identity_before_writing(
    tmp_path, ownership, qid, uid, message
):
    with pytest.raises(ValueError, match=message):
        materials.generate_materials(
            tmp_path, qid, ROOT / "components/LETS", operator_uid=uid
        )
    assert not list(tmp_path.iterdir())


def test_materials_reject_relative_nonempty_and_changed_upstream(tmp_path, ownership):
    with pytest.raises(ValueError, match="absolute real"):
        materials.generate_materials(Path("relative"), QID, ROOT)
    (tmp_path / "existing").write_text("retained")
    with pytest.raises(ValueError, match="canonical and empty"):
        materials.generate_materials(tmp_path, QID, ROOT)
    bad_source = tmp_path / "source/deploy/production/acceptance"
    bad_source.mkdir(parents=True)
    (bad_source / "signer_helper.py").write_text("unused")
    (bad_source / "init_node.py").write_text("unknown-contract")
    destination = tmp_path / "new"
    destination.mkdir()
    with pytest.raises(ValueError, match="initializer contract"):
        materials.generate_materials(destination, QID, tmp_path / "source")


def test_private_directory_requires_exact_owner_mode_and_no_existing_state(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1234, raising=False)
    path = MagicMock(spec=Path)
    path.is_absolute.return_value = True
    path.is_symlink.return_value = False
    path.is_dir.return_value = True
    path.resolve.return_value = path
    path.stat.return_value = SimpleNamespace(st_mode=0o40700, st_uid=1234)
    path.iterdir.return_value = iter(())
    services.private_directory(path, empty=True)
    path.is_symlink.return_value = True
    with pytest.raises(ValueError, match="canonical"):
        services.private_directory(path)
    path.is_symlink.return_value = False
    path.stat.return_value.st_mode = 0o40755
    with pytest.raises(ValueError, match="private"):
        services.private_directory(path)
    path.stat.return_value.st_mode = 0o40700
    path.iterdir.return_value = iter(("retained",))
    with pytest.raises(ValueError, match="reuse"):
        services.private_directory(path, empty=True)


@pytest.fixture
def setup(tmp_path, ownership, monkeypatch):
    source = tmp_path / "source"
    (source / "components/LETS").mkdir(parents=True)
    (source / "scripts").mkdir()
    for name in (
        "initialize_backend_web_services.py",
        "backend_web_service_materials.py",
        "wait_backend_web_services.py",
    ):
        (source / "scripts" / name).write_text("reviewed source")
    target = tmp_path / "materials"
    target.mkdir()
    monkeypatch.setattr(services, "private_directory", lambda *args, **kwargs: None)
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        output, status = b"", 0
        if args[0] == "git" and "rev-parse" in args:
            output = (
                services.LETS_COMMIT if args[2].endswith("LETS") else SHA
            ).encode()
        elif args[:3] == ["docker", "image", "inspect"]:
            output = json.dumps([{"Id": IMAGE}]).encode()
        elif args[:3] == ["docker", "network", "inspect"] and not kwargs.get(
            "missing_ok"
        ):
            output = json.dumps([network_details()]).encode()
        elif "inspect" in args:
            status = 1
        elif "/policy/scripts/backend_web_service_materials.py" in args:
            (target / "public-summary.json").write_text(
                json.dumps({"qualification_id": QID, "classification": "synthetic"})
            )
            (target / "runtime-fragment.env").write_text(
                "VOICE_CONTROL_SECRET=" + "s" * 64 + "\n"
            )
            (target / "livekit").mkdir()
        elif "/wait.py" in args:
            output = json.dumps(
                {
                    "qualification_id": QID,
                    "status": "infrastructure-ready",
                    "application_acceptance": False,
                }
            ).encode()
        return subprocess.CompletedProcess(args, status, output, b"sensitive stderr")

    monkeypatch.setattr(services, "command", execute)
    kwargs = dict(
        qualification_id=QID,
        source=source,
        material_root=target,
        candidate_commit=SHA,
        candidate_image=IMAGE,
        policy_root=source,
        policy_commit=SHA,
        materializer_image="sha256:" + "e" * 64,
        lets_image="sha256:" + "d" * 64,
    )
    return kwargs, calls, execute


def test_initializer_uses_only_new_labelled_private_resources_and_never_claims_acceptance(
    setup,
):
    kwargs, calls, _ = setup
    report = services.initialize(**kwargs)
    assert report["status"] == "started-acceptance-pending"
    assert report["service_network"] == "ad-bwq-services-" + QID
    assert report["material_root"].endswith("app")
    assert "sensitive stderr" not in json.dumps(report)
    assert len(report["source_sha256"]) == 3
    first_mutation = next(i for i, call in enumerate(calls) if "run" in call)
    assert sum("inspect" in call for call in calls[:first_mutation]) == 19
    running = [call for call in calls if call[:3] == ["docker", "run", "-d"]]
    assert len(running) == 5
    for call in running:
        assert "--label" in call and services.LABEL + "=" + QID in call
        assert "--publish" not in call and "-p" not in call
        assert "--env" not in call and not any("PASSWORD=" in value for value in call)
        assert "--cpus" in call and "--memory" in call
    warden = running[2]
    assert (
        "--production" in warden and "--client-ca" in warden and "--read-only" in warden
    )
    assert not any("astraldeep-postgres" in value for call in calls for value in call)
    media = running[-1]
    assert services.LIVEKIT_IMAGE in media and "--ip" in media and "--cap-drop" in media
    assert "--network-alias" in media and "ad-bwq-turn" in media
    assert report["livekit"]["node_ipv4"] == "172.30.0.254"
    assert report["infrastructure"]["application_acceptance"] is False


@pytest.mark.parametrize(
    "key,value,message",
    [
        ("qualification_id", "bad", "qualification ID"),
        ("candidate_commit", "bad", "candidate commit"),
        ("candidate_image", "mutable:latest", "immutable"),
        ("source", Path("relative"), "absolute canonical"),
    ],
)
def test_initializer_refuses_invalid_inputs_before_mutation(setup, key, value, message):
    kwargs, calls, _ = setup
    kwargs[key] = value
    with pytest.raises(ValueError, match=message):
        services.initialize(**kwargs)
    assert all("run" not in call for call in calls)


@pytest.mark.parametrize("kind", ["wrong-sha", "dirty", "collision", "marker"])
def test_initializer_fails_closed_on_source_collision_or_marker(
    setup, monkeypatch, kind
):
    kwargs, calls, execute = setup

    def reject(args, **options):
        result = execute(args, **options)
        if kind == "wrong-sha" and "rev-parse" in args:
            result.stdout = b"f" * 40
        if kind == "dirty" and "status" in args:
            result.stdout = b" M source.py"
        if kind == "collision" and args[:3] == ["docker", "container", "inspect"]:
            result.returncode = 0
        if (
            kind == "marker"
            and "/policy/scripts/backend_web_service_materials.py" in args
        ):
            (kwargs["material_root"] / "public-summary.json").write_text(
                '{"qualification_id":"other","classification":"synthetic"}'
            )
        return result

    monkeypatch.setattr(services, "command", reject)
    with pytest.raises(ValueError):
        services.initialize(**kwargs)
    assert not any("create" in call for call in calls)


def test_command_does_not_leak_captured_secrets(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 7, b"secret", b"secret"
        ),
    )
    with pytest.raises(RuntimeError, match="exit 7") as error:
        services.command(["docker", "inspect"])
    assert "secret" not in str(error.value)
    assert services.command(["docker", "inspect"], missing_ok=True).returncode == 7


def test_cli_only_prints_public_report(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        materials, "generate_materials", lambda *a, **k: {"classification": "synthetic"}
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "materials",
            "--output",
            str(tmp_path),
            "--qualification-id",
            QID,
            "--lets-source",
            str(ROOT),
        ],
    )
    assert materials.main() == 0
    assert json.loads(capsys.readouterr().out) == {"classification": "synthetic"}
    monkeypatch.setattr(
        services,
        "initialize",
        lambda **kwargs: {"status": "started-acceptance-pending"},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "services",
            "--source",
            str(ROOT),
            "--material-root",
            str(tmp_path),
            "--qualification-id",
            QID,
            "--candidate-commit",
            SHA,
            "--candidate-image",
            IMAGE,
            "--policy-root",
            str(ROOT),
            "--policy-commit",
            SHA,
            "--materializer-image",
            IMAGE,
            "--lets-image",
            IMAGE,
        ],
    )
    assert services.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "started-acceptance-pending"
    }


def test_host_platform_guard():
    if os.name == "posix":
        materials._require_posix()
        services._require_posix()
    else:
        with pytest.raises(ValueError, match="POSIX"):
            materials._require_posix()
        with pytest.raises(ValueError, match="POSIX"):
            services._require_posix()


def speech_path(content):
    path = MagicMock(spec=Path)
    path.is_absolute.return_value = True
    path.is_symlink.return_value = False
    path.is_file.return_value = True
    path.resolve.return_value = path
    path.stat.return_value = SimpleNamespace(st_mode=0o100600, st_uid=1234)
    path.read_text.return_value = content
    return path


def test_speech_input_is_private_narrow_and_https(ownership):
    path = speech_path(
        "VOICE_SPEECH_BASE_URL=https://speech.example/v1\nVOICE_SPEECH_API_KEY=synthetic-secret\n"
    )
    assert (
        services.read_speech_environment(path)["VOICE_SPEECH_API_KEY"]
        == "synthetic-secret"
    )
    path.is_symlink.return_value = True
    with pytest.raises(ValueError, match="canonical private"):
        services.read_speech_environment(path)
    path.is_symlink.return_value = False
    path.stat.return_value.st_mode = 0o100644
    with pytest.raises(ValueError, match="private and owned"):
        services.read_speech_environment(path)


@pytest.mark.parametrize(
    "content,message",
    [
        ("OPENAI_API_KEY=other", "only two"),
        ("VOICE_SPEECH_BASE_URL=https://speech.example", "both speech"),
        (
            "VOICE_SPEECH_BASE_URL=https://speech.example\nVOICE_SPEECH_API_KEY=one\nVOICE_SPEECH_API_KEY=two",
            "only two",
        ),
        (
            "VOICE_SPEECH_BASE_URL=http://speech.example\nVOICE_SPEECH_API_KEY=synthetic",
            "HTTPS",
        ),
        (
            "VOICE_SPEECH_BASE_URL=https://user:pass@speech.example\nVOICE_SPEECH_API_KEY=synthetic",
            "HTTPS",
        ),
        (
            "VOICE_SPEECH_BASE_URL=https://speech.example\nVOICE_SPEECH_API_KEY="
            + "a" * 8193,
            "worker bound",
        ),
    ],
)
def test_speech_input_rejects_other_keys_duplicates_and_insecure_endpoints(
    ownership, content, message
):
    with pytest.raises(ValueError, match=message):
        services.read_speech_environment(speech_path(content))


def test_voice_requires_complete_inputs_and_nonzero_closure(setup):
    kwargs, calls, _ = setup
    kwargs["voice_image"] = IMAGE
    with pytest.raises(ValueError, match="supplied together"):
        services.initialize(**kwargs)
    kwargs.update(voice_closure_sha256="0" * 64, speech_runtime_env=Path("private.env"))
    with pytest.raises(ValueError, match="nonzero"):
        services.initialize(**kwargs)
    assert not calls


def test_voice_worker_uses_fresh_control_and_private_approved_speech(
    setup, monkeypatch
):
    kwargs, calls, _ = setup
    kwargs.update(
        voice_image="sha256:" + "f" * 64,
        voice_closure_sha256="f" * 64,
        speech_runtime_env=Path("private.env"),
    )
    monkeypatch.setattr(
        services,
        "read_speech_environment",
        lambda path: {
            "VOICE_SPEECH_BASE_URL": "https://speech.example/v1",
            "VOICE_SPEECH_API_KEY": "synthetic-secret",
        },
    )
    report = services.initialize(**kwargs)
    assert report["voice"]["status"] == "worker-started-acceptance-pending"
    assert report["voice"]["enabled"] and report["voice"]["backend"] == "llm_factory"
    assert "synthetic-secret" not in json.dumps(report)
    assert "synthetic-secret" not in json.dumps(calls)
    env = dict(
        line.split("=", 1)
        for line in (kwargs["material_root"] / "voice-runtime.env")
        .read_text()
        .splitlines()
    )
    assert env["ASTRAL_ENV"] == "production"
    assert (
        env["ASTRAL_VOICE_CONTROL_URL"]
        == "wss://ad-bwq-web:9443/api/voice/worker-control"
    )
    assert env["VOICE_CONTROL_SECRET"] == "s" * 64
    assert env["VOICE_SPEECH_API_KEY"] == "synthetic-secret"
    assert not any(name.startswith("LIVEKIT_") for name in env)
    app_env = dict(
        line.split("=", 1)
        for line in (kwargs["material_root"] / "runtime-fragment.env")
        .read_text()
        .splitlines()
    )
    assert app_env["VOICE_WORKER_CLOSURE_SHA256"] == env["VOICE_WORKER_CLOSURE_SHA256"]
    from voice_agent.config import WorkerConfig

    assert WorkerConfig.from_environ(env).worker_identity == "bwq-voice-" + QID
    worker = [call for call in calls if call[:3] == ["docker", "run", "-d"]][-1]
    assert "--read-only" in worker and "--env-file" in worker
    assert "--network" in worker and "--privileged" not in worker


def test_policy_identity_and_private_material_location_fail_before_mutation(setup):
    kwargs, calls, _ = setup
    kwargs["policy_commit"] = "bad"
    with pytest.raises(ValueError, match="policy commit"):
        services.initialize(**kwargs)
    kwargs["policy_commit"] = SHA
    kwargs["material_root"] = kwargs["source"] / "materials"
    with pytest.raises(ValueError, match="outside both"):
        services.initialize(**kwargs)
    assert not calls


def network_details():
    return {
        "Name": "ad-bwq-services-" + QID,
        "Labels": {services.LABEL: QID},
        "Driver": "bridge",
        "IPAM": {"Config": [{"Subnet": "172.30.0.0/24", "Gateway": "172.30.0.1"}]},
        "Containers": {},
    }


@pytest.mark.parametrize(
    "mutation,message",
    [
        ("foreign", "exact owned"),
        ("host", "exact owned"),
        ("public", "private IPv4"),
        ("tiny", "private IPv4"),
        ("ipv6", "private IPv4"),
        ("duplicate", "private IPv4"),
        ("allocated", "allocated"),
    ],
)
def test_livekit_address_never_uses_foreign_or_broad_networks(mutation, message):
    details = network_details()
    if mutation == "foreign":
        details["Labels"][services.LABEL] = "other"
    elif mutation == "host":
        details["Driver"] = "host"
    elif mutation == "public":
        details["IPAM"]["Config"][0]["Subnet"] = "8.8.8.0/24"
    elif mutation == "tiny":
        details["IPAM"]["Config"][0]["Subnet"] = "172.30.0.0/30"
    elif mutation == "ipv6":
        details["IPAM"]["Config"][0]["Subnet"] = "fd00::/64"
    elif mutation == "duplicate":
        details["IPAM"]["Config"].append({"Subnet": "172.31.0.0/24"})
    else:
        details["Containers"] = {"other": {"IPv4Address": "172.30.0.254/24"}}
    with pytest.raises(ValueError, match=message):
        services.livekit_address(details, QID)


def test_livekit_profile_keeps_production_room_authority_and_narrow_turn():
    address = services.livekit_address(network_details(), QID)
    profile = services.livekit_profile(address)
    assert profile["development"] is False and profile["room"]["auto_create"] is False
    assert profile["rtc"]["node_ip"] == address
    assert profile["turn"]["allow_restricted_peer_cidrs"] == [address + "/32"]
    assert profile["turn"]["enabled"] and profile["turn"]["tls_port"] == 443
    assert profile["turn"]["external_tls"] is False
    assert (
        profile["logging"]["level"] == "warn"
        and profile["logging"]["pion_level"] == "error"
    )
    with pytest.raises(ValueError, match="private IPv4"):
        services.livekit_profile("8.8.8.8")


def test_postgres_wait_is_bounded_and_does_not_leak_output(monkeypatch):
    attempts = []
    monkeypatch.setattr(services.time, "sleep", lambda value: None)
    monkeypatch.setattr(
        services,
        "command",
        lambda *args, **kwargs: attempts.append(args) or SimpleNamespace(returncode=1),
    )
    with pytest.raises(RuntimeError, match="did not become ready"):
        services.wait_for_iam_postgres("owned-iam-pg")
    assert len(attempts) == 60


def test_initializer_rejects_wrong_infrastructure_observer_identity(setup, monkeypatch):
    kwargs, _, execute = setup

    def wrong(args, **options):
        result = execute(args, **options)
        if "/wait.py" in args:
            result.stdout = b'{"qualification_id":"foreign","status":"infrastructure-ready","application_acceptance":false}'
        return result

    monkeypatch.setattr(services, "command", wrong)
    with pytest.raises(ValueError, match="observation mismatch"):
        services.initialize(**kwargs)
