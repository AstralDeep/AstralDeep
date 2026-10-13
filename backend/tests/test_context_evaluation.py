"""Checks frozen paired evaluation and fail-closed promotion without assumed provider savings.
Development fixtures and held-out fixtures use separate immutable identities.
"""
import asyncio
import copy
from decimal import Decimal
import hashlib
import importlib
import json
import os
import runpy
import sys
from uuid import NAMESPACE_DNS, uuid5

import pytest
from context_evaluation.evaluate import load_workflows, evaluate, promotion
from context_evaluation.evaluate import _run

evaluation = importlib.import_module("context_evaluation.evaluate")
CANDIDATE = "a" * 40
ROUTE = {"provider": "unit-fixture-provider", "model": "unit-fixture-model",
         "endpoint_digest": "e" * 64, "configuration_digest": "c" * 64}


def identity(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def receipt(label):
    return hashlib.sha256(label.encode()).hexdigest()


def envelope(format_name):
    return {"format": format_name, "candidate": CANDIDATE,
            "implementation_digest": evaluation.implementation_identity(),
            "workflows_digest": identity(load_workflows()), "scoring": evaluation.SCORING,
            "scoring_digest": identity(evaluation.SCORING_RULES), "receipt_digest": receipt(format_name)}


@pytest.fixture
def inputs(monkeypatch):
    monkeypatch.setattr(evaluation, "implementation_identity", lambda: "9" * 64)
    runs = []
    prices = {"input_per_million": "1000000", "output_per_million": "2000000",
              "cached_input_per_million": "1000000", "date": "2026-10-08", "currency": "USD",
              "source_digest": receipt("unit pricing snapshot")}
    measurements = {**envelope(evaluation.MEASUREMENTS_FORMAT), "measurement_kind": "real_provider",
                    "provider_route": copy.deepcopy(ROUTE), "complete_cost_definition": evaluation.COMPLETE_COST_DEFINITION,
                    "promoted_control": "combined", "repetitions": 1, "measured_at": "2026-10-08T12:00:00Z",
                    "pricing": prices, "pricing_digest": identity(prices), "records": []}
    inventory = []
    for workflow in load_workflows():
        for control, (packing, compaction) in evaluation.CONTROLS.items():
            pair = f'{workflow["id"]}/{control}'
            runs.append({"workflow": workflow["id"], "split": workflow["split"], "control": control,
                         "source_identity": identity(workflow["source"]), "task_identity": identity(workflow["task"]),
                         "quality_passed": True, "boundary_passed": True, "failure_preservation_passed": True,
                         "scenario_passed": True, "exact_reconstruction": None})
            attempts = []
            for kind in ["direct", "auxiliary"] if compaction else ["direct"]:
                prompt = (10 if control == "baseline" else 8) if kind == "direct" else 1
                attempt_id = str(uuid5(NAMESPACE_DNS, f"unit-fixture/{pair}/{kind}"))
                attempts.append({"attempt_id": attempt_id, "kind": kind, "purpose": kind, "outcome": "success",
                                 "retry_of": None, "provider_route_digest": identity(ROUTE),
                                 "pricing_digest": identity(prices), "receipt_digest": receipt(attempt_id),
                                 "usage": {"prompt_tokens": prompt, "completion_tokens": 0,
                                           "cached_tokens": 0, "total_tokens": prompt}})
                inventory.append([workflow["id"], control, attempt_id])
            measurements["records"].append({**{key: runs[-1][key] for key in
                                                ("workflow", "split", "control", "source_identity", "task_identity",
                                                 "quality_passed", "boundary_passed", "failure_preservation_passed")},
                "accepted_task": True, "complete_cost_known": True, "non_model_cost_known": True,
                "non_model_cost": "0", "source_operations": 1, "recall_bytes": 0,
                "operation_receipt_digest": receipt(f"source/{pair}"), "auxiliary_model_attempts": int(compaction),
                "reported_total_cost": str(sum(attempt["usage"]["prompt_tokens"] for attempt in attempts)),
                "task_result": {"facts": list(workflow["facts"]), "outcome": workflow["expected"]["outcome"],
                                "source_refusal_reason": workflow["expected"]["packing_unavailable_reason"] if packing else None},
                "attempts": attempts})
    measurements["attempt_inventory_digest"] = identity(sorted(inventory))
    formative = {**envelope(evaluation.FORMATIVE_FORMAT), "synthetic_sources": True, "representative_users": True,
                 "participants": [{"participant": f"unit-participant-{index}", "distinguished_preview": index != 4,
                                   "recovered_omitted_fact": True, "without_assistance": True, "completed": True,
                                   "corrections": index, "repeated_retrievals": 1, "clarification_turns": index % 2,
                                   "receipt_digest": receipt(f"formative participant {index}")} for index in range(5)]}
    qualification = {**envelope(evaluation.QUALIFICATION_FORMAT), "provider_route_digest": identity(ROUTE),
                     "qualified_at": "2026-10-08T13:00:00Z", "ordinary_gates_passed": True,
                     "ordinary_gate_receipt_digest": receipt("ordinary gate unit receipt"),
                     "checks": {check: {"passed": True, "receipt_digest": receipt(f"unit check {check}")}
                                for check in evaluation.DETERMINISTIC_CHECKS},
                     "live_clients": [{"client": client, "candidate": CANDIDATE, "passed": True, "dispatch_verified": True,
                                       "receipt_digest": receipt(f"unit client {client}"),
                                       "states": dict.fromkeys(evaluation.SOURCE_STATES, True)}
                                      for client in sorted(evaluation.CLIENTS)]}
    return {"runs": runs, "candidate": CANDIDATE, "provider_route": copy.deepcopy(ROUTE),
            "provider_measurements": measurements, "formative": formative, "qualification": qualification}


def change(value, path, replacement):
    current = value
    for field in path[:-1]:
        current = current[field]
    if replacement is ...:
        del current[path[-1]]
    else:
        current[path[-1]] = copy.deepcopy(replacement)


def recalculate(measurements):
    prices = measurements["pricing"]
    inventory = []
    for record in measurements["records"]:
        total = Decimal(record["non_model_cost"])
        record["auxiliary_model_attempts"] = 0
        for attempt in record["attempts"]:
            usage = attempt["usage"]
            total += ((usage["prompt_tokens"] - usage["cached_tokens"]) * Decimal(prices["input_per_million"])
                      + usage["cached_tokens"] * Decimal(prices["cached_input_per_million"])
                      + usage["completion_tokens"] * Decimal(prices["output_per_million"])) / Decimal(1_000_000)
            record["auxiliary_model_attempts"] += attempt["kind"] == "auxiliary"
            attempt["pricing_digest"] = identity(prices)
            inventory.append([record["workflow"], record["control"], attempt["attempt_id"]])
        record["reported_total_cost"] = str(total)
    measurements["pricing_digest"] = identity(prices)
    measurements["attempt_inventory_digest"] = identity(sorted(inventory))


def test_fixed_workflow_split():
    workflows = load_workflows()
    assert len(workflows) == 24
    assert {x["split"] for x in workflows} == {"development", "held_out"}
    assert sum(x["split"] == "held_out" for x in workflows) == 12
    assert len({x["id"] for x in workflows}) == 24


def test_partitions_use_distinct_documents_tasks_and_frozen_oracles():
    workflows = load_workflows()
    assert all({"task", "operation", "expected"} <= set(item) for item in workflows)
    assert len({item["task"] for item in workflows}) == 24
    assert len({item["operation"] for item in workflows}) == 24
    development = [item for item in workflows if item["split"] == "development" and item["source"]]
    held_out = [item for item in workflows if item["split"] == "held_out" and item["source"]]
    for left in development:
        left_words = left["source"].lower().split()
        left_grams = {tuple(left_words[index:index + 4]) for index in range(len(left_words) - 3)}
        for right in held_out:
            right_words = right["source"].lower().split()
            right_grams = {tuple(right_words[index:index + 4]) for index in range(len(right_words) - 3)}
            assert len(left_grams & right_grams) / len(left_grams | right_grams) < 0.05


@pytest.mark.asyncio
async def test_paired_mechanisms_and_unknown_cost_cannot_promote():
    report = await evaluate(candidate="a" * 40)
    assert len(report["runs"]) == 96
    assert {run["control"] for run in report["runs"]} == {"baseline", "packing", "compaction", "combined"}
    assert all(run["quality_passed"] for run in report["runs"])
    assert all(run["source_operations"] == 1 for run in report["runs"])
    assert report["promotion"]["eligible"] is False
    assert "real_provider_measurements" in report["promotion"]["missing"]
    assert "five_user_formative_evidence" in report["promotion"]["missing"]


def test_failure_does_not_redefine_held_out_success():
    runs = [{"quality_passed": False, "split": "held_out", "boundary_passed": True}]
    assert promotion(runs)["eligible"] is False
    assert "held_out_regression" in promotion(runs)["failed"]
    assert runs[0]["quality_passed"] is False


@pytest.mark.asyncio
async def test_identity_and_split_are_validated():
    with pytest.raises(ValueError):
        await evaluate(candidate="main")
    fixtures = copy.deepcopy(load_workflows())
    fixtures[0]["split"] = "held_out"
    with pytest.raises(ValueError):
        await evaluate(candidate="b" * 40, workflows=fixtures)


@pytest.mark.asyncio
async def test_corruption_exercises_integrity_failure_instead_of_revocation():
    fixture = next(item for item in load_workflows() if item["kind"] == "corrupt")
    result = await _run(fixture, "packing")
    assert result["source_unavailable"] is True
    assert result["source_unavailable_reason"] == "integrity_failed"
    assert result["integrity_failure_verified"] is True
    assert result["exact_reconstruction"] is None


def test_unbound_receipts_and_claimed_unknown_costs_cannot_open_promotion():
    fixtures = load_workflows()
    controls = ["baseline", "packing", "compaction", "combined"]
    runs = [{"workflow": item["id"], "split": item["split"], "control": control,
             "quality_passed": True, "boundary_passed": True}
            for item in fixtures for control in controls]
    measurements = [{"workflow": run["workflow"], "control": run["control"],
                     "complete_cost_known": True, "quality_passed": True,
                     "receipt_digest": "f" * 64, "pricing_date": "2026-10-08"} for run in runs]
    result = promotion(runs, provider_measurements=measurements,
                       formative=[{"participant": str(index), "all_tasks_passed": True} for index in range(5)],
                       qualification={"ordinary_gates_passed": True,
                                      "live_clients": ["web", "windows", "android", "macos", "ios", "watchos"]})
    assert result["eligible"] is False


def test_exact_bound_unit_receipts_exercise_acceptance_without_changing_defaults(inputs):
    result = promotion(**inputs)
    assert result["eligible"] is True
    assert result["defaults_changed"] is False
    assert result["missing"] == result["failed"] == []
    assert result["cost_comparison"]["saving_ratio"] == "0.1"
    assert result["cost_comparison"]["accepted_tasks"] == dict.fromkeys(evaluation.CONTROLS, 12)
    assert result["formative_summary"] == {"participants": 5, "unaided_successes": 4, "completed": 5,
                                           "corrections": 10, "repeated_retrievals": 5, "clarification_turns": 2,
                                           "target_passed": True}


@pytest.mark.parametrize("field", ["provider_measurements", "formative", "qualification"])
def test_each_actual_measurement_class_is_required_separately(inputs, field):
    inputs[field] = None
    result = promotion(**inputs)
    assert not result["eligible"]
    assert len(result["missing"]) == 1
    assert result["failed"] == []


@pytest.mark.parametrize("envelope_name", ["provider_measurements", "formative", "qualification"])
@pytest.mark.parametrize("field,value", [
    ("candidate", "b" * 40), ("implementation_digest", "b" * 64), ("workflows_digest", "b" * 64),
    ("scoring", "tuned-on-held-out"), ("scoring_digest", "b" * 64), ("format", "unknown/v1"),
    ("receipt_digest", "invalid"), ("receipt_digest", None), ("candidate", ...), ("extra", "unexpected"),
])
def test_receipts_cannot_cross_candidate_source_or_scoring_boundaries(inputs, envelope_name, field, value):
    change(inputs[envelope_name], [field], value)
    result = promotion(**inputs)
    assert not result["eligible"]
    assert any(failed.endswith("_invalid") for failed in result["failed"])


@pytest.mark.parametrize("path,value", [
    (["measurement_kind"], "synthetic_capability"), (["provider_route", "model"], "different-model"),
    (["complete_cost_definition"], "direct-only"), (["promoted_control"], "packing"),
    (["repetitions"], 2), (["repetitions"], True), (["measured_at"], "2026-10-08T12:00:00"),
    (["measured_at"], "2026-10-08 12:00:00Z"), (["measured_at"], "invalid"), (["measured_at"], 0),
    (["measured_at"], "x" * 65), (["pricing_digest"], "0" * 64), (["records"], []),
    (["records"], {}), (["attempt_inventory_digest"], "b" * 64),
    (["records", 0, "workflow"], "unknown-01"), (["records", 0, "workflow"], []),
    (["records", 0, "split"], "held_out"), (["records", 0, "source_identity"], "b" * 64),
    (["records", 0, "task_identity"], "b" * 64), (["records", 0, "quality_passed"], False),
    (["records", 0, "boundary_passed"], False), (["records", 0, "failure_preservation_passed"], False),
    (["records", 0, "accepted_task"], False), (["records", 0, "complete_cost_known"], False),
    (["records", 0, "non_model_cost_known"], False), (["records", 0, "non_model_cost"], None),
    (["records", 0, "source_operations"], 2), (["records", 0, "source_operations"], True),
    (["records", 0, "recall_bytes"], -1), (["records", 0, "recall_bytes"], True),
    (["records", 0, "operation_receipt_digest"], "invalid"),
    (["records", 0, "task_result", "facts"], ["invented fact"]),
    (["records", 0, "task_result", "outcome"], "error"),
    (["records", 0, "task_result", "source_refusal_reason"], "revoked"),
    (["records", 0, "task_result", "extra"], True), (["records", 0, "attempts"], []),
    (["records", 0, "attempts"], {}), (["records", 0, "auxiliary_model_attempts"], 1),
    (["records", 0, "auxiliary_model_attempts"], True), (["records", 0, "reported_total_cost"], "0"),
    (["records", 0, "extra"], True), (["records", 0], None),
    (["records", 0, "attempts", 0, "kind"], "unknown"),
    (["records", 0, "attempts", 0, "purpose"], "https://credential.invalid"),
    (["records", 0, "attempts", 0, "outcome"], "unknown"),
    (["records", 0, "attempts", 0, "provider_route_digest"], "0" * 64),
    (["records", 0, "attempts", 0, "pricing_digest"], "0" * 64),
    (["records", 0, "attempts", 0, "receipt_digest"], "invalid"),
    (["records", 0, "attempts", 0, "attempt_id"], "00000000-0000-0000-0000-000000000000"),
    (["records", 0, "attempts", 0, "attempt_id"], "INVALID"),
    (["records", 0, "attempts", 0, "attempt_id"], None),
    (["records", 0, "attempts", 0, "retry_of"], "00000000-0000-4000-8000-000000000001"),
    (["records", 0, "attempts", 0, "retry_of"], []),
    (["records", 0, "attempts", 0, "usage", "total_tokens"], 1),
    (["records", 0, "attempts", 0, "usage", "cached_tokens"], 11),
    (["records", 0, "attempts", 0, "usage", "prompt_tokens"], True),
    (["records", 0, "attempts", 0, "usage", "prompt_tokens"], None),
    (["records", 0, "attempts", 0, "usage", "prompt_tokens"], -1),
    (["records", 0, "attempts", 0, "usage", "prompt_tokens"], 2**63),
    (["records", 0, "attempts", 0, "usage", "extra"], 1),
    (["records", 0, "attempts", 0, "usage"], None),
    (["records", 0, "attempts", 0], None),
])
def test_unknown_incomplete_or_foreign_provider_measurements_cannot_promote(inputs, path, value):
    change(inputs["provider_measurements"], path, value)
    result = promotion(**inputs)
    assert not result["eligible"]
    assert "real_provider_measurements_invalid" in result["failed"]


@pytest.mark.parametrize("field,value", [
    ("input_per_million", None), ("input_per_million", 0), ("input_per_million", ""),
    ("input_per_million", "NaN"), ("input_per_million", "Infinity"), ("input_per_million", "-1"),
    ("input_per_million", "invalid"), ("input_per_million", "1e10"),
    ("input_per_million", "0.0000000000001"), ("input_per_million", "1" * 65),
    ("date", "20261008"), ("date", "2026-10-09"), ("date", None),
    ("currency", "usd"), ("currency", None), ("source_digest", "invalid"), ("extra", True),
])
def test_invalid_undated_or_unknown_prices_are_never_zero_cost(inputs, field, value):
    prices = inputs["provider_measurements"]["pricing"]
    prices[field] = value
    inputs["provider_measurements"]["pricing_digest"] = identity(prices)
    result = promotion(**inputs)
    assert not result["eligible"]
    assert "real_provider_measurements_invalid" in result["failed"]


@pytest.mark.parametrize("field", ["attempt_id", "receipt_digest"])
def test_duplicate_physical_attempts_and_receipts_cannot_be_counted_twice(inputs, field):
    records = inputs["provider_measurements"]["records"]
    records[1]["attempts"][0][field] = records[0]["attempts"][0][field]
    recalculate(inputs["provider_measurements"])
    assert "real_provider_measurements_invalid" in promotion(**inputs)["failed"]


def test_duplicate_paired_records_and_missing_inventory_are_rejected(inputs):
    measurements = inputs["provider_measurements"]
    measurements["records"][-1] = copy.deepcopy(measurements["records"][0])
    assert "real_provider_measurements_invalid" in promotion(**inputs)["failed"]


def test_cache_output_auxiliary_failure_cancelled_retry_and_recall_cost_all_count(inputs):
    measurements = inputs["provider_measurements"]
    measurements["pricing"]["cached_input_per_million"] = "500000"
    for record in measurements["records"]:
        if record["control"] != "combined":
            continue
        attempt = record["attempts"][0]
        attempt["usage"] = {"prompt_tokens": 8, "completion_tokens": 1, "cached_tokens": 4, "total_tokens": 9}
        attempt["outcome"] = "failure"
        retry = copy.deepcopy(attempt)
        retry["attempt_id"] = str(uuid5(NAMESPACE_DNS, f'retry/{record["workflow"]}'))
        retry["retry_of"] = attempt["attempt_id"]
        retry["outcome"] = "cancelled"
        retry["receipt_digest"] = receipt(retry["attempt_id"])
        retry["usage"] = {"prompt_tokens": 1, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 1}
        record["attempts"].append(retry)
        record["non_model_cost"] = "1"
        record["recall_bytes"] = 8192
    recalculate(measurements)
    result = promotion(**inputs)
    assert Decimal(result["cost_comparison"]["cost_per_accepted_task"]["combined"]) == Decimal("11")
    assert not result["eligible"]
    assert "complete_cost_target_not_met" in result["failed"]


def test_retry_cannot_point_to_successful_work_and_hide_extra_attempts(inputs):
    measurements = inputs["provider_measurements"]
    record = measurements["records"][0]
    retry = copy.deepcopy(record["attempts"][0])
    retry["attempt_id"] = str(uuid5(NAMESPACE_DNS, "retry-success"))
    retry["receipt_digest"] = receipt(retry["attempt_id"])
    retry["retry_of"] = record["attempts"][0]["attempt_id"]
    retry["usage"] = dict.fromkeys(retry["usage"], 0)
    record["attempts"].append(retry)
    recalculate(measurements)
    assert "real_provider_measurements_invalid" in promotion(**inputs)["failed"]


def test_zero_baseline_and_subthreshold_savings_are_experiments(inputs):
    measurements = inputs["provider_measurements"]
    measurements["pricing"] = {**measurements["pricing"], "input_per_million": "0", "output_per_million": "0",
                               "cached_input_per_million": "0"}
    recalculate(measurements)
    result = promotion(**inputs)
    assert result["cost_comparison"]["saving_ratio"] is None
    assert "complete_cost_target_not_met" in result["failed"]


def test_exact_ten_percent_target_does_not_round_a_failure_to_success(inputs):
    measurements = inputs["provider_measurements"]
    for record in measurements["records"]:
        if record["control"] == "combined":
            record["non_model_cost"] = "0.000000000001"
    recalculate(measurements)
    result = promotion(**inputs)
    assert not result["eligible"]
    assert "complete_cost_target_not_met" in result["failed"]


@pytest.mark.parametrize("path,value", [
    (["synthetic_sources"], False), (["representative_users"], False), (["participants"], []),
    (["participants"], {}), (["participants", 0], None), (["participants", 0, "participant"], ""),
    (["participants", 0, "receipt_digest"], "invalid"), (["participants", 0, "distinguished_preview"], 1),
    (["participants", 0, "corrections"], -1), (["participants", 0, "repeated_retrievals"], True),
    (["participants", 0, "clarification_turns"], 10_001), (["participants", 0, "extra"], True),
])
def test_formative_scope_and_observations_are_bounded_and_complete(inputs, path, value):
    change(inputs["formative"], path, value)
    result = promotion(**inputs)
    assert not result["eligible"]
    assert "five_user_formative_evidence_invalid" in result["failed"]


@pytest.mark.parametrize("field", ["participant", "receipt_digest"])
def test_duplicate_participant_or_observation_receipt_is_not_five_users(inputs, field):
    people = inputs["formative"]["participants"]
    people[1][field] = people[0][field]
    assert "five_user_formative_evidence_invalid" in promotion(**inputs)["failed"]


@pytest.mark.parametrize("field", ["distinguished_preview", "recovered_omitted_fact", "without_assistance", "completed"])
def test_three_unaided_users_do_not_pass_a_four_of_five_target(inputs, field):
    inputs["formative"]["participants"][0][field] = False
    result = promotion(**inputs)
    assert not result["eligible"]
    assert result["formative_summary"]["unaided_successes"] == 3
    assert "formative_target_not_met" in result["failed"]


@pytest.mark.parametrize("path,value", [
    (["provider_route_digest"], "b" * 64), (["ordinary_gate_receipt_digest"], "invalid"),
    (["qualified_at"], "2026-10-08T11:59:59Z"), (["qualified_at"], "invalid"),
    (["ordinary_gates_passed"], 1), (["checks"], {}), (["checks", "integrity"], True),
    (["checks", "integrity", "passed"], 1), (["checks", "integrity", "receipt_digest"], "invalid"),
    (["live_clients"], []), (["live_clients"], {}), (["live_clients", 0], None),
    (["live_clients", 0, "client"], "unknown"), (["live_clients", 0, "candidate"], "b" * 40),
    (["live_clients", 0, "passed"], 1), (["live_clients", 0, "dispatch_verified"], 1),
    (["live_clients", 0, "receipt_digest"], "invalid"), (["live_clients", 0, "states"], {}),
    (["live_clients", 0, "states", "source"], 1), (["live_clients", 0, "extra"], True),
])
def test_qualification_requires_following_checks_and_six_bound_client_receipts(inputs, path, value):
    change(inputs["qualification"], path, value)
    result = promotion(**inputs)
    assert not result["eligible"]
    assert "product_and_client_qualification_invalid" in result["failed"]


@pytest.mark.parametrize("path", [
    ["ordinary_gates_passed"], ["checks", "integrity", "passed"], ["live_clients", 0, "passed"],
    ["live_clients", 0, "dispatch_verified"], ["live_clients", 0, "states", "source"],
])
def test_a_failed_product_gate_or_live_state_blocks_promotion(inputs, path):
    change(inputs["qualification"], path, False)
    assert "product_qualification_failed" in promotion(**inputs)["failed"]


@pytest.mark.parametrize("field", ["client", "receipt_digest"])
def test_duplicate_client_or_native_receipt_is_not_six_client_verification(inputs, field):
    clients = inputs["qualification"]["live_clients"]
    clients[1][field] = clients[0][field]
    assert "product_and_client_qualification_invalid" in promotion(**inputs)["failed"]


@pytest.mark.parametrize("path,value,reason", [
    (["runs"], [], "paired_workflows_incomplete"), (["runs"], {}, "paired_workflows_incomplete"),
    (["runs", 0], None, "paired_workflows_invalid"),
    (["runs", 0, "workflow"], [], "paired_workflows_invalid"),
    (["runs", 0, "source_identity"], "0" * 64, "paired_workflows_invalid"),
    (["runs", 0, "task_identity"], "0" * 64, "paired_workflows_invalid"),
    (["runs", 0, "quality_passed"], False, "deterministic_regression"),
    (["runs", 48, "quality_passed"], False, "held_out_regression"),
    (["runs", 0, "exact_reconstruction"], False, "evidence_integrity_regression"),
    (["runs", 0, "boundary_passed"], False, "boundary_regression"),
    (["candidate"], "main", "evaluation_binding_invalid"),
    (["promoted_control"], "baseline", "evaluation_binding_invalid"),
    (["promoted_control"], [], "evaluation_binding_invalid"),
])
def test_deterministic_failures_do_not_redefine_success(inputs, path, value, reason):
    change(inputs, path, value)
    result = promotion(**inputs)
    assert not result["eligible"]
    assert reason in result["failed"]


def test_oversized_or_duplicate_paired_diagnostics_cannot_open_promotion(inputs):
    inputs["runs"].append(copy.deepcopy(inputs["runs"][0]))
    assert "paired_workflows_incomplete" in promotion(**inputs)["failed"]
    inputs["runs"].pop()
    inputs["runs"][-1] = copy.deepcopy(inputs["runs"][0])
    assert "paired_workflows_invalid" in promotion(**inputs)["failed"]


@pytest.mark.parametrize("field,value", [
    ("id", "invalid"), ("kind", []), ("task", ""), ("operation", ""),
    ("source", "x" * 16385), ("source", None), ("facts", []), ("facts", ["absent", "absent", "absent"]),
    ("expected", {}), ("expected", {"outcome": "invented-success"}), ("extra", True),
])
@pytest.mark.asyncio
async def test_mutated_frozen_documents_and_oracles_are_rejected(field, value):
    workflows = copy.deepcopy(load_workflows())
    workflows[0][field] = value
    with pytest.raises(ValueError, match="evaluation_"):
        await evaluate(candidate=CANDIDATE, workflows=workflows)


@pytest.mark.asyncio
async def test_valid_shape_cannot_tune_a_held_out_task_or_source():
    workflows = copy.deepcopy(load_workflows())
    workflows[12]["task"] += " Redefine success."
    with pytest.raises(ValueError, match="evaluation_workflows_changed"):
        await evaluate(candidate=CANDIDATE, workflows=workflows)
    with pytest.raises(ValueError, match="evaluation_workflows_invalid"):
        await evaluate(candidate=CANDIDATE, workflows={})
    workflows = copy.deepcopy(load_workflows())
    workflows[-1]["id"] = workflows[0]["id"]
    with pytest.raises(ValueError, match="evaluation_workflows_invalid"):
        await evaluate(candidate=CANDIDATE, workflows=workflows)


@pytest.mark.asyncio
async def test_source_identity_changes_during_run_are_rejected(monkeypatch):
    identities = iter(["a" * 64, "b" * 64])
    monkeypatch.setattr(evaluation, "implementation_identity", lambda: next(identities))
    with pytest.raises(ValueError, match="evaluation_candidate_changed"):
        await evaluate(candidate=CANDIDATE)


@pytest.mark.asyncio
async def test_simulated_provider_cancellation_is_reported_without_aborting_run():
    workflow = next(item for item in load_workflows() if item["kind"] == "cancelled")
    result = await _run(workflow, "combined")
    assert result["context_reason"] == "provider_cancelled"
    assert result["context_status"] == "context_limit"
    assert result["auxiliary_model_attempts"] == 1
    assert result["failure_preservation_passed"] and result["quality_passed"]


@pytest.mark.asyncio
async def test_real_caller_cancellation_propagates_even_in_the_cancelled_fixture(monkeypatch):
    entered = asyncio.Event()

    async def wait_until_cancelled(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(evaluation, "compact_context", wait_until_cancelled)
    workflow = next(item for item in load_workflows() if item["kind"] == "cancelled")
    task = asyncio.create_task(_run(workflow, "combined"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_unexpected_provider_cancellation_does_not_become_a_diagnostic_success(monkeypatch):
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(evaluation, "compact_context", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await _run(load_workflows()[0], "compaction")


@pytest.mark.asyncio
async def test_protected_tool_group_mutation_and_rejected_history_are_scored_as_failures(monkeypatch):
    async def destroy_history(messages, **kwargs):
        return evaluation.CompactionResult([], "accepted", "accepted", 1000, 0)

    monkeypatch.setattr(evaluation, "compact_context", destroy_history)
    result = await _run(load_workflows()[0], "compaction")
    assert not result["quality_passed"]
    assert not result["failure_preservation_passed"]

    async def reject_with_different_history(messages, **kwargs):
        return evaluation.CompactionResult([], "context_limit", "failed", 1000, 0)

    monkeypatch.setattr(evaluation, "compact_context", reject_with_different_history)
    assert not (await _run(load_workflows()[0], "compaction"))["quality_passed"]


@pytest.mark.asyncio
async def test_instruction_loss_cannot_be_hidden_by_an_intact_tool_group(monkeypatch):
    async def discard_instruction(messages, **kwargs):
        candidate = messages[1:]
        return evaluation.CompactionResult(candidate, "accepted", "accepted",
                                           evaluation.estimate_context_tokens(messages),
                                           evaluation.estimate_context_tokens(candidate))

    monkeypatch.setattr(evaluation, "compact_context", discard_instruction)
    assert not (await _run(load_workflows()[0], "compaction"))["failure_preservation_passed"]


@pytest.mark.asyncio
async def test_archive_owner_bypass_and_missing_retention_refusal_fail_deterministic_oracles(monkeypatch):
    original_read = evaluation.EvidenceArchive.read

    def bypass(self, reference, **kwargs):
        kwargs["owner_id"] = "synthetic-owner"
        return original_read(self, reference, **kwargs)

    monkeypatch.setattr(evaluation.EvidenceArchive, "read", bypass)
    assert not (await _run(load_workflows()[0], "packing"))["boundary_passed"]
    original_capture = evaluation.EvidenceArchive.capture

    def ignore_retention(self, *args, **kwargs):
        if self.observation_limit_bytes == 512:
            return None
        return original_capture(self, *args, **kwargs)

    monkeypatch.setattr(evaluation.EvidenceArchive, "capture", ignore_retention)
    workflow = next(item for item in load_workflows() if item["kind"] == "oversized")
    assert not (await _run(workflow, "packing"))["quality_passed"]


@pytest.mark.asyncio
async def test_invalid_provider_binding_is_not_echoed_into_report(monkeypatch):
    secret = "synthetic-secret-never-echo"
    report = await evaluate(candidate=CANDIDATE, provider_route={"endpoint": f"https://{secret}@example.invalid"})
    assert secret not in json.dumps(report)
    assert report["measurement_provider_route"] is None


def test_cli_writes_honest_missing_evidence_and_preserves_independent_defaults(tmp_path, monkeypatch):
    output = tmp_path / "diagnostics" / "report.json"
    monkeypatch.setattr(sys, "argv", ["evaluate", "--candidate", CANDIDATE, "--output", str(output)])
    evaluation.main()
    report = json.loads(output.read_text())
    assert report["measurement_kind"] == "synthetic_capability"
    assert report["promotion"]["eligible"] is False
    assert report["promotion"]["missing"] == ["real_provider_measurements", "five_user_formative_evidence",
                                             "product_and_client_qualification"]
    assert report["promotion"]["defaults_changed"] is False


def test_cli_accepts_complete_unit_bound_evidence_as_schema_validation_only(inputs, tmp_path, monkeypatch):
    evidence_path, output = tmp_path / "evidence.json", tmp_path / "report.json"
    evidence_path.write_text(json.dumps({key: value for key, value in inputs.items() if key not in {"candidate", "runs"}}))
    monkeypatch.setattr(sys, "argv", ["evaluate", "--candidate", CANDIDATE, "--output", str(output),
                                     "--measurements", str(evidence_path)])
    evaluation.main()
    report = json.loads(output.read_text())
    assert report["promotion"]["eligible"] is True
    assert report["provider_route"] == evaluation.PROVIDER_ROUTE


@pytest.mark.parametrize("content", ["[]", '{"extra":true}', '{"provider_route":{},"provider_route":{}}', "NaN"])
def test_cli_rejects_unbounded_or_ambiguous_measurement_envelopes(tmp_path, monkeypatch, content):
    path, output = tmp_path / "inputs.json", tmp_path / "report.json"
    path.write_text(content)
    monkeypatch.setattr(sys, "argv", ["evaluate", "--candidate", CANDIDATE, "--output", str(output),
                                     "--measurements", str(path)])
    with pytest.raises(ValueError, match="evaluation_measurements_invalid"):
        evaluation.main()
    assert not output.exists()


def test_diagnostic_reader_rejects_missing_nonregular_and_oversized_sources(tmp_path, monkeypatch):
    pipe = tmp_path / "measurements-pipe"
    os.mkfifo(pipe)
    for path in (tmp_path / "missing", tmp_path, pipe):
        with pytest.raises(ValueError, match="^evaluation_measurements_invalid$"):
            evaluation._read_measurements(path)
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (16 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="evaluation_measurements_invalid"):
        evaluation._read_measurements(path)
    original_stat = evaluation.os.fstat

    def smaller_metadata(descriptor):
        value = list(original_stat(descriptor))
        value[6] = 0
        return os.stat_result(value)

    monkeypatch.setattr(evaluation.os, "fstat", smaller_metadata)
    with pytest.raises(ValueError, match="evaluation_measurements_invalid"):
        evaluation._read_measurements(path)


@pytest.mark.parametrize("content", [b"\xff", b"{invalid", b"[" * 1100 + b"]" * 1100])
def test_diagnostic_reader_denies_invalid_encoding_json_and_recursion(tmp_path, content):
    path = tmp_path / "measurements.json"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="evaluation_measurements_invalid"):
        evaluation._read_measurements(path)


def test_receipt_reuse_across_product_checks_and_clients_is_rejected(inputs):
    qualification = inputs["qualification"]
    qualification["ordinary_gate_receipt_digest"] = qualification["receipt_digest"]
    assert "product_and_client_qualification_invalid" in promotion(**inputs)["failed"]
    qualification["ordinary_gate_receipt_digest"] = receipt("distinct ordinary gate")
    qualification["checks"]["integrity"]["receipt_digest"] = qualification["ordinary_gate_receipt_digest"]
    assert "product_and_client_qualification_invalid" in promotion(**inputs)["failed"]


def test_module_entry_point_runs_the_same_fail_closed_diagnostic(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["evaluate", "--candidate", CANDIDATE, "--output", str(output)])
    runpy.run_path(evaluation.__file__, run_name="__main__")
    assert json.loads(output.read_text())["promotion"]["eligible"] is False
