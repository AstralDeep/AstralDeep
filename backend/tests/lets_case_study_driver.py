"""Exact-composition driver for the local AstralDeep/LETS case study.

The tracked LETS harness starts this file once per scenario.  This entrypoint
accepts one strict, bounded JSON request and emits one content-free result.  It
uses the real Deep lifecycle, governed-dispatch, authorization-gateway, and
public receipt-verifier boundaries with isolated deterministic test services;
the observations below are therefore collected from executed calls rather
than copied from the scenario's expected fields.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Literal

# ``python backend/tests/lets_case_study_driver.py`` is the documented harness
# invocation.  Make that direct entrypoint resolve the same Deep packages as a
# pytest process whose working directory is ``backend``.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = _BACKEND_ROOT.parent
_ASTRALPLANE_SOURCE_ROOT = _REPOSITORY_ROOT / "components/AstralPlane/src"
_LETS_SOURCE_ROOT = _REPOSITORY_ROOT / "components/LETS/src"
for source_root in (_BACKEND_ROOT, _ASTRALPLANE_SOURCE_ROOT, _LETS_SOURCE_ROOT):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
# LETS authority-anchor helpers start with ``python -m``. Give only those
# descendants the same fixed source roots; the runner removes caller-provided
# PYTHONPATH before this process starts.
os.environ["PYTHONPATH"] = os.pathsep.join(
    str(root) for root in (_BACKEND_ROOT, _ASTRALPLANE_SOURCE_ROOT, _LETS_SOURCE_ROOT)
)

import astralplane as astralplane_package  # noqa: E402
import lets as lets_package  # noqa: E402
from astralplane.authority import AuthorityPopulation  # noqa: E402
from lets.canonical import b64url_encode, canonical_digest, canonical_json  # noqa: E402
from lets.models import Receipt  # noqa: E402

from orchestrator.governed_dispatch import (  # noqa: E402
    DispatchRuntime,
    GovernedDispatchError,
    GovernedFinalDispatch,
)
from orchestrator.lets_client import LetsClientBoundaryError  # noqa: E402
from orchestrator.lets_gateway import (  # noqa: E402
    LETS_CALLER_CAPABILITY,
    LetsGatewayError,
)
from orchestrator.lets_lifecycle import (  # noqa: E402
    GovernedRuntime,
    LetsLifecycleService,
)
from orchestrator.lets_scope_profile import (  # noqa: E402
    RESOURCE_DIMENSIONS,
    SCOPE_BINDINGS,
    ScopeBinding,
    binding_for_scope,
)
from tests.lets_conformance_support import (  # noqa: E402
    MACHINE_DIGEST,
    POLICY_DIGEST,
    build_rig,
    host_arguments,
)
from tests.test_lets_lifecycle import (  # noqa: E402
    LifecycleClient,
    MemoryAuthorityRepository,
    MemoryPlane,
    _config as lifecycle_config,
)


SCENARIO_FORMAT = "lets.astraldeep-case-study-scenario/v1"
RESULT_FORMAT = "lets.astraldeep-case-study-result/v1"
EXECUTION_IDENTITY_FORMAT = "lets.astraldeep-execution-identity/v1"
DRIVER_RELATIVE_PATH = "backend/tests/lets_case_study_driver.py"
COMPOSITION_RELATIVE_PATH = "config/astral-composition.json"
MODES = ("off", "shadow", "enforce")
TOOL_SCOPES = tuple(binding.scope for binding in SCOPE_BINDINGS)
LIFECYCLE_EVENTS = (
    "provision",
    "spawn",
    "renew",
    "quiesce",
    "resume",
    "close",
    "revoke",
)
FAULT_SCENARIOS = (
    "warden-outage",
    "receipt-replay",
    "budget-exhaustion",
    "post-revocation-effect",
)
_REQUEST_KEYS = {
    "format",
    "scenario_id",
    "mode",
    "category",
    "scope",
    "lifecycle_event",
    "parallelism",
    "recursion_depth",
    "expected_effect",
    "expected_lets_behavior",
    "expected_lifecycle_state",
}
_MAX_STDIN_BYTES = 64 * 1024
_MAX_COUNTER = 100_000
_SAFE_ARGUMENTS: dict[str, object] = {"case": "synthetic", "limit": 1}
_SOURCE_SUFFIXES = frozenset({".py", ".pyi"})
_FAULT_DENIAL_CODES = {
    "warden-outage": "warden_unavailable",
    "receipt-replay": "receipt_replayed",
    "budget-exhaustion": "budget_exhausted",
    "post-revocation-effect": "binding_unavailable",
}
_Mode = Literal["off", "shadow", "enforce"]


class DriverError(RuntimeError):
    """Stable, value-free refusal safe for the retained stderr artifact."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise DriverError("execution_identity_unavailable") from None
    return digest.hexdigest()


