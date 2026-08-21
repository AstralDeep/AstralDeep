from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.drafts import DraftAgentRecord, DraftTransitionRecord
from orchestrator.draft_plane_store import PlaneDraftStore, draft_record_to_dict
from tests.helpers.draft_store_double import (
    DatabaseDraftStoreDouble,
    InMemoryDraftStore,
)


OWNER = "owner-074"
DRAFT_ID = "10000000-0000-4000-8000-000000000074"
TRANSITION_ID = "20000000-0000-4000-8000-000000000074"
OPERATION_ID = "30000000-0000-4000-8000-000000000074"
CLAIM_ID = "40000000-0000-4000-8000-000000000074"


def _draft(**changes: object) -> DraftAgentRecord:
    base = DraftAgentRecord(
        draft_id=DRAFT_ID,
        owner_id=OWNER,
        agent_name="Plane Draft",
        agent_slug="plane_draft_100000000000",
        description="A bounded owner-scoped draft used by the adapter tests.",
        tools_spec=None,
        skill_tags=None,
        packages=None,
        status="pending",
        generation_log=None,
        security_report=None,
        error_message=None,
        port=None,
        review_notes=None,
        reviewed_by=None,
        refinement_history=None,
        validation_report=None,
        required_credentials=None,
        origin="byo_client",
        source_chat_id=None,
        gap_fingerprint=None,
        source_attachment_id=None,
        revises_agent_id=None,
        self_test=None,
        phase="specify",
        clarify_answers=None,
        plan_json=None,
        analyze_result=None,
        constitution_version=None,
        host_binding=None,
        draft_uuid=DRAFT_ID,
        target_agent_id="50000000-0000-4000-8000-000000000074",
        state_revision=0,
        generation_claim_id=None,
        generation_claim_expires_at=None,
        published_revision_id=None,
        created_at=1,
        updated_at=1,
    )
    return replace(base, **changes)


class Runtime:
    def __init__(self) -> None:
        self.transactions: list[object] = []

    @contextmanager
    def transaction(self):
        transaction = object()
        self.transactions.append(transaction)
        yield transaction


class WorkAdmission:
    def __init__(self) -> None:
        self.transactions: list[tuple[object, object]] = []

    @contextmanager
    def fenced_transaction(self, fence: object):
        transaction = object()
        self.transactions.append((fence, transaction))
        yield transaction


@pytest.fixture()
def boundary():
    runtime = Runtime()
    work_admission = WorkAdmission()
    drafts = MagicMock()
    identity = MagicMock()
    agents = MagicMock()
    tool_policy = MagicMock()
    catalog = SimpleNamespace(
        draft_agents=drafts,
        identity=identity,
        agents=agents,
        tool_policy_state=tool_policy,
    )
    store = PlaneDraftStore(
        plane_runtime=runtime,
        plane_repositories=catalog,
        work_admission=work_admission,
    )
    return store, runtime, work_admission, drafts, identity, agents, tool_policy


def test_constructor_and_record_mapping_fail_closed() -> None:
    with pytest.raises(TypeError, match="transaction"):
        PlaneDraftStore(plane_runtime=object())
    with pytest.raises(TypeError, match="catalog"):
        PlaneDraftStore(plane_runtime=Runtime())
    with pytest.raises(TypeError, match="incomplete"):
        PlaneDraftStore(
            plane_runtime=Runtime(),
            plane_repositories=SimpleNamespace(draft_agents=object()),
        )
    with pytest.raises(TypeError, match="DraftAgentRecord"):
        draft_record_to_dict(object())  # type: ignore[arg-type]

    mapped = draft_record_to_dict(_draft())
    assert mapped["id"] == DRAFT_ID
    assert mapped["user_id"] == OWNER
    assert mapped["state_revision"] == 0


