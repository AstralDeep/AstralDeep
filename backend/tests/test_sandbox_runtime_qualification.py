"""Tests for the opt-in sandbox-runtime qualification runner
(orchestrator/sandbox_qualification.py): pinned artifact handling, fail-closed support
probing, the immutable SRT profile, real LETS receipt enforcement, parent-side
containment checks with unsandboxed negative controls, and the honest
unavailable/failed/passed record lifecycle on supported hosts.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from lets.canonical import b64url_encode, canonical_digest, canonical_json
from lets.crypto import Ed25519Signer
from lets.manifest import (
    ClusterManifest,
    ManifestPublicKey,
    ManifestSignature,
    WardenManifest,
)
from lets.models import Receipt
from lets.policy import MachineSpec, PolicySpec, ResourceDimension, TransitionSpec

from orchestrator import generated_lets_executor as lets_executor
from orchestrator import sandbox_qualification as sq

AGENT_ID = sq.QUALIFICATION_AGENT_ID
EXECUTOR_AUDIENCE = "sandbox-qualification-executor"
OWNER_ID = "owner-qual"
BINDING_ID = "binding-qual"
LEASE_ID = "lease-qual"
LINEAGE_ID = "lineage-qual"
RUNTIME_ID = "runtime-qual"


def _signed_manifest():
    resources = tuple(
        ResourceDimension(name, "count")
        for name in ("read", "write", "search", "system", "files", "execute")
    )
    transitions = tuple(
        TransitionSpec(
            name=transition,
            source="ready",
            target="ready",
            cost=tuple(1 if index == dimension else 0 for index in range(6)),
            capability=capability,
        )
        for _scope, capability, transition, dimension in lets_executor._SCOPE_BINDINGS
    )
    policy = PolicySpec(
        policy_id="astral-tools",
        policy_version="v1",
        dimensions=resources,
        machine=MachineSpec(
            machine_id="astral-tool-effects",
            initial_state="ready",
            transitions=transitions,
        ),
        max_lease_ttl_ns=60_000_000_000,
        receipt_ttl_ns=30_000_000_000,
        max_clock_uncertainty_ns=1_000_000_000,
        transfer_gap_window=64,
    )
    warden = Ed25519Signer.generate("warden-qual")
    operator = Ed25519Signer.generate("operator-qual")
    manifest = ClusterManifest(
        tenant_id="tenant-qual",
        envelope_id="envelope-qual",
        config_epoch=1,
        created_at="2026-10-06T00:00:00Z",
        resources=resources,
        initial_budget=(100, 100, 100, 100, 100, 100),
        wardens=(
            WardenManifest(
                warden_id=warden.warden_id,
                peer_endpoint="https://warden-qual.example",
                client_endpoint="https://warden-qual.example",
                initial_share=(100, 100, 100, 100, 100, 100),
                keys=(
                    ManifestPublicKey(
                        warden.key_id,
                        warden.public_key_bytes,
                    ),
                ),
                extensions={},
            ),
        ),
        policies=(policy,),
        extensions={},
    )
    manifest = replace(
        manifest,
        signatures=(
            ManifestSignature(
                operator.key_id,
                operator.sign(canonical_json(manifest.unsigned_dict())),
            ),
        ),
    )
    return manifest, policy, warden, operator


@pytest.fixture
def lets_values(tmp_path: Path):
    manifest, policy, warden, operator = _signed_manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), sort_keys=True), encoding="utf-8"
    )
    operator_path = tmp_path / "operators.json"
    operator_path.write_text(
        json.dumps(
            {
                "api_version": lets_executor.OPERATOR_TRUST_TYPE,
                "threshold": 1,
                "keys": [
                    {
                        "key_id": operator.key_id,
                        "algorithm": "Ed25519",
                        "public_key": b64url_encode(operator.public_key_bytes),
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (tmp_path / "replay").mkdir()
    (tmp_path / "authority").mkdir()
    values = {
        "ASTRAL_ENV": "production",
        "FF_LETS_EXTERNAL_WARDEN": "1",
        "LETS_MODE": "enforce",
        "LETS_SIGNED_TRUST_MANIFEST": str(manifest_path),
        "LETS_MANIFEST_OPERATOR_KEYS_FILE": str(operator_path),
        "LETS_WARDEN_ID": warden.warden_id,
        "LETS_TENANT_ID": manifest.tenant_id,
        "LETS_ENVELOPE_ID": manifest.envelope_id,
        "LETS_POLICY_DIGEST": policy.digest,
        "LETS_MACHINE_DIGEST": policy.machine.digest,
        "LETS_EXECUTOR_INSTANCE_ID": EXECUTOR_AUDIENCE,
        "LETS_EXECUTOR_DB_ROOT": str(tmp_path / "replay"),
        "LETS_EXECUTOR_AUTHORITY_ROOT": str(tmp_path / "authority"),
        "ASTRAL_AUTHORITY_OWNER_ID": OWNER_ID,
        "ASTRAL_AUTHORITY_BINDING_ID": BINDING_ID,
        "ASTRAL_AUTHORITY_LEASE_ID": LEASE_ID,
        "ASTRAL_AUTHORITY_LINEAGE_ID": LINEAGE_ID,
        "ASTRAL_RUNTIME_ID": RUNTIME_ID,
        "ASTRAL_RUNTIME_GENERATION": "1",
    }
    return values, manifest, policy, warden


@pytest.fixture
def lets_runtime(lets_values):
    values, _manifest, _policy, _warden = lets_values
    return lets_executor.load_protected_executor(values, agent_id=AGENT_ID)


@pytest.fixture
def receipt_factory(lets_values):
    _values, manifest, policy, warden = lets_values
    state = {"sequence": 0}

    def factory(arguments):
        sequence = state["sequence"]
        state["sequence"] += 1
        nonce = os.urandom(16).hex()
        operation_id = f"op-{uuid.uuid4().hex}"
        context = {
            "type": lets_executor.EVIDENCE_TYPE,
            "operation_id": operation_id,
            "agent_id": AGENT_ID,
            "runtime_id": RUNTIME_ID,
            "tool_id": sq.TOOL_ID,
            "scope": "tools:write",
            "capability": "astral.tools.write",
            "transition": "tool_write",
            "resource_dimension": 1,
            "executor_audience": EXECUTOR_AUDIENCE,
            "channel": "a2a",
            "audit_correlation_id": f"audit-{uuid.uuid4().hex}",
            "scope_profile_sha256": lets_executor.SCOPE_PROFILE_SHA256,
            "authorized_effect_sha256": "1" * 64,
            "effect_sha256": "",
        }
        context["effect_sha256"] = lets_executor._recompute_effect_sha256(
            context, expected_sequence=sequence, nonce=nonce
        )
        now = time.time_ns()
        receipt = Receipt(
            tenant_id=manifest.tenant_id,
            envelope_id=manifest.envelope_id,
            config_epoch=manifest.config_epoch,
            receipt_id=f"receipt-{uuid.uuid4().hex}",
            request_id=operation_id,
            warden_id=warden.warden_id,
            key_id=warden.key_id,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            policy_digest=policy.digest,
            machine_digest=policy.machine.digest,
            lease_id=LEASE_ID,
            lineage_id=LINEAGE_ID,
            subject_id=AGENT_ID,
            executor_audience=EXECUTOR_AUDIENCE,
            transition="tool_write",
            source_state="ready",
            target_state="ready",
            cost=(0, 1, 0, 0, 0, 0),
            resulting_sequence=sequence + 1,
            evidence_digest=canonical_digest(context),
            nonce=nonce,
            issued_at_ns=now - 1_000_000_000,
            expires_at_ns=now + 30_000_000_000,
        )
        receipt = replace(
            receipt,
            signature=b64url_encode(
                warden.sign(canonical_json(receipt.unsigned_payload()))
            ),
        )
        return {
            "type": lets_executor.PERMIT_TYPE,
            "binding_id": BINDING_ID,
            "owner_id": OWNER_ID,
            "runtime_generation": 1,
            "context": context,
            "expected_sequence": sequence,
            "nonce": nonce,
            "wire_arguments_sha256": hashlib.sha256(
                lets_executor._stable_canonical_bytes(dict(arguments))
            ).hexdigest(),
            "receipt": receipt.to_dict(),
        }

    return factory


def _stub_artifact(root: Path, *, stderr_line: str) -> tuple[Path, sq.SrtArtifactPin]:
    package = root / "package"
    (package / "dist").mkdir(parents=True)
    for arch in ("x64", "arm64"):
        (package / "vendor" / "seccomp" / arch).mkdir(parents=True)
        binary = package / "vendor" / "seccomp" / arch / "apply-seccomp"
        binary.write_bytes(b"stub-seccomp-helper")
        os.chmod(binary, 0o755)
    cli = package / "dist" / "cli.js"
    cli.write_text(
        "import sys\n"
        "sys.stderr.write(" + json.dumps(stderr_line + "\n") + ")\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    tarball = root / "sandbox-runtime-stub.tgz"
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(package, arcname="package")
    data = tarball.read_bytes()
    pin = sq.SrtArtifactPin(
        package=sq.SRT_PACKAGE,
        version=sq.SRT_VERSION,
        tarball_url=sq.SRT_TARBALL_URL,
        sha512_hex=hashlib.sha512(data).hexdigest(),
        size_bytes=len(data),
    )
    return tarball, pin


@pytest.fixture
def stub_runtime(tmp_path: Path):
    root = tmp_path / "artifact"
    root.mkdir()
    _tarball, pin = _stub_artifact(
        root,
        stderr_line="Error: Sandbox dependencies not available: bubblewrap (bwrap) not installed",
    )
    package_root = root / "package"
    return sq.SrtRuntime(
        node_path=Path(sys.executable),
        cli_js=package_root / "dist" / "cli.js",
        apply_seccomp_path=package_root
        / "vendor"
        / "seccomp"
        / "x64"
        / "apply-seccomp",
        bwrap_path=Path("/bin/true"),
        socat_path=Path("/bin/true"),
        artifact_root=package_root,
        pin=pin,
    )


@pytest.fixture
def available_support():
    return sq.SandboxSupport(
        available=True,
        platform="linux",
        machine=os.uname().machine,
        node_version="v24.0.0",
        checks=(sq.SupportCheck("injected_probe", True, "test"),),
    )


class UnsandboxedEffectSupervisor(sq.ProcessSupervisor):
    def spawn(
        self,
        *,
        process_id,
        owner,
        argv,
        cwd=None,
        env=None,
        private_output=False,
        **popen_kwargs,
    ):
        script, spec = str(argv[-2]), str(argv[-1])
        return super().spawn(
            process_id=process_id,
            owner=owner,
            argv=[sys.executable, script, spec],
            cwd=cwd,
            env=None,
        )


def _record_codes(record: sq.QualificationRecord) -> set[str]:
    return set(record.failure_codes)


def _check_by_name(record: sq.QualificationRecord, name: str) -> sq.QualificationCheck:
    matches = [check for check in record.checks if check.name == name]
    assert matches, f"missing check {name} in {record.checks}"
    return matches[-1]


def test_default_pin_matches_declared_manifest():
    pin = sq.default_artifact_pin()
    document = sq.load_declared_manifest()
    artifact = document["artifact"]
    assert artifact["package"] == pin.package
    assert artifact["version"] == pin.version == "0.0.78"
    assert artifact["sha512"] == pin.sha512_hex
    assert artifact["size_bytes"] == pin.size_bytes
    assert artifact["tarball_url"] == pin.tarball_url
    assert document["runtime"]["minimum_node_version"] == "22.12.0"
    assert set(document["runtime"]["linux_primitives"]) >= {
        "bubblewrap (bwrap) on PATH or pinned absolute path",
        "socat on PATH or pinned absolute path",
        "seccomp-BPF via the vendored apply-seccomp helper (vendor/seccomp/x64 and vendor/seccomp/arm64)",
    }


def test_verify_artifact_bytes_rejects_mismatch():
    pin = sq.SrtArtifactPin(
        package="p",
        version="1.2.3",
        tarball_url="https://registry.example/p-1.2.3.tgz",
        sha512_hex="a" * 128,
        size_bytes=4,
    )
    with pytest.raises(sq.SandboxQualificationError, match="artifact_size_mismatch"):
        sq.verify_artifact_bytes(b"abc", pin)
    with pytest.raises(sq.SandboxQualificationError, match="artifact_digest_mismatch"):
        sq.verify_artifact_bytes(b"abcd", pin)
    good = sq.SrtArtifactPin(
        package="p",
        version="1.2.3",
        tarball_url="https://registry.example/p-1.2.3.tgz",
        sha512_hex=hashlib.sha512(b"abcd").hexdigest(),
        size_bytes=4,
    )
    sq.verify_artifact_bytes(b"abcd", good)


def test_acquire_without_consent_fails_closed(tmp_path: Path):
    pin = sq.default_artifact_pin()
    with pytest.raises(
        sq.SandboxQualificationError,
        match="artifact_not_cached_download_not_authorized",
    ):
        sq.acquire_srt_artifact(tmp_path / "cache", pin, allow_download=False)


def test_acquire_uses_verified_cache(tmp_path: Path):
    pin = sq.default_artifact_pin()
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = cache / f"sandbox-runtime-{pin.version}.tgz"
    cached.write_bytes(b"x" * pin.size_bytes)
    with pytest.raises(sq.SandboxQualificationError, match="artifact_digest_mismatch"):
        sq.acquire_srt_artifact(cache, pin, allow_download=False)


def test_extract_rejects_corrupt_and_incomplete_artifacts(tmp_path: Path):
    _tarball, pin = _stub_artifact(tmp_path, stderr_line="x")
    corrupt = tmp_path / "corrupt.tgz"
    corrupt.write_bytes(b"definitely-not-a-tarball")
    with pytest.raises(sq.SandboxQualificationError, match="artifact_size_mismatch"):
        sq.extract_srt_artifact(corrupt, tmp_path / "out", pin)
    tampered_data = bytearray(_tarball.read_bytes())
    tampered_data[len(tampered_data) // 2] ^= 0xFF
    tampered = tmp_path / "tampered.tgz"
    tampered.write_bytes(bytes(tampered_data))
    with pytest.raises(sq.SandboxQualificationError, match="artifact_digest_mismatch"):
        sq.extract_srt_artifact(tampered, tmp_path / "out2", pin)


def test_prepare_runtime_requires_supported_arch(tmp_path: Path, stub_runtime):
    with pytest.raises(sq.SandboxQualificationError, match="unsupported_architecture"):
        sq.prepare_srt_runtime(
            pin=stub_runtime.pin,
            cache_dir=tmp_path / "cache",
            allow_download=False,
            machine="mips",
        )


def test_prepare_runtime_requires_node(tmp_path: Path, monkeypatch, stub_runtime):
    monkeypatch.setattr(sq.shutil, "which", lambda name: None)
    with pytest.raises(sq.SandboxQualificationError, match="node_unavailable"):
        sq.prepare_srt_runtime(
            pin=stub_runtime.pin, cache_dir=tmp_path / "c1", allow_download=False
        )


def test_prepare_runtime_requires_bwrap(tmp_path: Path, monkeypatch, stub_runtime):
    fake_node = str(_write_fake_node(tmp_path, "v24.0.0"))
    monkeypatch.setattr(
        sq.shutil, "which", lambda name: fake_node if name == "node" else None
    )
    with pytest.raises(sq.SandboxQualificationError, match="bubblewrap_unavailable"):
        sq.prepare_srt_runtime(
            pin=stub_runtime.pin, cache_dir=tmp_path / "c2", allow_download=False
        )


def shutil_which(name: str):
    import shutil

    return shutil.which(name)


def test_extracted_runtime_preserves_seccomp_exec_bit(tmp_path: Path, stub_runtime):
    cache = tmp_path / "cache"
    cache.mkdir()
    source_root = stub_runtime.artifact_root.parent
    tarball = source_root / "sandbox-runtime-stub.tgz"
    extracted = sq.extract_srt_artifact(tarball, cache / "x", stub_runtime.pin)
    binary = extracted / "vendor" / "seccomp" / "x64" / "apply-seccomp"
    assert binary.is_file()
    assert os.access(binary, os.X_OK)
    mode = stat.S_IMODE(binary.stat().st_mode)
    assert mode & 0o111


def test_probe_reports_unavailable_on_sandboxless_host(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sq.tempfile, "gettempdir", lambda: str(tmp_path))
    support = sq.probe_support()
    assert support.available is False
    names = {check.name for check in support.checks}
    assert {
        "platform_linux",
        "node_floor",
        "bubblewrap_present",
        "socat_present",
        "ripgrep_present",
        "artifact_ready",
        "sandbox_namespace_probe",
    } <= names
    assert support.failure_details()


def test_probe_rejects_unsupported_machine():
    support = sq.probe_support(machine="mips")
    architecture = {c.name: c for c in support.checks}["architecture_supported"]
    assert architecture.passed is False


def test_probe_node_floor_boundary(tmp_path: Path):
    fake = tmp_path / "node"
    fake.write_text("#!/bin/sh\necho v20.5.0\n", encoding="utf-8")
    os.chmod(fake, 0o755)
    support = sq.probe_support(node_path=fake, machine="x86_64")
    node_check = {c.name: c for c in support.checks}["node_floor"]
    assert node_check.passed is False
    assert "22.12" in node_check.detail


def test_probe_node_floor_passes_on_current_node():
    node = shutil_which("node")
    if node is None:
        pytest.skip("no node interpreter on this host")
    support = sq.probe_support(machine="x86_64")
    node_check = {c.name: c for c in support.checks}["node_floor"]
    assert node_check.passed is True


def test_node_version_tuple_parsing():
    assert sq._node_version_tuple("v24.22.0") == (24, 22, 0)
    assert sq._node_version_tuple("v22.12.0") == (22, 12, 0)
    assert sq._node_version_tuple("garbage") is None
    assert sq._satisfies_floor((24, 22, 0), (22, 12)) is True
    assert sq._satisfies_floor((22, 11, 9), (22, 12)) is False


def test_profile_denies_network_and_limits_writes(tmp_path: Path, stub_runtime):
    session = sq.prepare_session(session_dir=tmp_path / "session")
    settings = sq.build_srt_settings(session, stub_runtime)
    network = settings["network"]
    assert network["allowedDomains"] == []
    assert network["deniedDomains"] == ["*"]
    assert network["strictAllowlist"] is True
    assert "allowAllUnixSockets" not in network
    assert "allowLocalBinding" not in network
    filesystem = settings["filesystem"]
    assert filesystem["denyRead"] == ["/"]
    assert filesystem["allowWrite"] == [str(session.scratch_dir)]
    assert "/tmp/claude" in filesystem["denyWrite"]
    assert settings["seccomp"]["applyPath"] == str(stub_runtime.apply_seccomp_path)
    assert settings["bwrapPath"] == str(stub_runtime.bwrap_path)
    assert settings["socatPath"] == str(stub_runtime.socat_path)


def test_profile_validation_rejects_weakening(tmp_path: Path, stub_runtime):
    session = sq.prepare_session(session_dir=tmp_path / "session")
    settings = sq.build_srt_settings(session, stub_runtime)
    sq.validate_srt_settings(settings, session=session, runtime=stub_runtime)

    def mutated(change):
        document = json.loads(json.dumps(settings))
        change(document)
        return document

    with pytest.raises(
        sq.SandboxQualificationError, match="profile_network_not_denied"
    ):
        sq.validate_srt_settings(
            mutated(
                lambda d: d["network"].__setitem__("allowedDomains", ["example.com"])
            ),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_network_not_denied"
    ):
        sq.validate_srt_settings(
            mutated(lambda d: d["network"].__setitem__("allowAllUnixSockets", True)),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_network_not_denied"
    ):
        sq.validate_srt_settings(
            mutated(lambda d: d["network"].__setitem__("allowLocalBinding", True)),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_reads_not_allow_only"
    ):
        sq.validate_srt_settings(
            mutated(lambda d: d["filesystem"].__setitem__("denyRead", ["/home"])),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_reads_not_allow_only"
    ):
        sq.validate_srt_settings(
            mutated(
                lambda d: d["filesystem"]["allowRead"].append(str(session.session_dir))
            ),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_writes_not_scratch_only"
    ):
        sq.validate_srt_settings(
            mutated(
                lambda d: d["filesystem"]["allowWrite"].append(
                    str(tmp_path / "elsewhere")
                )
            ),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_writes_not_scratch_only"
    ):
        sq.validate_srt_settings(
            mutated(lambda d: d.__setitem__("credentials", {"envVars": []})),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_seccomp_not_pinned"
    ):
        sq.validate_srt_settings(
            mutated(lambda d: d.__setitem__("seccomp", {})),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_seccomp_not_pinned"
    ):
        sq.validate_srt_settings(
            mutated(lambda d: d.__setitem__("bwrapPath", "bwrap")),
            session=session,
            runtime=stub_runtime,
        )


def test_profile_file_is_owner_read_only_and_deterministic(
    tmp_path: Path, stub_runtime
):
    session = sq.prepare_session(session_dir=tmp_path / "session")
    settings = sq.build_srt_settings(session, stub_runtime)
    payload = sq.write_immutable_profile(session, settings)
    mode = stat.S_IMODE(session.profile_path.stat().st_mode)
    assert mode == 0o400
    assert sq.canonical_profile_bytes(settings) == payload
    digest = hashlib.sha256(payload).hexdigest()
    assert digest == hashlib.sha256(sq.canonical_profile_bytes(settings)).hexdigest()


def test_spawn_env_is_clean():
    env = sq.child_spawn_env({"ASTRAL_SRT_QUAL_NOTE": "x"})
    assert set(env) == {"PATH", "LANG", "LC_ALL", "ASTRAL_SRT_QUAL_NOTE"}
    assert "HOME" not in env
    assert "HTTP_PROXY" not in env
    with pytest.raises(sq.SandboxQualificationError, match="spawn_env_key_rejected"):
        sq.child_spawn_env({"SECRET_TOKEN": "x"})


def test_sandboxed_argv_shape(tmp_path: Path, stub_runtime):
    command = ("/usr/bin/python3", "/tmp/effect.py", "/tmp/spec.json")
    argv = sq.sandboxed_effect_argv(stub_runtime, tmp_path / "profile.json", command)
    assert argv[:5] == (
        str(stub_runtime.node_path),
        str(stub_runtime.cli_js),
        "--settings",
        str(tmp_path / "profile.json"),
        "--",
    )
    assert tuple(argv[5:]) == command


def test_run_reports_unavailable_without_effects(
    tmp_path: Path, lets_runtime, receipt_factory
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        artifact_cache_dir=tmp_path / "empty-cache",
    )
    assert record.status == sq.STATUS_UNAVAILABLE
    assert record.support is not None and record.support.available is False
    assert record.modes_run == ()
    assert record.session_dir is None


def test_run_requires_authorized_mode(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    with pytest.raises(sq.SandboxQualificationError, match="authorized_mode_required"):
        sq.run_sandbox_qualification(
            executor_runtime=lets_runtime,
            receipt_factory=receipt_factory,
            support=available_support,
            runtime=stub_runtime,
            modes=("hang",),
            session_dir=tmp_path / "s",
        )


def test_run_fails_closed_without_prepared_runtime(
    tmp_path: Path, lets_runtime, receipt_factory, available_support
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        artifact_cache_dir=tmp_path / "empty-cache",
        allow_artifact_download=False,
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE,),
    )
    assert record.status == sq.STATUS_FAILED
    assert record.failure_codes
    scratch_root = tmp_path / "scratch"
    created = list(scratch_root.rglob("*")) if scratch_root.exists() else []
    assert all(not path.is_file() for path in created)


def test_run_launch_failure_is_hard_with_degradation(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE,),
    )
    assert record.status == sq.STATUS_FAILED
    assert any(
        code.startswith("degradation_scan:")
        or code.startswith("launch_completed:launch_failed:")
        for code in record.failure_codes
    )
    _assert_no_authorized_output(record)


def _write_fake_node(tmp_path: Path, version: str) -> Path:
    fake = tmp_path / f"node-{version.replace('.', '-')}"
    fake.write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
    os.chmod(fake, 0o755)
    return fake


def _assert_no_authorized_output(record: sq.QualificationRecord) -> None:
    session_dir = Path(record.session_dir)
    assert not (session_dir / "scratch" / sq.EFFECT_OUTPUT_NAME).exists()


def test_wrong_scope_receipt_fails_before_launch(
    tmp_path: Path,
    lets_runtime,
    receipt_factory,
    available_support,
    stub_runtime,
    monkeypatch,
):
    def factory(arguments):
        permit = receipt_factory(arguments)
        permit["context"]["scope"] = "tools:read"
        return permit

    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=factory,
        support=available_support,
        runtime=stub_runtime,
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE,),
    )
    assert record.status == sq.STATUS_FAILED
    assert "receipt_enforcement:executor_host_binding_mismatch" in record.failure_codes
    _assert_no_authorized_output(record)


def test_wrong_audience_receipt_fails_before_launch(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    def factory(arguments):
        permit = receipt_factory(arguments)
        permit["context"]["executor_audience"] = "other-executor"
        return permit

    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=factory,
        support=available_support,
        runtime=stub_runtime,
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE,),
    )
    assert record.status == sq.STATUS_FAILED
    assert "receipt_enforcement:executor_host_binding_mismatch" in record.failure_codes
    _assert_no_authorized_output(record)


def test_tampered_arguments_fails_before_launch(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    def factory(arguments):
        permit = receipt_factory(arguments)
        permit["wire_arguments_sha256"] = "0" * 64
        return permit

    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=factory,
        support=available_support,
        runtime=stub_runtime,
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE,),
    )
    assert record.status == sq.STATUS_FAILED
    assert "receipt_enforcement:executor_arguments_mutated" in record.failure_codes
    _assert_no_authorized_output(record)


def test_malformed_permit_fails_before_launch(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    def factory(_arguments):
        return {"type": "not-a-permit"}

    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=factory,
        support=available_support,
        runtime=stub_runtime,
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE,),
    )
    assert record.status == sq.STATUS_FAILED
    assert "receipt_enforcement:invalid_protected_permit" in record.failure_codes
    _assert_no_authorized_output(record)


def test_expired_receipt_fails_before_launch(
    tmp_path: Path, lets_values, receipt_factory, available_support, stub_runtime
):
    values, _manifest, _policy, warden = lets_values
    runtime = lets_executor.load_protected_executor(values, agent_id=AGENT_ID)

    def factory(arguments):
        permit = receipt_factory(arguments)
        receipt = Receipt.from_dict(dict(permit["receipt"]))
        now = time.time_ns()
        receipt = replace(
            receipt,
            issued_at_ns=now - 40_000_000_000,
            expires_at_ns=now - 2_000_000_000,
        )
        receipt = replace(
            receipt,
            signature=b64url_encode(
                warden.sign(canonical_json(receipt.unsigned_payload()))
            ),
        )
        permit["receipt"] = receipt.to_dict()
        return permit

    record = sq.run_sandbox_qualification(
        executor_runtime=runtime,
        receipt_factory=factory,
        support=available_support,
        runtime=stub_runtime,
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE,),
    )
    assert record.status == sq.STATUS_FAILED
    assert "receipt_enforcement:receipt_policy_invalid" in record.failure_codes
    _assert_no_authorized_output(record)


def test_replayed_receipt_fails_before_second_launch(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    minted: list[object] = []

    def factory(arguments):
        if minted:
            return minted[0]
        permit = receipt_factory(arguments)
        minted.append(permit)
        return permit

    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=factory,
        support=available_support,
        runtime=stub_runtime,
        supervisor=UnsandboxedEffectSupervisor(),
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE, sq.AUTHORIZED_MODE),
    )
    assert record.status == sq.STATUS_FAILED
    assert "receipt_enforcement:receipt_replayed" in record.failure_codes


def test_non_enforcing_runtime_fails_closed(
    tmp_path: Path, receipt_factory, available_support, stub_runtime
):
    shadow = sq.ProtectedExecutorRuntime(mode="shadow")
    record = sq.run_sandbox_qualification(
        executor_runtime=shadow,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE,),
    )
    assert record.status == sq.STATUS_FAILED
    assert (
        "receipt_enforcement:protected_executor_not_enforcing" in record.failure_codes
    )


def test_factory_failure_fails_before_launch(
    tmp_path: Path, lets_runtime, available_support, stub_runtime
):
    def factory(_arguments):
        raise RuntimeError("warden unavailable")

    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=factory,
        support=available_support,
        runtime=stub_runtime,
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE,),
    )
    assert record.status == sq.STATUS_FAILED
    assert "receipt_factory_failed" in record.failure_codes
    _assert_no_authorized_output(record)


def test_unsandboxed_authorized_child_cannot_produce_pass(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        supervisor=UnsandboxedEffectSupervisor(),
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE,),
    )
    assert record.status == sq.STATUS_FAILED
    assert "diagnostic_runner_configuration" in record.failure_codes
    assert _check_by_name(record, "authorized_effect").held is True


def test_unsandboxed_secret_read_is_caught(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        supervisor=UnsandboxedEffectSupervisor(),
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE, "read_secret"),
    )
    assert record.status == sq.STATUS_FAILED
    assert any(code.startswith("secret_exfiltration:") for code in record.failure_codes)
    secret_token = (Path(record.session_dir) / sq.SECRET_NAME).read_text(
        encoding="utf-8"
    )
    assert secret_token not in json.dumps(record.to_dict())


def test_unsandboxed_outside_write_is_caught(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        supervisor=UnsandboxedEffectSupervisor(),
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE, "write_outside"),
    )
    assert record.status == sq.STATUS_FAILED
    assert any(code.startswith("outside_writes:") for code in record.failure_codes)
    session_dir = Path(record.session_dir)
    qualification_id = record.qualification_id
    for root in sq.OUTSIDE_WRITE_ROOTS:
        stray = Path(root) / f"astral-srt-{qualification_id}.txt"
        try:
            if stray.exists():
                stray.unlink()
        except OSError:
            pass
        assert not stray.exists()
    outside = session_dir / "outside.txt"
    if outside.exists():
        outside.unlink()
    assert not outside.exists()


def test_unsandboxed_network_connect_is_caught(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        supervisor=UnsandboxedEffectSupervisor(),
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE, "network_connect"),
    )
    assert record.status == sq.STATUS_FAILED
    assert any(code.startswith("network_denial:") for code in record.failure_codes)
    assert _check_by_name(record, "network_denial").held is False


def test_unsandboxed_unix_ipc_is_caught(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        supervisor=UnsandboxedEffectSupervisor(),
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE, "unix_ipc"),
    )
    assert record.status == sq.STATUS_FAILED
    assert any(code.startswith("ipc_denial:") for code in record.failure_codes)


def test_unsandboxed_symlink_escape_is_caught(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        supervisor=UnsandboxedEffectSupervisor(),
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE, "symlink_escape"),
    )
    assert record.status == sq.STATUS_FAILED
    assert any(code.startswith("symlink_containment:") for code in record.failure_codes)
    assert _check_by_name(record, "symlink_containment").held is False


def test_hang_deadline_enforcement_with_real_child(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        supervisor=UnsandboxedEffectSupervisor(),
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=("hang", sq.AUTHORIZED_MODE),
        hang_deadline_seconds=0.5,
        per_launch_deadline_seconds=10.0,
    )
    assert record.status == sq.STATUS_FAILED
    deadline = _check_by_name(record, "deadline_enforcement")
    assert deadline.held is True
    assert _check_by_name(record, "tree_cleanup").held is True
    assert "diagnostic_runner_configuration" in record.failure_codes


def test_descendant_cleanup_with_real_tree(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        supervisor=UnsandboxedEffectSupervisor(),
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=("spawn_descendant", sq.AUTHORIZED_MODE),
        hang_deadline_seconds=0.5,
        per_launch_deadline_seconds=10.0,
    )
    assert record.status == sq.STATUS_FAILED
    descendant = _check_by_name(record, "descendant_cleanup")
    assert descendant.held is True
    deadline = _check_by_name(record, "deadline_enforcement")
    assert deadline.held is True


def test_output_exhaustion_bookkeeping(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        supervisor=UnsandboxedEffectSupervisor(),
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE, "exhaust_output"),
        per_launch_deadline_seconds=20.0,
    )
    assert record.status == sq.STATUS_FAILED
    exhausted = _check_by_name(record, "output_exhausted")
    assert exhausted.held is True
    assert "dropped_bytes=" in exhausted.detail
    bounded = _check_by_name(record, "output_bound")
    assert bounded.held is True


def test_listener_pending_detection():
    listener = sq.ProbeTcpListener()
    try:
        assert listener.pending_connections() == 0
        client = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
        try:
            assert listener.pending_connections() == 1
        finally:
            client.close()
    finally:
        listener.close()


def test_secret_material_scans_scratch_and_output(tmp_path: Path):
    session = sq.prepare_session(session_dir=tmp_path / "session")
    (session.scratch_dir / "innocent.txt").write_text("nothing here", encoding="utf-8")

    fake_snapshot = SimpleNamespace(
        stdout=SimpleNamespace(lines=()),
        stderr=SimpleNamespace(lines=()),
    )

    class _FakeProcess:
        def snapshot(self):
            return fake_snapshot

    assert sq.secret_material_present(session, _FakeProcess()) is False
    (session.scratch_dir / "leak.txt").write_text(
        session.secret_token, encoding="utf-8"
    )
    assert sq.secret_material_present(session, _FakeProcess()) is True


def test_degradation_scan_markers():
    assert sq.degradation_hits(
        "seccomp not available - unix socket access not restricted"
    )
    assert sq.degradation_hits("Error: Sandbox dependencies not available: bwrap")
    assert sq.degradation_hits("[Sandbox Linux] apply-seccomp binary not available")
    assert sq.degradation_hits("clean output") == ()


def test_hash_chained_log_append_and_tamper_detection(tmp_path: Path):
    log = tmp_path / "log.jsonl"
    first = sq.append_hash_chained_entry(log, {"n": 1})
    second = sq.append_hash_chained_entry(log, {"n": 2})
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["entry_sha256"] == first
    assert lines[1]["previous_entry_sha256"] == first
    assert lines[1]["entry_sha256"] == second
    lines = log.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    del last["entry_sha256"]
    lines[-1] = json.dumps(last, sort_keys=True, separators=(",", ":"))
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(sq.SandboxQualificationError, match="qualification_log_corrupt"):
        sq.append_hash_chained_entry(log, {"n": 3})


def test_descendant_cleanup_check_zombie_aware(tmp_path: Path):
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert sq.descendant_cleanup_check(holder.pid).held is False
    finally:
        holder.kill()
        holder.wait()
    assert sq.descendant_cleanup_check(holder.pid).held is True
    assert sq.descendant_cleanup_check(999999).held is True


def test_permit_file_factory(tmp_path: Path):
    permit = {"type": "x", "nested": {"a": 1}}
    path = tmp_path / "permit.json"
    path.write_text(json.dumps(permit), encoding="utf-8")
    factory = sq.permit_file_factory(path)
    assert factory({}) == permit
    with pytest.raises(sq.SandboxQualificationError, match="permit_file_unreadable"):
        sq.permit_file_factory(tmp_path / "missing.json")


def test_record_to_dict_shape(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE,),
    )
    document = record.to_dict()
    assert document["status"] == sq.STATUS_FAILED
    assert document["qualification_id"]
    assert document["profile_sha256"]
    assert document["artifact_sha512"]
    assert document["receipt_sha256s"]
    assert document["audit_correlation_id"]
    assert document["modes_run"] == [sq.AUTHORIZED_MODE]
    assert all(check["detail"] for check in document["checks"])


def test_cli_probe_only_exit_codes(tmp_path: Path, capsys):
    expected_available = sq.probe_support().available
    exit_code = sq.main(["--probe-only"])
    document = json.loads(capsys.readouterr().out)
    if expected_available:
        assert exit_code == sq.EXIT_PASSED
        assert document["status"] == sq.STATUS_PROBE
    else:
        assert exit_code == sq.EXIT_UNAVAILABLE
        assert document["status"] == sq.STATUS_UNAVAILABLE
        assert document["support"]["available"] is False


def test_cli_full_mode_with_failing_factory(
    tmp_path: Path, capsys, lets_runtime, available_support, stub_runtime
):
    def factory(_arguments):
        raise RuntimeError("no warden")

    exit_code = sq.main(
        ["--session-dir", str(tmp_path / "s"), "--scratch-root", str(tmp_path / "w")],
        support=available_support,
        executor_runtime=lets_runtime,
        receipt_factory=factory,
        runtime=stub_runtime,
    )
    document = json.loads(capsys.readouterr().out)
    assert exit_code == sq.EXIT_FAILED
    assert document["status"] == sq.STATUS_FAILED
    assert "receipt_factory_failed" in document["failure_codes"]


def test_cli_full_mode_loads_executor_from_environment(
    tmp_path: Path, capsys, lets_values, monkeypatch, available_support, stub_runtime
):
    values, _manifest, _policy, _warden = lets_values
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    def factory(_arguments):
        raise RuntimeError("no warden")

    exit_code = sq.main(
        ["--session-dir", str(tmp_path / "s"), "--scratch-root", str(tmp_path / "w")],
        support=available_support,
        receipt_factory=factory,
        runtime=stub_runtime,
    )
    document = json.loads(capsys.readouterr().out)
    assert exit_code == sq.EXIT_FAILED
    assert "receipt_factory_failed" in document["failure_codes"]


def test_cli_full_mode_configuration_error(
    tmp_path: Path, capsys, monkeypatch, available_support
):
    monkeypatch.setenv("LETS_MODE", "enforce")
    monkeypatch.delenv("FF_LETS_EXTERNAL_WARDEN", raising=False)
    exit_code = sq.main(
        ["--session-dir", str(tmp_path / "s")],
        support=available_support,
    )
    document = json.loads(capsys.readouterr().out)
    assert exit_code == sq.EXIT_FAILED
    assert any(
        code.startswith("executor_configuration:") for code in document["failure_codes"]
    )


@pytest.mark.skipif(
    not sq.probe_support().available,
    reason="sandbox-runtime prerequisites unavailable on this host; "
    "the pilot reports unavailable instead of simulating containment",
)
def test_full_qualification_passes_on_supported_host(
    tmp_path: Path, lets_runtime, receipt_factory
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        scratch_root=tmp_path / "scratch",
        per_launch_deadline_seconds=30.0,
        hang_deadline_seconds=2.0,
    )
    assert record.status == sq.STATUS_PASSED, record.to_dict()
    session_dir = Path(record.session_dir)
    output = session_dir / "scratch" / sq.EFFECT_OUTPUT_NAME
    assert output.is_file()
    assert hashlib.sha256(output.read_bytes()).hexdigest() in (
        _check_by_name(record, "authorized_effect").detail,
    )
    assert all(check.held for check in record.checks)
    secret_token = (session_dir / sq.SECRET_NAME).read_text(encoding="utf-8")
    assert secret_token not in json.dumps(record.to_dict())


def test_pin_shape_validation_rejects_bad_pins():
    good_url = "https://registry.npmjs.org/@anthropic-ai/sandbox-runtime/-/sandbox-runtime-0.0.78.tgz"
    with pytest.raises(sq.SandboxQualificationError, match="invalid_artifact_pin"):
        sq._validate_pin_shape("", "0.0.78", good_url, "a" * 128, 10)
    with pytest.raises(sq.SandboxQualificationError, match="invalid_artifact_pin"):
        sq._validate_pin_shape("p", "bad version", good_url, "a" * 128, 10)
    with pytest.raises(sq.SandboxQualificationError, match="invalid_artifact_pin"):
        sq._validate_pin_shape(
            "p", "0.0.78", "http://insecure.example/t.tgz", "a" * 128, 10
        )
    with pytest.raises(sq.SandboxQualificationError, match="invalid_artifact_pin"):
        sq._validate_pin_shape("p", "0.0.78", good_url, "zz" * 64, 10)
    with pytest.raises(sq.SandboxQualificationError, match="invalid_artifact_pin"):
        sq._validate_pin_shape("p", "0.0.78", good_url, "a" * 127, 10)
    with pytest.raises(sq.SandboxQualificationError, match="invalid_artifact_pin"):
        sq._validate_pin_shape("p", "0.0.78", good_url, "a" * 128, 0)


def test_manifest_missing_or_invalid_fails_closed(tmp_path: Path, monkeypatch):
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(sq, "_manifest_path", lambda: missing)
    with pytest.raises(
        sq.SandboxQualificationError, match="qualification_manifest_unavailable"
    ):
        sq.load_declared_manifest()
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(sq, "_manifest_path", lambda: broken)
    with pytest.raises(
        sq.SandboxQualificationError, match="qualification_manifest_unavailable"
    ):
        sq.load_declared_manifest()
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"artifact": "not-a-dict"}), encoding="utf-8")
    monkeypatch.setattr(sq, "_manifest_path", lambda: invalid)
    with pytest.raises(
        sq.SandboxQualificationError, match="qualification_manifest_invalid"
    ):
        sq.load_declared_manifest()
    mismatched = tmp_path / "mismatched.json"
    mismatched.write_text(
        json.dumps(
            {
                "artifact": {
                    "package": sq.SRT_PACKAGE,
                    "version": "9.9.9",
                    "tarball_url": sq.SRT_TARBALL_URL,
                    "sha512": sq.SRT_TARBALL_SHA512_HEX,
                    "size_bytes": sq.SRT_TARBALL_SIZE_BYTES,
                },
                "runtime": {"minimum_node_version": "22.12.0"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sq, "_manifest_path", lambda: mismatched)
    with pytest.raises(
        sq.SandboxQualificationError, match="qualification_manifest_pin_mismatch"
    ):
        sq.load_declared_manifest()
    wrong_node = tmp_path / "wrong-node.json"
    wrong_node.write_text(
        json.dumps(
            {
                "artifact": {
                    "package": sq.SRT_PACKAGE,
                    "version": sq.SRT_VERSION,
                    "tarball_url": sq.SRT_TARBALL_URL,
                    "sha512": sq.SRT_TARBALL_SHA512_HEX,
                    "size_bytes": sq.SRT_TARBALL_SIZE_BYTES,
                },
                "runtime": {"minimum_node_version": "18.0.0"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sq, "_manifest_path", lambda: wrong_node)
    with pytest.raises(
        sq.SandboxQualificationError, match="qualification_manifest_pin_mismatch"
    ):
        sq.load_declared_manifest()


def test_verify_artifact_rejects_oversized(tmp_path: Path):
    big = b"x" * (sq.SRT_MAX_TARBALL_BYTES + 1)
    pin = sq.SrtArtifactPin(
        package="p",
        version="1.0.0",
        tarball_url="https://registry.example/p-1.0.0.tgz",
        sha512_hex=hashlib.sha512(big).hexdigest(),
        size_bytes=len(big),
    )
    with pytest.raises(sq.SandboxQualificationError, match="artifact_too_large"):
        sq.verify_artifact_bytes(big, pin)


def test_acquire_download_and_cached_paths(tmp_path: Path, monkeypatch):
    _tarball, pin = _stub_artifact(tmp_path / "src", stderr_line="x")
    payload = _tarball.read_bytes()
    cache = tmp_path / "cache"

    class _FakeResponse:
        def __init__(self, data):
            self._data = data

        def read(self, _limit):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        sq.urllib.request,
        "urlopen",
        lambda url, timeout: _FakeResponse(payload),
    )
    acquired = sq.acquire_srt_artifact(cache, pin, allow_download=True)
    assert acquired.read_bytes() == payload
    cached_again = sq.acquire_srt_artifact(cache, pin, allow_download=False)
    assert cached_again == acquired

    def _boom(url, timeout):
        raise OSError("registry unreachable")

    monkeypatch.setattr(sq.urllib.request, "urlopen", _boom)
    with pytest.raises(sq.SandboxQualificationError, match="artifact_download_failed"):
        sq.acquire_srt_artifact(tmp_path / "cache2", pin, allow_download=True)


def test_extract_rejects_incomplete_package(tmp_path: Path):
    root = tmp_path / "src"
    package = root / "package"
    package.mkdir(parents=True)
    tarball = root / "incomplete.tgz"
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(package, arcname="package")
    data = tarball.read_bytes()
    pin = sq.SrtArtifactPin(
        package="p",
        version="1.0.0",
        tarball_url="https://registry.example/p-1.0.0.tgz",
        sha512_hex=hashlib.sha512(data).hexdigest(),
        size_bytes=len(data),
    )
    with pytest.raises(sq.SandboxQualificationError, match="artifact_incomplete"):
        sq.extract_srt_artifact(tarball, tmp_path / "out", pin)


def test_prepare_runtime_full_success_path(tmp_path: Path):
    node = shutil_which("node")
    if node is None:
        pytest.skip("no node interpreter on this host")
    _tarball, pin = _stub_artifact(tmp_path / "src", stderr_line="x")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"sandbox-runtime-{pin.version}.tgz").write_bytes(_tarball.read_bytes())
    runtime = sq.prepare_srt_runtime(
        pin=pin,
        cache_dir=cache,
        allow_download=False,
        node_path=node,
        bwrap_path="/bin/true",
        socat_path="/bin/true",
        machine="aarch64",
    )
    assert runtime.node_path == Path(node)
    assert runtime.cli_js.is_file()
    assert runtime.apply_seccomp_path.is_file()
    with pytest.raises(sq.SandboxQualificationError, match="socat_unavailable"):
        sq.prepare_srt_runtime(
            pin=pin,
            cache_dir=tmp_path / "cache3",
            allow_download=False,
            node_path=node,
            bwrap_path="/bin/true",
            machine="aarch64",
        )


def test_prepare_runtime_rejects_non_executable_seccomp_helper(tmp_path: Path):
    root = tmp_path / "src"
    package = root / "package"
    (package / "dist").mkdir(parents=True)
    (package / "dist" / "cli.js").write_text("stub", encoding="utf-8")
    for arch in ("x64", "arm64"):
        (package / "vendor" / "seccomp" / arch).mkdir(parents=True)
        binary = package / "vendor" / "seccomp" / arch / "apply-seccomp"
        binary.write_bytes(b"stub")
        os.chmod(binary, 0o644)
    tarball = root / "noexec.tgz"
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(package, arcname="package")
    data = tarball.read_bytes()
    pin = sq.SrtArtifactPin(
        package="p",
        version="1.0.0",
        tarball_url="https://registry.example/p-1.0.0.tgz",
        sha512_hex=hashlib.sha512(data).hexdigest(),
        size_bytes=len(data),
    )
    node = str(_write_fake_node(tmp_path, "v24.0.0"))
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"sandbox-runtime-{pin.version}.tgz").write_bytes(tarball.read_bytes())
    with pytest.raises(
        sq.SandboxQualificationError, match="seccomp_helper_not_executable"
    ):
        sq.prepare_srt_runtime(
            pin=pin,
            cache_dir=cache,
            allow_download=False,
            node_path=node,
            bwrap_path="/bin/true",
            socat_path="/bin/true",
            machine="x86_64",
        )


def test_probe_with_injected_runtime_runs_smoke(stub_runtime):
    support = sq.probe_support(runtime=stub_runtime, machine="x86_64")
    checks = {check.name: check for check in support.checks}
    assert checks["artifact_ready"].passed is True
    assert checks["sandbox_namespace_probe"].passed is True


def test_probe_missing_node_override_reports_absent():
    support = sq.probe_support(node_path="/nonexistent/node", machine="x86_64")
    node_check = {check.name: check for check in support.checks}["node_floor"]
    assert node_check.passed is False
    assert "not found" in node_check.detail


def test_probe_override_paths_reach_dependency_checks(stub_runtime):
    support = sq.probe_support(
        bwrap_path="/bin/true",
        socat_path="/bin/true",
        machine="x86_64",
    )
    checks = {check.name: check for check in support.checks}
    assert checks["bubblewrap_present"].passed is True
    assert checks["socat_present"].passed is True
    assert checks["ripgrep_present"].passed is False


def test_probe_run_probe_command_handles_failure(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr(sq.subprocess, "run", _boom)
    passed, detail = sq._run_probe_command(["/bin/true"], timeout=1)
    assert passed is False
    assert detail == "TimeoutExpired"


def test_validate_settings_remaining_denials(tmp_path: Path, stub_runtime):
    session = sq.prepare_session(session_dir=tmp_path / "session")
    settings = sq.build_srt_settings(session, stub_runtime)
    with pytest.raises(sq.SandboxQualificationError, match="profile_invalid"):
        sq.validate_srt_settings({}, session=session, runtime=stub_runtime)

    def mutated(change):
        document = json.loads(json.dumps(settings))
        change(document)
        return document

    with pytest.raises(
        sq.SandboxQualificationError, match="profile_network_not_denied"
    ):
        sq.validate_srt_settings(
            mutated(lambda d: d["network"].__setitem__("strictAllowlist", False)),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_network_not_denied"
    ):
        sq.validate_srt_settings(
            mutated(lambda d: d["network"].__setitem__("deniedDomains", [])),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_reads_not_allow_only"
    ):
        sq.validate_srt_settings(
            mutated(lambda d: d["filesystem"].__setitem__("allowRead", [])),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_reads_not_allow_only"
    ):
        sq.validate_srt_settings(
            mutated(lambda d: d["filesystem"]["allowRead"].append("relative/path")),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_reads_not_allow_only"
    ):
        sq.validate_srt_settings(
            mutated(lambda d: d["filesystem"]["allowRead"].append("/opt/secret")),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_writes_not_scratch_only"
    ):
        sq.validate_srt_settings(
            mutated(
                lambda d: d["filesystem"].__setitem__(
                    "allowWrite", [str(session.scratch_dir), "/tmp"]
                )
            ),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_writes_not_scratch_only"
    ):
        sq.validate_srt_settings(
            mutated(lambda d: d["filesystem"].__setitem__("denyWrite", [])),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_seccomp_not_pinned"
    ):
        sq.validate_srt_settings(
            mutated(
                lambda d: d["seccomp"].__setitem__(
                    "applyPath", "/elsewhere/apply-seccomp"
                )
            ),
            session=session,
            runtime=stub_runtime,
        )
    with pytest.raises(
        sq.SandboxQualificationError, match="profile_seccomp_not_pinned"
    ):
        sq.validate_srt_settings(
            mutated(lambda d: d.__setitem__("socatPath", "/bin/false")),
            session=session,
            runtime=stub_runtime,
        )


def test_read_marker_rejects_non_dict(tmp_path: Path):
    session = sq.prepare_session(session_dir=tmp_path / "session")
    assert sq.read_marker(session, "authorized") is None
    (session.scratch_dir / f"{sq.MARKER_PREFIX}authorized.json").write_text(
        "[1,2,3]", encoding="utf-8"
    )
    assert sq.read_marker(session, "authorized") is None


def test_authorized_check_content_and_marker_variants(tmp_path: Path):
    session = sq.prepare_session(session_dir=tmp_path / "session")

    class _Snapshot:
        state = sq.ProcessState.EXITED

    absent = sq.authorized_effect_checks(session, _Snapshot())
    assert absent[0].held is False
    (session.scratch_dir / sq.EFFECT_OUTPUT_NAME).write_text("wrong", encoding="utf-8")
    mismatch = sq.authorized_effect_checks(session, _Snapshot())
    assert mismatch[0].detail == "authorized_content_mismatch"
    (session.scratch_dir / sq.EFFECT_OUTPUT_NAME).write_text(
        sq.effect_content(session), encoding="utf-8"
    )
    incomplete = sq.authorized_effect_checks(session, _Snapshot())
    assert incomplete[0].detail == "effect_marker_incomplete"
    (session.scratch_dir / f"{sq.MARKER_PREFIX}authorized.json").write_text(
        json.dumps({"observed": "ok"}), encoding="utf-8"
    )
    held = sq.authorized_effect_checks(session, _Snapshot())
    assert held[0].held is True
    assert held[0].detail.startswith("sha256:")
    assert held[1].held is True


def test_secret_check_variants(tmp_path: Path):
    session = sq.prepare_session(session_dir=tmp_path / "session")

    class _FakeProcess:
        def snapshot(self):
            return SimpleNamespace(
                stdout=SimpleNamespace(lines=()), stderr=SimpleNamespace(lines=())
            )

    marker_path = session.scratch_dir / f"{sq.MARKER_PREFIX}read_secret.json"
    marker_path.write_text(json.dumps({"observed": "ok"}), encoding="utf-8")
    reported = sq.secret_read_checks(session, _FakeProcess())
    assert reported[0].detail == "secret_read_reported_ok"
    marker_path.write_text(
        json.dumps({"observed": "PermissionError"}), encoding="utf-8"
    )
    held = sq.secret_read_checks(session, _FakeProcess())
    assert held[0].held is True
    marker_path.unlink()
    incomplete = sq.secret_read_checks(session, _FakeProcess())
    assert incomplete[0].detail == "probe_incomplete"


def test_outside_check_variants(tmp_path: Path):
    session = sq.prepare_session(session_dir=tmp_path / "session")
    stray = tmp_path / "stray.txt"
    stray.write_text("violation", encoding="utf-8")
    present = sq.outside_write_checks(session, {"outside_paths": [str(stray)]})
    assert present[0].held is False
    assert "outside_file_present" in present[0].detail
    stray.unlink()
    incomplete = sq.outside_write_checks(session, {"outside_paths": []})
    assert incomplete[0].detail == "probe_incomplete"
    marker = session.scratch_dir / f"{sq.MARKER_PREFIX}write_outside.json"
    marker.write_text(
        json.dumps({"attempts": {"/tmp/a": "ok", "/var/b": "OSError"}}),
        encoding="utf-8",
    )
    escaped = sq.outside_write_checks(session, {"outside_paths": []})
    assert escaped[0].detail == "outside_write_reported_ok"


def test_network_check_variants(stub_runtime):
    listener = sq.ProbeTcpListener()
    try:
        incomplete = sq.network_checks(listener, None)
        assert incomplete[0].detail == "probe_incomplete"
        reached = sq.network_checks(listener, {"attempts": {"h:1": "ok"}})
        assert reached[0].detail == "connection_reported_ok"
        denied = sq.network_checks(listener, {"attempts": {"h:1": "OSError"}})
        assert denied[0].held is True
    finally:
        listener.close()


def test_ipc_check_variants(tmp_path: Path):
    session = sq.prepare_session(session_dir=tmp_path / "session")
    socket_path = tmp_path / "probe.sock"
    listener = sq.ProbeUnixListener(socket_path)
    try:
        incomplete = sq.ipc_checks(session, listener, None)
        assert incomplete[0].detail == "probe_incomplete"
        succeeded = sq.ipc_checks(session, listener, {"attempts": {"connect": "ok"}})
        assert succeeded[0].detail == "ipc_reported_ok"
        created = session.scratch_dir / "ipc-probe.sock"
        created.write_bytes(b"")
        detected = sq.ipc_checks(
            session, listener, {"attempts": {"connect": "OSError"}}
        )
        assert detected[0].detail == "unix_socket_created_in_scratch"
        created.unlink()
        denied = sq.ipc_checks(session, listener, {"attempts": {"connect": "OSError"}})
        assert denied[0].held is True
    finally:
        listener.close()


def test_symlink_check_variants(tmp_path: Path):
    session = sq.prepare_session(session_dir=tmp_path / "session")
    incomplete = sq.symlink_checks(session)
    assert incomplete[0].detail == "probe_incomplete"
    marker = session.scratch_dir / f"{sq.MARKER_PREFIX}symlink_escape.json"
    marker.write_text(
        json.dumps({"attempts": {"write_through": "ok"}}), encoding="utf-8"
    )
    reported = sq.symlink_checks(session)
    assert reported[0].detail == "symlink_write_reported_ok"
    marker.write_text(
        json.dumps({"attempts": {"write_through": "PermissionError"}}), encoding="utf-8"
    )
    held = sq.symlink_checks(session)
    assert held[0].held is True


def test_output_bound_no_drop_detected():
    snapshot = SimpleNamespace(
        stdout=SimpleNamespace(
            retained_bytes=10,
            dropped_bytes=0,
            total_bytes=1024,
        ),
        stderr=SimpleNamespace(retained_bytes=0, dropped_bytes=0, total_bytes=0),
    )
    checks = sq.output_bound_checks(snapshot, expect_dropped=True)
    assert checks[1].held is False
    assert checks[1].detail == "no_dropped_output_observed"


def test_tree_cleanup_failure_variants():
    snapshot = SimpleNamespace(
        process_tree_terminated=False,
        readers_joined=False,
        pipes_closed=False,
        cleanup_error="process tree remains alive",
    )
    check = sq.tree_cleanup_checks(snapshot)[0]
    assert check.held is False
    assert "tree_alive" in check.detail
    assert "cleanup_error" in check.detail


def test_launch_outcome_nonterminal():
    snapshot = SimpleNamespace(state=sq.ProcessState.RUNNING)
    check = sq.launch_outcome_checks(snapshot)[0]
    assert check.held is False


def test_record_to_dict_without_support():
    record = sq.QualificationRecord(
        status=sq.STATUS_FAILED,
        failure_codes=("x",),
        checks=(),
        modes_run=(),
        support=None,
        profile_sha256=None,
        artifact_sha512=None,
        receipt_sha256s=(),
        audit_correlation_id=None,
        session_dir=None,
    )
    document = record.to_dict()
    assert document["support"] is None


def test_run_rejects_unknown_mode(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    with pytest.raises(sq.SandboxQualificationError, match="unknown_probe_mode"):
        sq.run_sandbox_qualification(
            executor_runtime=lets_runtime,
            receipt_factory=receipt_factory,
            support=available_support,
            runtime=stub_runtime,
            modes=(sq.AUTHORIZED_MODE, "bogus_mode"),
            session_dir=tmp_path / "s",
        )


def test_run_handles_spawn_os_error(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    class _ExplodingSupervisor(sq.ProcessSupervisor):
        def spawn(self, **_kwargs):
            raise FileNotFoundError("node missing")

    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        supervisor=_ExplodingSupervisor(),
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=(sq.AUTHORIZED_MODE,),
    )
    assert record.status == sq.STATUS_FAILED
    assert any(
        code.startswith("launch_completed:launch_failed:FileNotFoundError")
        for code in record.failure_codes
    )


def test_hang_child_exiting_early_is_reported(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    class _ImmediateExitSupervisor(sq.ProcessSupervisor):
        def spawn(
            self,
            *,
            process_id,
            owner,
            argv,
            cwd=None,
            env=None,
            private_output=False,
            **popen_kwargs,
        ):
            return super().spawn(
                process_id=process_id,
                owner=owner,
                argv=["/bin/true"],
                cwd=cwd,
                env=None,
            )

    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        supervisor=_ImmediateExitSupervisor(),
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=("hang", sq.AUTHORIZED_MODE),
        hang_deadline_seconds=5.0,
        per_launch_deadline_seconds=10.0,
    )
    assert record.status == sq.STATUS_FAILED
    deadline = _check_by_name(record, "deadline_enforcement")
    assert deadline.held is False
    assert deadline.detail == "hang_not_cancelled"


def test_descendant_pid_file_absent(
    tmp_path: Path, lets_runtime, receipt_factory, available_support, stub_runtime
):
    record = sq.run_sandbox_qualification(
        executor_runtime=lets_runtime,
        receipt_factory=receipt_factory,
        support=available_support,
        runtime=stub_runtime,
        supervisor=_ImmediateExitSupervisorForFile(),
        session_dir=tmp_path / "session",
        scratch_root=tmp_path / "scratch",
        modes=("spawn_descendant", sq.AUTHORIZED_MODE),
        hang_deadline_seconds=5.0,
        per_launch_deadline_seconds=10.0,
    )
    assert record.status == sq.STATUS_FAILED
    assert _check_by_name(record, "descendant_cleanup").detail == "pid_file_absent"


class _ImmediateExitSupervisorForFile(sq.ProcessSupervisor):
    def spawn(
        self,
        *,
        process_id,
        owner,
        argv,
        cwd=None,
        env=None,
        private_output=False,
        **popen_kwargs,
    ):
        return super().spawn(
            process_id=process_id,
            owner=owner,
            argv=["/bin/true"],
            cwd=cwd,
            env=None,
        )


def test_cli_permit_file_full_mode(
    tmp_path: Path, capsys, lets_runtime, available_support, stub_runtime
):
    permit_path = tmp_path / "permit.json"
    permit_path.write_text(json.dumps({"type": "bogus"}), encoding="utf-8")
    exit_code = sq.main(
        [
            "--permit-file",
            str(permit_path),
            "--session-dir",
            str(tmp_path / "s"),
            "--scratch-root",
            str(tmp_path / "w"),
        ],
        support=available_support,
        executor_runtime=lets_runtime,
        runtime=stub_runtime,
    )
    document = json.loads(capsys.readouterr().out)
    assert exit_code == sq.EXIT_FAILED
    assert "receipt_enforcement:invalid_protected_permit" in document["failure_codes"]
    assert document["modes_run"] == []