def _source_tree_identity(root: Path) -> dict[str, object]:
    try:
        resolved = root.resolve(strict=True)
    except OSError:
        raise DriverError("execution_identity_unavailable") from None
    records: list[dict[str, str]] = []
    try:
        candidates = sorted(
            (
                path
                for path in resolved.rglob("*")
                if path.is_file() and path.suffix in _SOURCE_SUFFIXES
            ),
            key=lambda path: path.relative_to(resolved).as_posix(),
        )
        for path in candidates:
            if path.is_symlink():
                raise DriverError("execution_identity_unavailable")
            records.append(
                {
                    "relative_path": path.relative_to(resolved).as_posix(),
                    "sha256": _sha256_file(path),
                }
            )
    except OSError:
        raise DriverError("execution_identity_unavailable") from None
    if not records:
        raise DriverError("execution_identity_unavailable")
    payload = json.dumps(
        records,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return {
        "file_count": len(records),
        "tree_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _strict_json_file(path: Path) -> dict[str, object]:
    def reject_constant(_value: str) -> None:
        raise ValueError

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise DriverError("execution_identity_unavailable") from None
    if not isinstance(value, dict):
        raise DriverError("execution_identity_unavailable")
    return value


def _git_head() -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(_REPOSITORY_ROOT), "rev-parse", "--verify", "HEAD"],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="ascii",
            errors="strict",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise DriverError("execution_identity_unavailable") from None
    commit = completed.stdout.strip()
    if (
        completed.returncode != 0
        or len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
    ):
        raise DriverError("execution_identity_unavailable")
    return commit


def _component_identity(
    *,
    package: object,
    component: Mapping[str, object],
    expected_package_root: Path,
    include_release: bool = False,
) -> dict[str, object]:
    module_file = getattr(package, "__file__", None)
    commit = component.get("commit")
    if (
        not isinstance(module_file, str)
        or not isinstance(commit, str)
        or len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
    ):
        raise DriverError("execution_identity_unavailable")
    imported_package_root = Path(module_file).resolve().parent
    if imported_package_root != expected_package_root.resolve(strict=True):
        raise DriverError("execution_identity_unavailable")
    identity = {
        "component_commit": commit,
        **_source_tree_identity(imported_package_root),
    }
    if include_release:
        release = component.get("ref")
        if not isinstance(release, str) or not release.startswith("v"):
            raise DriverError("execution_identity_unavailable")
        identity["release"] = release
    return identity


@lru_cache(maxsize=1)
def _execution_identity() -> dict[str, object]:
    expected_driver = (_REPOSITORY_ROOT / DRIVER_RELATIVE_PATH).resolve(strict=True)
    if Path(__file__).resolve(strict=True) != expected_driver:
        raise DriverError("execution_identity_unavailable")
    composition = _strict_json_file(_REPOSITORY_ROOT / COMPOSITION_RELATIVE_PATH)
    components = composition.get("components")
    if not isinstance(components, Mapping):
        raise DriverError("execution_identity_unavailable")
    plane_component = components.get("astral-plane")
    lets_component = components.get("lets")
    if not isinstance(plane_component, Mapping) or not isinstance(
        lets_component, Mapping
    ):
        raise DriverError("execution_identity_unavailable")
    executable = Path(sys.executable).resolve(strict=True)
    return {
        "format": EXECUTION_IDENTITY_FORMAT,
        "astraldeep": {
            "commit": _git_head(),
            "driver_relative_path": DRIVER_RELATIVE_PATH,
            "driver_sha256": _sha256_file(expected_driver),
        },
        "interpreter": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "executable_sha256": _sha256_file(executable),
        },
        "imports": {
            "astralplane": _component_identity(
                package=astralplane_package,
                component=plane_component,
                expected_package_root=_ASTRALPLANE_SOURCE_ROOT / "astralplane",
            ),
            "lets": _component_identity(
                package=lets_package,
                component=lets_component,
                expected_package_root=_LETS_SOURCE_ROOT / "lets",
                include_release=True,
            ),
        },
    }


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    mode: _Mode
    category: str
    scope: str | None = None
    lifecycle_event: str | None = None
    parallelism: int = 1
    recursion_depth: int = 0
    expected_effect: str = "none"
    expected_lets_behavior: str = "off"
    expected_lifecycle_state: str | None = None

    def to_request(self) -> dict[str, object]:
        return {"format": SCENARIO_FORMAT, **asdict(self)}


def _scenario_matrix(mode: _Mode) -> tuple[Scenario, ...]:
    behavior = {"off": "off", "shadow": "evaluate", "enforce": "enforce"}[mode]
    scenarios = [
        Scenario(
            scenario_id=f"{mode}-scope-{scope.removeprefix('tools:')}",
            mode=mode,
            category="scope",
            scope=scope,
            expected_effect="execute",
            expected_lets_behavior=behavior,
        )
        for scope in TOOL_SCOPES
    ]
    lifecycle_states = {
        "provision": "active",
        "spawn": "active",
        "renew": "active",
        "quiesce": "quiescent",
        "resume": "active",
        "close": "closed",
        "revoke": "revoked",
    }
    scenarios.extend(
        Scenario(
            scenario_id=f"{mode}-lifecycle-{event}",
            mode=mode,
            category="lifecycle",
            lifecycle_event=event,
            expected_lets_behavior=behavior,
            expected_lifecycle_state=lifecycle_states[event],
        )
        for event in LIFECYCLE_EVENTS
    )
    scenarios.extend(
        (
            Scenario(
                scenario_id=f"{mode}-parallel-dispatch",
                mode=mode,
                category="parallel-dispatch",
                scope="tools:execute",
                parallelism=4,
                expected_effect="execute",
                expected_lets_behavior=behavior,
            ),
            Scenario(
                scenario_id=f"{mode}-recursive-dispatch",
                mode=mode,
                category="recursive-dispatch",
                scope="tools:execute",
                recursion_depth=3,
                expected_effect="execute",
                expected_lets_behavior=behavior,
            ),
        )
    )
    scenarios.extend(
        Scenario(
            scenario_id=f"{mode}-{category}",
            mode=mode,
            category=category,
            scope="tools:execute",
            expected_effect="deny" if mode == "enforce" else "execute",
            expected_lets_behavior=behavior,
        )
        for category in FAULT_SCENARIOS
    )
    return tuple(scenarios)