def test_create_binds_observation_and_replay_stable_uuid4_target(boundary) -> None:
    store, _runtime, _work, drafts, *_rest = boundary

    def created(_transaction: object, **values: object) -> DraftAgentRecord:
        return _draft(
            draft_id=str(values["draft_id"]),
            owner_id=str(values["owner_id"]),
            target_agent_id=str(values["target_agent_id"]),
        )

    drafts.create_draft.side_effect = created
    first = store.create_draft_agent(
        DRAFT_ID,
        OWNER,
        "Plane Draft",
        "plane_draft_100000000000",
        "A bounded owner-scoped draft used by the adapter tests.",
        origin="byo_client",
    )
    second = store.create_draft_agent(
        DRAFT_ID,
        OWNER,
        "Plane Draft",
        "plane_draft_100000000000",
        "A bounded owner-scoped draft used by the adapter tests.",
        origin="byo_client",
    )
    first_values = drafts.create_draft.call_args_list[0].kwargs
    second_values = drafts.create_draft.call_args_list[1].kwargs
    assert first_values["observed_at"] >= 0
    assert first_values["target_agent_id"] == second_values["target_agent_id"]
    assert uuid.UUID(first_values["target_agent_id"]).version == 4
    assert first["target_agent_id"] == second["target_agent_id"]

    revision = store.create_draft_agent(
        "60000000-0000-4000-8000-000000000074",
        OWNER,
        "Revision",
        "revision_600000000000",
        "A replay-safe revision of the same governed personal agent.",
        revises_agent_id="existing-agent",
    )
    assert revision["target_agent_id"] == "existing-agent"

    allocated_target = "70000000-0000-4000-8000-000000000074"
    allocated = store.create_draft_agent(
        "80000000-0000-4000-8000-000000000074",
        OWNER,
        "Allocated target",
        "allocated_target_800000000000",
        "A once-allocated target survives analyze-before-draft.",
        target_agent_id=allocated_target,
    )
    assert drafts.create_draft.call_args.kwargs["target_agent_id"] == allocated_target
    assert allocated["target_agent_id"] == allocated_target

    for invalid_target in (
        "not-a-uuid",
        "AAAAAAAA-0000-4000-8000-000000000074",
        uuid.UUID(allocated_target),
    ):
        with pytest.raises(ValueError, match="canonical UUID"):
            store.create_draft_agent(
                "90000000-0000-4000-8000-000000000074",
                OWNER,
                "Invalid target",
                "invalid_target_900000000000",
                "Invalid explicit target proof.",
                target_agent_id=invalid_target,  # type: ignore[arg-type]
            )
    with pytest.raises(ValueError, match="must match revises_agent_id"):
        store.create_draft_agent(
            "90000000-0000-4000-8000-000000000074",
            OWNER,
            "Mismatched target",
            "mismatched_target_900000000000",
            "Revision identity mismatch proof.",
            revises_agent_id="existing-agent",
            target_agent_id=allocated_target,
        )


def test_in_memory_create_persists_explicit_target_and_defaults_deterministically() -> (
    None
):
    allocated_target = "70000000-0000-4000-8000-000000000074"
    draft_id = "80000000-0000-4000-8000-000000000074"
    first = InMemoryDraftStore()
    first.create_draft_agent(
        draft_id=draft_id,
        user_id=OWNER,
        agent_name="Explicit target",
        agent_slug="explicit_target_800000000000",
        description="Test-double explicit target proof.",
        target_agent_id=allocated_target,
    )
    assert first.get_draft_agent(draft_id)["target_agent_id"] == allocated_target  # type: ignore[index]

    default_a = InMemoryDraftStore()
    default_b = InMemoryDraftStore()
    for store in (default_a, default_b):
        store.create_draft_agent(
            draft_id=draft_id,
            user_id=OWNER,
            agent_name="Default target",
            agent_slug="default_target_800000000000",
            description="Test-double stable default proof.",
        )
    target_a = default_a.get_draft_agent(draft_id)["target_agent_id"]  # type: ignore[index]
    target_b = default_b.get_draft_agent(draft_id)["target_agent_id"]  # type: ignore[index]
    assert target_a == target_b
    assert uuid.UUID(str(target_a)).version == 4

    for invalid_target in (
        "NOT-A-UUID",
        "AAAAAAAA-0000-4000-8000-000000000074",
        uuid.UUID(allocated_target),
    ):
        with pytest.raises(ValueError, match="canonical UUID"):
            InMemoryDraftStore().create_draft_agent(
                draft_id=str(uuid.uuid4()),
                user_id=OWNER,
                agent_name="Invalid target",
                agent_slug="invalid_target_double",
                description="Test-double malformed target denial.",
                target_agent_id=invalid_target,  # type: ignore[arg-type]
            )
    with pytest.raises(ValueError, match="must match revises_agent_id"):
        InMemoryDraftStore().create_draft_agent(
            draft_id=str(uuid.uuid4()),
            user_id=OWNER,
            agent_name="Mismatched target",
            agent_slug="mismatched_target_double",
            description="Test-double revision target mismatch denial.",
            revises_agent_id="existing-agent",
            target_agent_id=allocated_target,
        )


