"""Phase B §7.9: server_dynamic runtimes receive their LETS authority at spawn.

Pins (1) the exact child-environment hand-off built from the admitted Plane
binding, (2) per-runtime executor audience agreement between the receipt the
orchestrator requests and the executor the child builds, (3) private
per-runtime executor roots, and (4) byte-identical spawn kwargs when LETS is
off, shadow-degraded, or produced no active binding.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lets.canonical import b64url_encode, canonical_digest, canonical_json
from lets.models import Receipt

from orchestrator import generated_lets_executor as executor
from orchestrator.agent_lifecycle import (
    GENERATED,
    AgentLifecycleManager,
    LetsLifecycleError,
)
from orchestrator.dynamic_runtime_authority import (
    DEFAULT_RETENTION_DAYS,
    RETENTION_ENV,
    DynamicRuntimeAuthority,
    DynamicRuntimeAuthorityError,
    derive_dynamic_executor_audience,
    dynamic_runtime_environment,
    prepare_dynamic_runtime_roots,
    remove_dynamic_runtime_roots,
    retention_days,
    sweep_dynamic_runtime_roots,
)
from orchestrator.governed_dispatch import DispatchRuntime
from orchestrator.lets_config import load_lets_config
from orchestrator.lets_gateway import create_executor_gateway
from orchestrator.lets_lifecycle import LifecycleConvergence
from tests.test_generated_lets_executor import AGENT_ID, _environment
from tests.test_lets_agent_lifecycle_wiring import LifecycleStore

RUNTIME_ID = "0f3b2a7c-9d1e-4c5b-8a6f-123456789abc"
INSTANCE_ID = "astral-gateway-a"


def _binding(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = dict(
        binding_id="binding-a",
        owner_id="owner-a",
        agent_id=AGENT_ID,
        runtime_id=RUNTIME_ID,
        runtime_generation=3,
        population=SimpleNamespace(value="server_dynamic"),
        lease_id="lease-a",
        lineage_id="lineage-a",
        state=SimpleNamespace(value="active"),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _authority(**overrides: Any) -> DynamicRuntimeAuthority:
    return DynamicRuntimeAuthority.from_binding(
        _binding(**overrides),
        owner_id="owner-a",
        agent_id=AGENT_ID,
        runtime_id=RUNTIME_ID,
        executor_instance_id=INSTANCE_ID,
    )


# --------------------------------------------------------------------------
# Derivation + validation
# --------------------------------------------------------------------------


def test_audience_is_per_runtime_filesystem_neutral_and_bounded() -> None:
    audience = derive_dynamic_executor_audience(INSTANCE_ID, RUNTIME_ID)
    other = derive_dynamic_executor_audience(INSTANCE_ID, str(uuid.uuid4()))

    assert audience == f"{INSTANCE_ID}--{RUNTIME_ID}"
    assert audience != other
    assert "/" not in audience and not re.search(r"\s", audience)
    assert len(audience) <= 128
    with pytest.raises(DynamicRuntimeAuthorityError, match="^runtime_id_invalid$"):
        derive_dynamic_executor_audience(INSTANCE_ID, "runtime-a")
    with pytest.raises(
        DynamicRuntimeAuthorityError, match="^executor_audience_invalid$"
    ):
        derive_dynamic_executor_audience("x" * 100, RUNTIME_ID)
    with pytest.raises(
        DynamicRuntimeAuthorityError, match="^executor_instance_id_invalid$"
    ):
        derive_dynamic_executor_audience(" padded", RUNTIME_ID)


def test_authority_is_taken_only_from_an_active_matching_server_binding() -> None:
    authority = _authority()
    assert authority.population == "server_dynamic"
    assert authority.runtime_generation == 3
    assert authority.executor_audience == derive_dynamic_executor_audience(
        INSTANCE_ID, RUNTIME_ID
    )

    cases = [
        ({"state": SimpleNamespace(value="provisioning")}, "binding_not_active"),
        (
            {"population": SimpleNamespace(value="byo_user")},
            "binding_population_mismatch",
        ),
        ({"owner_id": "owner-b"}, "binding_fence_mismatch"),
        ({"runtime_id": str(uuid.uuid4())}, "binding_fence_mismatch"),
        ({"agent_id": "other-agent"}, "binding_fence_mismatch"),
        ({"runtime_generation": 0}, "binding_generation_invalid"),
        ({"lease_id": ""}, "lease_id_invalid"),
    ]
    for overrides, code in cases:
        with pytest.raises(DynamicRuntimeAuthorityError, match=f"^{code}$"):
            _authority(**overrides)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " owner",
        "owner ",
        "own er",  # internal whitespace
        "own\ter",
        "own\x00er",  # category Cc
        "own\u200ber",  # category Cf (zero-width space)
        "own\u0085er",  # NEL, Cc
        "x" * 513,  # over the child's 512-byte bound
    ],
)
def test_identifiers_use_the_child_executor_predicate(bad: str) -> None:
    """A hand-off value the child would refuse fails here with a named code.

    ``generated_lets_executor._identifier_value`` is the predicate the spawned
    runtime re-applies; a looser parent check would let the spawn proceed and
    surface only as an unexplained exit-78 child.
    """

    with pytest.raises(executor.ProtectedExecutorError):
        executor._identifier_value(bad, "x")
    with pytest.raises(DynamicRuntimeAuthorityError, match="^owner_id_invalid$"):
        DynamicRuntimeAuthority.from_binding(
            _binding(owner_id=bad),
            owner_id=bad,
            agent_id=AGENT_ID,
            runtime_id=RUNTIME_ID,
            executor_instance_id=INSTANCE_ID,
        )
    with pytest.raises(DynamicRuntimeAuthorityError, match="^lease_id_invalid$"):
        _authority(lease_id=bad)
    with pytest.raises(DynamicRuntimeAuthorityError, match="^warden_id_invalid$"):
        dynamic_runtime_environment(
            {},
            _authority(),
            warden_id=bad,
            database_root=Path("/tmp"),
            authority_root=None,
        )
    if len(bad) < 100:
        with pytest.raises(
            DynamicRuntimeAuthorityError, match="^executor_instance_id_invalid$"
        ):
            derive_dynamic_executor_audience(bad, RUNTIME_ID)


# --------------------------------------------------------------------------
# Per-runtime roots
# --------------------------------------------------------------------------


def _config(tmp_path: Path, *, authority: bool = True) -> SimpleNamespace:
    db_root = tmp_path / "exec-db"
    authority_root = tmp_path / "exec-authority"
    db_root.mkdir()
    authority_root.mkdir()
    return SimpleNamespace(
        mode="enforce",
        executor_instance_id=INSTANCE_ID,
        executor_db_root=db_root,
        executor_authority_root=authority_root if authority else None,
        trust_manifest=SimpleNamespace(warden_id="warden-a"),
    )


def test_roots_are_private_runtime_keyed_subdirectories(tmp_path: Path) -> None:
    config = _config(tmp_path)
    database_root, authority_root = prepare_dynamic_runtime_roots(config, RUNTIME_ID)

    assert database_root == (config.executor_db_root / RUNTIME_ID).resolve()
    assert authority_root == (config.executor_authority_root / RUNTIME_ID).resolve()
    for path in (database_root, authority_root):
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    # Idempotent, and a second runtime never shares a directory.
    assert prepare_dynamic_runtime_roots(config, RUNTIME_ID) == (
        database_root,
        authority_root,
    )
    other = prepare_dynamic_runtime_roots(config, str(uuid.uuid4()))
    assert other[0] != database_root and other[1] != authority_root

    with pytest.raises(DynamicRuntimeAuthorityError, match="^runtime_id_invalid$"):
        prepare_dynamic_runtime_roots(config, "../escape")


def test_roots_refuse_symlinked_or_missing_parents(tmp_path: Path) -> None:
    config = _config(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (config.executor_db_root / RUNTIME_ID).symlink_to(elsewhere)
    with pytest.raises(
        DynamicRuntimeAuthorityError, match="^executor_db_root_unavailable$"
    ):
        prepare_dynamic_runtime_roots(config, RUNTIME_ID)

    missing = SimpleNamespace(
        executor_db_root=tmp_path / "absent",
        executor_authority_root=None,
    )
    with pytest.raises(
        DynamicRuntimeAuthorityError, match="^executor_db_root_unavailable$"
    ):
        prepare_dynamic_runtime_roots(missing, str(uuid.uuid4()))

    without_authority = SimpleNamespace(
        executor_db_root=config.executor_db_root, executor_authority_root=None
    )
    assert prepare_dynamic_runtime_roots(without_authority, str(uuid.uuid4()))[1] is None


def test_remove_roots_only_touches_the_runtime_private_subdirectories(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    database_root, authority_root = prepare_dynamic_runtime_roots(config, RUNTIME_ID)
    (database_root / "x.sqlite3").write_bytes(b"")
    other = prepare_dynamic_runtime_roots(config, str(uuid.uuid4()))
    operator_file = config.executor_db_root / "orchestrator.sqlite3"
    operator_file.write_bytes(b"")

    remove_dynamic_runtime_roots(config, RUNTIME_ID)

    assert not database_root.exists() and not authority_root.exists()
    assert other[0].is_dir() and other[1].is_dir()
    assert operator_file.is_file()
    # Not UUID-shaped / symlinked / absent: silently ignored.
    remove_dynamic_runtime_roots(config, "../escape")
    remove_dynamic_runtime_roots(config, str(uuid.uuid4()))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    linked = str(uuid.uuid4())
    (config.executor_db_root / linked).symlink_to(elsewhere)
    remove_dynamic_runtime_roots(config, linked)
    assert elsewhere.is_dir() and (config.executor_db_root / linked).is_symlink()
    remove_dynamic_runtime_roots(SimpleNamespace(), RUNTIME_ID)


def test_retention_days_defaults_and_never_collapses_to_zero_on_a_typo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(RETENTION_ENV, raising=False)
    assert retention_days() == DEFAULT_RETENTION_DAYS == 30
    assert retention_days({RETENTION_ENV: ""}) == 30
    assert retention_days({RETENTION_ENV: " 7 "}) == 7
    assert retention_days({RETENTION_ENV: "0"}) == 0
    for bad in ("-1", "1.5", "abc", "99999", "1e3"):
        assert retention_days({RETENTION_ENV: bad}) == 30


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    for item in [path, *path.iterdir()]:
        os.utime(item, (stamp, stamp), follow_symlinks=False)


def test_sweep_removes_only_expired_non_running_runtime_roots(tmp_path: Path) -> None:
    config = _config(tmp_path)
    day = 86_400
    expired = str(uuid.uuid4())
    fresh = str(uuid.uuid4())
    running = str(uuid.uuid4())
    touched = str(uuid.uuid4())
    for runtime in (expired, fresh, running, touched):
        db, anchor = prepare_dynamic_runtime_roots(config, runtime)
        (db / "r.sqlite3").write_bytes(b"")
        (anchor / "r.anchor").write_bytes(b"")
    for runtime in (expired, running, touched):
        _age(config.executor_db_root / runtime, 31 * day)
        _age(config.executor_authority_root / runtime, 31 * day)
    # A directory whose mtime is old but whose newest file is recent is live,
    # and liveness of EITHER root keeps BOTH of the runtime's roots.
    (config.executor_db_root / touched / "r.sqlite3").write_bytes(b"x")
    operator_dir = config.executor_db_root / "not-a-runtime"
    operator_dir.mkdir()
    _age(operator_dir, 400 * day)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _age(elsewhere, 400 * day)
    linked = str(uuid.uuid4())
    (config.executor_db_root / linked).symlink_to(elsewhere)

    removed = sweep_dynamic_runtime_roots(
        config, keep=frozenset({running}), environ={}
    )

    assert removed == 2  # expired db root + expired authority root
    assert not (config.executor_db_root / expired).exists()
    assert not (config.executor_authority_root / expired).exists()
    for runtime in (fresh, running):
        assert (config.executor_db_root / runtime).is_dir()
        assert (config.executor_authority_root / runtime).is_dir()
    assert (config.executor_db_root / touched).is_dir()
    assert (config.executor_authority_root / touched).is_dir()
    assert operator_dir.is_dir() and elsewhere.is_dir()
    assert (config.executor_db_root / linked).is_symlink()

    # Retention 0 sweeps every non-running runtime root at boot.
    removed = sweep_dynamic_runtime_roots(
        config, keep=frozenset({running}), environ={RETENTION_ENV: "0"}
    )
    assert removed == 4
    assert (config.executor_db_root / running).is_dir()
    # Missing roots are a no-op, never an exception.
    assert sweep_dynamic_runtime_roots(SimpleNamespace(), environ={}) == 0
    absent = SimpleNamespace(executor_db_root=tmp_path / "absent", executor_authority_root=None)
    assert sweep_dynamic_runtime_roots(absent, environ={}) == 0


# --------------------------------------------------------------------------
# Environment contents
# --------------------------------------------------------------------------


def _executor_source_variables() -> set[str]:
    source = Path(executor.__file__).read_text(encoding="utf-8")
    host_block = source[source.index("host = HostBinding(") : source.index("registry = PublicKeyRegistry()")]
    return set(re.findall(r'"((?:ASTRAL|LETS)_[A-Z_]+)"', host_block))


def test_environment_carries_exactly_what_the_child_executor_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRAL_RUNTIME_ID", "stale-from-parent")
    monkeypatch.setenv("LETS_EXECUTOR_AUTHORITY_ROOT", "/stale/parent/root")
    base = {"PATH": "/usr/bin", "LETS_MODE": "enforce", "ASTRAL_RUNTIME_COHORT": "byo_user"}
    authority = _authority()
    database_root, authority_root = prepare_dynamic_runtime_roots(
        _config(tmp_path), RUNTIME_ID
    )

    env = dynamic_runtime_environment(
        base,
        authority,
        warden_id="warden-a",
        database_root=database_root,
        authority_root=authority_root,
    )

    assert env == {
        "PATH": "/usr/bin",
        "LETS_MODE": "enforce",
        "ASTRAL_AUTHORITY_OWNER_ID": "owner-a",
        "ASTRAL_AUTHORITY_BINDING_ID": "binding-a",
        "ASTRAL_AUTHORITY_LEASE_ID": "lease-a",
        "ASTRAL_AUTHORITY_LINEAGE_ID": "lineage-a",
        "ASTRAL_RUNTIME_COHORT": "server_dynamic",
        "ASTRAL_RUNTIME_ID": RUNTIME_ID,
        "ASTRAL_RUNTIME_GENERATION": "3",
        "LETS_EXECUTOR_INSTANCE_ID": authority.executor_audience,
        "LETS_EXECUTOR_DB_ROOT": str(database_root),
        "LETS_EXECUTOR_AUTHORITY_ROOT": str(authority_root),
        "LETS_WARDEN_ID": "warden-a",
    }
    assert base == {"PATH": "/usr/bin", "LETS_MODE": "enforce", "ASTRAL_RUNTIME_COHORT": "byo_user"}
    # Every variable load_protected_executor reads for its HostBinding/roots is
    # set; ASTRAL_ENV is operator posture the child inherits unchanged.
    assert _executor_source_variables() - {"ASTRAL_ENV"} <= set(env)

    # A dev posture (no authority root) must not inherit the parent's root.
    dev_env = dynamic_runtime_environment(
        None, authority, warden_id="warden-a", database_root=database_root, authority_root=None
    )
    assert "LETS_EXECUTOR_AUTHORITY_ROOT" not in dev_env
    assert dev_env["ASTRAL_RUNTIME_ID"] == RUNTIME_ID


# --------------------------------------------------------------------------
# Audience agreement: orchestrator receipt <-> child executor
# --------------------------------------------------------------------------


def _permit_for(manifest, policy, warden, *, audience: str, arguments: dict[str, Any]):
    nonce = "cd" * 16
    context = {
        "type": executor.EVIDENCE_TYPE,
        "operation_id": "operation-b",
        "agent_id": AGENT_ID,
        "runtime_id": RUNTIME_ID,
        "tool_id": "write_value",
        "scope": "tools:write",
        "capability": "astral.tools.write",
        "transition": "tool_write",
        "resource_dimension": 1,
        "executor_audience": audience,
        "channel": "a2a",
        "audit_correlation_id": "audit-b",
        "scope_profile_sha256": executor.SCOPE_PROFILE_SHA256,
        "authorized_effect_sha256": "1" * 64,
        "effect_sha256": "",
    }
    context["effect_sha256"] = executor._recompute_effect_sha256(
        context, expected_sequence=0, nonce=nonce
    )
    now = time.time_ns()
    receipt = Receipt(
        tenant_id=manifest.tenant_id,
        envelope_id=manifest.envelope_id,
        config_epoch=manifest.config_epoch,
        receipt_id="receipt-b",
        request_id="operation-b",
        warden_id=warden.warden_id,
        key_id=warden.key_id,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_digest=policy.digest,
        machine_digest=policy.machine.digest,
        lease_id="lease-a",
        lineage_id="lineage-a",
        subject_id=AGENT_ID,
        executor_audience=audience,
        transition="tool_write",
        source_state="ready",
        target_state="ready",
        cost=(0, 1, 0, 0, 0, 0),
        resulting_sequence=1,
        evidence_digest=canonical_digest(context),
        nonce=nonce,
        issued_at_ns=now - 1_000_000_000,
        expires_at_ns=now + 30_000_000_000,
    )
    receipt = replace(
        receipt,
        signature=b64url_encode(warden.sign(canonical_json(receipt.unsigned_payload()))),
    )
    return {
        "type": executor.PERMIT_TYPE,
        "binding_id": "binding-a",
        "owner_id": "owner-a",
        "runtime_generation": 3,
        "context": context,
        "expected_sequence": 0,
        "nonce": nonce,
        "wire_arguments_sha256": hashlib.sha256(
            executor._stable_canonical_bytes(arguments)
        ).hexdigest(),
        "receipt": receipt.to_dict(),
    }


def test_child_executor_claims_receipts_issued_for_its_per_runtime_audience(
    tmp_path: Path,
) -> None:
    parent_values, manifest, policy, warden = _environment(tmp_path)
    parent_values["LETS_EXECUTOR_INSTANCE_ID"] = INSTANCE_ID
    config = SimpleNamespace(
        executor_db_root=Path(parent_values["LETS_EXECUTOR_DB_ROOT"]),
        executor_authority_root=Path(parent_values["LETS_EXECUTOR_AUTHORITY_ROOT"]),
    )
    authority = _authority()
    database_root, authority_root = prepare_dynamic_runtime_roots(config, RUNTIME_ID)
    child_env = dynamic_runtime_environment(
        parent_values,
        authority,
        warden_id=warden.warden_id,
        database_root=database_root,
        authority_root=authority_root,
    )
    dispatch_runtime = DispatchRuntime(
        owner_id=authority.owner_id,
        agent_id=authority.agent_id,
        population=authority.population,
        runtime_id=authority.runtime_id,
        runtime_generation=authority.runtime_generation,
        executor_audience=authority.executor_audience,
        executor_conformant=True,
        dispatch_posture="protected_executor",
    )
    arguments = {"value": "exact"}

    runtime = executor.load_protected_executor(child_env, agent_id=AGENT_ID)
    try:
        # The receipt the orchestrator requests for this DispatchRuntime verifies.
        runtime.verify_and_claim(
            metadata=_permit_for(
                manifest, policy, warden,
                audience=dispatch_runtime.executor_audience, arguments=arguments,
            ),
            final_arguments=arguments,
            tool_id="write_value",
            tool_scope="tools:write",
        )
        # A receipt minted for the shared orchestrator audience (pre-fix
        # behaviour) is refused by the child.
        with pytest.raises(
            executor.ProtectedExecutorError, match="^executor_host_binding_mismatch$"
        ):
            runtime.verify_and_claim(
                metadata=_permit_for(
                    manifest, policy, warden, audience=INSTANCE_ID, arguments=arguments
                ),
                final_arguments=arguments,
                tool_id="write_value",
                tool_scope="tools:write",
            )
    finally:
        runtime.close()

    # Replay state landed in THIS runtime's private roots only.
    instance_key = hashlib.sha256(authority.executor_audience.encode()).hexdigest()
    assert (database_root / f"{instance_key}.sqlite3").is_file()
    assert (authority_root / f"{instance_key}.anchor").exists()
    assert not list(config.executor_db_root.glob("*.sqlite3"))


def test_hand_off_environment_loads_as_the_child_base_agent_config(
    tmp_path: Path,
) -> None:
    """``BaseA2AAgent`` path: ``load_lets_config(env)`` + ``create_executor_gateway``.

    The spawned child re-parses the hand-off through the strict production
    config loader (manifest authentication, data-root checks, identifier
    bounds) and builds its verifier from it; the audience and roots it ends
    up with must be exactly the per-runtime ones the orchestrator published.
    """

    parent_values, manifest, policy, warden = _environment(tmp_path)
    parent_values["LETS_EXECUTOR_INSTANCE_ID"] = INSTANCE_ID
    token = tmp_path / "service-token"
    token.write_bytes(b"\xff\x00opaque-token")
    parent_values.update(
        {
            "LETS_WARDEN_URL": "https://warden-a.example",
            "LETS_SERVICE_TOKEN_FILE": str(token),
            "LETS_GOVERNED_COHORTS": "server_dynamic",
            "LETS_DEFAULT_ALLOCATION": "1,1,1,1,1,1",
            "LETS_DEFAULT_TTL_SECONDS": "30",
            "LETS_REQUEST_TIMEOUT_SECONDS": "2.5",
            "LETS_REQUEST_ATTEMPTS": "2",
        }
    )
    config = SimpleNamespace(
        executor_db_root=Path(parent_values["LETS_EXECUTOR_DB_ROOT"]),
        executor_authority_root=Path(parent_values["LETS_EXECUTOR_AUTHORITY_ROOT"]),
    )
    authority = _authority()
    database_root, authority_root = prepare_dynamic_runtime_roots(config, RUNTIME_ID)
    child_env = dynamic_runtime_environment(
        parent_values,
        authority,
        warden_id=warden.warden_id,
        database_root=database_root,
        authority_root=authority_root,
    )

    loaded = load_lets_config(child_env)
    assert loaded.config is not None, loaded.readiness
    assert loaded.readiness.status == "configured"
    assert loaded.config.mode == "enforce"
    assert loaded.config.executor_instance_id == authority.executor_audience
    assert loaded.config.executor_db_root == database_root
    assert loaded.config.executor_authority_root == authority_root
    assert loaded.config.trust_manifest is not None
    assert loaded.config.trust_manifest.warden_id == warden.warden_id

    runtime = create_executor_gateway(loaded.config)
    try:
        assert runtime.authority_anchor is not None
    finally:
        runtime.close()
    # Replay state landed in THIS runtime's private roots, under the
    # per-runtime audience, and nowhere in the orchestrator's shared roots.
    assert (database_root / f"{authority.executor_audience}.sqlite3").is_file()
    assert (authority_root / f"{authority.executor_audience}.anchor").exists()
    assert not list(config.executor_db_root.glob("*.sqlite3"))
    assert not list(config.executor_authority_root.glob("*.anchor"))
    # Parent-only identity never leaks into the child config.
    assert loaded.config.executor_instance_id != INSTANCE_ID


# --------------------------------------------------------------------------
# Lifecycle wiring: start_draft_agent
# --------------------------------------------------------------------------


class _Supervisor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def spawn(self, **values: Any) -> SimpleNamespace:
        self.calls.append(values)
        return SimpleNamespace(returncode=None, poll=lambda: None, terminate=lambda **_: None)


class _Orchestrator:
    def __init__(self) -> None:
        self.registered: list[DispatchRuntime] = []
        self.agents: dict[str, Any] = {}
        self.agent_urls: dict[str, str] = {}
        self.tool_permissions = SimpleNamespace(
            set_agent_scopes=lambda *a, **k: None,
            get_tool_scope_map=lambda *a, **k: {},
            set_tool_permission=lambda *a, **k: None,
        )

    def register_governed_dispatch_runtime(self, runtime: DispatchRuntime) -> None:
        self.registered.append(runtime)

    def unregister_governed_dispatch_runtime(self, agent_id: str) -> None:
        return None

    async def discover_agent(self, url: str) -> None:
        return None


class _Coordinator:
    def __init__(self, config: Any, *, binding: Any = None) -> None:
        self.service = SimpleNamespace(config=config)
        self.binding = binding
        self.admitted: list[dict[str, Any]] = []

    async def admit_new_runtime(self, **values: Any) -> LifecycleConvergence:
        self.admitted.append(values)
        if self.binding is None:
            return LifecycleConvergence(protected=False)
        binding = self.binding(values["runtime_id"])
        return LifecycleConvergence(protected=True, binding=binding)

    async def quiesce_current(self, **_values: Any) -> LifecycleConvergence:
        return LifecycleConvergence(protected=False)

    async def close_current(self, **_values: Any) -> LifecycleConvergence:
        self.closed = getattr(self, "closed", 0) + 1
        return LifecycleConvergence(protected=False)


def _manager(tmp_path: Path, coordinator: Any, monkeypatch: pytest.MonkeyPatch):
    draft = {
        "id": "draft-a",
        "user_id": "owner-a",
        "agent_name": "Agent A",
        "agent_slug": "agent_a",
        "origin": "server",
        "status": GENERATED,
        "port": None,
    }
    orchestrator = _Orchestrator()
    supervisor = _Supervisor()
    manager = AgentLifecycleManager(
        LifecycleStore([], draft),
        orchestrator=orchestrator,
        process_supervisor=supervisor,
    )
    agents_dir = tmp_path / "agents"
    agent_dir = agents_dir / "agent_a"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent_a_agent.py").write_text("print('ready')\n", encoding="utf-8")
    (agent_dir / "mcp_tools.py").write_text(
        "TOOL_REGISTRY = {'write': {'scope': 'tools:write'}}\n", encoding="utf-8"
    )
    manager._agents_dir = str(agents_dir)
    manager.governed_lifecycle = coordinator

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("orchestrator.agent_lifecycle.asyncio.sleep", no_sleep)
    monkeypatch.setattr(manager, "_find_next_port", lambda: 8123)
    return manager, orchestrator, supervisor


def _active_binding_for(runtime_id: str) -> SimpleNamespace:
    return _binding(agent_id="agent-a-1", runtime_id=runtime_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "enforce"])
async def test_start_draft_agent_hands_binding_and_per_runtime_audience_to_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "false")
    monkeypatch.setenv("LETS_MODE", mode)
    config = _config(tmp_path)
    config.mode = mode
    coordinator = _Coordinator(config, binding=_active_binding_for)
    manager, orchestrator, supervisor = _manager(tmp_path, coordinator, monkeypatch)

    await manager.start_draft_agent("draft-a")

    assert len(supervisor.calls) == 1
    spawn = supervisor.calls[0]
    runtime_id = str(spawn["process_id"])
    env = spawn["env"]
    assert coordinator.admitted[0]["runtime_id"] == runtime_id
    expected_audience = derive_dynamic_executor_audience(INSTANCE_ID, runtime_id)
    assert env["ASTRAL_RUNTIME_ID"] == runtime_id
    assert env["ASTRAL_RUNTIME_COHORT"] == "server_dynamic"
    assert env["ASTRAL_RUNTIME_GENERATION"] == "3"
    assert env["ASTRAL_AUTHORITY_OWNER_ID"] == "owner-a"
    assert env["ASTRAL_AUTHORITY_BINDING_ID"] == "binding-a"
    assert env["ASTRAL_AUTHORITY_LEASE_ID"] == "lease-a"
    assert env["ASTRAL_AUTHORITY_LINEAGE_ID"] == "lineage-a"
    assert env["LETS_WARDEN_ID"] == "warden-a"
    assert env["LETS_EXECUTOR_INSTANCE_ID"] == expected_audience
    assert env["LETS_EXECUTOR_DB_ROOT"] == str(config.executor_db_root / runtime_id)
    assert env["LETS_EXECUTOR_AUTHORITY_ROOT"] == str(
        config.executor_authority_root / runtime_id
    )
    for root in (config.executor_db_root, config.executor_authority_root):
        assert stat.S_IMODE((root / runtime_id).stat().st_mode) == 0o700
    # The orchestrator's receipt audience for this runtime is the child's.
    assert [r.executor_audience for r in orchestrator.registered] == [expected_audience]
    assert orchestrator.registered[0].runtime_id == runtime_id
    assert orchestrator.registered[0].agent_id == "agent-a-1"
    # Nothing secret was added: only the documented hand-off keys differ from
    # the inherited process environment.
    inherited = {k: v for k, v in env.items() if k in os.environ}
    added = set(env) - set(inherited)
    assert added <= {
        "ASTRAL_AUTHORITY_OWNER_ID", "ASTRAL_AUTHORITY_BINDING_ID",
        "ASTRAL_AUTHORITY_LEASE_ID", "ASTRAL_AUTHORITY_LINEAGE_ID",
        "ASTRAL_RUNTIME_COHORT", "ASTRAL_RUNTIME_ID", "ASTRAL_RUNTIME_GENERATION",
        "LETS_EXECUTOR_INSTANCE_ID", "LETS_EXECUTOR_DB_ROOT",
        "LETS_EXECUTOR_AUTHORITY_ROOT", "LETS_WARDEN_ID",
    }


@pytest.mark.asyncio
async def test_hand_off_layers_over_the_sandbox_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "true")
    monkeypatch.setenv("LETS_MODE", "enforce")
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "must-not-reach-child")
    coordinator = _Coordinator(_config(tmp_path), binding=_active_binding_for)
    manager, _orchestrator, supervisor = _manager(tmp_path, coordinator, monkeypatch)

    await manager.start_draft_agent("draft-a")

    env = supervisor.calls[0]["env"]
    assert "CREDENTIAL_ENCRYPTION_KEY" not in env
    assert env["TMPDIR"].endswith("_sandbox_tmp")
    assert env["ASTRAL_RUNTIME_ID"] == str(supervisor.calls[0]["process_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox", ["false", "true"])
async def test_no_active_binding_leaves_spawn_kwargs_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox: str
) -> None:
    """Off / shadow-degraded / ungoverned cohort: the child env is untouched."""

    monkeypatch.setenv("FF_SANDBOX_CODEGEN", sandbox)
    monkeypatch.delenv("LETS_MODE", raising=False)
    for key in ("ASTRAL_RUNTIME_ID", "ASTRAL_RUNTIME_COHORT", "LETS_EXECUTOR_INSTANCE_ID"):
        monkeypatch.delenv(key, raising=False)
    config = _config(tmp_path)
    config.mode = "shadow"
    coordinator = _Coordinator(config, binding=None)
    manager, orchestrator, supervisor = _manager(tmp_path, coordinator, monkeypatch)

    await manager.start_draft_agent("draft-a")

    spawn = supervisor.calls[0]
    assert orchestrator.registered == []
    assert not any(config.executor_db_root.iterdir())
    if sandbox == "false":
        assert "env" not in spawn and "preexec_fn" not in spawn
    else:
        from orchestrator import sandbox as _sandbox

        expected = _sandbox.sandbox_env(
            None, str(tmp_path / "agents" / "agent_a" / "_sandbox_tmp")
        )
        assert spawn["env"] == expected
    assert not any(k.startswith("ASTRAL_AUTHORITY_") for k in spawn.get("env", {}))


class _Socket:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_text(self, text: str) -> None:
        import json

        self.frames.append(json.loads(text))


@pytest.mark.asyncio
async def test_enforce_refuses_to_spawn_when_hand_off_cannot_be_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "false")
    monkeypatch.setenv("LETS_MODE", "enforce")
    config = _config(tmp_path)
    config.executor_instance_id = None
    coordinator = _Coordinator(config, binding=_active_binding_for)
    manager, orchestrator, supervisor = _manager(tmp_path, coordinator, monkeypatch)
    socket = _Socket()

    with pytest.raises(LetsLifecycleError, match="^executor_audience_unavailable$"):
        await manager.start_draft_agent("draft-a", socket)
    assert supervisor.calls == []
    assert orchestrator.registered == []
    # The granted lease is released rather than left dangling.
    assert coordinator.closed == 1
    # Refusal UX matches the post-spawn death branch: the draft row records
    # the error and the client receives the progress error frame.
    draft = manager.db.get_draft_agent("draft-a")
    assert draft["status"] == "error"
    assert "executor_audience_unavailable" in draft["error_message"]
    errors = [f for f in socket.frames if f["step"] == "error"]
    assert len(errors) == 1
    assert errors[0]["type"] == "agent_creation_progress"
    assert errors[0]["draft_id"] == "draft-a"
    assert errors[0]["status"] == "error"
    assert "executor_audience_unavailable" in errors[0]["message"]
    assert "executor_audience_unavailable" in str(draft.get("generation_log") or "")


@pytest.mark.asyncio
async def test_enforce_refusal_after_roots_were_prepared_removes_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing ever ran under roots prepared for a refused runtime."""

    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "false")
    monkeypatch.setenv("LETS_MODE", "enforce")
    config = _config(tmp_path)
    config.trust_manifest = SimpleNamespace(warden_id="bad warden")  # child would refuse
    coordinator = _Coordinator(config, binding=_active_binding_for)
    manager, orchestrator, supervisor = _manager(tmp_path, coordinator, monkeypatch)

    with pytest.raises(LetsLifecycleError, match="^warden_id_invalid$"):
        await manager.start_draft_agent("draft-a")
    assert supervisor.calls == []
    assert orchestrator.registered == []
    assert not any(config.executor_db_root.iterdir())
    assert not any(config.executor_authority_root.iterdir())