def _strict_json(payload: bytes) -> dict[str, object]:
    if not payload or len(payload) > _MAX_STDIN_BYTES:
        raise DriverError("invalid_input_size")

    def reject_constant(_value: str) -> None:
        raise ValueError

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise DriverError("invalid_scenario_json") from None
    if not isinstance(value, dict) or set(value) != _REQUEST_KEYS:
        raise DriverError("invalid_scenario_shape")
    return value


def _load_scenario(payload: bytes) -> Scenario:
    request = _strict_json(payload)
    scenario_id = request.get("scenario_id")
    mode = request.get("mode")
    if not isinstance(scenario_id, str) or mode not in MODES:
        raise DriverError("unsupported_scenario")
    expected = {item.scenario_id: item for item in _scenario_matrix(mode)}.get(
        scenario_id
    )
    if expected is None:
        raise DriverError("unsupported_scenario")
    expected_request = expected.to_request()
    for key, expected_value in expected_request.items():
        actual = request[key]
        if type(actual) is not type(expected_value) or actual != expected_value:
            raise DriverError("scenario_contract_mismatch")
    return expected


class _AuditSink:
    def __init__(self) -> None:
        self.count = 0

    async def record(self, _event: object) -> None:
        self.count += 1


@contextmanager
def _isolated_audit() -> Iterator[_AuditSink]:
    import audit.recorder as recorder_module

    sink = _AuditSink()
    original = recorder_module.get_recorder
    recorder_module.get_recorder = lambda: sink
    try:
        yield sink
    finally:
        recorder_module.get_recorder = original


class _MeasuringWarden:
    """Deterministic signed-receipt service with a measured finite budget."""

    def __init__(
        self,
        signer: Any,
        *,
        mode: _Mode,
        expected_binding: ScopeBinding,
        allocation: Sequence[int] | None = None,
        failure_code: str | None = None,
    ) -> None:
        selected = tuple(allocation or (32,) * RESOURCE_DIMENSIONS)
        if len(selected) != RESOURCE_DIMENSIONS:
            raise DriverError("invalid_test_allocation")
        self.signer = signer
        self.mode = mode
        self.expected_binding = expected_binding
        self.initial = selected
        self.remaining = list(selected)
        self.failure_code = failure_code
        self.requests = 0
        self.mapping_checks = 0
        self.issued_sequences: list[int] = []
        self.committed_costs: list[tuple[int, ...]] = []
        self.latencies_ns: list[int] = []
        self._lock = threading.Lock()

    def authorize_tool(self, **values: object) -> Receipt:
        started = time.perf_counter_ns()
        try:
            with self._lock:
                self.requests += 1
                evidence = values.get("evidence")
                if not isinstance(evidence, dict):
                    raise LetsClientBoundaryError("invalid_response")
                dimension = evidence.get("resource_dimension")
                transition = evidence.get("transition")
                expected = self.expected_binding
                if (
                    type(dimension) is not int
                    or values.get("declared_scope") != expected.scope
                    or evidence.get("scope") != expected.scope
                    or evidence.get("capability") != expected.capability
                    or transition != expected.transition
                    or dimension != expected.resource_dimension
                ):
                    raise LetsClientBoundaryError("scope_mapping_mismatch")
                self.mapping_checks += 1
                if self.failure_code is not None:
                    raise LetsClientBoundaryError(
                        self.failure_code,
                        retryable=self.failure_code != "budget_exhausted",
                    )
                if self.remaining[dimension] < 1:
                    raise LetsClientBoundaryError("budget_exhausted")
                cost = tuple(
                    1 if index == dimension else 0
                    for index in range(RESOURCE_DIMENSIONS)
                )
                if self.mode == "enforce":
                    self.remaining[dimension] -= 1
                    self.committed_costs.append(cost)
                sequence = int(values["expected_sequence"]) + 1
                receipt = Receipt(
                    tenant_id="tenant-a",
                    envelope_id="envelope-a",
                    config_epoch=7,
                    receipt_id=f"receipt-{values['operation_id']}",
                    request_id=str(values["operation_id"]),
                    warden_id=self.signer.warden_id,
                    key_id=self.signer.key_id,
                    policy_id="astral-policy",
                    policy_version="1",
                    policy_digest=POLICY_DIGEST,
                    machine_digest=MACHINE_DIGEST,
                    lease_id=str(values["lease_id"]),
                    lineage_id="lineage-a",
                    subject_id=str(values["agent_id"]),
                    executor_audience=str(values["executor_audience"]),
                    transition=transition,
                    source_state="ready",
                    target_state="ready",
                    cost=cost,
                    resulting_sequence=sequence,
                    evidence_digest=canonical_digest(evidence),
                    nonce=str(values["nonce"]),
                    issued_at_ns=90,
                    expires_at_ns=1_000,
                )
                signed = replace(
                    receipt,
                    signature=b64url_encode(
                        self.signer.sign(canonical_json(receipt.unsigned_payload()))
                    ),
                )
                self.issued_sequences.append(sequence)
                return signed
        finally:
            self.latencies_ns.append(max(1, time.perf_counter_ns() - started))

    def budget_conserved(self) -> bool:
        spent = [0] * RESOURCE_DIMENSIONS
        for cost in self.committed_costs:
            for index, value in enumerate(cost):
                spent[index] += value
        return all(
            self.remaining[index] >= 0
            and self.initial[index] == self.remaining[index] + spent[index]
            for index in range(RESOURCE_DIMENSIONS)
        )