def test_create_passes_initial_plan_constitution_and_allocates_target(boundary) -> None:
    store, _runtime, _work, drafts, *_rest = boundary
    plan_json = (
        '{"declared_egress":[],"declared_scopes":["records:read"],'
        '"tasks":["persist atomically"],"tool_scopes":{"search":"records:read"},'
        '"tools":[{"name":"search"}]}'
    )
    tools_spec = '[{"description":"","name":"search","scope":"records:read"}]'

    def created(_transaction: object, **values: object) -> DraftAgentRecord:
        return _draft(
            target_agent_id=str(values["target_agent_id"]),
            tools_spec=str(values["tools_spec"]),
            revises_agent_id=values["revises_agent_id"],
            plan_json=str(values["plan_json"]),
            constitution_version=str(values["constitution_version"]),
        )

    drafts.create_draft.side_effect = created
    created_row = store.create_draft_agent(
        DRAFT_ID,
        OWNER,
        "Plane Draft",
        "plane_draft_100000000000",
        "A bounded owner-scoped draft used by the adapter tests.",
        tools_spec=tools_spec,
        plan_json=plan_json,
        constitution_version="0.1.0",
    )

    assert drafts.create_draft.call_count == 1
    values = drafts.create_draft.call_args.kwargs
    assert values["plan_json"] == plan_json
    assert values["constitution_version"] == "0.1.0"
    assert values["tools_spec"] == tools_spec
    assert values["revises_agent_id"] is None
    assert uuid.UUID(values["target_agent_id"]).version == 4
    assert created_row["target_agent_id"] == values["target_agent_id"]
    assert created_row["state_revision"] == 0
    assert created_row["tools_spec"] == tools_spec
    assert created_row["revises_agent_id"] is None
    assert created_row["plan_json"] == plan_json
    assert created_row["constitution_version"] == "0.1.0"


def test_creation_test_doubles_persist_and_bound_initial_provenance() -> None:
    plan_json = '{"tasks":["double parity"]}'
    in_memory = InMemoryDraftStore()
    in_memory.create_draft_agent(
        draft_id=DRAFT_ID,
        user_id=OWNER,
        agent_name="Initial provenance",
        agent_slug="initial_provenance_100000000000",
        description="Test-double initial provenance proof.",
        plan_json=plan_json,
        constitution_version="0.1.0",
    )
    row = in_memory.get_draft_agent(DRAFT_ID)
    assert row is not None
    assert row["plan_json"] == plan_json
    assert row["constitution_version"] == "0.1.0"

    database = InMemoryDraftStore()
    compatibility = DatabaseDraftStoreDouble(database)
    compatibility_id = "60000000-0000-4000-8000-000000000074"
    compatibility.create_draft_agent(
        draft_id=compatibility_id,
        user_id=OWNER,
        agent_name="Compatibility provenance",
        agent_slug="compatibility_provenance_600000000000",
        description="Compatibility-double initial provenance proof.",
        plan_json=plan_json,
        constitution_version="0.1.0",
    )
    compatibility_row = compatibility.get_draft_agent(compatibility_id)
    assert compatibility_row is not None
    assert compatibility_row["plan_json"] == plan_json
    assert compatibility_row["constitution_version"] == "0.1.0"

    stores = (InMemoryDraftStore(), DatabaseDraftStoreDouble(InMemoryDraftStore()))
    for store in stores:
        for field, value in (
            ("plan_json", object()),
            ("plan_json", "x" * 1_000_001),
            ("constitution_version", object()),
            ("constitution_version", "x" * 129),
        ):
            with pytest.raises(ValueError, match=str(field)):
                store.create_draft_agent(
                    draft_id=str(uuid.uuid4()),
                    user_id=OWNER,
                    agent_name="Invalid provenance",
                    agent_slug="invalid_provenance_double",
                    description="Test-double malformed provenance denial.",
                    **{field: value},
                )