@pytest.mark.asyncio
async def test_shadow_keeps_coverage_when_hand_off_cannot_be_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Shadow hand-off miss: plain spawn, shared-audience registration, warning + audit."""

    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "false")
    monkeypatch.setenv("LETS_MODE", "shadow")
    config = _config(tmp_path)
    config.mode = "shadow"
    config.executor_db_root = tmp_path / "absent"
    coordinator = _Coordinator(config, binding=_active_binding_for)
    manager, orchestrator, supervisor = _manager(tmp_path, coordinator, monkeypatch)
    audited: list[tuple[str, dict[str, Any]]] = []

    class _Observer:
        def __init__(self, **values: Any) -> None:
            self.values = values

        async def __call__(self, event: str, evidence: dict[str, Any]) -> None:
            audited.append((event, {**evidence, "_observer": self.values}))

    monkeypatch.setattr("orchestrator.lets_audit.LetsAuditObserver", _Observer)

    import logging

    with caplog.at_level(logging.WARNING, logger="AstralDeep"):
        await manager.start_draft_agent("draft-a")

    spawn = supervisor.calls[0]
    runtime_id = str(spawn["process_id"])
    assert "env" not in spawn
    # Coverage is kept: the DispatchRuntime is registered with the SHARED
    # audience exactly as before §7.9, so would-deny telemetry still sees it.
    assert len(orchestrator.registered) == 1
    registered = orchestrator.registered[0]
    assert registered.executor_audience == INSTANCE_ID
    assert registered.runtime_id == runtime_id
    assert registered.agent_id == "agent-a-1"
    assert registered.population == "server_dynamic"
    assert registered.runtime_generation == 3
    # A WARNING names the reason and says enforce would refuse.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "LETS shadow" in r.getMessage()]
    assert warnings and "executor_db_root_unavailable" in warnings[0].getMessage()
    assert "enforce would refuse" in warnings[0].getMessage()
    # And one value-free would_deny audit line correlated by runtime id.
    assert [event for event, _ in audited] == ["would_deny"]
    evidence = audited[0][1]
    assert evidence["audit_correlation_id"] == runtime_id
    assert evidence["runtime_id"] == runtime_id
    assert evidence["code"] == "executor_db_root_unavailable"
    assert evidence["enforced"] is False
    assert evidence["binding_id"] == "binding-a"
    assert evidence["_observer"]["strict"] is False
    assert evidence["_observer"]["actor_user_id"] == "owner-a"
    # Refusal is NOT surfaced as an error in shadow.
    assert manager.db.get_draft_agent("draft-a")["status"] == "testing"


@pytest.mark.asyncio
async def test_shadow_without_shared_audience_registers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No audience at all: nothing to register (pre-existing shadow behaviour)."""

    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "false")
    monkeypatch.setenv("LETS_MODE", "shadow")
    config = _config(tmp_path)
    config.mode = "shadow"
    config.executor_instance_id = None
    coordinator = _Coordinator(config, binding=_active_binding_for)
    manager, orchestrator, supervisor = _manager(tmp_path, coordinator, monkeypatch)

    await manager.start_draft_agent("draft-a")

    assert "env" not in supervisor.calls[0]
    assert orchestrator.registered == []