class _EffectCounter:
    def __init__(self) -> None:
        self.physical = 0
        self.unreceipted_governed = 0
        self._lock = threading.Lock()

    def invoke(self, *, governed: bool, receipted: bool, binding: object) -> str:
        with self._lock:
            if governed and not receipted:
                self.unreceipted_governed += 1
            self.physical += 1
            if receipted:
                binding.lease_sequence += 1
        return "effect-complete"


@dataclass(slots=True)
class _LifecycleOutcome:
    requests: int
    call_trace: tuple[tuple[str, str], ...]
    state: str
    converged: bool
    budget_conserved: bool
    sequence_monotonic: bool
    duration_ns: int
    binding: object | None


class _HostLifecycle:
    """Minimal deterministic host state machine surrounding the LETS adapter."""

    def __init__(self) -> None:
        self.state = "new"

    def apply(self, event: str) -> None:
        allowed = {
            "provision": ({"new"}, "active"),
            "spawn": ({"new"}, "active"),
            "renew": ({"active", "quiescent"}, self.state),
            "quiesce": ({"active"}, "quiescent"),
            "resume": ({"quiescent"}, "active"),
            "close": ({"active", "quiescent"}, "closed"),
            "revoke": ({"active", "quiescent"}, "revoked"),
        }
        if event not in allowed:
            raise DriverError("unsupported_lifecycle_event")
        source_states, target_state = allowed[event]
        if self.state not in source_states:
            raise DriverError("invalid_host_lifecycle_transition")
        self.state = target_state


def _runtime(agent_id: str, runtime_id: str) -> GovernedRuntime:
    return GovernedRuntime(
        owner_id="owner-a",
        agent_id=agent_id,
        runtime_id=runtime_id,
        runtime_generation=1,
        population=AuthorityPopulation.SERVER_DYNAMIC,
        declared_scopes=("tools:read",),
    )


class _CaseStudyPlane:
    @contextmanager
    def transaction(self, **_options: object) -> Iterator[object]:
        yield object()


class _RevokedBindingRepository:
    """Expose no active row only after observing the lifecycle-produced revocation."""

    def __init__(self, binding: object | None) -> None:
        self.binding = binding
        self.inactive_observed = False

    def get_active_binding(
        self,
        _transaction: object,
        *,
        owner_id: str,
        agent_id: str,
        runtime_id: str,
        runtime_generation: int,
    ) -> object | None:
        binding = self.binding
        if binding is None:
            return None
        identity = (
            getattr(binding, "owner_id", None),
            getattr(binding, "agent_id", None),
            getattr(binding, "runtime_id", None),
            getattr(binding, "runtime_generation", None),
        )
        if identity != (owner_id, agent_id, runtime_id, runtime_generation):
            return None
        state = getattr(getattr(binding, "state", None), "value", None)
        if state != "active":
            self.inactive_observed = state == "revoked"
            return None
        return binding