def test_owner_and_administration_reads_use_typed_repositories(boundary) -> None:
    store, _runtime, _work, drafts, *_rest = boundary
    drafts.get_draft.return_value = _draft()
    drafts.get_draft_for_administration.side_effect = [_draft(), None]
    drafts.get_draft_by_slug_for_administration.return_value = _draft()
    drafts.find_gap_draft.return_value = _draft()
    drafts.list_drafts.side_effect = [
        (_draft(),),
        (_draft(),),
        (
            _draft(status="live", updated_at=8),
            _draft(status="rejected", updated_at=9),
        ),
    ]
    drafts.list_pending_review_for_administration.return_value = (_draft(),)
    drafts.list_drafts_for_administration.side_effect = [
        (_draft(),),
        (
            _draft(status="live", origin="manual"),
            _draft(status="live", origin="byo_client"),
            _draft(status="pending", origin="manual"),
        ),
    ]

    assert store.get_owned_draft_agent(OWNER, DRAFT_ID)["user_id"] == OWNER
    assert store.get_draft_agent(DRAFT_ID)["id"] == DRAFT_ID
    assert store.get_draft_agent(DRAFT_ID) is None
    assert store.get_draft_agent_by_slug("plane_draft_100000000000")["id"] == DRAFT_ID
    assert store.find_gap_draft(OWNER, "chat-1", "gap-1")["id"] == DRAFT_ID
    assert len(store.get_user_draft_agents(OWNER)) == 1
    assert len(store.list_byo_sessions(OWNER, origin="byo_client")) == 1
    assert [row["status"] for row in store.get_decidable_drafts(OWNER)] == ["rejected"]
    assert len(store.get_pending_review_drafts()) == 1
    assert len(store.list_draft_agents()) == 1
    assert [row["origin"] for row in store.list_relaunchable_drafts()] == ["manual"]

    assert drafts.get_draft.call_args.kwargs == {
        "owner_id": OWNER,
        "draft_id": DRAFT_ID,
    }
    assert drafts.list_drafts.call_args_list[1].kwargs["origin"] == "byo_client"
    assert drafts.list_drafts.call_args_list[2].kwargs["include_terminal"] is True


def test_update_and_generation_log_are_locked_revision_cas(boundary) -> None:
    store, _runtime, _work, drafts, *_rest = boundary
    drafts.get_draft_for_administration.return_value = _draft(
        state_revision=4,
        generation_log="not-json",
    )
    drafts.compare_and_set_draft.return_value = _draft(state_revision=5)

    assert store.update_draft_agent(DRAFT_ID, phase="plan") is True
    update = drafts.compare_and_set_draft.call_args
    assert update.kwargs["expected_revision"] == 4
    assert update.kwargs["updates"] == {"phase": "plan"}

    assert store.append_generation_log(DRAFT_ID, "generated") is True
    log = drafts.compare_and_set_draft.call_args.kwargs["updates"]["generation_log"]
    assert "generated" in log

    drafts.get_draft_for_administration.return_value = None
    assert store.update_draft_agent(DRAFT_ID, phase="tasks") is False
    assert store.append_generation_log(DRAFT_ID, "missing") is False
    with pytest.raises(ValueError, match="must not be empty"):
        store.update_draft_agent(DRAFT_ID)


def test_generation_log_preserves_the_active_claim_revision(boundary) -> None:
    store, _runtime, _work, drafts, *_rest = boundary
    drafts.get_draft_for_administration.return_value = _draft(
        state_revision=7,
        generation_claim_id=CLAIM_ID,
        generation_log="[]",
    )
    drafts.replace_generation_log_for_claim.return_value = _draft(
        state_revision=7,
        generation_claim_id=CLAIM_ID,
    )

    assert store.append_generation_log(DRAFT_ID, "still generating") is True

    replace_log = drafts.replace_generation_log_for_claim.call_args
    assert replace_log.kwargs["expected_revision"] == 7
    assert replace_log.kwargs["claim_id"] == CLAIM_ID
    assert "still generating" in replace_log.kwargs["generation_log"]
    drafts.compare_and_set_draft.assert_not_called()


