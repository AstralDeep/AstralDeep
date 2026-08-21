"""Feature 060 BYO revision generation, activation, and crash recovery.

The tests use a deterministic transactional fake for the activation coordinator.
PostgreSQL transition details remain covered by the runtime repository suite; this
module stresses the lifecycle rule at every externally visible boundary without
turning timing or process scheduling into test authority.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import astralprojection  # noqa: E402
from astralplane import (  # noqa: E402
    GENERATED_AGENT_BUNDLE_CONTRACT,
    FinalizedBundle,
)
from orchestrator.agent_constitution import (  # noqa: E402
    USER_AGENT_POLICY_REVISION,
)
from orchestrator.agent_generator import (  # noqa: E402
    BYO_BUNDLE_FILENAMES,
    BYO_RUNTIME_CONTRACT_VERSION,
    BYO_RUNTIME_LOCK_ARTIFACT,
    BYO_RUNTIME_LOCK_SHA256,
    AgentCodeGenerator,
    FinalizedBYOBundle,
)
from orchestrator.agent_lifecycle import (  # noqa: E402
    AgentRevisionActivator,
    CandidateAgentMetadata,
    CandidatePreparation,
    CandidateRevision,
    PromotionCommit,
    PhysicalStopReceipt,
    RecoveryPlan,
    RevisionActivationError,
    RevisionActivationRecoveryPendingError,
    StaleRuntimeGenerationError,
    _join_task_outcome_through_cancellation,
)
from orchestrator.work_admission import OperationState  # noqa: E402


PROJECTION_ROOT = Path(astralprojection.__file__).resolve().parents[2]
PROJECTION_LOCK = PROJECTION_ROOT / Path(BYO_RUNTIME_LOCK_ARTIFACT).relative_to(
    Path("components") / "AstralProjection"
)

AGENT_ID = "ua-recovery-owner"
OWNER_ID = "recovery-owner"
OLD_REVISION = str(uuid.UUID(int=1))
OLD_RUNTIME = str(uuid.UUID(int=2))
HOST_ID = str(uuid.UUID(int=3))
HOST_SESSION_ID = str(uuid.UUID(int=4))


def _source_files() -> dict[str, str]:
    return {
        "agent_main.py": "from astralprims_ui import normalize_tool_result\n",
        "astralprims_ui.py": "def normalize_tool_result(value):\n    return value\n",
        "protected_executor.py": "# public LETS executor adapter\n",
        "mcp_tools.py": "TOOL_REGISTRY = {}\n",
    }


@pytest.mark.skipif(
    not PROJECTION_LOCK.is_file(),
    reason="active Projection runtime lock is not part of the product image",
)
def test_runtime_manifest_constants_match_reviewed_lock_fixture():
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "runtime_reliability_060"
            / "runtime-lock-contract.json"
        ).read_text(encoding="utf-8")
    )
    assert fixture["runtime_contract_version"] == BYO_RUNTIME_CONTRACT_VERSION
    assert fixture["lock_artifact"] == BYO_RUNTIME_LOCK_ARTIFACT
    assert fixture["lock_digest"] == BYO_RUNTIME_LOCK_SHA256
    assert hashlib.sha256(PROJECTION_LOCK.read_bytes()).hexdigest() == (
        BYO_RUNTIME_LOCK_SHA256
    )
    digest_vector = fixture["bundle_digest_vector"]
    assert fixture["bundle_digest_contract"] == "canonical-json-utf8-v1"
    assert AgentCodeGenerator._bundle_digest(digest_vector["files"]) == (
        digest_vector["bundle_sha256"]
    )


def test_runtime_manifest_is_deterministic_complete_and_revision_bound():
    generator = AgentCodeGenerator(llm_client=object(), llm_model="unused")
    revision_id = str(uuid.UUID(int=10, version=4))
    files = _source_files()

    first = generator.finalize_byo_bundle(
        files=files,
        agent_id=AGENT_ID,
        revision_id=revision_id,
        agent_name="Recovery Agent",
        description="keeps the prior revision available",
        constitution_version="0.1.0",
        required_runtime_lock_sha256=BYO_RUNTIME_LOCK_SHA256,
    )
    second = generator.finalize_byo_bundle(
        files=dict(reversed(tuple(files.items()))),
        agent_id=AGENT_ID,
        revision_id=revision_id,
        agent_name="Recovery Agent",
        description="keeps the prior revision available",
        constitution_version="0.1.0",
        required_runtime_lock_sha256=BYO_RUNTIME_LOCK_SHA256,
    )

    assert tuple(first.files) == BYO_BUNDLE_FILENAMES
    assert isinstance(first, FinalizedBundle)
    assert FinalizedBYOBundle is FinalizedBundle
    assert first.contract is GENERATED_AGENT_BUNDLE_CONTRACT
    assert first.bundle_sha256 == second.bundle_sha256
    assert first.manifest_json == second.manifest_json
    assert first.manifest == second.manifest
    assert first.manifest["manifest_version"] == 2
    assert first.manifest["runtime_contract_version"] == BYO_RUNTIME_CONTRACT_VERSION
    assert first.manifest["revision_id"] == revision_id
    assert first.manifest["required_runtime_lock_sha256"] == BYO_RUNTIME_LOCK_SHA256
    assert first.manifest["bundle_sha256"] == first.bundle_sha256
    assert [entry["name"] for entry in first.manifest["files"]] == list(
        BYO_BUNDLE_FILENAMES
    )
    assert "generated_at" not in first.manifest
    assert json.loads(first.manifest_json) == first.manifest_dict()
    with pytest.raises(TypeError):
        first.manifest["files"][0]["name"] = "changed"
    detached = first.manifest_dict()
    detached["files"][0]["name"] = "changed"
    assert first.manifest["files"][0]["name"] == "agent_main.py"

    expected_file_hashes = {
        name: hashlib.sha256(files[name].encode("utf-8")).hexdigest()
        for name in BYO_BUNDLE_FILENAMES
    }
    assert {
        entry["name"]: entry["sha256"] for entry in first.manifest["files"]
    } == expected_file_hashes


def test_generated_v3_child_registers_and_echoes_complete_request_fence(
    tmp_path: Path,
) -> None:
    generator = AgentCodeGenerator(llm_client=object(), llm_model="unused")
    revision_id = str(uuid.uuid4())
    files = generator.generate_byo_scaffold(
        agent_name="Fenced Child",
        description="proves the exact runtime and request generations",
        agent_id=AGENT_ID,
    ) | {"mcp_tools.py": "TOOL_REGISTRY = {}\n"}
    finalized = generator.finalize_byo_bundle(
        files=files,
        agent_id=AGENT_ID,
        revision_id=revision_id,
        agent_name="Fenced Child",
        description="proves the exact runtime and request generations",
        constitution_version="0.1.0",
        required_runtime_lock_sha256=BYO_RUNTIME_LOCK_SHA256,
    )
    for name, source in finalized.files.items():
        (tmp_path / name).write_text(source, encoding="utf-8")

    fence = {
        "agent_id": AGENT_ID,
        "host_id": str(uuid.uuid4()),
        "host_session_id": str(uuid.uuid4()),
        "delivery_id": str(uuid.uuid4()),
        "revision_id": revision_id,
        "runtime_instance_id": str(uuid.uuid4()),
        "process_id": str(uuid.uuid4()),
        "lifecycle_generation": 17,
    }
    request_id = str(uuid.uuid4())
    request_generation = str(uuid.uuid4())
    stale_request = {
        "type": "mcp_request",
        "method": "tools/list",
        "params": {},
        "fence": fence,
        "request_id": request_id,
        "request_generation": "not-a-uuid",
    }
    valid_request = stale_request | {"request_generation": request_generation}
    environment = os.environ.copy()
    environment.update(
        {
            "ASTRAL_RUNTIME_FENCE_JSON": json.dumps(fence),
            "ASTRAL_RUNTIME_CONTRACT_VERSION": "3",
            "ASTRAL_RUNTIME_BUNDLE_SHA256": finalized.bundle_sha256,
            "LETS_MODE": "off",
        }
    )
    completed = subprocess.run(
        [sys.executable, "agent_main.py"],
        cwd=tmp_path,
        env=environment,
        input=f"{json.dumps(stale_request)}\n{json.dumps(valid_request)}\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    frames = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [frame["type"] for frame in frames] == [
        "agent_runtime_register",
        "mcp_response",
    ]
    registration, response = frames
    assert registration["fence"] == fence
    assert registration["runtime_contract_version"] == 3
    assert registration["bundle_sha256"] == finalized.bundle_sha256
    assert registration["agent_card"]["agent_id"] == AGENT_ID
    assert response["fence"] == fence
    assert response["request_id"] == request_id
    assert response["request_generation"] == request_generation


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"agent_main.py": "x", "mcp_tools.py": "y"},
        {**_source_files(), "manifest.json": "{}"},
        {**_source_files(), "nested/file.py": "x"},
        {**_source_files(), "mcp_tools.py": b"not text"},
    ],
)
def test_runtime_manifest_refuses_incomplete_or_ambiguous_bundle(files):
    generator = AgentCodeGenerator(llm_client=object(), llm_model="unused")
    with pytest.raises((TypeError, ValueError)):
        generator.finalize_byo_bundle(
            files=files,
            agent_id=AGENT_ID,
            revision_id=str(uuid.UUID(int=11, version=4)),
            agent_name="Recovery Agent",
            description="keeps the prior revision available",
            constitution_version="0.1.0",
            required_runtime_lock_sha256=BYO_RUNTIME_LOCK_SHA256,
        )


def test_digest_changes_for_every_file_and_not_for_mapping_order():
    generator = AgentCodeGenerator(llm_client=object(), llm_model="unused")
    baseline = generator.finalize_byo_bundle(
        files=_source_files(),
        agent_id=AGENT_ID,
        revision_id=str(uuid.UUID(int=12, version=4)),
        agent_name="Recovery Agent",
        description="keeps the prior revision available",
        constitution_version="0.1.0",
        required_runtime_lock_sha256=BYO_RUNTIME_LOCK_SHA256,
    )
    for name in BYO_BUNDLE_FILENAMES:
        changed = _source_files()
        changed[name] += "# changed\n"
        candidate = generator.finalize_byo_bundle(
            files=changed,
            agent_id=AGENT_ID,
            revision_id=str(uuid.UUID(int=12, version=4)),
            agent_name="Recovery Agent",
            description="keeps the prior revision available",
            constitution_version="0.1.0",
            required_runtime_lock_sha256=BYO_RUNTIME_LOCK_SHA256,
        )
        assert candidate.bundle_sha256 != baseline.bundle_sha256


class SimulatedCrash(BaseException):
    """Power loss: bypass ordinary Exception cleanup and preserve durable state."""


class _TransactionalRevisionStore:
    """Small deterministic implementation of the lifecycle store protocol."""

    def __init__(self) -> None:
        self.active_revision_id = OLD_REVISION
        self.last_known_good_revision_id = OLD_REVISION
        self.authoritative_runtime_id = OLD_RUNTIME
        self.invocable_runtime_ids = {OLD_RUNTIME}
        self.candidates: dict[str, CandidateRevision] = {}
        self.failed_revision_ids: set[str] = set()
        self.revision_states = {OLD_REVISION: "active"}
        self.revision_failure_codes: dict[str, str | None] = {OLD_REVISION: None}
        self.runtime_revision_ids = {OLD_RUNTIME: OLD_REVISION}
        self.process_runtime_ids = {OLD_RUNTIME}
        self.staged_runtime_ids: set[str] = set()
        self.terminal_runtime_ids: set[str] = set()
        self.runtime_failure_codes: dict[str, str] = {}
        self.operation_states = {OLD_RUNTIME: OperationState.COMPLETED}
        self.operation_terminal_codes: dict[str, str | None] = {OLD_RUNTIME: None}
        self.events: list[str] = []
        self._counter = 20
        self.fail_promote = False
        self.lose_promote_ack_once = False

    def _uuid(self) -> str:
        self._counter += 1
        return str(uuid.UUID(int=self._counter))

    def prepare_candidate(self, request: CandidatePreparation) -> CandidateRevision:
        candidate = CandidateRevision(
            owner_user_id=request.owner_user_id,
            agent_id=request.agent_id,
            revision_id=request.revision_id,
            promotion_token=self._uuid(),
            runtime_instance_id=self._uuid(),
            previous_active_revision_id=self.active_revision_id,
            previous_runtime_instance_id=self.authoritative_runtime_id,
            agent_metadata=request.agent_metadata,
        )
        self.candidates[candidate.revision_id] = candidate
        self.revision_states[candidate.revision_id] = "prepared"
        self.revision_failure_codes[candidate.revision_id] = None
        self.runtime_revision_ids[candidate.runtime_instance_id] = (
            candidate.revision_id
        )
        self.operation_states[candidate.runtime_instance_id] = OperationState.RUNNING
        self.operation_terminal_codes[candidate.runtime_instance_id] = None
        self.events.append("prepared")
        return candidate

    def mark_candidate_starting(self, candidate: CandidateRevision) -> None:
        assert candidate.revision_id in self.candidates
        self.revision_states[candidate.revision_id] = "starting"
        self.process_runtime_ids.add(candidate.runtime_instance_id)
        self.events.append("starting")

    def confirm_candidate_ready(
        self, candidate: CandidateRevision, ready_runtime_instance_id: str
    ) -> CandidateRevision:
        if ready_runtime_instance_id != candidate.runtime_instance_id:
            raise RevisionActivationError("stale_runtime_generation")
        self.revision_states[candidate.revision_id] = "ready"
        self.events.append("ready")
        return candidate

    def promote_candidate(self, candidate: CandidateRevision) -> PromotionCommit:
        # Snapshot + restore models one database transaction. Every injected
        # failure before commit leaves all authoritative pointers untouched.
        before = (
            self.active_revision_id,
            self.last_known_good_revision_id,
            self.authoritative_runtime_id,
            set(self.invocable_runtime_ids),
        )
        try:
            if self.fail_promote:
                raise RevisionActivationError("revision_promotion_failed")
            if (
                self.active_revision_id == candidate.revision_id
                and self.authoritative_runtime_id == candidate.runtime_instance_id
            ):
                return PromotionCommit(
                    owner_user_id=candidate.owner_user_id,
                    agent_id=candidate.agent_id,
                    revision_id=candidate.revision_id,
                    runtime_instance_id=candidate.runtime_instance_id,
                    previous_revision_id=candidate.previous_active_revision_id,
                    previous_runtime_instance_id=(
                        candidate.previous_runtime_instance_id
                    ),
                )
            if self.active_revision_id != candidate.previous_active_revision_id:
                raise RevisionActivationError("revision_promotion_failed")
            previous_runtime = self.authoritative_runtime_id
            previous_revision = self.active_revision_id
            self.active_revision_id = candidate.revision_id
            self.last_known_good_revision_id = candidate.previous_active_revision_id
            self.authoritative_runtime_id = candidate.runtime_instance_id
            self.invocable_runtime_ids = {candidate.runtime_instance_id}
            self.revision_states[candidate.revision_id] = "active"
            if previous_revision is not None:
                self.revision_states[previous_revision] = "retired"
            if previous_runtime is not None:
                self.staged_runtime_ids.add(previous_runtime)
                self.runtime_failure_codes[previous_runtime] = (
                    "revision_promotion_failed"
                )
            self.operation_states[candidate.runtime_instance_id] = (
                OperationState.COMPLETED
            )
            self.operation_terminal_codes[candidate.runtime_instance_id] = None
            self.events.append("promoted")
            commit = PromotionCommit(
                owner_user_id=candidate.owner_user_id,
                agent_id=candidate.agent_id,
                revision_id=candidate.revision_id,
                runtime_instance_id=candidate.runtime_instance_id,
                previous_revision_id=candidate.previous_active_revision_id,
                previous_runtime_instance_id=previous_runtime,
            )
            if self.lose_promote_ack_once:
                self.lose_promote_ack_once = False
                raise OSError("promotion commit acknowledgement was lost")
            return commit
        except BaseException:
            if self.active_revision_id == candidate.revision_id:
                raise
            (
                self.active_revision_id,
                self.last_known_good_revision_id,
                self.authoritative_runtime_id,
                self.invocable_runtime_ids,
            ) = before
            raise

    def stage_candidate_failure(
        self, candidate: CandidateRevision, failure_code: str
    ) -> bool:
        if self.active_revision_id == candidate.revision_id:
            raise RevisionActivationRecoveryPendingError(
                "revision_promotion_recovery_pending"
            )
        self.staged_runtime_ids.add(candidate.runtime_instance_id)
        self.revision_failure_codes[candidate.revision_id] = failure_code
        self.runtime_failure_codes[candidate.runtime_instance_id] = failure_code
        if (
            self.operation_states[candidate.runtime_instance_id]
            is OperationState.RUNNING
        ):
            self.operation_states[candidate.runtime_instance_id] = OperationState.FAILED
            self.operation_terminal_codes[candidate.runtime_instance_id] = failure_code
        self.events.append(f"staged:{failure_code}")
        return candidate.runtime_instance_id in self.process_runtime_ids

    def fail_candidate(self, candidate: CandidateRevision, failure_code: str) -> None:
        if self.active_revision_id == candidate.revision_id:
            raise RevisionActivationRecoveryPendingError(
                "revision_promotion_recovery_pending"
            )
        assert candidate.runtime_instance_id in self.staged_runtime_ids
        assert self.runtime_failure_codes[candidate.runtime_instance_id] == failure_code
        self.failed_revision_ids.add(candidate.revision_id)
        self.revision_states[candidate.revision_id] = "failed"
        self.invocable_runtime_ids.discard(candidate.runtime_instance_id)
        self.terminal_runtime_ids.add(candidate.runtime_instance_id)
        self.process_runtime_ids.discard(candidate.runtime_instance_id)
        self.operation_states[candidate.runtime_instance_id] = OperationState.FAILED
        self.events.append(f"failed:{failure_code}")

    def recovery_plan(self, owner_user_id: str, agent_id: str) -> RecoveryPlan:
        assert owner_user_id == OWNER_ID and agent_id == AGENT_ID
        permanent_codes = {
            "bundle_install_failed",
            "child_start_failed",
            "child_registration_timeout",
            "revision_promotion_failed",
        }
        reset_pending = "revision_delivery_retry_reset_pending"
        mutable_states = {"prepared", "starting", "ready"}
        for revision_id, revision_state in self.revision_states.items():
            disposition = self.revision_failure_codes.get(revision_id)
            if (
                revision_state in mutable_states
                and disposition is not None
                and disposition not in {*permanent_codes, reset_pending}
            ):
                raise StaleRuntimeGenerationError(
                    "candidate revision disposition is invalid"
                )
        retry_reset_candidates = tuple(
            (revision_id, runtime_instance_id)
            for runtime_instance_id, revision_id in self.runtime_revision_ids.items()
            if runtime_instance_id != self.authoritative_runtime_id
            and self.revision_states[revision_id] in mutable_states
            and self.revision_failure_codes.get(revision_id) == reset_pending
            and self.runtime_failure_codes.get(runtime_instance_id)
            != "revision_delivery_retry_reset"
            and self.operation_states.get(runtime_instance_id)
            in {None, OperationState.RETRYABLE}
        )
        terminal_candidates = tuple(
            runtime_instance_id
            for runtime_instance_id in self.terminal_runtime_ids
            if (
                self.revision_states[self.runtime_revision_ids[runtime_instance_id]]
                in mutable_states
                and (
                    self.revision_failure_codes.get(
                        self.runtime_revision_ids[runtime_instance_id]
                    )
                    in permanent_codes
                    or (
                        self.operation_states.get(runtime_instance_id)
                        in {OperationState.FAILED, OperationState.CANCELLED}
                        and self.operation_terminal_codes.get(runtime_instance_id)
                        in permanent_codes
                    )
                )
            )
        )
        stop = tuple(
            runtime_instance_id
            for runtime_instance_id in self.runtime_revision_ids
            if runtime_instance_id != self.authoritative_runtime_id
            and not any(
                candidate_runtime_id == runtime_instance_id
                for _revision_id, candidate_runtime_id in retry_reset_candidates
            )
            and (
                (
                    runtime_instance_id not in self.terminal_runtime_ids
                    and not (
                        self.revision_states[
                            self.runtime_revision_ids[runtime_instance_id]
                        ]
                        in mutable_states
                        and self.revision_failure_codes.get(
                            self.runtime_revision_ids[runtime_instance_id]
                        )
                        is None
                        and self.operation_states.get(runtime_instance_id)
                        in {OperationState.RUNNING, OperationState.RETRYABLE}
                    )
                )
                or (
                    runtime_instance_id in terminal_candidates
                    and runtime_instance_id in self.process_runtime_ids
                )
            )
        )
        for runtime_instance_id in stop:
            revision_id = self.runtime_revision_ids[runtime_instance_id]
            if (
                self.revision_states[revision_id]
                in {"prepared", "starting", "ready"}
                and self.operation_states.get(runtime_instance_id)
                is OperationState.RETRYABLE
                and self.revision_failure_codes.get(revision_id) is None
            ):
                raise RevisionActivationRecoveryPendingError(
                    "revision_retry_reset_required"
                )
            self.staged_runtime_ids.add(runtime_instance_id)
            self.runtime_failure_codes.setdefault(
                runtime_instance_id, "revision_promotion_failed"
            )
        start_revision = (
            None if self.authoritative_runtime_id else self.active_revision_id
        )
        return RecoveryPlan(
            owner_user_id=owner_user_id,
            agent_id=agent_id,
            active_revision_id=self.active_revision_id,
            authoritative_runtime_instance_id=self.authoritative_runtime_id,
            start_revision_id=start_revision,
            stop_runtime_instance_ids=stop,
            finalize_runtime_instance_ids=tuple(
                runtime_instance_id
                for runtime_instance_id in terminal_candidates
                if runtime_instance_id not in self.process_runtime_ids
            ),
            retry_reset_candidates=retry_reset_candidates,
        )

    def finalize_recovery_runtime(
        self, owner_user_id: str, agent_id: str, runtime_instance_id: str
    ) -> None:
        assert owner_user_id == OWNER_ID and agent_id == AGENT_ID
        assert runtime_instance_id in self.staged_runtime_ids
        self.terminal_runtime_ids.add(runtime_instance_id)
        self.process_runtime_ids.discard(runtime_instance_id)
        revision_id = self.runtime_revision_ids[runtime_instance_id]
        if self.revision_states[revision_id] in {"prepared", "starting", "ready"}:
            self.revision_states[revision_id] = "failed"
            self.failed_revision_ids.add(revision_id)
            if (
                self.operation_states.get(runtime_instance_id)
                is OperationState.RUNNING
            ):
                self.operation_states[runtime_instance_id] = OperationState.FAILED
                self.operation_terminal_codes[runtime_instance_id] = (
                    self.runtime_failure_codes[runtime_instance_id]
                )
        self.events.append(f"finalized:{runtime_instance_id}")

    def stage_retryable_candidate_reset(
        self, owner_user_id: str, agent_id: str, revision_id: str
    ) -> str:
        assert owner_user_id == OWNER_ID and agent_id == AGENT_ID
        candidates = [
            runtime_instance_id
            for runtime_instance_id, observed_revision_id
            in self.runtime_revision_ids.items()
            if observed_revision_id == revision_id
            and runtime_instance_id not in self.terminal_runtime_ids
        ]
        assert len(candidates) == 1
        runtime_instance_id = candidates[0]
        operation_state = self.operation_states.get(runtime_instance_id)
        assert operation_state in {None, OperationState.RETRYABLE}
        if self.runtime_failure_codes.get(runtime_instance_id) not in {
            None,
            "revision_delivery_retry_reset_pending",
        }:
            raise RevisionActivationRecoveryPendingError(
                "revision_runtime_cleanup_pending"
            )
        self.staged_runtime_ids.add(runtime_instance_id)
        self.revision_failure_codes[revision_id] = (
            "revision_delivery_retry_reset_pending"
        )
        self.runtime_failure_codes[runtime_instance_id] = (
            "revision_delivery_retry_reset_pending"
        )
        self.events.append(f"retry-reset-staged:{runtime_instance_id}")
        return runtime_instance_id

    def finalize_retryable_candidate_reset(
        self,
        owner_user_id: str,
        agent_id: str,
        revision_id: str,
        runtime_instance_id: str,
    ) -> None:
        assert owner_user_id == OWNER_ID and agent_id == AGENT_ID
        assert self.runtime_revision_ids[runtime_instance_id] == revision_id
        operation_state = self.operation_states.get(runtime_instance_id)
        if (
            runtime_instance_id in self.terminal_runtime_ids
            and self.runtime_failure_codes[runtime_instance_id]
            == "revision_delivery_retry_reset"
            and self.revision_states[revision_id] == "prepared"
            and self.revision_failure_codes[revision_id] is None
        ):
            return
        assert (
            self.revision_failure_codes[revision_id]
            == "revision_delivery_retry_reset_pending"
        )
        if operation_state is None:
            terminal_physical_proof = (
                runtime_instance_id in self.terminal_runtime_ids
                and self.runtime_failure_codes[runtime_instance_id]
                in {"child_exited", "agent_offline"}
            )
            processless_prelaunch = (
                runtime_instance_id not in self.terminal_runtime_ids
                and runtime_instance_id not in self.process_runtime_ids
                and self.runtime_failure_codes[runtime_instance_id]
                == "revision_delivery_retry_reset_pending"
            )
            assert terminal_physical_proof or processless_prelaunch
        else:
            assert operation_state is OperationState.RETRYABLE
            assert self.runtime_failure_codes[runtime_instance_id] in {
                "revision_delivery_retry_reset_pending",
                "child_exited",
                "agent_offline",
            }
        self.terminal_runtime_ids.add(runtime_instance_id)
        self.process_runtime_ids.discard(runtime_instance_id)
        self.runtime_failure_codes[runtime_instance_id] = (
            "revision_delivery_retry_reset"
        )
        self.revision_states[revision_id] = "prepared"
        self.revision_failure_codes[revision_id] = None
        self.events.append(f"retry-reset-finalized:{runtime_instance_id}")


def _preparation(revision_id: str) -> CandidatePreparation:
    revision_id = str(uuid.UUID(str(revision_id), version=4))
    finalized = AgentCodeGenerator(
        llm_client=object(), llm_model="unused"
    ).finalize_byo_bundle(
        files=_source_files(),
        agent_id=AGENT_ID,
        revision_id=revision_id,
        agent_name="Recovery Agent",
        description="keeps the prior revision available",
        constitution_version="0.1.0",
        required_runtime_lock_sha256=BYO_RUNTIME_LOCK_SHA256,
    )
    return CandidatePreparation(
        owner_user_id=OWNER_ID,
        agent_id=AGENT_ID,
        revision_id=revision_id,
        bundle_sha256=finalized.bundle_sha256,
        runtime_manifest=finalized.manifest,
        artifact_relative_path=f"{AGENT_ID}/{revision_id}",
        runtime_contract_version=BYO_RUNTIME_CONTRACT_VERSION,
        required_runtime_lock_sha256=BYO_RUNTIME_LOCK_SHA256,
        host_session_id=HOST_SESSION_ID,
        operation_fence=None,
        agent_metadata=CandidateAgentMetadata(
            draft_id=str(uuid.UUID(int=13)),
            draft_state_revision=3,
            display_name="Recovery Agent",
            constitution_version="0.1.0",
            validated_policy_revision=USER_AGENT_POLICY_REVISION,
            declared_tools=(),
            declared_scopes=(),
            declared_egress=(),
        ),
    )


async def _activate(store, *, fault_boundary=None, start_failure=False):
    stopped: list[str] = []

    async def start(candidate):
        if start_failure:
            raise RuntimeError("host refused candidate")
        return candidate.runtime_instance_id

    async def ready(candidate):
        return candidate.runtime_instance_id

    async def stop(runtime_instance_id):
        stopped.append(runtime_instance_id)
        store.events.append(f"stop:{runtime_instance_id}")

    def fault(boundary, _candidate):
        if boundary == fault_boundary:
            raise SimulatedCrash(boundary)

    activator = AgentRevisionActivator(
        store,
        start_candidate=start,
        await_candidate_ready=ready,
        stop_runtime=stop,
        fault_hook=fault,
    )
    revision_id = str(uuid.UUID(int=100 + len(store.candidates)))
    return activator, stopped, _preparation(revision_id)


async def test_preparation_or_start_failure_never_stops_working_runtime():
    store = _TransactionalRevisionStore()
    activator, stopped, request = await _activate(store, start_failure=True)

    with pytest.raises(RevisionActivationError, match="child_start_failed"):
        await activator.activate(request)

    assert store.active_revision_id == OLD_REVISION
    assert store.authoritative_runtime_id == OLD_RUNTIME
    assert store.invocable_runtime_ids == {OLD_RUNTIME}
    # A refused start has no durable process identity, so there is nothing
    # exact to stop; cleanup still finalizes the prelaunch runtime atomically.
    assert stopped == []
    assert request.revision_id in store.failed_revision_ids


async def test_inventory_refusal_creates_no_candidate_or_stop_side_effect():
    store = _TransactionalRevisionStore()
    stopped: list[str] = []

    def refuse(_request):
        raise RevisionActivationError("inventory_required")

    store.prepare_candidate = refuse
    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda candidate: candidate.runtime_instance_id,
        await_candidate_ready=lambda candidate: candidate.runtime_instance_id,
        stop_runtime=lambda runtime_id: stopped.append(runtime_id),
    )

    with pytest.raises(RevisionActivationError, match="inventory_required"):
        await activator.activate(_preparation(str(uuid.UUID(int=6100))))

    assert store.candidates == {}
    assert stopped == []
    assert store.invocable_runtime_ids == {OLD_RUNTIME}


async def test_prepare_commit_ack_loss_replays_prepared_revision_exactly():
    store = _TransactionalRevisionStore()
    activator, _stopped, request = await _activate(store)
    prepare_candidate = store.prepare_candidate
    first = True

    def commit_revision_then_raise(preparation: CandidatePreparation):
        nonlocal first
        if first:
            first = False
            store.revision_states[preparation.revision_id] = "prepared"
            store.events.append("revision-prepared-only")
            raise OSError("prepared revision commit acknowledgement was lost")
        return prepare_candidate(preparation)

    store.prepare_candidate = commit_revision_then_raise

    result = await activator.activate(request)

    assert result.commit.revision_id == request.revision_id
    assert store.active_revision_id == request.revision_id
    assert store.events.count("revision-prepared-only") == 1
    assert store.events.count("prepared") == 1


async def test_repeated_prepare_commit_ambiguity_remains_recovery_pending():
    store = _TransactionalRevisionStore()
    activator, stopped, request = await _activate(store)

    def ambiguous(preparation: CandidatePreparation):
        store.revision_states[preparation.revision_id] = "prepared"
        raise OSError("prepared revision acknowledgement remains unavailable")

    store.prepare_candidate = ambiguous

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_preparation_recovery_pending",
    ):
        await activator.activate(request)

    assert store.revision_states[request.revision_id] == "prepared"
    assert request.revision_id not in store.failed_revision_ids
    assert store.active_revision_id == OLD_REVISION
    assert stopped == []


async def test_promotion_failure_terminalizes_only_candidate():
    store = _TransactionalRevisionStore()
    store.fail_promote = True
    activator, stopped, request = await _activate(store)

    with pytest.raises(RevisionActivationError, match="revision_promotion_failed"):
        await activator.activate(request)

    assert store.active_revision_id == OLD_REVISION
    assert store.last_known_good_revision_id == OLD_REVISION
    assert store.authoritative_runtime_id == OLD_RUNTIME
    assert store.invocable_runtime_ids == {OLD_RUNTIME}
    assert stopped and OLD_RUNTIME not in stopped
    assert request.revision_id in store.failed_revision_ids


@pytest.mark.parametrize("stop_outcome", ["missing", "send_false"])
async def test_failed_physical_stop_preserves_discoverable_cleanup_debt(
    stop_outcome: str,
) -> None:
    store = _TransactionalRevisionStore()
    calls: list[str] = []

    async def ready_failed(_candidate):
        raise RuntimeError("candidate never became ready")

    async def stop(runtime_instance_id):
        calls.append(runtime_instance_id)
        if stop_outcome == "missing":
            raise LookupError("exact runtime host is missing")
        return False

    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda candidate: candidate.runtime_instance_id,
        await_candidate_ready=ready_failed,
        stop_runtime=stop,
    )
    request = _preparation(str(uuid.UUID(int=6250)))

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_runtime_cleanup_pending",
    ):
        await activator.activate(request)

    candidate = store.candidates[request.revision_id]
    assert calls == [candidate.runtime_instance_id]
    assert request.revision_id not in store.failed_revision_ids
    assert store.revision_states[request.revision_id] == "starting"
    assert store.operation_states[candidate.runtime_instance_id] is OperationState.FAILED
    retry_plan = store.recovery_plan(OWNER_ID, AGENT_ID)
    assert candidate.runtime_instance_id in retry_plan.stop_runtime_instance_ids

    recovered_stops: list[str] = []
    recovery = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=lambda runtime_id: recovered_stops.append(runtime_id),
    )
    await recovery.reconcile_after_crash(OWNER_ID, AGENT_ID)

    assert recovered_stops == [candidate.runtime_instance_id]
    assert request.revision_id in store.failed_revision_ids
    assert store.operation_states[candidate.runtime_instance_id] is OperationState.FAILED
    assert (
        candidate.runtime_instance_id
        not in store.recovery_plan(OWNER_ID, AGENT_ID).stop_runtime_instance_ids
    )


async def test_finalization_failure_retries_stop_before_terminalizing_candidate():
    store = _TransactionalRevisionStore()
    original_finalize = store.fail_candidate
    finalize_calls = 0
    stopped: list[str] = []

    def fail_once(candidate, failure_code):
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            raise RuntimeError("Plane finalization unavailable")
        original_finalize(candidate, failure_code)

    async def ready_failed(_candidate):
        raise RuntimeError("candidate never became ready")

    store.fail_candidate = fail_once
    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda candidate: candidate.runtime_instance_id,
        await_candidate_ready=ready_failed,
        stop_runtime=lambda runtime_id: stopped.append(runtime_id),
    )
    request = _preparation(str(uuid.UUID(int=6251)))

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_runtime_cleanup_pending",
    ):
        await activator.activate(request)

    candidate = store.candidates[request.revision_id]
    assert stopped == [candidate.runtime_instance_id]
    assert request.revision_id not in store.failed_revision_ids
    assert candidate.runtime_instance_id in store.staged_runtime_ids

    await activator.reconcile_after_crash(OWNER_ID, AGENT_ID)

    assert stopped == [candidate.runtime_instance_id, candidate.runtime_instance_id]
    assert request.revision_id in store.failed_revision_ids
    assert finalize_calls == 1  # Recovery uses the generic exact-runtime finalizer.


async def test_stop_receipt_is_released_only_after_durable_finalization():
    store = _TransactionalRevisionStore()
    released: list[str] = []

    async def ready_failed(_candidate):
        raise RuntimeError("candidate never became ready")

    async def stop(runtime_instance_id):
        def release() -> None:
            revision_id = store.runtime_revision_ids[runtime_instance_id]
            assert store.revision_states[revision_id] == "failed"
            assert runtime_instance_id in store.terminal_runtime_ids
            released.append(runtime_instance_id)

        return PhysicalStopReceipt(runtime_instance_id, release)

    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda candidate: candidate.runtime_instance_id,
        await_candidate_ready=ready_failed,
        stop_runtime=stop,
    )
    request = _preparation(str(uuid.UUID(int=6252)))

    with pytest.raises(RevisionActivationError, match="child_registration_timeout"):
        await activator.activate(request)

    candidate = store.candidates[request.revision_id]
    assert released == [candidate.runtime_instance_id]


async def test_retryable_attempt_reset_waits_for_stop_before_reopening_revision():
    store = _TransactionalRevisionStore()
    candidate = store.prepare_candidate(_preparation(str(uuid.UUID(int=6253))))
    store.mark_candidate_starting(candidate)
    store.operation_states[candidate.runtime_instance_id] = OperationState.RETRYABLE
    released: list[str] = []

    def stop(runtime_instance_id: str) -> PhysicalStopReceipt:
        assert runtime_instance_id == candidate.runtime_instance_id
        assert store.revision_states[candidate.revision_id] == "starting"
        assert runtime_instance_id not in store.terminal_runtime_ids
        assert (
            store.runtime_failure_codes[runtime_instance_id]
            == "revision_delivery_retry_reset_pending"
        )
        return PhysicalStopReceipt(
            runtime_instance_id,
            lambda: released.append(runtime_instance_id),
        )

    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=stop,
    )
    old_runtime_id = await activator.reset_retryable_candidate(
        OWNER_ID,
        AGENT_ID,
        candidate.revision_id,
    )

    assert old_runtime_id == candidate.runtime_instance_id
    assert store.revision_states[candidate.revision_id] == "prepared"
    assert candidate.revision_id not in store.failed_revision_ids
    assert store.operation_states[old_runtime_id] is OperationState.RETRYABLE
    assert old_runtime_id in store.terminal_runtime_ids
    assert released == [old_runtime_id]


async def test_retryable_attempt_stop_failure_keeps_revision_and_runtime_staged():
    store = _TransactionalRevisionStore()
    candidate = store.prepare_candidate(_preparation(str(uuid.UUID(int=6257))))
    store.mark_candidate_starting(candidate)
    store.operation_states[candidate.runtime_instance_id] = OperationState.RETRYABLE
    stop_calls: list[str] = []

    def missing_stop(runtime_instance_id: str) -> bool:
        stop_calls.append(runtime_instance_id)
        return False

    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=missing_stop,
    )

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_runtime_cleanup_pending",
    ):
        await activator.reset_retryable_candidate(
            OWNER_ID,
            AGENT_ID,
            candidate.revision_id,
        )

    assert stop_calls == [candidate.runtime_instance_id]
    assert store.revision_states[candidate.revision_id] == "starting"
    assert candidate.runtime_instance_id not in store.terminal_runtime_ids
    assert candidate.runtime_instance_id in store.process_runtime_ids
    assert (
        store.runtime_failure_codes[candidate.runtime_instance_id]
        == "revision_delivery_retry_reset_pending"
    )

    released: list[str] = []
    retry = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=lambda runtime_id: PhysicalStopReceipt(
            runtime_id,
            lambda: released.append(runtime_id),
        ),
    )
    recovered_runtime_id = await retry.reset_retryable_candidate(
        OWNER_ID,
        AGENT_ID,
        candidate.revision_id,
    )

    assert recovered_runtime_id == candidate.runtime_instance_id
    assert store.revision_states[candidate.revision_id] == "prepared"
    assert candidate.runtime_instance_id in store.terminal_runtime_ids
    assert released == [candidate.runtime_instance_id]


async def test_retry_reset_finalizer_commit_ack_loss_replays_before_release():
    store = _TransactionalRevisionStore()
    candidate = store.prepare_candidate(_preparation(str(uuid.UUID(int=6259))))
    store.mark_candidate_starting(candidate)
    store.operation_states[candidate.runtime_instance_id] = OperationState.RETRYABLE
    finalize = store.finalize_retryable_candidate_reset
    first = True
    released: list[str] = []

    def commit_then_raise(*args):
        nonlocal first
        finalize(*args)
        if first:
            first = False
            raise OSError("retry reset finalizer acknowledgement was lost")

    store.finalize_retryable_candidate_reset = commit_then_raise
    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=lambda runtime_id: PhysicalStopReceipt(
            runtime_id,
            lambda: released.append(runtime_id),
        ),
    )

    reset_runtime_id = await activator.reset_retryable_candidate(
        OWNER_ID,
        AGENT_ID,
        candidate.revision_id,
    )

    assert reset_runtime_id == candidate.runtime_instance_id
    assert store.revision_states[candidate.revision_id] == "prepared"
    assert candidate.runtime_instance_id in store.terminal_runtime_ids
    assert released == [candidate.runtime_instance_id]


async def test_retry_reset_commit_wins_but_caller_cancellation_has_precedence():
    store = _TransactionalRevisionStore()
    candidate = store.prepare_candidate(_preparation(str(uuid.UUID(int=6264))))
    store.mark_candidate_starting(candidate)
    store.operation_states[candidate.runtime_instance_id] = OperationState.RETRYABLE
    finalize_committed = threading.Event()
    release_response = threading.Event()
    released_receipts: list[str] = []
    finalize = store.finalize_retryable_candidate_reset

    def commit_then_hold_response(*args):
        finalize(*args)
        finalize_committed.set()
        assert release_response.wait(2)

    store.finalize_retryable_candidate_reset = commit_then_hold_response
    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=lambda runtime_id: PhysicalStopReceipt(
            runtime_id,
            lambda: released_receipts.append(runtime_id),
        ),
    )
    reset = asyncio.create_task(
        activator.reset_retryable_candidate(
            OWNER_ID,
            AGENT_ID,
            candidate.revision_id,
        )
    )
    assert await asyncio.to_thread(finalize_committed.wait, 1)

    reset.cancel()
    release_response.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(reset, timeout=2)
    assert store.revision_states[candidate.revision_id] == "prepared"
    assert store.revision_failure_codes[candidate.revision_id] is None
    assert candidate.runtime_instance_id in store.terminal_runtime_ids
    assert released_receipts == [candidate.runtime_instance_id]


async def test_retryable_state_cannot_erase_staged_permanent_failure_intent():
    store = _TransactionalRevisionStore()
    candidate = store.prepare_candidate(_preparation(str(uuid.UUID(int=6254))))
    store.mark_candidate_starting(candidate)
    assert store.stage_candidate_failure(candidate, "child_registration_timeout")
    # Model a legacy outer settlement/lease expiry after cleanup staging. The
    # persisted runtime failure code remains the permanent disposition source.
    store.operation_states[candidate.runtime_instance_id] = OperationState.RETRYABLE

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_runtime_cleanup_pending",
    ):
        store.stage_retryable_candidate_reset(
            OWNER_ID,
            AGENT_ID,
            candidate.revision_id,
        )

    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=lambda _runtime_id: None,
    )
    await activator.reconcile_after_crash(OWNER_ID, AGENT_ID)

    assert store.revision_states[candidate.revision_id] == "failed"
    assert candidate.revision_id in store.failed_revision_ids
    assert (
        store.operation_states[candidate.runtime_instance_id]
        is OperationState.RETRYABLE
    )


async def test_terminal_host_cleanup_recovers_permanent_operation_disposition():
    store = _TransactionalRevisionStore()
    candidate = store.prepare_candidate(_preparation(str(uuid.UUID(int=6255))))
    store.mark_candidate_starting(candidate)
    assert store.stage_candidate_failure(candidate, "child_registration_timeout")
    # Model disconnect/re-register cleanup winning before lifecycle finalization.
    store.terminal_runtime_ids.add(candidate.runtime_instance_id)
    store.process_runtime_ids.discard(candidate.runtime_instance_id)
    store.runtime_failure_codes[candidate.runtime_instance_id] = "host_lost"

    plan = store.recovery_plan(OWNER_ID, AGENT_ID)
    assert plan.stop_runtime_instance_ids == ()
    assert plan.finalize_runtime_instance_ids == (candidate.runtime_instance_id,)
    observed_terminal_proofs: list[str] = []

    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=lambda runtime_id: observed_terminal_proofs.append(runtime_id),
    )
    await activator.reconcile_after_crash(OWNER_ID, AGENT_ID)

    assert observed_terminal_proofs == [candidate.runtime_instance_id]
    assert store.revision_states[candidate.revision_id] == "failed"
    assert candidate.revision_id in store.failed_revision_ids
    assert (
        store.operation_terminal_codes[candidate.runtime_instance_id]
        == "child_registration_timeout"
    )


async def test_revision_disposition_survives_operation_purge_and_physical_proof():
    store = _TransactionalRevisionStore()
    candidate = store.prepare_candidate(_preparation(str(uuid.UUID(int=6260))))
    store.mark_candidate_starting(candidate)
    assert store.stage_candidate_failure(candidate, "child_registration_timeout")
    assert (
        store.revision_failure_codes[candidate.revision_id]
        == "child_registration_timeout"
    )

    # Plane's exact exit reducer owns the physical fact, while retention later
    # clears the delivery-operation FK. The mutable revision remains the
    # self-contained semantic authority for recovery.
    store.terminal_runtime_ids.add(candidate.runtime_instance_id)
    store.process_runtime_ids.discard(candidate.runtime_instance_id)
    store.runtime_failure_codes[candidate.runtime_instance_id] = "child_exited"
    store.operation_states.pop(candidate.runtime_instance_id)
    store.operation_terminal_codes.pop(candidate.runtime_instance_id)

    plan = store.recovery_plan(OWNER_ID, AGENT_ID)
    assert plan.stop_runtime_instance_ids == ()
    assert plan.finalize_runtime_instance_ids == (candidate.runtime_instance_id,)

    physical_proofs: list[str] = []
    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=lambda runtime_id: physical_proofs.append(runtime_id),
    )
    await activator.reconcile_after_crash(OWNER_ID, AGENT_ID)

    assert physical_proofs == [candidate.runtime_instance_id]
    assert store.revision_states[candidate.revision_id] == "failed"
    assert (
        store.revision_failure_codes[candidate.revision_id]
        == "child_registration_timeout"
    )


async def test_retry_reset_marker_survives_operation_purge_and_clears_atomically():
    store = _TransactionalRevisionStore()
    candidate = store.prepare_candidate(_preparation(str(uuid.UUID(int=6261))))
    store.mark_candidate_starting(candidate)
    store.operation_states[candidate.runtime_instance_id] = OperationState.RETRYABLE
    runtime_instance_id = store.stage_retryable_candidate_reset(
        OWNER_ID,
        AGENT_ID,
        candidate.revision_id,
    )
    assert (
        store.revision_failure_codes[candidate.revision_id]
        == "revision_delivery_retry_reset_pending"
    )

    store.terminal_runtime_ids.add(runtime_instance_id)
    store.process_runtime_ids.discard(runtime_instance_id)
    store.runtime_failure_codes[runtime_instance_id] = "agent_offline"
    store.operation_states.pop(runtime_instance_id)
    store.operation_terminal_codes.pop(runtime_instance_id)

    plan = store.recovery_plan(OWNER_ID, AGENT_ID)
    assert plan.retry_reset_candidates == (
        (candidate.revision_id, runtime_instance_id),
    )
    assert plan.stop_runtime_instance_ids == ()
    assert plan.finalize_runtime_instance_ids == ()

    proof_checks: list[str] = []
    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=lambda observed_runtime_id: proof_checks.append(
            observed_runtime_id
        ),
    )
    await activator.reconcile_after_crash(OWNER_ID, AGENT_ID)

    assert proof_checks == [runtime_instance_id]
    assert store.revision_states[candidate.revision_id] == "prepared"
    assert store.revision_failure_codes[candidate.revision_id] is None
    assert (
        store.runtime_failure_codes[runtime_instance_id]
        == "revision_delivery_retry_reset"
    )
    assert store.recovery_plan(OWNER_ID, AGENT_ID).retry_reset_candidates == ()


async def test_processless_retry_reset_survives_operation_purge_without_stop():
    store = _TransactionalRevisionStore()
    candidate = store.prepare_candidate(_preparation(str(uuid.UUID(int=6263))))
    store.operation_states[candidate.runtime_instance_id] = OperationState.RETRYABLE
    runtime_instance_id = store.stage_retryable_candidate_reset(
        OWNER_ID,
        AGENT_ID,
        candidate.revision_id,
    )
    store.operation_states.pop(runtime_instance_id)
    store.operation_terminal_codes.pop(runtime_instance_id)

    plan = store.recovery_plan(OWNER_ID, AGENT_ID)
    assert plan.retry_reset_candidates == (
        (candidate.revision_id, runtime_instance_id),
    )
    assert runtime_instance_id not in store.process_runtime_ids

    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=lambda _runtime_id: None,
    )
    await activator.reconcile_after_crash(OWNER_ID, AGENT_ID)

    assert store.revision_states[candidate.revision_id] == "prepared"
    assert store.revision_failure_codes[candidate.revision_id] is None
    assert runtime_instance_id in store.terminal_runtime_ids


def test_arbitrary_mutable_revision_disposition_fails_closed():
    store = _TransactionalRevisionStore()
    candidate = store.prepare_candidate(_preparation(str(uuid.UUID(int=6262))))
    store.revision_failure_codes[candidate.revision_id] = "untrusted_cleanup_hint"

    with pytest.raises(
        StaleRuntimeGenerationError,
        match="candidate revision disposition is invalid",
    ):
        store.recovery_plan(OWNER_ID, AGENT_ID)


async def test_terminal_process_candidate_still_requires_exact_stop_proof():
    store = _TransactionalRevisionStore()
    candidate = store.prepare_candidate(_preparation(str(uuid.UUID(int=6258))))
    store.mark_candidate_starting(candidate)
    assert store.stage_candidate_failure(candidate, "child_registration_timeout")
    # A generic terminal state can win before the host's full-fence exit frame;
    # retain the process identity so recovery must obtain the exact proof.
    store.terminal_runtime_ids.add(candidate.runtime_instance_id)
    stop_calls: list[str] = []
    released: list[str] = []

    plan = store.recovery_plan(OWNER_ID, AGENT_ID)
    assert plan.stop_runtime_instance_ids == (candidate.runtime_instance_id,)
    assert plan.finalize_runtime_instance_ids == ()

    def stop(runtime_instance_id: str) -> PhysicalStopReceipt:
        stop_calls.append(runtime_instance_id)
        return PhysicalStopReceipt(
            runtime_instance_id,
            lambda: released.append(runtime_instance_id),
        )

    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=stop,
    )
    await activator.reconcile_after_crash(OWNER_ID, AGENT_ID)

    assert stop_calls == [candidate.runtime_instance_id]
    assert released == [candidate.runtime_instance_id]
    assert store.revision_states[candidate.revision_id] == "failed"


async def test_agent_wide_recovery_does_not_consume_concurrent_running_revision():
    store = _TransactionalRevisionStore()
    candidate = store.prepare_candidate(_preparation(str(uuid.UUID(int=6256))))
    store.mark_candidate_starting(candidate)
    before_candidate = candidate

    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda _candidate: pytest.fail("must not start"),
        await_candidate_ready=lambda _candidate: pytest.fail("must not await"),
        stop_runtime=lambda _runtime_id: pytest.fail(
            "active replay cleanup must not stop a concurrent revision"
        ),
    )
    plan = await activator.reconcile_after_crash(OWNER_ID, AGENT_ID)

    assert plan.stop_runtime_instance_ids == ()
    assert plan.finalize_runtime_instance_ids == ()
    assert store.candidates[candidate.revision_id] == before_candidate
    assert store.revision_states[candidate.revision_id] == "starting"
    assert (
        store.operation_states[candidate.runtime_instance_id]
        is OperationState.RUNNING
    )
    assert candidate.runtime_instance_id not in store.staged_runtime_ids


def test_two_ready_candidates_cannot_both_become_authoritative():
    store = _TransactionalRevisionStore()
    first = store.prepare_candidate(_preparation(str(uuid.UUID(int=6300))))
    second = store.prepare_candidate(_preparation(str(uuid.UUID(int=6301))))
    store.mark_candidate_starting(first)
    store.mark_candidate_starting(second)
    store.confirm_candidate_ready(first, first.runtime_instance_id)
    store.confirm_candidate_ready(second, second.runtime_instance_id)

    committed = store.promote_candidate(first)
    with pytest.raises(RevisionActivationError, match="revision_promotion_failed"):
        store.promote_candidate(second)
    assert store.stage_candidate_failure(second, "revision_promotion_failed")
    store.fail_candidate(second, "revision_promotion_failed")

    assert store.active_revision_id == first.revision_id
    assert store.authoritative_runtime_id == committed.runtime_instance_id
    assert store.invocable_runtime_ids == {first.runtime_instance_id}
    assert second.revision_id in store.failed_revision_ids


async def test_prior_runtime_stops_only_after_promotion_commit():
    store = _TransactionalRevisionStore()
    activator, stopped, request = await _activate(store)

    result = await activator.activate(request)

    assert result.commit.revision_id == request.revision_id
    assert result.prior_runtime_stopped
    assert store.active_revision_id == request.revision_id
    assert store.last_known_good_revision_id == OLD_REVISION
    assert store.authoritative_runtime_id == result.commit.runtime_instance_id
    assert stopped == [OLD_RUNTIME]
    assert store.events.index("promoted") < store.events.index(f"stop:{OLD_RUNTIME}")


async def test_post_commit_observer_failure_cannot_relabel_promotion_failed():
    store = _TransactionalRevisionStore()
    stopped: list[str] = []

    async def start(candidate):
        return candidate.runtime_instance_id

    async def ready(candidate):
        return candidate.runtime_instance_id

    async def stop(runtime_instance_id):
        stopped.append(runtime_instance_id)

    def observer(boundary, _candidate):
        if boundary == "after_promote_commit":
            raise RuntimeError("metrics sink unavailable")

    activator = AgentRevisionActivator(
        store,
        start_candidate=start,
        await_candidate_ready=ready,
        stop_runtime=stop,
        fault_hook=observer,
    )
    request = _preparation(str(uuid.UUID(int=6000)))

    result = await activator.activate(request)

    assert result.commit.revision_id == request.revision_id
    assert store.active_revision_id == request.revision_id
    assert request.revision_id not in store.failed_revision_ids
    assert stopped == [OLD_RUNTIME]


async def test_post_commit_stop_failure_reports_cleanup_without_rollback():
    store = _TransactionalRevisionStore()

    async def start(candidate):
        return candidate.runtime_instance_id

    async def ready(candidate):
        return candidate.runtime_instance_id

    async def stop(_runtime_instance_id):
        raise RuntimeError("host disconnected during stop")

    activator = AgentRevisionActivator(
        store,
        start_candidate=start,
        await_candidate_ready=ready,
        stop_runtime=stop,
    )
    request = _preparation(str(uuid.UUID(int=6200)))

    result = await activator.activate(request)

    assert result.cleanup_pending
    assert not result.prior_runtime_stopped
    assert store.active_revision_id == request.revision_id
    assert request.revision_id not in store.failed_revision_ids


async def test_activation_store_calls_never_block_the_event_loop_thread():
    loop_thread = threading.get_ident()
    store = _TransactionalRevisionStore()
    observed_threads: list[int] = []

    for method_name in (
        "prepare_candidate",
        "mark_candidate_starting",
        "confirm_candidate_ready",
        "promote_candidate",
        "recovery_plan",
    ):
        original = getattr(store, method_name)

        def record_thread(*args, _original=original):
            observed_threads.append(threading.get_ident())
            return _original(*args)

        setattr(store, method_name, record_thread)

    activator, _stopped, request = await _activate(store)
    result = await activator.activate(request)
    plan = await activator.reconcile_after_crash(OWNER_ID, AGENT_ID)

    assert result.commit.revision_id == request.revision_id
    assert plan.active_revision_id == request.revision_id
    assert observed_threads
    assert all(thread_id != loop_thread for thread_id in observed_threads)


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_same_turn_completion_and_caller_cancel_preserves_cancellation(
    cancel_first: bool,
) -> None:
    loop = asyncio.get_running_loop()
    released: asyncio.Future[str] = loop.create_future()

    async def worker() -> str:
        return await released

    retained = asyncio.create_task(worker())
    joining = asyncio.create_task(
        _join_task_outcome_through_cancellation(retained)
    )
    await asyncio.sleep(0)
    callbacks = (
        (joining.cancel, lambda: released.set_result("committed"))
        if cancel_first
        else (lambda: released.set_result("committed"), joining.cancel)
    )
    loop.call_soon(callbacks[0])
    loop.call_soon(callbacks[1])

    result, error, cancellation = await joining

    assert result == "committed"
    assert error is None
    assert isinstance(cancellation, asyncio.CancelledError)


async def test_repeated_cancellation_after_prepare_joins_candidate_cleanup():
    store = _TransactionalRevisionStore()
    start_entered = asyncio.Event()
    release_start = asyncio.Event()
    failure_entered = threading.Event()
    release_failure = threading.Event()
    stopped: list[str] = []
    original_fail = store.fail_candidate

    async def start(candidate):
        start_entered.set()
        await release_start.wait()
        return candidate.runtime_instance_id

    def fail_candidate(candidate, failure_code):
        failure_entered.set()
        assert release_failure.wait(2)
        original_fail(candidate, failure_code)

    async def stop(runtime_instance_id):
        stopped.append(runtime_instance_id)

    store.fail_candidate = fail_candidate
    activator = AgentRevisionActivator(
        store,
        start_candidate=start,
        await_candidate_ready=lambda candidate: candidate.runtime_instance_id,
        stop_runtime=stop,
    )
    request = _preparation(str(uuid.UUID(int=6201)))
    activation = asyncio.create_task(activator.activate(request))
    await asyncio.wait_for(start_entered.wait(), timeout=1)

    activation.cancel()
    release_start.set()
    assert await asyncio.to_thread(failure_entered.wait, 1)
    activation.cancel()
    release_failure.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(activation, timeout=2)
    assert request.revision_id in store.failed_revision_ids
    candidate = store.candidates[request.revision_id]
    assert stopped == []
    assert candidate.runtime_instance_id in store.terminal_runtime_ids
    assert store.active_revision_id == OLD_REVISION
    assert store.invocable_runtime_ids == {OLD_RUNTIME}


async def test_cancellation_after_promotion_commit_preserves_new_authority():
    store = _TransactionalRevisionStore()
    promote_committed = threading.Event()
    release_promote_response = threading.Event()
    stopped: list[str] = []
    original_promote = store.promote_candidate

    def commit_then_hold_response(candidate):
        commit = original_promote(candidate)
        promote_committed.set()
        assert release_promote_response.wait(2)
        return commit

    async def stop(runtime_instance_id):
        stopped.append(runtime_instance_id)

    store.promote_candidate = commit_then_hold_response
    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda candidate: candidate.runtime_instance_id,
        await_candidate_ready=lambda candidate: candidate.runtime_instance_id,
        stop_runtime=stop,
    )
    request = _preparation(str(uuid.UUID(int=6202)))
    activation = asyncio.create_task(activator.activate(request))
    assert await asyncio.to_thread(promote_committed.wait, 1)

    activation.cancel()
    release_promote_response.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(activation, timeout=2)
    assert store.active_revision_id == request.revision_id
    assert request.revision_id not in store.failed_revision_ids
    assert stopped == [OLD_RUNTIME]


async def test_lost_promotion_commit_ack_replays_authority_without_failure():
    store = _TransactionalRevisionStore()
    store.lose_promote_ack_once = True
    activator, stopped, request = await _activate(store)

    result = await activator.activate(request)

    assert result.commit.revision_id == request.revision_id
    assert store.active_revision_id == request.revision_id
    assert request.revision_id not in store.failed_revision_ids
    assert stopped == [OLD_RUNTIME]
    assert store.events.count("promoted") == 1


async def test_repeated_promotion_ack_failure_is_recovery_pending_without_cleanup():
    store = _TransactionalRevisionStore()
    stopped: list[str] = []

    def ambiguous_promote(_candidate):
        raise OSError("promotion acknowledgement unavailable")

    store.promote_candidate = ambiguous_promote
    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda candidate: candidate.runtime_instance_id,
        await_candidate_ready=lambda candidate: candidate.runtime_instance_id,
        stop_runtime=lambda runtime_id: stopped.append(runtime_id),
    )
    request = _preparation(str(uuid.UUID(int=6203)))

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_promotion_recovery_pending",
    ):
        await activator.activate(request)

    assert request.revision_id not in store.failed_revision_ids
    assert stopped == []


async def test_cancel_during_promotion_ambiguity_never_stops_unknown_winner():
    store = _TransactionalRevisionStore()
    replay_entered = threading.Event()
    release_replay = threading.Event()
    stopped: list[str] = []
    calls = 0

    def ambiguous_promote(_candidate):
        nonlocal calls
        calls += 1
        if calls == 2:
            replay_entered.set()
            assert release_replay.wait(2)
        raise OSError("promotion acknowledgement unavailable")

    store.promote_candidate = ambiguous_promote
    activator = AgentRevisionActivator(
        store,
        start_candidate=lambda candidate: candidate.runtime_instance_id,
        await_candidate_ready=lambda candidate: candidate.runtime_instance_id,
        stop_runtime=lambda runtime_id: stopped.append(runtime_id),
    )
    request = _preparation(str(uuid.UUID(int=6204)))
    activation = asyncio.create_task(activator.activate(request))
    assert await asyncio.to_thread(replay_entered.wait, 1)

    activation.cancel()
    release_replay.set()

    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(activation, timeout=2)
    assert isinstance(
        caught.value.__cause__,
        RevisionActivationRecoveryPendingError,
    )
    assert request.revision_id not in store.failed_revision_ids
    assert stopped == []


@pytest.mark.parametrize(
    "boundary",
    [
        "after_prepare",
        "before_start",
        "after_start",
        "before_ready",
        "after_ready",
        "before_promote",
        "after_promote_commit",
        "before_prior_stop",
        "after_prior_stop",
    ],
)
async def test_one_hundred_fault_boundaries_preserve_one_durable_authority(boundary):
    # Nine boundaries x twelve deterministic trials = 108, satisfying SC-004's
    # minimum while covering both sides of the database commit.
    elapsed_ms = []
    outcomes = {"prior_authority": 0, "candidate_authority": 0}
    for trial in range(12):
        store = _TransactionalRevisionStore()
        activator, _stopped, request = await _activate(store, fault_boundary=boundary)
        request = replace(request, revision_id=str(uuid.UUID(int=1000 + trial)))

        started = time.perf_counter()
        with pytest.raises(SimulatedCrash):
            await activator.activate(request)
        elapsed_ms.append(round((time.perf_counter() - started) * 1000, 3))

        if boundary in {
            "after_promote_commit",
            "before_prior_stop",
            "after_prior_stop",
        }:
            outcomes["candidate_authority"] += 1
            assert store.active_revision_id == request.revision_id
            assert store.authoritative_runtime_id != OLD_RUNTIME
            assert store.invocable_runtime_ids != {OLD_RUNTIME}
        else:
            outcomes["prior_authority"] += 1
            assert store.active_revision_id == OLD_REVISION
            assert store.authoritative_runtime_id == OLD_RUNTIME
            assert store.invocable_runtime_ids == {OLD_RUNTIME}
    ordered = sorted(elapsed_ms)
    print(
        "US2_PROMOTION_DISTRIBUTION="
        + json.dumps(
            {
                "boundary": boundary,
                "count": len(ordered),
                "outcomes": outcomes,
                "p50_ms": ordered[5],
                "p95_ms": ordered[11],
                "max_ms": ordered[-1],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


async def test_crash_recovery_follows_durable_pointer_and_stops_candidates():
    store = _TransactionalRevisionStore()
    activator, stopped, request = await _activate(
        store, fault_boundary="after_promote_commit"
    )
    with pytest.raises(SimulatedCrash):
        await activator.activate(request)

    # A second candidate was durable but never promoted. Recovery must not infer
    # authority from recency or readiness; only the committed active pointer wins.
    orphan = store.prepare_candidate(
        _preparation(str(uuid.UUID(int=5000)))
    )
    store.stage_candidate_failure(orphan, "revision_promotion_failed")
    plan = await activator.reconcile_after_crash(OWNER_ID, AGENT_ID)

    assert plan.active_revision_id == request.revision_id
    assert plan.authoritative_runtime_instance_id == store.authoritative_runtime_id
    assert orphan.runtime_instance_id in plan.stop_runtime_instance_ids
    assert orphan.runtime_instance_id in stopped
    assert plan.start_revision_id is None