async def _lifecycle_outcome(mode: _Mode, event: str) -> _LifecycleOutcome:
    client = LifecycleClient()
    repository = MemoryAuthorityRepository()
    active = mode != "off"
    service = LetsLifecycleService(
        config=lifecycle_config(mode=mode),
        plane=MemoryPlane() if active else None,
        repository=repository if active else None,  # type: ignore[arg-type]
        client=client if active else None,  # type: ignore[arg-type]
    )
    root_runtime = _runtime("agent-root", "runtime-root")
    sequence_history: dict[str, list[int]] = {}
    root_host = _HostLifecycle()
    target_host = root_host

    def observe(convergence: object) -> None:
        binding = getattr(convergence, "binding", None)
        if binding is not None:
            sequence_history.setdefault(binding.binding_id, []).append(
                binding.lease_sequence
            )

    async def provision_root() -> object:
        root_host.apply("provision")
        convergence = await service.provision(
            root_runtime,
            binding_id="binding-root",
            operation_id="operation-provision-root",
        )
        observe(convergence)
        return convergence

    started = time.perf_counter_ns()
    if event == "provision":
        convergence = await provision_root()
    elif event == "spawn":
        await provision_root()
        target_host = _HostLifecycle()
        target_host.apply("spawn")
        convergence = await service.spawn(
            _runtime("agent-child", "runtime-child"),
            parent_binding_id="binding-root",
            binding_id="binding-child",
            operation_id="operation-spawn-child",
        )
        observe(convergence)
        parent = repository.bindings.get(("owner-a", "binding-root"))
        if parent is not None:
            sequence_history.setdefault(parent.binding_id, []).append(
                parent.lease_sequence
            )
    else:
        await provision_root()
        if event == "resume":
            root_host.apply("quiesce")
            quiesced = await service.quiesce(
                owner_id="owner-a",
                binding_id="binding-root",
                operation_id="operation-quiesce-root",
            )
            observe(quiesced)
        arguments = {
            "owner_id": "owner-a",
            "binding_id": "binding-root",
            "operation_id": f"operation-{event}-root",
        }
        if event == "revoke":
            root_host.apply("revoke")
            convergence = await service.revoke(
                **arguments,
                reason_code="case_study_revocation",
            )
        else:
            root_host.apply(event)
            method = getattr(service, event)
            convergence = await method(**arguments)
        observe(convergence)
    duration_ns = max(1, time.perf_counter_ns() - started)
    final_binding = getattr(convergence, "binding", None)
    expected_call_trace = {
        "provision": (("provision", "operation-provision-root"),),
        "spawn": (
            ("provision", "operation-provision-root"),
            ("spawn", "operation-spawn-child"),
        ),
        "renew": (
            ("provision", "operation-provision-root"),
            ("renew", "operation-renew-root"),
        ),
        "quiesce": (
            ("provision", "operation-provision-root"),
            ("quiesce", "operation-quiesce-root"),
        ),
        "resume": (
            ("provision", "operation-provision-root"),
            ("quiesce", "operation-quiesce-root"),
            ("resume", "operation-resume-root"),
        ),
        "close": (
            ("provision", "operation-provision-root"),
            ("close", "operation-close-root"),
        ),
        "revoke": (
            ("provision", "operation-provision-root"),
            ("revoke", "operation-revoke-root"),
        ),
    }
    call_trace = tuple(client.calls)

    if mode == "off":
        state = target_host.state
        converged = (
            not getattr(convergence, "protected", True)
            and not call_trace
            and final_binding is None
        )
    else:
        state_value = getattr(final_binding, "state", None)
        state = getattr(state_value, "value", "unknown")
        converged = (
            final_binding is not None
            and getattr(convergence, "error_code", None) is None
            and state != "unknown"
            and state == target_host.state
            and call_trace == expected_call_trace[event]
        )

    budget_conserved = all(
        tuple(snapshot.residual) == tuple(client.grants[lease_id].allocation)
        for lease_id, snapshot in client.snapshots.items()
    )
    sequence_monotonic = all(
        all(later >= earlier for earlier, later in zip(values, values[1:]))
        for values in sequence_history.values()
    )
    if mode != "off" and event in {"spawn", "renew", "quiesce", "resume", "close"}:
        sequence_deltas = [
            later - earlier
            for values in sequence_history.values()
            for earlier, later in zip(values, values[1:])
        ]
        sequence_monotonic = (
            sequence_monotonic
            and bool(sequence_deltas)
            and all(delta == 1 for delta in sequence_deltas)
        )
    return _LifecycleOutcome(
        requests=len(call_trace),
        call_trace=call_trace,
        state=state,
        converged=converged,
        budget_conserved=budget_conserved,
        sequence_monotonic=sequence_monotonic,
        duration_ns=duration_ns,
        binding=final_binding,
    )


def _measurement(
    name: str, unit: str, samples: Sequence[int | float]
) -> dict[str, object]:
    if not samples or any(
        isinstance(value, bool) or not math.isfinite(float(value)) for value in samples
    ):
        raise DriverError("invalid_measurement")
    return {
        "name": name,
        "unit": unit,
        "samples": list(samples),
        "exclusions": [],
    }


async def _execute_lifecycle(scenario: Scenario) -> dict[str, object]:
    assert scenario.lifecycle_event is not None
    outcome = await _lifecycle_outcome(scenario.mode, scenario.lifecycle_event)
    result = {
        "format": RESULT_FORMAT,
        "scenario_id": scenario.scenario_id,
        "mode": scenario.mode,
        "category": scenario.category,
        "status": "passed",
        "execution_identity": _execution_identity(),
        "observations": {
            "astral_decision": "not-applicable",
            "lets_requests": outcome.requests,
            "receipts_issued": 0,
            "receipts_claimed": 0,
            "physical_effects": 0,
            "denied_effects": 0,
            "unreceipted_governed_effects": 0,
            "budget_conserved": outcome.budget_conserved,
            "sequence_monotonic": outcome.sequence_monotonic,
            "lifecycle_converged": outcome.converged,
            "lifecycle_state": outcome.state,
            "denial_code": None,
            "scope_binding": None,
        },
        "measurements": [
            _measurement("lifecycle_convergence", "ns", [outcome.duration_ns])
        ],
    }
    _assert_scenario_result(result, scenario)
    return result


