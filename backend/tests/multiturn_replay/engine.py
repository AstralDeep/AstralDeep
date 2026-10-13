"""Runs synthetic scenarios through selected production dispatch and authorization checks.
Fixture-owned services and an independent effect oracle provide bounded diagnostic observations.
Run records identify doubled boundaries and outstanding acceptance evidence.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

from lets.canonical import b64url_encode, canonical_digest, canonical_json
from lets.clock import ManualClock
from lets.crypto import Ed25519Signer, PublicKeyRegistry
from lets.executor import (
    ExecutorPolicy,
    ReceiptVerifier,
    SQLiteReceiptReplayStore,
    executor_replay_identity,
)
from lets.executor_authority import ProcessFileExecutorAuthorityAnchor
from lets.models import Receipt

from orchestrator.delegation import (
    authorize_chained_tool_call,
    mint_child_delegation,
    verify_delegation_chain,
)
from orchestrator.governed_dispatch import (
    DispatchRuntime,
    GovernedDispatchError,
    GovernedFinalDispatch,
)
from orchestrator.lets_client import LetsClientBoundaryError
from orchestrator.lets_config import LetsHostConfig
from orchestrator.lets_gateway import (
    LETS_CALLER_CAPABILITY,
    LetsAuthorizationGateway,
    LetsGatewayError,
    ReceiptExecutorGateway,
)
from orchestrator.lets_scope_profile import RESOURCE_DIMENSIONS, binding_for_scope
from orchestrator.policy import evaluate_policy
from orchestrator.protected_dispatch import canonical_wire_arguments_sha256
from shared import external_http
from shared.phi_redactor import redact

from .scenarios import (
    HELD_OUT_NAMESPACE,
    DEVELOPMENT_NAMESPACE,
    DECISION_ALLOWED,
    DECISION_DENIED,
    DECISION_NO_ATTEMPT,
    MAX_ATTEMPTS_PER_TURN,
    MAX_TURNS,
    OUTCOME_INFRASTRUCTURE,
    OUTCOME_OCCURRED,
    OUTCOME_PREVENTED,
    OUTCOME_UNATTEMPTED,
    OUTCOME_UNKNOWN,
    STATUS_FAILED,
    STATUS_INCONCLUSIVE,
    STATUS_PASSED,
    TRANSITION_ATTENUATED,
    TRANSITION_BUDGET_EXHAUSTED,
    TRANSITION_CONFIRMED,
    TRANSITION_GRANTED,
    TRANSITION_REVOKED,
    AttemptedEffect,
    ProviderTurn,
    ReplayScenario,
    corpus_digest,
)

RESULT_FORMAT = "astraldeep.multiturn-replay-result/v1"
RUN_RECORD_FORMAT = "astraldeep.multiturn-replay-run-record/v1"

_DELEGATION_FAKE_NOW = 1_000_000
_DELEGATION_TTL_SECONDS = 3_600
_EXECUTOR_AUDIENCE = "executor-a"
_TENANT_ID = "tenant-a"
_ENVELOPE_ID = "envelope-a"
_POLICY_ID = "astral-policy"
_POLICY_DIGEST = "sha256:" + "1" * 64
_MACHINE_DIGEST = "sha256:" + "2" * 64
_LINEAGE_ID = "lineage-a"
_LEASE_ID = "lease-a"
_RUNTIME_GENERATION = 3
_ISSUED_AT_NS = 90
_EXPIRES_AT_NS = 1_000
_CONFIG_EPOCH = 7
_FULL_ALLOCATION = (64,) * RESOURCE_DIMENSIONS

_INFRASTRUCTURE_CODES = frozenset(
    {
        "warden_unavailable",
        "protected_authorization_failed",
        "authorization_outcome_uncertain",
        "effect_persistence_unavailable",
        "governed_dispatch_unavailable",
        "runtime_identity_unavailable",
        "protected_dispatch_failed",
        "protected_retry_failed",
        "replay_store_unavailable",
        "clock_uncertain",
        "effect_failed",
        "effect_result_lost",
        "outcome_uncertain",
        "plane_claim_unavailable",
        "replay_capture_unavailable",
    }
)


class ReplayError(RuntimeError):
    pass


class _EffectFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReplayBounds:
    max_turns: int = MAX_TURNS
    max_attempts_per_turn: int = MAX_ATTEMPTS_PER_TURN
    max_message_bytes: int = 2048
    infrastructure_retries: int = 1

    def __post_init__(self) -> None:
        if self.max_turns > MAX_TURNS or self.max_attempts_per_turn > MAX_ATTEMPTS_PER_TURN:
            raise ReplayError("bounds_exceed_corpus_limits")


@dataclass(slots=True)
class EffectOracle:
    scenario_id: str
    path: Path
    records: list[dict[str, object]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def apply(
        self, attempt_id: str, arguments_sha256: str, *, observe: bool, failure: str | None
    ) -> object:
        if not observe:
            return {"observed": False}
        with self._lock:
            record: dict[str, object] = {
                "attempt_id": attempt_id,
                "arguments_sha256": arguments_sha256,
                "applied": failure is None,
                "error_code": failure,
            }
            record["effect_digest"] = hashlib.sha256(
                canonical_json(record)
            ).hexdigest()
            with self.path.open("ab") as stream:
                stream.write(canonical_json(record) + b"\n")
            self.records.append(record)
            if failure is not None:
                raise _EffectFailure(failure)
        return record

    def applied_for(self, attempt_id: str) -> dict[str, object] | None:
        for record in self.records:
            if record["attempt_id"] == attempt_id and record["applied"] is True:
                return record
        return None


class _ReplayWarden:
    def __init__(self, signer: Ed25519Signer, *, allocation: tuple[int, ...]) -> None:
        if len(allocation) != RESOURCE_DIMENSIONS:
            raise ReplayError("invalid_budget_allocation")
        self.signer = signer
        self.calls: list[dict[str, object]] = []
        self.failure_code: str | None = None
        self.initial = list(allocation)
        self.remaining = list(allocation)
        self.committed_costs: list[tuple[int, ...]] = []
        self._lock = threading.Lock()

    def authorize_tool(self, **values: object) -> Receipt:
        with self._lock:
            self.calls.append(dict(values))
            if self.failure_code is not None:
                raise LetsClientBoundaryError(
                    self.failure_code,
                    retryable=self.failure_code != "budget_exhausted",
                )
            evidence = values.get("evidence")
            if not isinstance(evidence, dict):
                raise LetsClientBoundaryError("invalid_response")
            scope = evidence.get("scope")
            if not isinstance(scope, str):
                raise LetsClientBoundaryError("scope_mapping_mismatch")
            expected = binding_for_scope(scope)
            if (
                values.get("declared_scope") != scope
                or evidence.get("capability") != expected.capability
                or evidence.get("transition") != expected.transition
                or evidence.get("resource_dimension") != expected.resource_dimension
            ):
                raise LetsClientBoundaryError("scope_mapping_mismatch")
            dimension = expected.resource_dimension
            if self.remaining[dimension] < 1:
                raise LetsClientBoundaryError("budget_exhausted")
            cost = tuple(
                1 if index == dimension else 0 for index in range(RESOURCE_DIMENSIONS)
            )
            receipt = Receipt(
                tenant_id=_TENANT_ID,
                envelope_id=_ENVELOPE_ID,
                config_epoch=_CONFIG_EPOCH,
                receipt_id=f"receipt-{values['operation_id']}",
                request_id=str(values["operation_id"]),
                warden_id=self.signer.warden_id,
                key_id=self.signer.key_id,
                policy_id=_POLICY_ID,
                policy_version="1",
                policy_digest=_POLICY_DIGEST,
                machine_digest=_MACHINE_DIGEST,
                lease_id=str(values["lease_id"]),
                lineage_id=_LINEAGE_ID,
                subject_id=str(values["agent_id"]),
                executor_audience=str(values["executor_audience"]),
                transition=expected.transition,
                source_state="ready",
                target_state="ready",
                cost=cost,
                resulting_sequence=int(values["expected_sequence"]) + 1,
                evidence_digest=canonical_digest(evidence),
                nonce=str(values["nonce"]),
                issued_at_ns=_ISSUED_AT_NS,
                expires_at_ns=_EXPIRES_AT_NS,
            )
            self.remaining[dimension] -= 1
            self.committed_costs.append(cost)
            return dataclasses.replace(
                receipt,
                signature=b64url_encode(
                    self.signer.sign(canonical_json(receipt.unsigned_payload()))
                ),
            )

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


class _RevocableBindingRepository:
    def __init__(self, binding: SimpleNamespace) -> None:
        self.binding = binding
        self.revoked = False
        self.inactive_observed = False

    def get_active_binding(self, _transaction: object, **_values: object) -> object | None:
        if self.revoked:
            self.inactive_observed = True
            return None
        return self.binding


class _ReplayAuditSink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def record(self, event: object) -> None:
        self.events.append(
            {
                "action_type": getattr(event, "action_type", None),
                "correlation_id": getattr(event, "correlation_id", None),
                "outcome": getattr(event, "outcome", None),
            }
        )


@contextmanager
def _isolated_audit() -> Iterator[_ReplayAuditSink]:
    import audit.recorder as recorder_module

    sink = _ReplayAuditSink()
    original = recorder_module.get_recorder
    recorder_module.get_recorder = lambda: sink
    try:
        yield sink
    finally:
        recorder_module.get_recorder = original


class _RecordingCoordinator:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.claims: list[tuple[object, object]] = []
        self.outcomes: list[tuple[str, str, str | None]] = []

    def prepare_authorization(self, **_values: object) -> object:
        self.events.append("intent")
        return object()

    def record_receipt(self, *, envelope: object, **_values: object) -> None:
        self.events.append("receipt")

    def claim_for_execution(self, *, envelope: object, replay_status: object) -> None:
        self.events.append("claim")
        self.claims.append((envelope, replay_status))

    def record_outcome(
        self,
        *,
        operation_id: str,
        outcome: str,
        error_code: str | None = None,
        **_values: object,
    ) -> None:
        self.events.append(outcome)
        self.outcomes.append((operation_id, outcome, error_code))


@dataclass(slots=True)
class _AttemptTrace:
    attempt_id: str
    turn_index: int
    tool_id: str
    scope: str
    decisions: list[dict[str, object]] = field(default_factory=list)
    outcome: str = OUTCOME_UNATTEMPTED
    denial_code: str | None = None
    gate: str | None = None
    receipt: dict[str, object] | None = None
    oracle: dict[str, object] | None = None
    phi_boundary_transformed: bool = False
    egress_url: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "turn_index": self.turn_index,
            "tool_id": self.tool_id,
            "scope": self.scope,
            "decisions": list(self.decisions),
            "outcome": self.outcome,
            "denial_code": self.denial_code,
            "gate": self.gate,
            "receipt": self.receipt,
            "oracle": self.oracle,
            "phi_boundary_transformed": self.phi_boundary_transformed,
            "egress_url": self.egress_url,
        }


@dataclass(slots=True)
class _TurnTrace:
    turn_index: int
    user_message_sha256: str
    provider_message_sha256: str
    claims_completion: bool
    false_completion: bool = False
    cancelled: bool = False
    attempts: list[_AttemptTrace] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "turn_index": self.turn_index,
            "user_message_sha256": self.user_message_sha256,
            "provider_message_sha256": self.provider_message_sha256,
            "claims_completion": self.claims_completion,
            "false_completion": self.false_completion,
            "cancelled": self.cancelled,
            "attempts": [attempt.as_dict() for attempt in self.attempts],
        }


def _candidate_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="ascii",
            errors="strict",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise ReplayError("candidate_identity_unavailable") from None
    commit = completed.stdout.strip()
    if completed.returncode != 0 or len(commit) != 40:
        raise ReplayError("candidate_identity_unavailable")
    return commit


def _driver_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _authorized_effect(attempt: AttemptedEffect) -> dict[str, object]:
    return {
        "effect_class": attempt.effect_class,
        "target_class": attempt.target_class,
        "data_classification": attempt.data_classification,
        "egress_class": "approved_external" if attempt.egress_url else "none",
        "credential_class": "delegated_user" if attempt.delegated else "service_identity",
        "idempotency_class": "read_only"
        if attempt.effect_class in {"read", "search"}
        else "domain_key",
        "confirmation_class": "confirmed",
        "writes_state": attempt.effect_class in {"write", "execute", "files", "system"},
        "network_effect": attempt.egress_url is not None,
        "external_actuator": attempt.egress_url is not None,
        "target_binding_sha256": hashlib.sha256(attempt.tool_id.encode("ascii")).hexdigest(),
    }


def _delegation_parent(scenario: ReplayScenario) -> dict[str, object]:
    return {
        "sub": scenario.owner_id,
        "act": {"sub": f"agent:{scenario.agent_id}"},
        "scope": "tools:read tool:clinical.search_v2",
        "iss": "mock-astral-delegation",
        "aud": "agent-service",
        "iat": _DELEGATION_FAKE_NOW,
        "exp": _DELEGATION_FAKE_NOW + _DELEGATION_TTL_SECONDS,
        "delegation": True,
    }


def _namespace_for(split: str) -> str:
    return DEVELOPMENT_NAMESPACE if split == "development" else HELD_OUT_NAMESPACE


class ReplayEngine:
    def __init__(
        self,
        scenario: ReplayScenario,
        root: Path,
        *,
        bounds: ReplayBounds | None = None,
    ) -> None:
        self.scenario = scenario
        self.root = root
        self.bounds = bounds or ReplayBounds()
        self.oracle = EffectOracle(
            scenario_id=scenario.scenario_id,
            path=root / f"{scenario.scenario_id}.oracle.jsonl",
        )
        self.transitions: list[str] = []
        self.turns: list[_TurnTrace] = []
        self.truncated_after_turn: int | None = None
        self._cancelled = False
        self._captured_metadata: object | None = None
        self._rig: SimpleNamespace | None = None
        self._warden: _ReplayWarden | None = None
        self._repository: _RevocableBindingRepository | None = None
        self._child_token: dict[str, object] | None = None
        self._rig_closed = False

    def run(self) -> dict[str, object]:
        namespace = _namespace_for(self.scenario.split)
        if not self.scenario.owner_id.startswith(namespace) or not self.scenario.agent_id.startswith(
            namespace
        ):
            raise ReplayError("identity_namespace_mismatch")
        if len(self.scenario.turns) > self.bounds.max_turns:
            raise ReplayError("scenario_turn_bound_exceeded")
        with tempfile.TemporaryDirectory(
            prefix=f"multiturn-replay-{self.scenario.scenario_id}-"
        ) as temp_root:
            self._rig = self._build_rig(Path(temp_root))
            self._warden = self._rig.warden
            self._repository = self._rig.repository
            with _isolated_audit() as audit_sink:
                try:
                    asyncio.run(self._run_turns())
                finally:
                    self._rig.anchor.close()
                    self._rig_closed = True
            return self._result(audit_sink, Path(temp_root).name)

    def _build_rig(self, root: Path) -> SimpleNamespace:
        scenario = self.scenario
        signer = Ed25519Signer.generate("warden-a")
        clock = ManualClock(100)
        registry = PublicKeyRegistry(clock=clock)
        registry.register(signer.warden_id, signer.key_id, signer.public_key_bytes)
        policy = ExecutorPolicy(
            audience=_EXECUTOR_AUDIENCE,
            tenant_id=_TENANT_ID,
            envelope_id=_ENVELOPE_ID,
            config_epoch=_CONFIG_EPOCH,
            allowed_policy_digests=frozenset({_POLICY_DIGEST}),
            allowed_machine_digests=frozenset({_MACHINE_DIGEST}),
            trusted_wardens=frozenset({signer.warden_id}),
            max_clock_uncertainty_ns=5,
        )
        anchor_root = root / "authority"
        anchor_root.mkdir(parents=True, exist_ok=True)
        anchor = ProcessFileExecutorAuthorityAnchor(anchor_root / "executor.anchor")
        store = SQLiteReceiptReplayStore.initialize(
            root / "executor.sqlite3",
            authority_anchor=anchor,
            identity=executor_replay_identity(policy, registry),
        )
        coordinator = _RecordingCoordinator()
        verifier = ReceiptVerifier(registry, store, policy, clock=clock)
        executor = ReceiptExecutorGateway(
            verifier,
            replay_status=store.status,
            effect_coordinator=coordinator,
        )
        warden = _ReplayWarden(
            signer,
            allocation=scenario.budget_allocation or _FULL_ALLOCATION,
        )
        config = LetsHostConfig(
            master_enabled=True,
            mode="enforce",
            environment="test",
            governed_cohorts=("server_dynamic", "byo_user"),
            governed_agent_allowlist=(),
        )
        gateway = LetsAuthorizationGateway(
            config,
            warden,  # type: ignore[arg-type]
            effect_coordinator=coordinator,
        )
        binding = SimpleNamespace(
            binding_id="binding-a",
            owner_id=scenario.owner_id,
            agent_id=scenario.agent_id,
            runtime_id=f"{scenario.agent_id}-runtime",
            runtime_generation=_RUNTIME_GENERATION,
            population="server_dynamic",
            tenant_id=_TENANT_ID,
            envelope_id=_ENVELOPE_ID,
            warden_id=signer.warden_id,
            lease_id=_LEASE_ID,
            lineage_id=_LINEAGE_ID,
            subject_id=scenario.agent_id,
            policy_digest=_POLICY_DIGEST,
            machine_digest=_MACHINE_DIGEST,
            config_epoch=_CONFIG_EPOCH,
            capabilities=tuple(scenario.binding_capabilities),
            lease_sequence=0,
            lease_expires_at_ns=_EXPIRES_AT_NS,
            state="active",
        )
        repository = _RevocableBindingRepository(binding)

        class _Plane:
            @contextmanager
            def transaction(self, **_options: object) -> Iterator[object]:
                yield object()

        def resolve(_agent_id: str, _owner_id: str | None) -> DispatchRuntime:
            return DispatchRuntime(
                owner_id=scenario.owner_id,
                agent_id=scenario.agent_id,
                population="server_dynamic",
                runtime_id=binding.runtime_id,
                runtime_generation=_RUNTIME_GENERATION,
                executor_audience=_EXECUTOR_AUDIENCE,
                executor_conformant=True,
                dispatch_posture="protected_executor",
            )

        dispatch = GovernedFinalDispatch.active(
            gateway=gateway,
            plane=_Plane(),
            authority_repository=repository,
            runtime_resolver=resolve,
        )
        return SimpleNamespace(
            signer=signer,
            clock=clock,
            store=store,
            anchor=anchor,
            coordinator=coordinator,
            warden=warden,
            binding=binding,
            gateway=gateway,
            executor=executor,
            dispatch=dispatch,
            repository=repository,
        )

    async def _run_turns(self) -> None:
        for turn in self.scenario.turns:
            if self._cancelled:
                self.turns.append(
                    _TurnTrace(
                        turn_index=turn.turn_index,
                        user_message_sha256=_sha256_text(turn.user_message),
                        provider_message_sha256=_sha256_text(turn.provider_message),
                        claims_completion=turn.claims_completion,
                        cancelled=True,
                    )
                )
                continue
            await self._run_turn(turn)

    async def _run_turn(self, turn: ProviderTurn) -> None:
        if turn.revoke_binding:
            assert self._repository is not None
            self._repository.revoked = True
            self.transitions.append(TRANSITION_REVOKED)
        trace = _TurnTrace(
            turn_index=turn.turn_index,
            user_message_sha256=_sha256_text(turn.user_message),
            provider_message_sha256=_sha256_text(turn.provider_message),
            claims_completion=turn.claims_completion,
        )
        if turn.delegated:
            self._mint_child_token(turn)
        if len(turn.attempts) > self.bounds.max_attempts_per_turn:
            raise ReplayError("attempt_bound_exceeded")
        for position, attempt in enumerate(turn.attempts):
            attempt_id = f"{self.scenario.scenario_id}-t{turn.turn_index}-a{position + 1}"
            trace.attempts.append(await self._run_attempt(turn, attempt, attempt_id))
        occurred = any(item.outcome == OUTCOME_OCCURRED for item in trace.attempts)
        trace.false_completion = bool(turn.claims_completion and not occurred)
        if turn.cancel_after:
            self._cancelled = True
            trace.cancelled = True
            if self.truncated_after_turn is None:
                self.truncated_after_turn = turn.turn_index
        self.turns.append(trace)

    def _mint_child_token(self, turn: ProviderTurn) -> None:
        parent = _delegation_parent(self.scenario)
        ok, _reason = verify_delegation_chain(parent, now=_DELEGATION_FAKE_NOW)
        if not ok:
            raise ReplayError("parent_delegation_invalid")
        child = mint_child_delegation(
            parent,
            f"{self.scenario.agent_id}-worker",
            list(turn.delegated_requests),
            now=_DELEGATION_FAKE_NOW,
        )
        child_scopes = set(str(child["scope"]).split())
        if child_scopes != set(turn.delegated_requests):
            self.transitions.append(TRANSITION_ATTENUATED)
        self._child_token = child

    async def _run_attempt(
        self, turn: ProviderTurn, attempt: AttemptedEffect, attempt_id: str
    ) -> _AttemptTrace:
        trace = _AttemptTrace(
            attempt_id=attempt_id,
            turn_index=turn.turn_index,
            tool_id=attempt.tool_id,
            scope=attempt.scope,
            egress_url=attempt.egress_url,
        )
        final_arguments: dict[str, object] = dict(attempt.arguments)
        failure = self._apply_gates(turn, attempt, trace, final_arguments)
        if failure is not None:
            trace.outcome = OUTCOME_PREVENTED
            trace.denial_code = failure[0]
            trace.gate = failure[1]
            return trace
        return await self._dispatch_attempt(
            turn, attempt, attempt_id, trace, final_arguments
        )

    def _apply_gates(
        self,
        turn: ProviderTurn,
        attempt: AttemptedEffect,
        trace: _AttemptTrace,
        final_arguments: dict[str, object],
    ) -> tuple[str, str] | None:
        if turn.delegated:
            assert self._child_token is not None
            ok, reason = authorize_chained_tool_call(
                self._child_token,
                attempt.tool_id,
                required_scope=attempt.scope,
                now=_DELEGATION_FAKE_NOW,
            )
            if not ok:
                code = (
                    "delegation_scope_denied"
                    if "outside delegated scope" in reason
                    else "delegation_chain_invalid"
                )
                return code, "delegation"
            trace.decisions.append({"gate": "delegation", "code": None})
        decision = evaluate_policy(
            list(self.scenario.policy_rules),
            {
                "tool": attempt.tool_id,
                "agent": self.scenario.agent_id,
                "args": final_arguments,
                "roles": ["user"],
            },
        )
        if decision.effect == "deny":
            return "policy_deny", "policy"
        if decision.effect == "confirm":
            if (attempt.tool_id, turn.turn_index) not in self.scenario.approvals:
                return "confirmation_required", "confirmation"
            self.transitions.append(TRANSITION_CONFIRMED)
            trace.decisions.append({"gate": "confirmation", "code": None})
        elif decision.args is not None:
            final_arguments.clear()
            final_arguments.update(decision.args)
        if attempt.egress_url is not None:
            try:
                external_http.validate_egress_url(attempt.egress_url)
            except external_http.EgressBlockedError:
                return "egress_blocked", "egress"
            trace.decisions.append({"gate": "egress", "code": None})
        original_serialized = json.dumps(final_arguments, default=str, ensure_ascii=False)
        redacted_serialized, _truncated = redact(dict(final_arguments), kind="args")
        if redacted_serialized != original_serialized:
            trace.phi_boundary_transformed = True
            reparsed = json.loads(redacted_serialized)
            if not isinstance(reparsed, dict):
                return "phi_unredactable", "phi"
            final_arguments.clear()
            final_arguments.update(reparsed)
            second_serialized, _truncated = redact(dict(final_arguments), kind="args")
            if second_serialized != json.dumps(
                final_arguments, default=str, ensure_ascii=False
            ):
                return "phi_unredactable", "phi"
            trace.decisions.append({"gate": "phi", "code": None})
        return None

    async def _dispatch_attempt(
        self,
        turn: ProviderTurn,
        attempt: AttemptedEffect,
        attempt_id: str,
        trace: _AttemptTrace,
        final_arguments: dict[str, object],
    ) -> _AttemptTrace:
        assert self._rig is not None and self._warden is not None
        self._warden.failure_code = self._warden_failure(turn.turn_index)
        dispatch_owner = attempt.dispatch_owner_id or self.scenario.owner_id
        arguments_sha256 = canonical_wire_arguments_sha256(final_arguments)

        def actuator() -> object:
            return self.oracle.apply(
                attempt_id,
                arguments_sha256,
                observe=attempt.observe,
                failure=attempt.actuator_failure,
            )

        async def invoke(capabilities: dict[str, object]) -> object:
            metadata = capabilities[LETS_CALLER_CAPABILITY]
            selected = self._captured_metadata if attempt.replay_captured else metadata
            if selected is None:
                raise GovernedDispatchError("replay_capture_unavailable")
            receipt = (
                selected.get("receipt", {}) if isinstance(selected, dict) else {}
            )
            trace.receipt = {
                "receipt_id": receipt.get("request_id"),
                "resulting_sequence": receipt.get("resulting_sequence"),
                "replayed": attempt.replay_captured,
            }
            result = await asyncio.to_thread(
                    self._rig.executor.claim_and_invoke,
                    metadata=selected,
                    actuator=actuator,
                    failure_is_uncertain=attempt.actuator_failure is None,
                    final_arguments=dict(final_arguments),
                    owner_id=dispatch_owner,
                    binding_id=self._rig.binding.binding_id,
                    lease_id=self._rig.binding.lease_id,
                    lineage_id=self._rig.binding.lineage_id,
                    agent_id=self._rig.binding.agent_id,
                    runtime_id=self._rig.binding.runtime_id,
                    runtime_generation=self._rig.binding.runtime_generation,
                    tool_id=attempt.tool_id,
                    executor_audience=_EXECUTOR_AUDIENCE,
                )
            if not attempt.replay_captured:
                self._captured_metadata = metadata
            return result

        claims_before = len(self._rig.coordinator.claims)
        try:
            await self._rig.dispatch.execute(
                owner_id=dispatch_owner,
                agent_id=self.scenario.agent_id,
                tool_id=attempt.tool_id,
                scope=attempt.scope,
                channel="chained" if attempt.delegated else "websocket",
                audit_correlation_id=attempt_id,
                final_arguments=final_arguments,
                invoke=invoke,
                actor_user_id=attempt.actor_user_id,
                conversation_id=self.scenario.scenario_id,
                authorized_effect=_authorized_effect(attempt),
            )
        except _EffectFailure:
            trace.outcome = OUTCOME_INFRASTRUCTURE
            trace.denial_code = attempt.actuator_failure
            trace.gate = "actuator"
            return trace
        except (GovernedDispatchError, LetsGatewayError) as exc:
            code = getattr(exc, "code", "governed_dispatch_failed")
            if code == "budget_exhausted" and TRANSITION_BUDGET_EXHAUSTED not in self.transitions:
                self.transitions.append(TRANSITION_BUDGET_EXHAUSTED)
            if code in _INFRASTRUCTURE_CODES:
                trace.outcome = OUTCOME_INFRASTRUCTURE
            else:
                trace.outcome = OUTCOME_PREVENTED
            trace.denial_code = code
            trace.gate = "lests"
            return trace
        finally:
            claimed = len(self._rig.coordinator.claims) - claims_before
            self._rig.binding.lease_sequence += claimed
        record = self.oracle.applied_for(attempt_id)
        if record is not None:
            trace.outcome = OUTCOME_OCCURRED
            trace.oracle = dict(record)
            if TRANSITION_GRANTED not in self.transitions:
                self.transitions.append(TRANSITION_GRANTED)
            return trace
        trace.outcome = OUTCOME_UNKNOWN
        return trace

    def _warden_failure(self, turn_index: int) -> str | None:
        failures = self.scenario.warden_failures
        if 1 <= turn_index <= len(failures):
            return failures[turn_index - 1]
        return None

    def _result(self, audit_sink: _ReplayAuditSink, temp_root_name: str) -> dict[str, object]:
        mismatches = self._expectation_mismatches()
        inconclusive_reasons = self._inconclusive_reasons()
        if mismatches:
            status = STATUS_FAILED
        elif inconclusive_reasons:
            status = STATUS_INCONCLUSIVE
        else:
            status = STATUS_PASSED
        attempts = [attempt.as_dict() for turn in self.turns for attempt in turn.attempts]
        return {
            "format": RESULT_FORMAT,
            "scope": "test_only_diagnostic",
            "corpus_review_status": "pending",
            "ordinary_dispatch_qualified": False,
            "scenario_id": self.scenario.scenario_id,
            "split": self.scenario.split,
            "category": self.scenario.category,
            "digest": self.scenario.digest,
            "status": status,
            "mismatches": mismatches,
            "inconclusive_reasons": inconclusive_reasons,
            "execution_identity": {
                "candidate_commit": _candidate_commit(),
                "driver_sha256": _driver_sha256(),
                "scenario_sha256": self.scenario.digest,
            },
            "authority_transitions": list(self.transitions),
            "turns": [turn.as_dict() for turn in self.turns],
            "attempts": attempts,
            "receipts": [
                attempt["receipt"]
                for attempt in attempts
                if attempt["receipt"] is not None
            ],
            "oracle_effects": list(self.oracle.records),
            "audit_events": list(audit_sink.events),
            "bounds": {
                "max_turns": self.bounds.max_turns,
                "max_attempts_per_turn": self.bounds.max_attempts_per_turn,
                "max_message_bytes": self.bounds.max_message_bytes,
                "infrastructure_retries": self.bounds.infrastructure_retries,
            },
            "teardown": {
                "rig_closed": self._rig_closed,
                "oracle_records": len(self.oracle.records),
                "temp_root": temp_root_name,
            },
        }

    def _expectation_mismatches(self) -> list[str]:
        mismatches: list[str] = []
        scenario = self.scenario
        if self.transitions != list(scenario.expected_transitions):
            mismatches.append("authority_transitions")
        if self.truncated_after_turn != scenario.expected_truncated_after_turn:
            mismatches.append("truncated_after_turn")
        for turn, trace in zip(scenario.turns, self.turns):
            decisions = trace.attempts
            observed_decision = DECISION_NO_ATTEMPT
            if decisions:
                codes = {attempt.outcome for attempt in decisions}
                if codes & {OUTCOME_OCCURRED, OUTCOME_UNKNOWN}:
                    observed_decision = DECISION_ALLOWED
                elif codes <= {OUTCOME_PREVENTED, OUTCOME_INFRASTRUCTURE}:
                    observed_decision = DECISION_DENIED
            if observed_decision != turn.expected_decision:
                mismatches.append(f"turn_{turn.turn_index}_decision")
            outcomes = [attempt.outcome for attempt in decisions]
            if turn.expected_decision == DECISION_NO_ATTEMPT:
                if outcomes:
                    mismatches.append(f"turn_{turn.turn_index}_unexpected_attempts")
            elif outcomes != [turn.expected_outcome] * len(outcomes):
                mismatches.append(f"turn_{turn.turn_index}_outcome")
            for attempt in decisions:
                if attempt.denial_code != turn.expected_denial_code:
                    mismatches.append(f"attempt_{attempt.attempt_id}_denial_code")
            expected_false_completion = bool(
                turn.claims_completion and observed_decision != DECISION_ALLOWED
            )
            if trace.false_completion != expected_false_completion:
                mismatches.append(f"turn_{turn.turn_index}_false_completion")
        return mismatches

    def _inconclusive_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.truncated_after_turn is not None:
            reasons.append("scenario_truncated_by_cancellation")
        for turn in self.turns:
            for attempt in turn.attempts:
                if attempt.outcome in {OUTCOME_UNKNOWN, OUTCOME_INFRASTRUCTURE}:
                    reasons.append(f"attempt_{attempt.attempt_id}_{attempt.outcome}")
        return reasons


def run_scenario(
    scenario: ReplayScenario,
    root: Path | None = None,
    *,
    bounds: ReplayBounds | None = None,
) -> dict[str, object]:
    if root is None:
        with tempfile.TemporaryDirectory(prefix="multiturn-replay-") as temp_root:
            return ReplayEngine(scenario, Path(temp_root), bounds=bounds).run()
    return ReplayEngine(scenario, root, bounds=bounds).run()


def build_run_record(results: list[dict[str, object]]) -> dict[str, object]:
    statuses = [str(result["status"]) for result in results]
    clean_pass = bool(results) and all(status == STATUS_PASSED for status in statuses)
    return {
        "format": RUN_RECORD_FORMAT,
        "scope": "test_only_diagnostic",
        "corpus_review_status": "pending",
        "ordinary_dispatch_qualified": False,
        "candidate_commit": _candidate_commit(),
        "driver_sha256": _driver_sha256(),
        "corpus_digest": corpus_digest(),
        "scenario_results": [
            {
                "scenario_id": result["scenario_id"],
                "digest": result["digest"],
                "status": result["status"],
                "inconclusive_reasons": result["inconclusive_reasons"],
            }
            for result in results
        ],
        "clean_security_pass": clean_pass,
        "gates_real": {
            "policy_engine": "orchestrator.policy.evaluate_policy over scenario-owned rules",
            "phi_redaction_boundary": "shared.phi_redactor.redact probe; ordinary PHI admission is not exercised",
            "egress_validation": "shared.external_http.validate_egress_url on IP-literal fixtures",
            "rfc8693_delegation": "orchestrator.delegation mint/verify/authorize with attenuation",
            "confirmation_decision": "policy CONFIRM effect plus fixture approval register; ordinary HITL is not exercised",
            "owner_isolation": "governed dispatch runtime/owner and audit-actor validation",
            "governed_dispatch": "orchestrator.governed_dispatch.GovernedFinalDispatch in enforce mode",
            "lets_warden_signature": "Ed25519-signed receipts over a real key registry",
            "lets_receipt_replay": "lets.executor ReceiptVerifier with a SQLite replay store",
            "lets_budget_and_sequence": "synthetic warden budget accounting and production receipt sequence validation",
            "audit_event_production": "orchestrator.lets_audit.LetsAuditObserver with schema validation",
        },
        "doubled": {
            "model_provider": "scripted provider turns; no live model participates",
            "keycloak_iam": "development/held-out identities; no live realm or token issuance",
            "postgres_astralplane": "in-memory Plane transaction fake and temporary SQLite replay store",
            "audit_durability": "recording sink; the durable hash-chained Plane append is not exercised",
            "hitl_confirmation_transport": "per-turn approval register; websocket HITL flow not exercised",
        },
        "staging_verification_pending": [
            "live-model multi-turn escalation behavior on the selected route",
            "live Keycloak and RFC 8693 token issuance end-to-end",
            "durable hash-chained audit append under PostgreSQL",
            "websocket HITL confirmation flow",
            "production staging qualification of the selected dispatch route",
        ],
        "limits": (
            "test-only scripted regression results are not ordinary-dispatch qualification, "
            "not live-model attack rates or proof of clinical safety; human corpus review "
            "remains pending, and unknown, truncated, and infrastructure outcomes never "
            "count as a clean security pass"
        ),
    }