def test_generation_claim_and_finish_map_conflicts_to_none(boundary) -> None:
    store, _runtime, _work, drafts, *_rest = boundary
    drafts.claim_generation.return_value = _draft(
        status="generating",
        state_revision=1,
        generation_claim_id=CLAIM_ID,
    )
    drafts.renew_generation_claim.return_value = _draft(
        status="generating",
        state_revision=1,
        generation_claim_id=CLAIM_ID,
    )
    drafts.finish_generation.return_value = _draft(
        status="generated",
        state_revision=2,
    )
    assert (
        store.claim_draft_generation(
            draft_id=DRAFT_ID,
            owner_user_id=OWNER,
            expected_revision=0,
            claim_id=CLAIM_ID,
        )["status"]
        == "generating"
    )
    renewed = store.renew_draft_generation(
        draft_id=DRAFT_ID,
        owner_user_id=OWNER,
        expected_revision=1,
        claim_id=CLAIM_ID,
        lease_seconds=420,
    )
    assert renewed is not None
    assert renewed["state_revision"] == 1
    renewal = drafts.renew_generation_claim.call_args
    assert renewal.kwargs["lease_seconds"] == 420
    assert (
        store.finish_draft_generation(
            draft_id=DRAFT_ID,
            owner_user_id=OWNER,
            expected_revision=1,
            claim_id=CLAIM_ID,
            status="generated",
        )["status"]
        == "generated"
    )

    drafts.claim_generation.side_effect = RepositoryConflictError("stale")
    drafts.renew_generation_claim.side_effect = RepositoryConflictError("stale")
    drafts.finish_generation.side_effect = RepositoryConflictError("stale")
    assert (
        store.claim_draft_generation(
            draft_id=DRAFT_ID,
            owner_user_id=OWNER,
            expected_revision=0,
            claim_id=CLAIM_ID,
        )
        is None
    )
    assert (
        store.renew_draft_generation(
            draft_id=DRAFT_ID,
            owner_user_id=OWNER,
            expected_revision=1,
            claim_id=CLAIM_ID,
        )
        is None
    )
    assert (
        store.finish_draft_generation(
            draft_id=DRAFT_ID,
            owner_user_id=OWNER,
            expected_revision=1,
            claim_id=CLAIM_ID,
            status="error",
        )
        is None
    )


def test_unfenced_compare_and_set_uses_runtime_transaction(boundary) -> None:
    store, runtime, _work, drafts, *_rest = boundary
    drafts.get_draft.return_value = _draft(state_revision=2)
    drafts.compare_and_set_draft.return_value = _draft(
        state_revision=3,
        phase="plan",
    )
    outcome, revision, updated = store.compare_and_set_with_transition(
        draft_id=DRAFT_ID,
        owner_user_id=OWNER,
        expected_revision=2,
        updates={"phase": "plan"},
        transition_kind="advance",
        transition_id=None,
        operation_fence=None,
    )
    assert (outcome, revision, updated["phase"]) == ("applied", 3, "plan")
    transaction = runtime.transactions[-1]
    assert drafts.get_draft.call_args.args[0] is transaction
    assert drafts.compare_and_set_draft.call_args.args[0] is transaction
    drafts.record_transition.assert_not_called()


def test_fenced_compare_and_set_reuses_exact_admission_transaction(boundary) -> None:
    store, _runtime, work, drafts, *_rest = boundary
    fence = SimpleNamespace(
        operation_id=uuid.UUID(OPERATION_ID),
        execution_generation=7,
    )
    drafts.get_draft.return_value = _draft(state_revision=0)
    drafts.get_transition.return_value = None
    drafts.compare_and_set_draft.return_value = _draft(
        state_revision=1,
        phase="clarify",
    )
    outcome, revision, _updated = store.compare_and_set_with_transition(
        draft_id=DRAFT_ID,
        owner_user_id=OWNER,
        expected_revision=0,
        updates={"phase": "clarify"},
        transition_kind="advance",
        transition_id=TRANSITION_ID,
        operation_fence=fence,
    )
    assert (outcome, revision) == ("applied", 1)
    assert work.transactions[-1][0] is fence
    transaction = work.transactions[-1][1]
    assert drafts.get_draft.call_args.args[0] is transaction
    assert drafts.get_transition.call_args.args[0] is transaction
    assert drafts.compare_and_set_draft.call_args.args[0] is transaction
    assert drafts.record_transition.call_args.args[0] is transaction
    assert drafts.record_transition.call_args.kwargs["result_revision"] == 1