async def _execute_dispatch(scenario: Scenario, root: Path) -> dict[str, object]:
    mode = scenario.mode
    counter = _EffectCounter()
    lifecycle: _LifecycleOutcome | None = None
    failure_code = None
    allocation: tuple[int, ...] | None = None
    if scenario.category == "warden-outage":
        failure_code = "warden_unavailable"
    elif scenario.category == "budget-exhaustion":
        allocation = (0,) * RESOURCE_DIMENSIONS
        failure_code = None
    elif scenario.category == "post-revocation-effect":
        lifecycle = await _lifecycle_outcome(mode, "revoke")

    expected_scope_binding = binding_for_scope(scenario.scope or "tools:execute")
    rig = build_rig(root, mode=mode, name="dispatch")
    rig.binding.capabilities = tuple(binding.capability for binding in SCOPE_BINDINGS)
    warden = _MeasuringWarden(
        rig.signer,
        mode=mode,
        expected_binding=expected_scope_binding,
        allocation=allocation,
        failure_code=failure_code,
    )
    rig.warden.authorize_tool = warden.authorize_tool  # type: ignore[method-assign]
    revoked_repository: _RevokedBindingRepository | None = None
    revoked_dispatch: GovernedFinalDispatch | None = None
    if lifecycle is not None:
        revoked_repository = _RevokedBindingRepository(lifecycle.binding)

        async def resolve_revoked(
            agent_id: str, owner_id: str | None
        ) -> DispatchRuntime:
            return DispatchRuntime(
                owner_id=owner_id,
                agent_id=agent_id,
                population="server_dynamic",
                runtime_id="runtime-root",
                runtime_generation=1,
                executor_audience="executor-a",
                executor_conformant=True,
                dispatch_posture="protected_executor",
            )

        revoked_dispatch = GovernedFinalDispatch.active(
            gateway=rig.authorization,
            plane=_CaseStudyPlane(),
            authority_repository=revoked_repository,
            runtime_resolver=resolve_revoked,
        )
    replay_path = root / "dispatch" / "executor.sqlite3"
    storage_before = replay_path.stat().st_size
    denied_effects = 0
    denial_code: str | None = None
    started = time.perf_counter_ns()

    async def invoke_one(
        *,
        dispatch: GovernedFinalDispatch | None = None,
        binding: object | None = None,
        agent_id: str = "agent-a",
        runtime_id: str = "runtime-a",
        runtime_generation: int = 3,
    ) -> object:
        selected_dispatch = rig.dispatch if dispatch is None else dispatch
        selected_binding = rig.binding if binding is None else binding

        async def invoke(capabilities: dict[str, object]) -> object:
            if mode == "enforce":
                return await asyncio.to_thread(
                    rig.executor.claim_and_invoke,
                    metadata=capabilities[LETS_CALLER_CAPABILITY],
                    **host_arguments(
                        final_arguments=_SAFE_ARGUMENTS,
                        binding_id=selected_binding.binding_id,
                        lease_id=selected_binding.lease_id,
                        lineage_id=selected_binding.lineage_id,
                        agent_id=agent_id,
                        runtime_id=runtime_id,
                        runtime_generation=runtime_generation,
                    ),
                    actuator=lambda: counter.invoke(
                        governed=True,
                        receipted=True,
                        binding=selected_binding,
                    ),
                )
            return counter.invoke(
                governed=False,
                receipted=False,
                binding=selected_binding,
            )

        return await selected_dispatch.execute(
            owner_id="owner-a",
            agent_id=agent_id,
            tool_id="clinical.search_v2",
            scope=scenario.scope or "tools:execute",
            channel="background",
            audit_correlation_id="audit-case-study",
            final_arguments=dict(_SAFE_ARGUMENTS),
            invoke=invoke,
        )

    async def invoke_replay() -> object:
        async def invoke(capabilities: dict[str, object]) -> object:
            if mode != "enforce":
                return counter.invoke(
                    governed=False,
                    receipted=False,
                    binding=rig.binding,
                )
            metadata = capabilities[LETS_CALLER_CAPABILITY]
            await asyncio.to_thread(
                rig.executor.verify_and_claim,
                metadata=metadata,
                **host_arguments(final_arguments=_SAFE_ARGUMENTS),
            )
            return await asyncio.to_thread(
                rig.executor.claim_and_invoke,
                metadata=metadata,
                actuator=lambda: counter.invoke(
                    governed=True,
                    receipted=True,
                    binding=rig.binding,
                ),
                **host_arguments(final_arguments=_SAFE_ARGUMENTS),
            )

        return await rig.dispatch.execute(
            owner_id="owner-a",
            agent_id="agent-a",
            tool_id="clinical.search_v2",
            scope="tools:execute",
            channel="background",
            audit_correlation_id="audit-case-study-replay",
            final_arguments=dict(_SAFE_ARGUMENTS),
            invoke=invoke,
        )

    try:
        try:
            if scenario.category == "parallel-dispatch":
                bindings = {
                    f"agent-{index}": SimpleNamespace(
                        **{
                            **vars(rig.binding),
                            "binding_id": f"binding-{index}",
                            "agent_id": f"agent-{index}",
                            "runtime_id": f"runtime-{index}",
                            "runtime_generation": index + 1,
                            "lease_id": f"lease-{index}",
                            "subject_id": f"agent-{index}",
                        }
                    )
                    for index in range(scenario.parallelism)
                }

                class ParallelPlane:
                    @contextmanager
                    def transaction(self, **_options: object) -> Iterator[object]:
                        yield object()

                class ParallelRepository:
                    def get_active_binding(
                        self,
                        _transaction: object,
                        *,
                        owner_id: str,
                        agent_id: str,
                        runtime_id: str,
                        runtime_generation: int,
                    ) -> object | None:
                        candidate = bindings.get(agent_id)
                        if candidate is None:
                            return None
                        identity = (
                            candidate.owner_id,
                            candidate.runtime_id,
                            candidate.runtime_generation,
                        )
                        if identity != (owner_id, runtime_id, runtime_generation):
                            return None
                        return candidate

                async def resolve(
                    agent_id: str, owner_id: str | None
                ) -> DispatchRuntime:
                    binding = bindings[agent_id]
                    return DispatchRuntime(
                        owner_id=owner_id,
                        agent_id=agent_id,
                        population="server_dynamic",
                        runtime_id=binding.runtime_id,
                        runtime_generation=binding.runtime_generation,
                        executor_audience="executor-a",
                        executor_conformant=True,
                        dispatch_posture="protected_executor",
                    )

                parallel_dispatch = GovernedFinalDispatch.active(
                    gateway=rig.authorization,
                    plane=ParallelPlane(),
                    authority_repository=ParallelRepository(),
                    runtime_resolver=resolve,
                )
                await asyncio.gather(
                    *(
                        invoke_one(
                            dispatch=parallel_dispatch,
                            binding=bindings[f"agent-{index}"],
                            agent_id=f"agent-{index}",
                            runtime_id=f"runtime-{index}",
                            runtime_generation=index + 1,
                        )
                        for index in range(scenario.parallelism)
                    )
                )
            elif scenario.category == "recursive-dispatch":

                async def recurse(depth: int) -> None:
                    await invoke_one()
                    if depth:
                        await recurse(depth - 1)

                await recurse(scenario.recursion_depth)
            elif scenario.category == "receipt-replay":
                await invoke_replay()
            elif scenario.category == "post-revocation-effect":
                assert lifecycle is not None and revoked_dispatch is not None
                await invoke_one(
                    dispatch=revoked_dispatch,
                    binding=lifecycle.binding or rig.binding,
                    agent_id="agent-root",
                    runtime_id="runtime-root",
                    runtime_generation=1,
                )
            else:
                await invoke_one()
        except (GovernedDispatchError, LetsGatewayError) as exc:
            denied_effects += 1
            denial_code = exc.code
    finally:
        duration_ns = max(1, time.perf_counter_ns() - started)
        storage_after = replay_path.stat().st_size
        rig.close()

    lifecycle_requests = 0 if lifecycle is None else lifecycle.requests
    lets_requests = warden.requests + lifecycle_requests
    claimed_sequences: dict[str, list[int]] = {}
    for envelope, _status in rig.coordinator.claims:
        claimed_sequences.setdefault(envelope.binding_id, []).append(
            envelope.receipt.resulting_sequence
        )
    sequence_monotonic = all(
        all(later > earlier for earlier, later in zip(values, values[1:]))
        for values in claimed_sequences.values()
    ) and (lifecycle is None or lifecycle.sequence_monotonic)
    budget_conserved = warden.budget_conserved() and (
        lifecycle is None or lifecycle.budget_conserved
    )
    lifecycle_state = None if lifecycle is None else lifecycle.state
    lifecycle_converged = lifecycle is not None and lifecycle.converged
    if (
        scenario.category == "post-revocation-effect"
        and mode != "off"
        and (revoked_repository is None or not revoked_repository.inactive_observed)
    ):
        raise DriverError("revocation_causality_invariant_failed")
    decision = "denied" if denied_effects else "allowed"
    measurements = [_measurement("end_to_end_latency", "ns", [duration_ns])]
    if warden.latencies_ns:
        measurements.append(
            _measurement("authorization_latency", "ns", warden.latencies_ns)
        )
    if counter.physical:
        measurements.append(
            _measurement(
                "throughput",
                "effects/s",
                [counter.physical * 1_000_000_000 / duration_ns],
            )
        )
    if denied_effects:
        measurements.append(_measurement("refusal_latency", "ns", [duration_ns]))
    measurements.append(
        _measurement(
            "storage_growth", "bytes", [max(0, storage_after - storage_before)]
        )
    )
    result = {
        "format": RESULT_FORMAT,
        "scenario_id": scenario.scenario_id,
        "mode": mode,
        "category": scenario.category,
        "status": "passed",
        "execution_identity": _execution_identity(),
        "observations": {
            "astral_decision": decision,
            "lets_requests": lets_requests,
            "receipts_issued": len(warden.issued_sequences),
            "receipts_claimed": len(rig.coordinator.claims),
            "physical_effects": counter.physical,
            "denied_effects": denied_effects,
            "unreceipted_governed_effects": counter.unreceipted_governed,
            "budget_conserved": budget_conserved,
            "sequence_monotonic": sequence_monotonic,
            "lifecycle_converged": lifecycle_converged,
            "lifecycle_state": lifecycle_state,
            "denial_code": denial_code,
            "scope_binding": {
                "scope": expected_scope_binding.scope,
                "capability": expected_scope_binding.capability,
                "transition": expected_scope_binding.transition,
                "resource_dimension": expected_scope_binding.resource_dimension,
                "checks": warden.mapping_checks,
            },
        },
        "measurements": measurements,
    }
    _assert_scenario_result(result, scenario)
    return result