def test_manager_boot_sweep_respects_running_runtimes_and_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(RETENTION_ENV, raising=False)
    config = _config(tmp_path)
    coordinator = _Coordinator(config, binding=_active_binding_for)
    manager, _orchestrator, _supervisor = _manager(tmp_path, coordinator, monkeypatch)
    day = 86_400
    expired = str(uuid.uuid4())
    running = str(uuid.uuid4())
    for runtime in (expired, running):
        prepare_dynamic_runtime_roots(config, runtime)
        _age(config.executor_db_root / runtime, 40 * day)
        _age(config.executor_authority_root / runtime, 40 * day)
    manager._draft_processes["draft-a"] = SimpleNamespace(process_id=uuid.UUID(running))

    assert manager.sweep_dynamic_runtime_roots() == 2
    assert not (config.executor_db_root / expired).exists()
    assert (config.executor_db_root / running).is_dir()
    assert (config.executor_authority_root / running).is_dir()

    # LETS off / no coordinator: nothing is touched.
    prepare_dynamic_runtime_roots(config, expired)
    _age(config.executor_db_root / expired, 40 * day)
    config.mode = "off"
    assert manager.sweep_dynamic_runtime_roots() == 0
    manager.governed_lifecycle = None
    assert manager.sweep_dynamic_runtime_roots() == 0
    assert (config.executor_db_root / expired).is_dir()