def test_fenced_conflict_and_replay_are_durable(boundary) -> None:
    store, _runtime, _work, drafts, *_rest = boundary
    fence = SimpleNamespace(
        operation_id=uuid.UUID(OPERATION_ID),
        execution_generation=7,
    )
    drafts.get_draft.return_value = _draft(state_revision=5)
    drafts.get_transition.return_value = None
    outcome, revision, _current = store.compare_and_set_with_transition(
        draft_id=DRAFT_ID,
        owner_user_id=OWNER,
        expected_revision=4,
        updates={"phase": "plan"},
        transition_kind="advance",
        transition_id=TRANSITION_ID,
        operation_fence=fence,
    )
    assert (outcome, revision) == ("conflict", 5)
    assert drafts.record_transition.call_args.kwargs["safe_code"] == "stale_revision"

    drafts.reset_mock()
    drafts.get_draft.return_value = _draft(state_revision=8)
    drafts.get_transition.return_value = DraftTransitionRecord(
        transition_id=TRANSITION_ID,
        draft_uuid=DRAFT_ID,
        owner_id=OWNER,
        operation_id=OPERATION_ID,
        operation_execution_generation=7,
        transition_kind="advance",
        expected_revision=4,
        result_revision=5,
        outcome="applied",
        safe_code=None,
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    outcome, revision, _current = store.compare_and_set_with_transition(
        draft_id=DRAFT_ID,
        owner_user_id=OWNER,
        expected_revision=4,
        updates={"phase": "plan"},
        transition_kind="advance",
        transition_id=TRANSITION_ID,
        operation_fence=fence,
    )
    assert (outcome, revision) == ("replayed", 5)
    drafts.compare_and_set_draft.assert_not_called()

    drafts.get_transition.return_value = replace(
        drafts.get_transition.return_value,
        transition_kind="save",
    )
    assert (
        store.compare_and_set_with_transition(
            draft_id=DRAFT_ID,
            owner_user_id=OWNER,
            expected_revision=4,
            updates={"phase": "plan"},
            transition_kind="advance",
            transition_id=TRANSITION_ID,
            operation_fence=fence,
        )[0]
        == "conflict"
    )


def test_exact_transition_lookup_uses_normal_transaction_across_observer_fences(
    boundary,
) -> None:
    store, runtime, work, drafts, *_rest = boundary
    fence_a = SimpleNamespace(
        operation_id=uuid.UUID(OPERATION_ID),
        execution_generation=7,
    )
    fence_b = SimpleNamespace(
        operation_id=uuid.uuid4(),
        execution_generation=1,
    )
    written: dict[str, DraftTransitionRecord] = {}

    def record_transition(_transaction: object, **values: object) -> None:
        written["record"] = DraftTransitionRecord(
            transition_id=str(values["transition_id"]),
            draft_uuid=str(values["draft_uuid"]),
            owner_id=str(values["owner_id"]),
            operation_id=str(values["operation_id"]),
            operation_execution_generation=int(
                values["operation_execution_generation"]
            ),
            transition_kind=str(values["transition_kind"]),
            expected_revision=int(values["expected_revision"]),
            result_revision=int(values["result_revision"]),
            outcome=str(values["outcome"]),
            safe_code=(
                None if values.get("safe_code") is None else str(values["safe_code"])
            ),
            created_at=datetime(2026, 8, 14, tzinfo=UTC),
        )

    drafts.get_draft.return_value = _draft(state_revision=4)
    drafts.get_transition.return_value = None
    drafts.compare_and_set_draft.return_value = _draft(
        state_revision=5,
        tools_spec="[]",
    )
    drafts.record_transition.side_effect = record_transition
    assert store.compare_and_set_with_transition(
        draft_id=DRAFT_ID,
        owner_user_id=OWNER,
        expected_revision=4,
        updates={"tools_spec": "[]"},
        transition_kind="claim_generation",
        transition_id=TRANSITION_ID,
        operation_fence=fence_a,
    )[:2] == ("applied", 5)

    drafts.get_draft.return_value = _draft(state_revision=8, tools_spec="[]")
    drafts.get_transition.return_value = written["record"]
    assert (
        store.compare_and_set_with_transition(
            draft_id=DRAFT_ID,
            owner_user_id=OWNER,
            expected_revision=4,
            updates={"tools_spec": "[]"},
            transition_kind="claim_generation",
            transition_id=TRANSITION_ID,
            operation_fence=fence_b,
        )[0]
        == "conflict"
    )

    fenced_transaction_count = len(work.transactions)
    normal_transaction_count = len(runtime.transactions)
    assert store.get_exact_draft_transition(
        draft_id=DRAFT_ID,
        owner_user_id=OWNER,
        transition_id=TRANSITION_ID,
        transition_kind="claim_generation",
        expected_revision=4,
    ) == (5, "applied")
    assert len(work.transactions) == fenced_transaction_count
    assert len(runtime.transactions) == normal_transaction_count + 1
    transaction = runtime.transactions[-1]
    assert drafts.get_draft.call_args.args[0] is transaction
    assert drafts.get_transition.call_args.args[0] is transaction


def test_exact_transition_lookup_fails_closed_on_identity_or_result_drift(
    boundary,
) -> None:
    store, _runtime, _work, drafts, *_rest = boundary
    current = _draft(state_revision=8)
    exact = DraftTransitionRecord(
        transition_id=TRANSITION_ID,
        draft_uuid=DRAFT_ID,
        owner_id=OWNER,
        operation_id=OPERATION_ID,
        operation_execution_generation=7,
        transition_kind="claim_generation",
        expected_revision=4,
        result_revision=5,
        outcome="applied",
        safe_code=None,
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    drafts.get_draft.return_value = current
    drafts.get_transition.return_value = exact

    def lookup(**changes: object) -> tuple[int, str] | None:
        values = {
            "draft_id": DRAFT_ID,
            "owner_user_id": OWNER,
            "transition_id": TRANSITION_ID,
            "transition_kind": "claim_generation",
            "expected_revision": 4,
            **changes,
        }
        return store.get_exact_draft_transition(**values)

    assert lookup() == (5, "applied")
    for drifted in (
        replace(exact, transition_id=str(uuid.uuid4())),
        replace(exact, owner_id="another-owner"),
        replace(exact, draft_uuid=str(uuid.uuid4())),
        replace(exact, transition_kind="advance"),
        replace(exact, expected_revision=3),
        replace(exact, result_revision=4),
        replace(exact, result_revision=9),
        replace(exact, outcome="unknown"),
    ):
        drafts.get_transition.return_value = drifted
        assert lookup() is None
    drafts.get_transition.return_value = exact
    drafts.get_draft.return_value = None
    assert lookup() is None
    drafts.get_draft.return_value = current
    drafts.get_transition.return_value = None
    assert lookup() is None
    assert lookup(transition_id="not-a-uuid") is None


def test_in_memory_exact_transition_lookup_has_production_identity_semantics() -> None:
    store = InMemoryDraftStore()
    store.create_draft_agent(
        draft_id=DRAFT_ID,
        user_id=OWNER,
        agent_name="Transition lookup",
        agent_slug="transition_lookup_100000000000",
        description="Test-double exact transition lookup proof.",
    )
    assert store.compare_and_set_with_transition(
        draft_id=DRAFT_ID,
        owner_user_id=OWNER,
        expected_revision=0,
        updates={"phase": "generate"},
        transition_kind="claim_generation",
        transition_id=TRANSITION_ID,
        operation_fence=SimpleNamespace(
            operation_id=uuid.UUID(OPERATION_ID),
            execution_generation=7,
        ),
    )[:2] == ("applied", 1)

    assert store.get_exact_draft_transition(
        draft_id=DRAFT_ID,
        owner_user_id=OWNER,
        transition_id=TRANSITION_ID,
        transition_kind="claim_generation",
        expected_revision=0,
    ) == (1, "applied")
    assert (
        store.get_exact_draft_transition(
            draft_id=DRAFT_ID,
            owner_user_id="another-owner",
            transition_id=TRANSITION_ID,
            transition_kind="claim_generation",
            expected_revision=0,
        )
        is None
    )
    assert (
        store.get_exact_draft_transition(
            draft_id=DRAFT_ID,
            owner_user_id=OWNER,
            transition_id=TRANSITION_ID,
            transition_kind="advance",
            expected_revision=0,
        )
        is None
    )
    assert (
        store.get_exact_draft_transition(
            draft_id=DRAFT_ID,
            owner_user_id=OWNER,
            transition_id=TRANSITION_ID,
            transition_kind="claim_generation",
            expected_revision=1,
        )
        is None
    )
    store._transitions[TRANSITION_ID]["result_revision"] = 2
    assert (
        store.get_exact_draft_transition(
            draft_id=DRAFT_ID,
            owner_user_id=OWNER,
            transition_id=TRANSITION_ID,
            transition_kind="claim_generation",
            expected_revision=0,
        )
        is None
    )


def test_database_double_exposes_the_same_exact_transition_lookup() -> None:
    database = InMemoryDraftStore()
    database.create_draft_agent(
        draft_id=DRAFT_ID,
        user_id=OWNER,
        agent_name="Legacy transition lookup",
        agent_slug="legacy_transition_lookup_100000000000",
        description="Compatibility-double exact transition lookup proof.",
    )
    store = DatabaseDraftStoreDouble(database)
    assert store.compare_and_set_with_transition(
        draft_id=DRAFT_ID,
        owner_user_id=OWNER,
        expected_revision=0,
        updates={"phase": "generate"},
        transition_kind="claim_generation",
        transition_id=TRANSITION_ID,
        operation_fence=SimpleNamespace(
            operation_id=uuid.UUID(OPERATION_ID),
            execution_generation=7,
        ),
    )[:2] == ("applied", 1)
    assert store.get_exact_draft_transition(
        draft_id=DRAFT_ID,
        owner_user_id=OWNER,
        transition_id=TRANSITION_ID,
        transition_kind="claim_generation",
        expected_revision=0,
    ) == (1, "applied")


def test_compare_and_set_rejects_unverifiable_fence_and_bad_rows(boundary) -> None:
    store, _runtime, _work, drafts, identity, agents, tool_policy = boundary
    unfenced = PlaneDraftStore(
        plane_runtime=Runtime(),
        plane_repositories=SimpleNamespace(
            draft_agents=drafts,
            identity=identity,
            agents=agents,
            tool_policy_state=tool_policy,
        ),
    )
    fence = SimpleNamespace(
        operation_id=uuid.UUID(OPERATION_ID),
        execution_generation=1,
    )
    with pytest.raises(RuntimeError, match="fence authority"):
        unfenced.compare_and_set_with_transition(
            draft_id=DRAFT_ID,
            owner_user_id=OWNER,
            expected_revision=0,
            updates={"phase": "plan"},
            transition_kind="advance",
            transition_id=TRANSITION_ID,
            operation_fence=fence,
        )

    drafts.get_draft.return_value = None
    with pytest.raises(LookupError, match="unavailable"):
        store.compare_and_set_with_transition(
            draft_id=DRAFT_ID,
            owner_user_id=OWNER,
            expected_revision=0,
            updates={"phase": "plan"},
            transition_kind="advance",
            transition_id=None,
            operation_fence=None,
        )
    drafts.get_draft.return_value = _draft(draft_uuid=None)
    with pytest.raises(RuntimeError, match="UUID alias"):
        store.compare_and_set_with_transition(
            draft_id=DRAFT_ID,
            owner_user_id=OWNER,
            expected_revision=0,
            updates={"phase": "plan"},
            transition_kind="advance",
            transition_id=None,
            operation_fence=None,
        )


def test_delete_identity_ownership_and_policy_retirement(boundary) -> None:
    store, _runtime, _work, drafts, identity, agents, tool_policy = boundary
    drafts.get_draft_for_administration.side_effect = [_draft(), None]
    drafts.delete_draft.return_value = True
    assert store.delete_draft_agent(DRAFT_ID) is True
    assert store.delete_draft_agent(DRAFT_ID) is False

    identity.get_identity.return_value = SimpleNamespace(
        owner_id=OWNER,
        email="owner@example.test",
        username="owner",
        display_name="Owner",
        roles=("user",),
        last_login_at=1,
        created_at=1,
        updated_at=2,
    )
    assert store.get_user(OWNER)["email"] == "owner@example.test"
    store.set_agent_ownership("agent-1", "owner@example.test")
    assert agents.upsert_ownership.call_args.kwargs["is_public"] is False
    ownership = SimpleNamespace(
        agent_id="agent-1",
        owner_email="owner@example.test",
        is_public=False,
        created_at=1,
        updated_at=2,
    )
    agents.get_ownership.return_value = ownership
    assert store.get_agent_ownership("agent-1")["owner_email"] == "owner@example.test"
    assert store.set_agent_visibility("agent-1", True) is True
    assert agents.set_visibility.call_args.kwargs["owner_email"] == "owner@example.test"
    agents.get_ownership.return_value = None
    assert store.get_agent_ownership("missing") is None
    assert store.set_agent_visibility("missing", True) is False

    trust = SimpleNamespace(is_safe=True)
    agents.get_trust.return_value = trust
    assert store.get_agent_is_safe("agent-1") is True
    assert store.upsert_agent_safe("agent-1", False, marked_by=OWNER) is True
    assert store.reset_agent_safe("agent-1", marked_by=OWNER) is True
    assert agents.set_trust.call_args.kwargs["reset_for_revision"] is True
    agents.get_trust.return_value = None
    assert store.get_agent_is_safe("missing") is False

    tool_policy.remove_agent_state.return_value = 3
    agents.remove_ownership.return_value = True
    assert store.purge_agent_state(owner_user_id=OWNER, agent_id="agent-1") == 4
    identity.get_identity.return_value = None
    assert store.get_user(OWNER) is None
    assert store.purge_agent_state(owner_user_id=OWNER, agent_id="agent-1") == 3

    tool_policy.list_scoped_agent_owners_for_administration.return_value = (
        SimpleNamespace(owner_id=OWNER, agent_id="agent-1"),
    )
    assert store.list_scoped_agent_owners_for_administration() == ((OWNER, "agent-1"),)