def _counter(observations: Mapping[str, object], name: str) -> int:
    value = observations.get(name)
    if type(value) is not int or not 0 <= value <= _MAX_COUNTER:
        raise DriverError("counter_out_of_bounds")
    return value


def _assert_scope_binding(
    observations: Mapping[str, object], scenario: Scenario, lets_requests: int
) -> None:
    observed = observations.get("scope_binding")
    if scenario.category == "lifecycle":
        if observed is not None:
            raise DriverError("scope_mapping_invariant_failed")
        return
    if not isinstance(observed, Mapping):
        raise DriverError("scope_mapping_invariant_failed")
    expected = binding_for_scope(scenario.scope or "tools:execute")
    checks = observed.get("checks")
    if type(checks) is not int or checks < 0:
        raise DriverError("scope_mapping_invariant_failed")
    expected_observation = {
        "scope": expected.scope,
        "capability": expected.capability,
        "transition": expected.transition,
        "resource_dimension": expected.resource_dimension,
        "checks": checks,
    }
    if dict(observed) != expected_observation:
        raise DriverError("scope_mapping_invariant_failed")
    expected_checks = (
        0
        if scenario.mode == "off" or scenario.category == "post-revocation-effect"
        else lets_requests
    )
    if checks != expected_checks:
        raise DriverError("scope_mapping_invariant_failed")


