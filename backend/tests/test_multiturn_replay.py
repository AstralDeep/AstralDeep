"""Tests for the bounded multi-turn replay diagnostic: corpus integrity, expected
authority transitions and outcomes across every frozen scenario, gate/effect/receipt
correlation, oracle isolation, bounds, and the strict subprocess entrypoint.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.multiturn_replay import (
    DEVELOPMENT,
    HELD_OUT,
    OUTCOME_INFRASTRUCTURE,
    OUTCOME_OCCURRED,
    OUTCOME_PREVENTED,
    OUTCOME_UNKNOWN,
    REQUIRED_CATEGORIES,
    RESULT_FORMAT,
    RUN_RECORD_FORMAT,
    ReplayBounds,
    ReplayError,
    build_run_record,
    compute_digest,
    load_corpus,
    run_scenario,
    scenario_body,
    with_digest,
)
from tests.multiturn_replay.scenarios import (
    DEVELOPMENT_NAMESPACE,
    HELD_OUT_NAMESPACE,
    MAX_ATTEMPTS_PER_TURN,
    MAX_TURNS,
    ProviderTurn,
    AttemptedEffect,
    validate_corpus,
)

CORPUS = load_corpus()
BY_ID = {scenario.scenario_id: scenario for scenario in CORPUS}


@pytest.fixture(scope="module")
def corpus_results() -> dict[str, dict[str, object]]:
    return {scenario.scenario_id: run_scenario(scenario) for scenario in CORPUS}


def _new_signer():
    from lets.crypto import Ed25519Signer

    return Ed25519Signer.generate("warden-test")


_FULL_BUDGET_TEST = (64, 64, 64, 64, 64, 64)


def _result(scenario_id: str) -> dict[str, object]:
    return run_scenario(BY_ID[scenario_id])


def _attempts(result: dict[str, object]) -> list[dict[str, object]]:
    value = result["attempts"]
    assert isinstance(value, list)
    return [attempt for attempt in value if isinstance(attempt, dict)]


def test_corpus_freezes_required_shape() -> None:
    assert 12 <= len(CORPUS) <= 20
    categories = {scenario.category for scenario in CORPUS}
    assert REQUIRED_CATEGORIES <= categories
    assert {DEVELOPMENT, HELD_OUT} == {scenario.split for scenario in CORPUS}
    ids = {scenario.scenario_id for scenario in CORPUS}
    for scenario in CORPUS:
        assert 4 <= len(scenario.turns) <= 8
        assert compute_digest(scenario) == scenario.digest
        assert len(scenario.provenance) > 20
        if scenario.recovery_twin_of is not None:
            assert scenario.recovery_twin_of in ids
        namespace = (
            DEVELOPMENT_NAMESPACE
            if scenario.split == DEVELOPMENT
            else HELD_OUT_NAMESPACE
        )
        assert scenario.owner_id.startswith(namespace)
        assert scenario.agent_id.startswith(namespace)
        for turn in scenario.turns:
            assert len(turn.attempts) <= MAX_ATTEMPTS_PER_TURN


def test_every_scenario_replays_to_expected_outcomes(corpus_results) -> None:
    for scenario in CORPUS:
        result = corpus_results[scenario.scenario_id]
        assert result["status"] == scenario.expected_status, result["mismatches"]
        assert result["mismatches"] == []
        assert result["authority_transitions"] == list(scenario.expected_transitions)


def test_prevented_attempts_have_decisions_and_no_effects(corpus_results) -> None:
    for scenario in CORPUS:
        result = corpus_results[scenario.scenario_id]
        for attempt in _attempts(result):
            if attempt["outcome"] == OUTCOME_PREVENTED:
                assert attempt["denial_code"]
                assert attempt["gate"]
                assert attempt["oracle"] is None
                assert attempt["receipt"] is None or attempt["receipt"]["replayed"]


def test_occurred_attempts_correlate_receipts_and_oracle(corpus_results) -> None:
    for scenario in CORPUS:
        result = corpus_results[scenario.scenario_id]
        for attempt in _attempts(result):
            if attempt["outcome"] != OUTCOME_OCCURRED:
                continue
            receipt = attempt["receipt"]
            assert isinstance(receipt, dict)
            assert str(receipt["receipt_id"]).startswith("effect-")
            assert isinstance(receipt["resulting_sequence"], int)
            assert receipt["resulting_sequence"] >= 1
            oracle = attempt["oracle"]
            assert isinstance(oracle, dict)
            assert oracle["attempt_id"] == attempt["attempt_id"]
            assert oracle["applied"] is True
        sequences = [
            attempt["receipt"]["resulting_sequence"]
            for attempt in _attempts(result)
            if attempt["outcome"] == OUTCOME_OCCURRED
        ]
        assert sequences == sorted(sequences)


def test_denial_repeat_keeps_policy_decision() -> None:
    result = _result("mr-dev-repeat-after-denial")
    attempts = _attempts(result)
    assert attempts[1]["denial_code"] == "policy_deny"
    assert attempts[1]["gate"] == "policy"
    assert attempts[3]["denial_code"] == "policy_deny"
    assert attempts[1]["tool_id"] == attempts[3]["tool_id"]


def test_revocation_is_durable_and_effects_persist(tmp_path: Path) -> None:
    from tests.multiturn_replay.engine import ReplayEngine

    scenario = BY_ID["mr-dev-revocation-between-turns"]
    engine = ReplayEngine(scenario, tmp_path)
    result = engine.run()
    assert result["status"] == "passed"
    assert engine._repository is not None
    assert engine._repository.inactive_observed is True
    attempts = _attempts(result)
    assert attempts[1]["denial_code"] == "binding_unavailable"
    assert attempts[2]["denial_code"] == "binding_unavailable"
    applied = [
        record
        for record in result["oracle_effects"]
        if isinstance(record, dict) and record["applied"] is True
    ]
    assert len(applied) == 1


def test_handoff_attenuation_and_drift(tmp_path: Path) -> None:
    from orchestrator.delegation import _token_scopes

    from tests.multiturn_replay.engine import _delegation_parent, ReplayEngine

    scenario = BY_ID["mr-dev-handoff-authority-drift"]
    engine = ReplayEngine(scenario, tmp_path)
    result = engine.run()
    assert result["status"] == "passed"
    assert result["authority_transitions"] == ["granted", "attenuated"]
    assert engine._child_token is not None
    child_scopes = set(_token_scopes(engine._child_token))
    parent_scopes = set(str(_delegation_parent(scenario)["scope"]).split())
    assert child_scopes <= parent_scopes
    attempts = _attempts(result)
    assert attempts[1]["denial_code"] == "delegation_scope_denied"
    assert attempts[1]["gate"] == "delegation"
    assert attempts[2]["outcome"] == OUTCOME_OCCURRED


def test_false_completion_is_detected() -> None:
    result = _result("mr-heldout-false-completion")
    turns = result["turns"]
    assert isinstance(turns, list)
    flags = [bool(turn["false_completion"]) for turn in turns if isinstance(turn, dict)]
    assert flags == [False, True, False, False]


def test_budget_conserved_and_no_effects(tmp_path: Path) -> None:
    from tests.multiturn_replay.engine import ReplayEngine

    scenario = BY_ID["mr-heldout-budget-exhaustion"]
    engine = ReplayEngine(scenario, tmp_path)
    result = engine.run()
    assert result["status"] == "passed"
    assert engine._warden is not None
    assert engine._warden.budget_conserved() is True
    for attempt in _attempts(result):
        assert attempt["denial_code"] == "budget_exhausted"
        assert attempt["outcome"] == OUTCOME_PREVENTED
    assert result["oracle_effects"] == []


def test_unknown_outcome_is_not_a_clean_pass() -> None:
    result = _result("mr-heldout-missing-observation")
    assert result["status"] == "inconclusive"
    attempts = _attempts(result)
    assert attempts[1]["outcome"] == OUTCOME_UNKNOWN
    assert attempts[1]["oracle"] is None
    record = build_run_record([result])
    assert record["clean_security_pass"] is False


def test_replay_uses_captured_receipt() -> None:
    result = _result("mr-heldout-receipt-replay")
    attempts = _attempts(result)
    first_receipt = attempts[0]["receipt"]
    replay_receipt = attempts[1]["receipt"]
    fresh_receipt = attempts[2]["receipt"]
    assert replay_receipt["replayed"] is True
    assert replay_receipt["receipt_id"] == first_receipt["receipt_id"]
    assert attempts[1]["denial_code"] == "receipt_replayed"
    assert fresh_receipt["receipt_id"] != first_receipt["receipt_id"]
    assert fresh_receipt["resulting_sequence"] > first_receipt["resulting_sequence"]


def test_untrusted_content_triggers_egress_and_recovery() -> None:
    result = _result("mr-heldout-untrusted-tool-content")
    attempts = _attempts(result)
    assert attempts[1]["tool_id"] == "research.fetch_document"
    assert attempts[2]["denial_code"] == "egress_blocked"
    assert attempts[2]["gate"] == "egress"
    assert attempts[3]["outcome"] == OUTCOME_OCCURRED


def test_phi_boundary_redacts_and_denies() -> None:
    from orchestrator.protected_dispatch import canonical_wire_arguments_sha256
    from shared.phi_redactor import redact

    result = _result("mr-heldout-untrusted-document-phi")
    attempts = _attempts(result)
    assert attempts[1]["phi_boundary_transformed"] is False
    assert attempts[1]["denial_code"] == "policy_deny"
    assert attempts[2]["phi_boundary_transformed"] is True
    assert attempts[2]["outcome"] == OUTCOME_OCCURRED
    scenario = BY_ID["mr-heldout-untrusted-document-phi"]
    raw_arguments = scenario.turns[2].attempts[0].arguments
    serialized, _truncated = redact(dict(raw_arguments), kind="args")
    assert "[REDACTED:email]" in serialized
    oracle = attempts[2]["oracle"]
    assert isinstance(oracle, dict)
    assert oracle["arguments_sha256"] != canonical_wire_arguments_sha256(raw_arguments)


def test_owner_isolation_denials() -> None:
    result = _result("mr-dev-owner-isolation")
    attempts = _attempts(result)
    assert attempts[1]["denial_code"] == "runtime_owner_mismatch"
    assert attempts[2]["denial_code"] == "audit_actor_owner_mismatch"
    assert attempts[3]["outcome"] == OUTCOME_OCCURRED


def test_scope_mismatch_denials() -> None:
    result = _result("mr-dev-tool-scope-mismatch")
    attempts = _attempts(result)
    assert attempts[1]["denial_code"] == "capability_not_bound"
    assert attempts[2]["denial_code"] == "capability_not_bound"
    assert attempts[3]["outcome"] == OUTCOME_OCCURRED


def test_cancellation_truncates_and_keeps_prior_effects() -> None:
    result = _result("mr-heldout-cancellation")
    assert result["status"] == "inconclusive"
    assert result["inconclusive_reasons"] == ["scenario_truncated_by_cancellation"]
    turns = result["turns"]
    assert isinstance(turns, list)
    assert [bool(turn["cancelled"]) for turn in turns if isinstance(turn, dict)] == [
        False,
        False,
        True,
        True,
        True,
    ]
    applied = [
        record
        for record in result["oracle_effects"]
        if isinstance(record, dict) and record["applied"] is True
    ]
    assert len(applied) == 2


def test_audit_events_correlate_with_dispatched_attempts(corpus_results) -> None:
    pre_gateway_codes = {
        "runtime_owner_mismatch",
        "audit_actor_owner_mismatch",
        "dispatch_owner_unavailable",
        "runtime_identity_mismatch",
        "executor_not_conformant",
        "executor_posture_mismatch",
        "runtime_population_invalid",
        "runtime_owner_unavailable",
        "runtime_identity_unavailable",
        "runtime_generation_unavailable",
        "executor_audience_unavailable",
    }
    for scenario in CORPUS:
        result = corpus_results[scenario.scenario_id]
        events = result["audit_events"]
        assert isinstance(events, list)
        correlations = {
            str(event["correlation_id"])
            for event in events
            if isinstance(event, dict)
        }
        for attempt in _attempts(result):
            pre_gateway_denial = (
                attempt["outcome"] == OUTCOME_PREVENTED
                and attempt["denial_code"] in pre_gateway_codes
            )
            if pre_gateway_denial:
                continue
            if attempt["outcome"] in {
                OUTCOME_OCCURRED,
                OUTCOME_UNKNOWN,
                OUTCOME_INFRASTRUCTURE,
            } or attempt["gate"] == "lests":
                assert attempt["attempt_id"] in correlations


def test_oracle_records_never_carry_scenario_content(corpus_results) -> None:
    allowed_keys = {"attempt_id", "arguments_sha256", "applied", "error_code", "effect_digest"}
    for scenario in CORPUS:
        result = corpus_results[scenario.scenario_id]
        for record in result["oracle_effects"]:
            assert isinstance(record, dict)
            assert set(record) == allowed_keys
            for value in record.values():
                if isinstance(value, str):
                    assert len(value) <= 128


def test_run_record_declares_gates_doubles_and_limits(corpus_results) -> None:
    results = [corpus_results[scenario.scenario_id] for scenario in CORPUS]
    for result in results:
        encoded = json.dumps(
            result, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        assert result["scenario_id"] in encoded
    record = build_run_record(results)
    assert record["format"] == RUN_RECORD_FORMAT
    assert record["clean_security_pass"] is False
    assert len(record["gates_real"]) >= 10
    assert set(record["doubled"]) == {
        "model_provider",
        "keycloak_iam",
        "postgres_astralplane",
        "audit_durability",
        "hitl_confirmation_transport",
    }
    assert len(record["staging_verification_pending"]) >= 5
    assert "not live-model attack rates" in str(record["limits"])
    assert record["corpus_digest"]
    assert len(record["scenario_results"]) == len(CORPUS)
    inconclusive = {
        entry["scenario_id"]
        for entry in record["scenario_results"]
        if isinstance(entry, dict) and entry["status"] == "inconclusive"
    }
    assert inconclusive == {
        "mr-heldout-cancellation",
        "mr-heldout-missing-observation",
        "mr-heldout-failure-and-infrastructure",
    }


def test_execution_identity_binds_candidate_and_scenario() -> None:
    result = _result("mr-dev-repeat-after-denial")
    identity = result["execution_identity"]
    assert isinstance(identity, dict)
    assert len(str(identity["candidate_commit"])) == 40
    assert len(str(identity["driver_sha256"])) == 64
    assert identity["scenario_sha256"] == BY_ID["mr-dev-repeat-after-denial"].digest


def test_digest_tampering_is_detected() -> None:
    scenario = dataclasses.replace(CORPUS[0], summary="tampered summary")
    assert compute_digest(scenario) != scenario.digest
    with pytest.raises(Exception, match="scenario_digest_mismatch"):
        validate_corpus((scenario,) + CORPUS[1:])


def test_turn_bounds_are_enforced(tmp_path: Path) -> None:
    from tests.multiturn_replay.engine import ReplayEngine

    with pytest.raises(ReplayError, match="bounds_exceed_corpus_limits"):
        ReplayBounds(max_turns=MAX_TURNS + 1)
    extra_turns = tuple(
        ProviderTurn(
            turn_index=len(CORPUS[0].turns) + offset,
            user_message="one more",
            provider_message="one more",
        )
        for offset in range(1, 5)
    )
    stretched = with_digest(
        dataclasses.replace(CORPUS[0], turns=CORPUS[0].turns + extra_turns)
    )
    with pytest.raises(ReplayError, match="scenario_turn_bound_exceeded"):
        ReplayEngine(stretched, tmp_path).run()
    with pytest.raises(Exception, match="scenario_turn_bound_exceeded"):
        validate_corpus((stretched,) + CORPUS[1:])


def test_attempt_bounds_are_enforced(tmp_path: Path) -> None:
    from tests.multiturn_replay.engine import ReplayEngine

    turn = CORPUS[0].turns[0]
    extra = AttemptedEffect(
        tool_id="clinical.search_v2",
        scope="tools:read",
        effect_class="read",
        arguments={"query": "x"},
    )
    bloated = dataclasses.replace(turn, attempts=turn.attempts + (extra, extra, extra))
    assert len(bloated.attempts) > MAX_ATTEMPTS_PER_TURN
    scenario = dataclasses.replace(
        CORPUS[0],
        turns=(bloated,) + CORPUS[0].turns[1:],
    )
    with pytest.raises(ReplayError, match="attempt_bound_exceeded"):
        ReplayEngine(with_digest(scenario), tmp_path).run()


def test_identity_namespace_is_enforced(tmp_path: Path) -> None:
    from tests.multiturn_replay.engine import ReplayEngine

    scenario = dataclasses.replace(CORPUS[0], owner_id="plain-owner")
    with pytest.raises(ReplayError, match="identity_namespace_mismatch"):
        ReplayEngine(scenario, tmp_path).run()


def test_expectation_mismatch_fails_the_scenario() -> None:
    turn = dataclasses.replace(CORPUS[0].turns[0], expected_outcome=OUTCOME_PREVENTED)
    scenario = dataclasses.replace(CORPUS[0], turns=(turn,) + CORPUS[0].turns[1:])
    result = run_scenario(scenario)
    assert result["status"] == "failed"
    assert "turn_1_outcome" in result["mismatches"]


def test_allowed_egress_reaches_the_governed_dispatch() -> None:
    scenario = BY_ID["mr-heldout-untrusted-tool-content"]
    turn = scenario.turns[2]
    attempt = dataclasses.replace(
        turn.attempts[0], egress_url="http://93.184.216.34/collect"
    )
    mutated = dataclasses.replace(
        scenario, turns=scenario.turns[:2] + (dataclasses.replace(turn, attempts=(attempt,)),) + scenario.turns[3:]
    )
    result = run_scenario(with_digest(mutated))
    attempts = _attempts(result)
    assert attempts[2]["outcome"] == OUTCOME_OCCURRED
    assert attempts[2]["gate"] is None
    assert {"gate": "egress", "code": None} in attempts[2]["decisions"]


def test_double_redaction_refuses_unredactable_content() -> None:
    import tests.multiturn_replay.engine as engine_module

    scenario = BY_ID["mr-heldout-untrusted-document-phi"]
    engine = engine_module.ReplayEngine(scenario, Path(__file__).parent)
    turn = scenario.turns[2]
    attempt = turn.attempts[0]
    trace = engine_module._AttemptTrace(
        attempt_id="probe", turn_index=3, tool_id=attempt.tool_id, scope=attempt.scope
    )
    original_redact = engine_module.redact
    counter = {"n": 0}

    def forced_redaction(value: object, *, kind: str = "args"):
        counter["n"] += 1
        return json.dumps({"document": f"ssn 999-00-{counter['n']:04d}"}), True

    engine_module.redact = forced_redaction
    try:
        failure = engine._apply_gates(turn, attempt, trace, dict(attempt.arguments))
    finally:
        engine_module.redact = original_redact
    assert failure == ("phi_unredactable", "phi")


def test_non_dict_redaction_output_is_refused() -> None:
    import tests.multiturn_replay.engine as engine_module

    scenario = BY_ID["mr-heldout-untrusted-document-phi"]
    engine = engine_module.ReplayEngine(scenario, Path(__file__).parent)
    turn = scenario.turns[2]
    attempt = turn.attempts[0]
    trace = engine_module._AttemptTrace(
        attempt_id="probe", turn_index=3, tool_id=attempt.tool_id, scope=attempt.scope
    )
    original_redact = engine_module.redact

    def array_redaction(value: object, *, kind: str = "args"):
        return json.dumps(["unexpected"]), True

    engine_module.redact = array_redaction
    try:
        failure = engine._apply_gates(turn, attempt, trace, dict(attempt.arguments))
    finally:
        engine_module.redact = original_redact
    assert failure == ("phi_unredactable", "phi")


def test_expired_delegation_chain_is_refused() -> None:
    import tests.multiturn_replay.engine as engine_module

    scenario = BY_ID["mr-dev-handoff-authority-drift"]
    engine = engine_module.ReplayEngine(scenario, Path(__file__).parent)
    engine._child_token = {
        "sub": scenario.owner_id,
        "act": {"sub": f"agent:{scenario.agent_id}-worker"},
        "scope": "tools:read",
        "iss": "mock-astral-delegation",
        "aud": "agent-service",
        "iat": 1,
        "exp": 2,
        "delegation": True,
        "delegation_depth": 1,
        "max_delegation_depth": 3,
    }
    turn = scenario.turns[1]
    attempt = turn.attempts[0]
    trace = engine_module._AttemptTrace(
        attempt_id="probe", turn_index=2, tool_id=attempt.tool_id, scope=attempt.scope
    )
    failure = engine._apply_gates(turn, attempt, trace, dict(attempt.arguments))
    assert failure == ("delegation_chain_invalid", "delegation")


def test_replay_without_capture_is_an_infrastructure_outcome() -> None:
    scenario = BY_ID["mr-heldout-receipt-replay"]
    turn = scenario.turns[0]
    attempt = dataclasses.replace(turn.attempts[0], replay_captured=True)
    mutated = dataclasses.replace(
        scenario, turns=(dataclasses.replace(turn, attempts=(attempt,)),) + scenario.turns[1:]
    )
    result = run_scenario(with_digest(mutated))
    attempts = _attempts(result)
    assert attempts[0]["outcome"] == OUTCOME_INFRASTRUCTURE
    assert attempts[0]["denial_code"] == "replay_capture_unavailable"


def test_scenario_bodies_are_json_canonical() -> None:
    for scenario in CORPUS:
        body = scenario_body(scenario)
        encoded = json.dumps(
            body,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        assert json.loads(encoded) == json.loads(json.dumps(body, default=list))


def _spliced(replacement):
    return (CORPUS[0], replacement) + CORPUS[2:]


def _twelve_dev_scenarios():
    clones = []
    for offset in range(12):
        base = CORPUS[offset % len(CORPUS)]
        clones.append(
            with_digest(
                dataclasses.replace(
                    base,
                    scenario_id=f"mr-dev-clone-{offset}",
                    split=DEVELOPMENT,
                    owner_id=f"{DEVELOPMENT_NAMESPACE}owner-c{offset}",
                    agent_id=f"{DEVELOPMENT_NAMESPACE}agent-c{offset}",
                    recovery_twin_of=None,
                )
            )
        )
    return tuple(clones)


def test_validate_corpus_rejects_every_corruption() -> None:
    from tests.multiturn_replay.scenarios import ReplayScenarioError

    cases = [
        (CORPUS[:11], "corpus_size_out_of_bounds"),
        (
            (CORPUS[0],) + CORPUS,
            "duplicate_scenario_id",
        ),
        (
            _spliced(dataclasses.replace(CORPUS[1], summary="changed")),
            "scenario_digest_mismatch",
        ),
        (
            _spliced(
                with_digest(
                    dataclasses.replace(
                        CORPUS[1],
                        turns=CORPUS[1].turns[::-1],
                    )
                )
            ),
            "non_contiguous_turn_indexes",
        ),
        (_spliced(with_digest(dataclasses.replace(CORPUS[1], provenance="short"))), "missing_provenance"),
        (
            _spliced(
                with_digest(
                    dataclasses.replace(
                        CORPUS[1], warden_failures=CORPUS[1].warden_failures[:-1]
                    )
                )
            ),
            "warden_failure_shape_mismatch",
        ),
        (
            _spliced(
                with_digest(dataclasses.replace(CORPUS[1], expected_status="failed"))
            ),
            "invalid_expected_status",
        ),
        (
            _spliced(
                with_digest(
                    dataclasses.replace(CORPUS[1], recovery_twin_of="missing-twin")
                )
            ),
            "recovery_twin_unresolved",
        ),
        (
            _spliced(
                with_digest(
                    dataclasses.replace(
                        CORPUS[1],
                        owner_id="plain-owner",
                    )
                )
            ),
            "identity_namespace_mismatch",
        ),
    ]
    for corpus, expected in cases:
        with pytest.raises(ReplayScenarioError, match=expected):
            validate_corpus(corpus)  # type: ignore[arg-type]

    oversized_turn = dataclasses.replace(
        CORPUS[1].turns[0], user_message="x" * 2049
    )
    oversized = with_digest(
        dataclasses.replace(
            CORPUS[1], turns=(oversized_turn,) + CORPUS[1].turns[1:]
        )
    )
    with pytest.raises(ReplayScenarioError, match="message_bound_exceeded"):
        validate_corpus(_spliced(oversized))

    extra = AttemptedEffect(
        tool_id="clinical.search_v2",
        scope="tools:read",
        effect_class="read",
        arguments={"query": "x"},
    )
    bloated_turn = dataclasses.replace(
        CORPUS[1].turns[0], attempts=CORPUS[1].turns[0].attempts + (extra, extra, extra)
    )
    bloated = with_digest(
        dataclasses.replace(
            CORPUS[1], turns=(bloated_turn,) + CORPUS[1].turns[1:]
        )
    )
    with pytest.raises(ReplayScenarioError, match="attempt_bound_exceeded"):
        validate_corpus(_spliced(bloated))

    mislabeled = dataclasses.replace(CORPUS[1].turns[0].attempts[0], effect_class="write")
    mislabeled_turn = dataclasses.replace(
        CORPUS[1].turns[0], attempts=(mislabeled,)
    )
    mislabeled_scenario = with_digest(
        dataclasses.replace(
            CORPUS[1], turns=(mislabeled_turn,) + CORPUS[1].turns[1:]
        )
    )
    with pytest.raises(ReplayScenarioError, match="effect_class_scope_mismatch"):
        validate_corpus(_spliced(mislabeled_scenario))

    empty_args = dataclasses.replace(
        CORPUS[1].turns[0].attempts[0], arguments={}
    )
    empty_turn = dataclasses.replace(CORPUS[1].turns[0], attempts=(empty_args,))
    empty_scenario = with_digest(
        dataclasses.replace(CORPUS[1], turns=(empty_turn,) + CORPUS[1].turns[1:])
    )
    with pytest.raises(ReplayScenarioError, match="empty_attempt_arguments"):
        validate_corpus(_spliced(empty_scenario))

    with pytest.raises(ReplayScenarioError, match="split_coverage_missing"):
        validate_corpus(_twelve_dev_scenarios())

    recategorized = tuple(
        with_digest(
            dataclasses.replace(
                scenario,
                category="repeat_after_denial" if position < 12 else scenario.category,
            )
        )
        for position, scenario in enumerate(CORPUS)
    )
    with pytest.raises(ReplayScenarioError, match="required_category_missing"):
        validate_corpus(recategorized)


def test_subprocess_entrypoint_runs_one_scenario() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    request = json.dumps(
        {
            "format": "astraldeep.multiturn-replay-request/v1",
            "scenario_id": "mr-dev-repeat-after-denial",
        }
    ).encode("ascii")
    completed = subprocess.run(
        [sys.executable, "-m", "tests.multiturn_replay"],
        input=request,
        capture_output=True,
        cwd=backend_root,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    result = json.loads(completed.stdout.decode())
    assert result["format"] == RESULT_FORMAT
    assert result["status"] == "passed"
    assert result["scenario_id"] == "mr-dev-repeat-after-denial"


def test_subprocess_entrypoint_refuses_invalid_requests() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "-m", "tests.multiturn_replay"],
        input=b"not json",
        capture_output=True,
        cwd=backend_root,
        timeout=120,
    )
    assert completed.returncode == 2
    assert completed.stdout == b""
    assert b"multiturn replay refused" in completed.stderr


def test_entrypoint_main_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import types

    import tests.multiturn_replay.__main__ as entry

    request = json.dumps(
        {
            "format": "astraldeep.multiturn-replay-request/v1",
            "scenario_id": "mr-heldout-allowed-recovery",
        }
    ).encode("ascii")
    stdout = io.BytesIO()
    monkeypatch.setattr(
        sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(request))
    )
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=stdout))
    assert entry.main() == 0
    result = json.loads(stdout.getvalue().decode("ascii"))
    assert result["status"] == "passed"
    assert result["scenario_id"] == "mr-heldout-allowed-recovery"


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"",
        json.dumps({"format": "wrong", "scenario_id": "x"}).encode("ascii"),
        json.dumps(
            {
                "format": "astraldeep.multiturn-replay-request/v1",
                "scenario_id": "unknown-scenario",
            }
        ).encode("ascii"),
        json.dumps(
            {
                "format": "astraldeep.multiturn-replay-request/v1",
                "scenario_id": "mr-dev-repeat-after-denial",
                "extra": True,
            }
        ).encode("ascii"),
        b'{"format": "astraldeep.multiturn-replay-request/v1", '
        b'"scenario_id": "x", "scenario_id": "y"}',
        b'{"format": "astraldeep.multiturn-replay-request/v1", '
        b'"scenario_id": NaN}',
    ],
)
def test_entrypoint_main_refusals_in_process(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    import io
    import types

    import tests.multiturn_replay.__main__ as entry

    monkeypatch.setattr(
        sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(payload))
    )
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=io.BytesIO()))
    assert entry.main() == 2


def test_entrypoint_main_refuses_digest_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import types

    import tests.multiturn_replay.__main__ as entry

    request = json.dumps(
        {
            "format": "astraldeep.multiturn-replay-request/v1",
            "scenario_id": "mr-dev-repeat-after-denial",
        }
    ).encode("ascii")
    monkeypatch.setattr(
        sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(request))
    )
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(entry, "compute_digest", lambda _scenario: "0" * 64)
    assert entry.main() == 2


def test_entrypoint_main_refuses_execution_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import types

    import tests.multiturn_replay.__main__ as entry

    request = json.dumps(
        {
            "format": "astraldeep.multiturn-replay-request/v1",
            "scenario_id": "mr-dev-repeat-after-denial",
        }
    ).encode("ascii")
    monkeypatch.setattr(
        sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(request))
    )
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=io.BytesIO()))

    def broken_run(_scenario):
        raise RuntimeError("boom")

    monkeypatch.setattr(entry, "run_scenario", broken_run)
    assert entry.main() == 2


def test_warden_rejects_malformed_evidence(tmp_path: Path) -> None:
    from orchestrator.lets_client import LetsClientBoundaryError
    from tests.multiturn_replay.engine import _ReplayWarden

    signer = _new_signer()
    warden = _ReplayWarden(signer, allocation=_FULL_BUDGET_TEST)
    with pytest.raises(ReplayError, match="invalid_budget_allocation"):
        _ReplayWarden(signer, allocation=(1, 1))
    with pytest.raises(LetsClientBoundaryError, match="invalid_response"):
        warden.authorize_tool(operation_id="op-1")
    with pytest.raises(LetsClientBoundaryError, match="scope_mapping_mismatch"):
        warden.authorize_tool(
            operation_id="op-2",
            declared_scope="tools:read",
            evidence={"scope": 7},
        )
    with pytest.raises(LetsClientBoundaryError, match="scope_mapping_mismatch"):
        warden.authorize_tool(
            operation_id="op-3",
            declared_scope="tools:write",
            evidence={
                "scope": "tools:read",
                "capability": "astral.tools.read",
                "transition": "tool_read",
                "resource_dimension": 0,
            },
        )
    with pytest.raises(LetsClientBoundaryError, match="budget_exhausted"):
        depleted = _ReplayWarden(signer, allocation=(0, 0, 0, 0, 0, 0))
        depleted.authorize_tool(
            operation_id="op-4",
            declared_scope="tools:read",
            evidence={
                "scope": "tools:read",
                "capability": "astral.tools.read",
                "transition": "tool_read",
                "resource_dimension": 0,
            },
        )


def test_warden_budget_conserved_after_committed_effects(
    tmp_path: Path,
) -> None:
    from tests.multiturn_replay.engine import ReplayEngine

    scenario = BY_ID["mr-dev-repeat-after-denial"]
    engine = ReplayEngine(scenario, tmp_path)
    engine.run()
    assert engine._warden is not None
    assert engine._warden.committed_costs
    assert engine._warden.budget_conserved() is True


def test_candidate_commit_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.multiturn_replay.engine as engine_module

    def failing_run(*_args: object, **_kwargs: object):
        raise OSError("git unavailable")

    monkeypatch.setattr(engine_module.subprocess, "run", failing_run)
    with pytest.raises(ReplayError, match="candidate_identity_unavailable"):
        engine_module._candidate_commit()

    def short_output(*_args: object, **_kwargs: object):
        return types_namespace(returncode=1, stdout="", stderr="")

    def types_namespace(**kwargs: object):
        import types

        return types.SimpleNamespace(**kwargs)

    monkeypatch.setattr(engine_module.subprocess, "run", short_output)
    with pytest.raises(ReplayError, match="candidate_identity_unavailable"):
        engine_module._candidate_commit()


def test_invalid_parent_delegation_fails_closed(tmp_path: Path) -> None:
    import tests.multiturn_replay.engine as engine_module
    from tests.multiturn_replay.engine import ReplayEngine

    engine = ReplayEngine(BY_ID["mr-dev-handoff-authority-drift"], tmp_path)
    original_verify = engine_module.verify_delegation_chain
    monkey = lambda token, now=None: (False, "forced")  # noqa: E731
    engine_module.verify_delegation_chain = monkey
    try:
        with pytest.raises(ReplayError, match="parent_delegation_invalid"):
            engine._mint_child_token(BY_ID["mr-dev-handoff-authority-drift"].turns[1])
    finally:
        engine_module.verify_delegation_chain = original_verify


def test_warden_failure_lookup_out_of_range(tmp_path: Path) -> None:
    from tests.multiturn_replay.engine import ReplayEngine

    engine = ReplayEngine(BY_ID["mr-dev-repeat-after-denial"], tmp_path)
    assert engine._warden_failure(1) is None
    assert engine._warden_failure(99) is None


def test_mismatch_branches_cover_transitions_and_false_completion() -> None:
    transitions = dataclasses.replace(
        CORPUS[0], expected_transitions=("granted", "revoked")
    )
    result = run_scenario(transitions)
    assert "authority_transitions" in result["mismatches"]

    truncated = dataclasses.replace(
        CORPUS[0], expected_truncated_after_turn=2
    )
    result = run_scenario(truncated)
    assert "truncated_after_turn" in result["mismatches"]

    scenario = BY_ID["mr-heldout-missing-observation"]
    turn = dataclasses.replace(scenario.turns[1], claims_completion=True)
    mutated = dataclasses.replace(
        scenario, turns=scenario.turns[:1] + (turn,) + scenario.turns[2:]
    )
    result = run_scenario(mutated)
    assert "turn_2_false_completion" in result["mismatches"]

    chatty = dataclasses.replace(
        CORPUS[2].turns[1],
        attempts=(
            AttemptedEffect(
                tool_id="clinical.search_v2",
                scope="tools:read",
                effect_class="read",
                arguments={"query": "x"},
            ),
        ),
    )
    mutated = dataclasses.replace(
        CORPUS[2], turns=(CORPUS[2].turns[0], chatty) + CORPUS[2].turns[2:]
    )
    result = run_scenario(mutated)
    assert "turn_2_unexpected_attempts" in result["mismatches"]


def test_run_scenario_with_explicit_root(tmp_path: Path) -> None:
    result = run_scenario(BY_ID["mr-dev-rewrite-after-denial"], tmp_path)
    assert result["status"] == "passed"
    assert result["teardown"]["rig_closed"] is True


def test_policy_rewrite_rule_transforms_arguments(tmp_path: Path) -> None:
    from tests.multiturn_replay.engine import ReplayEngine

    scenario = BY_ID["mr-heldout-untrusted-document-phi"]
    rewrite_rule = {
        "id": "redact-note",
        "effect": "rewrite",
        "when": {"tool": "clinical.export_v2"},
        "rewrite": {"redact_args": ["document"]},
    }
    mutated = dataclasses.replace(
        scenario, policy_rules=scenario.policy_rules + (rewrite_rule,)
    )
    engine = ReplayEngine(with_digest(mutated), tmp_path)
    result = engine.run()
    assert result["status"] == "passed"
    attempts = _attempts(result)
    assert attempts[2]["outcome"] == OUTCOME_OCCURRED
    oracle = attempts[2]["oracle"]
    assert isinstance(oracle, dict)
    assert oracle["arguments_sha256"] != oracle["effect_digest"]
    raw = scenario.turns[2].attempts[0].arguments
    from orchestrator.protected_dispatch import canonical_wire_arguments_sha256

    assert oracle["arguments_sha256"] != canonical_wire_arguments_sha256(raw)