def _assert_scenario_result(result: Mapping[str, object], scenario: Scenario) -> None:
    observations = result.get("observations")
    if not isinstance(observations, Mapping):
        raise DriverError("scenario_invariant_failed")
    lets_requests = _counter(observations, "lets_requests")
    receipts_issued = _counter(observations, "receipts_issued")
    receipts_claimed = _counter(observations, "receipts_claimed")
    effects = _counter(observations, "physical_effects")
    denied = _counter(observations, "denied_effects")
    unreceipted = _counter(observations, "unreceipted_governed_effects")
    _assert_scope_binding(observations, scenario, lets_requests)
    if (
        observations.get("budget_conserved") is not True
        or observations.get("sequence_monotonic") is not True
        or unreceipted
        or receipts_claimed > receipts_issued
    ):
        raise DriverError("scenario_invariant_failed")
    if scenario.mode == "off":
        if lets_requests or receipts_issued or receipts_claimed:
            raise DriverError("flag_off_invariant_failed")
    elif lets_requests < 1:
        raise DriverError("missing_lets_request")
    if scenario.mode == "shadow" and receipts_claimed:
        raise DriverError("shadow_claim_invariant_failed")
    if scenario.category == "post-revocation-effect" and (
        observations.get("lifecycle_converged") is not True
        or observations.get("lifecycle_state") != "revoked"
    ):
        raise DriverError("revocation_lifecycle_invariant_failed")

    decision = observations.get("astral_decision")
    if scenario.category == "lifecycle":
        if (
            decision != "not-applicable"
            or effects
            or denied
            or observations.get("lifecycle_converged") is not True
            or observations.get("lifecycle_state") != scenario.expected_lifecycle_state
        ):
            raise DriverError("lifecycle_invariant_failed")
    elif scenario.expected_effect == "deny":
        if (
            decision != "denied"
            or effects
            or denied < 1
            or observations.get("denial_code")
            != _FAULT_DENIAL_CODES.get(scenario.category)
        ):
            raise DriverError("denial_invariant_failed")
    else:
        if decision != "allowed" or effects < 1 or denied:
            raise DriverError("effect_invariant_failed")
        if scenario.mode == "enforce" and receipts_claimed != effects:
            raise DriverError("receipt_effect_invariant_failed")
        if scenario.category == "parallel-dispatch" and effects != scenario.parallelism:
            raise DriverError("parallel_invariant_failed")
        if (
            scenario.category == "recursive-dispatch"
            and effects != scenario.recursion_depth + 1
        ):
            raise DriverError("recursive_invariant_failed")


async def _execute_scenario(scenario: Scenario) -> dict[str, object]:
    with _isolated_audit():
        if scenario.category == "lifecycle":
            return await _execute_lifecycle(scenario)
        with tempfile.TemporaryDirectory(prefix="astraldeep-074-case-study-") as root:
            return await _execute_dispatch(scenario, Path(root))


def main() -> int:
    try:
        payload = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
        scenario = _load_scenario(payload)
        result = asyncio.run(_execute_scenario(scenario))
        output = json.dumps(
            result,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except DriverError as exc:
        sys.stderr.buffer.write(f"case-study driver refused: {exc}\n".encode("ascii"))
        return 2
    except Exception:
        sys.stderr.buffer.write(b"case-study driver refused: execution_failed\n")
        return 2
    sys.stdout.buffer.write((output + "\n").encode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
