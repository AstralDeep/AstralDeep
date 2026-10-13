"""Runs paired frozen synthetic context capabilities and reports missing promotion evidence honestly.
This diagnostic does not change defaults, replace product qualification, or claim measured provider savings.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID

from orchestrator.evidence_archive import EvidenceArchive, EvidenceDenied, EvidenceError, EvidenceUnavailable, RetentionGrant
from orchestrator.safe_compaction import CompactionResult, compact_context, estimate_context_tokens

CONTROLS = {"baseline": (False, False), "packing": (True, False),
            "compaction": (False, True), "combined": (True, True)}
PROVIDER_ROUTE = "deterministic-synthetic-provider-v1"
SCORING = "exact-frozen-task-outcome-and-source-v1"
CLIENTS = {"web", "windows", "android", "macos", "ios", "watchos"}
DETERMINISTIC_CHECKS = {"capability", "owner_isolation", "revocation", "privacy", "confirmation",
                        "integrity", "failure_preservation", "retention", "concurrency", "accounting"}
SOURCE_STATES = {"source", "preview", "summary", "missing", "blocked", "usage"}
COMPLETE_COST_DEFINITION = "all-physical-direct-auxiliary-retry-cache-failed-cancelled-source-recall-costs-v1"
SCORING_RULES = {"task_oracle": "exact-permitted-facts-or-honest-retention-refusal",
                 "cost_scope": "held_out", "minimum_saving_percent": 10,
                 "lost_accepted_tasks_permitted": 0, "formative_participants": 5,
                 "minimum_unaided_successes": 4, "complete_cost_definition": COMPLETE_COST_DEFINITION,
                 "repetitions": 1, "required_checks": sorted(DETERMINISTIC_CHECKS), "required_clients": sorted(CLIENTS)}
MEASUREMENTS_FORMAT = "astral.context-provider-measurements/v1"
FORMATIVE_FORMAT = "astral.context-formative/v1"
QUALIFICATION_FORMAT = "astral.context-qualification/v1"
_KINDS = {"manifest", "unicode", "repeated", "error", "denial", "incomplete", "empty", "oversized",
          "revoked", "expired", "corrupt", "cancelled"}
_SCOPE_FIELDS = {"candidate", "implementation_digest", "workflows_digest", "scoring", "scoring_digest"}
_IMPLEMENTATION_FILES = (
    "context_evaluation/evaluate.py", "orchestrator/evidence_archive.py", "orchestrator/safe_compaction.py",
    "orchestrator/context_usage.py", "orchestrator/context_presentation.py", "orchestrator/evidence_context.py",
    "orchestrator/context_authority.py", "orchestrator/context_budget.py", "orchestrator/orchestrator.py",
)


def _identity(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def load_workflows() -> list[dict]:
    return json.loads(Path(__file__).with_name("workflows.json").read_text())


def implementation_identity() -> str:
    root = Path(__file__).resolve().parent.parent
    return _identity({path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in _IMPLEMENTATION_FILES})


def _validate(workflows):
    if (type(workflows) is not list or len(workflows) != 24
            or any(type(item) is not dict or set(item) != {"id", "split", "kind", "task", "operation", "source", "facts", "expected"}
                   or type(item["id"]) is not str or re.fullmatch(r"[a-z_]+-[0-9]{2}", item["id"]) is None
                   or type(item["kind"]) is not str or item["kind"] not in _KINDS
                   or type(item["task"]) is not str or not item["task"] or len(item["task"].encode()) > 2048
                   or not _label(item["operation"], 64)
                   for item in workflows)
            or len({item["id"] for item in workflows}) != 24
            or len({item["task"] for item in workflows}) != 24
            or len({item["operation"] for item in workflows}) != 24):
        raise ValueError("evaluation_workflows_invalid")
    for split in ("development", "held_out"):
        group = [item for item in workflows if item.get("split") == split]
        if len(group) != 12 or {item.get("kind") for item in group} != _KINDS:
            raise ValueError("evaluation_split_invalid")
        for item in group:
            if (type(item.get("source")) is not str or len(item["source"].encode()) > 16384
                    or type(item.get("facts")) is not list or len(item["facts"]) != (0 if item["kind"] == "empty" else 3)
                    or any(type(fact) is not str or not fact or fact not in item["source"] for fact in item["facts"])):
                raise ValueError("evaluation_source_invalid")
            expected = {"outcome": item["kind"] if item["kind"] in {"error", "denial", "incomplete", "cancelled"} else "success",
                        "source_empty": item["kind"] == "empty",
                        "packing_unavailable_reason": {"revoked": "revoked", "expired": "expired", "corrupt": "integrity_failed",
                                                       "oversized": "evidence_retention_limit"}.get(item["kind"]),
                        "provider_cancelled": item["kind"] == "cancelled"}
            if (type(item["expected"]) is not dict or set(item["expected"]) != set(expected)
                    or any(type(item["expected"][key]) is not type(value) or item["expected"][key] != value
                           for key, value in expected.items()) or (item["source"] == "") != expected["source_empty"]):
                raise ValueError("evaluation_expectations_invalid")


async def _run(workflow, control):
    packing, compaction = CONTROLS[control]
    moment = datetime(2026, 10, 8, tzinfo=UTC)
    clock = [moment]
    archive = EvidenceArchive(clock=lambda: clock[0], page_limit_bytes=512)
    source = workflow["source"]
    outcome = workflow["expected"]["outcome"]
    grant = RetentionGrant("synthetic-owner", workflow["id"], "user:synthetic-owner", "synthetic-1",
                           "manifest", moment + timedelta(hours=1))
    observation, recalled, boundary_passed, unavailable = None, "", True, False
    unavailable_reason = None
    if packing:
        try:
            if workflow["kind"] == "oversized":
                limited = EvidenceArchive(clock=lambda: clock[0], observation_limit_bytes=512)
                limited.capture(source, grant=grant, operation_id="synthetic-operation", source_args={}, outcome=outcome)
                boundary_passed = False
            else:
                observation = archive.capture(source, grant=grant, operation_id="synthetic-operation",
                                               source_args={}, outcome=outcome)
                try:
                    archive.read(observation.reference, grant=grant, owner_id="foreign-owner",
                                 conversation_id=workflow["id"], audience_id="user:synthetic-owner")
                    boundary_passed = False
                except EvidenceDenied:
                    pass
                if workflow["kind"] == "revoked":
                    archive.revoke(owner_id="synthetic-owner", conversation_id=workflow["id"])
                if workflow["kind"] == "corrupt":
                    archive._records[observation.reference].content = b"corrupted synthetic source"
                if workflow["kind"] == "expired":
                    clock[0] += timedelta(hours=2)
                offset = 0
                while True:
                    page = archive.read(observation.reference, offset=offset, grant=grant,
                                        owner_id="synthetic-owner", conversation_id=workflow["id"],
                                        audience_id="user:synthetic-owner")
                    recalled += page.text
                    if page.at_end:
                        break
                    offset = page.next_offset
        except EvidenceError as exc:
            unavailable = True
            unavailable_reason = exc.reason if isinstance(exc, EvidenceUnavailable) else exc.code
    working = source
    if observation is not None and not unavailable:
        working = f"Partial preview; outcome={outcome}; reference={observation.reference}; " + source[:128]
    messages = [{"role": "system", "content": "Use only permitted source evidence; preserve operation outcomes."}]
    for index in range(6):
        messages.append({"role": "user" if index % 2 == 0 else "assistant",
                         "content": f"Older bounded synthetic dialogue {index}. " + "ordinary context " * 20})
    messages.extend([
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "synthetic-call", "type": "function", "function": {"name": workflow["operation"], "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "synthetic-call", "content": f"Outcome={outcome}\n{working}"},
        {"role": "user", "content": workflow["task"]},
    ])
    before = estimate_context_tokens(messages)
    physical_calls = 0

    async def summarize(prompt):
        nonlocal physical_calls
        physical_calls += 1
        if workflow["expected"]["provider_cancelled"]:
            raise asyncio.CancelledError
        return "Generated synthetic dialogue summary. Source observations and actual operation outcomes remain in their complete tool groups."

    result = None
    if compaction:
        try:
            result = await compact_context(messages, llm_call=summarize, context_tokens=before - 128,
                                           min_recent_turns=0, read_protected_state=lambda: _ready(),
                                           validate_references=lambda text: _ready(True))
        except asyncio.CancelledError:
            if workflow["kind"] != "cancelled" or asyncio.current_task().cancelling():
                raise
            result = CompactionResult(messages, "context_limit", "provider_cancelled", before, before)
    available = recalled if observation is not None and not unavailable else source
    preservation = (result is None or result.status != "accepted"
                    or (bool(result.messages) and result.messages[0] == messages[0] and messages[-3:] == result.messages[-3:]))
    if result is not None and result.status != "accepted":
        preservation = preservation and result.messages == messages
    expected_unavailable = workflow["expected"]["packing_unavailable_reason"] if packing else None
    compaction_passed = (not compaction or (result.status == "context_limit" and result.reason == "provider_cancelled"
                         if workflow["expected"]["provider_cancelled"] else result.status == "accepted"))
    scenario_passed = unavailable_reason == expected_unavailable and compaction_passed
    exact = recalled == source if observation is not None and not unavailable else None
    return {
        "workflow": workflow["id"], "split": workflow["split"], "control": control,
        "source_identity": _identity(source), "task_identity": _identity(workflow["task"]), "source_operations": 1,
        "quality_passed": preservation and scenario_passed and (source == "" or all(fact in available for fact in workflow["facts"])),
        "boundary_passed": boundary_passed, "exact_reconstruction": exact,
        "failure_preservation_passed": preservation, "scenario_passed": scenario_passed,
        "integrity_failure_verified": packing and workflow["kind"] == "corrupt" and unavailable_reason == "integrity_failed",
        "source_unavailable": unavailable, "source_unavailable_reason": unavailable_reason,
        "context_status": result.status if result else "unchanged",
        "context_reason": result.reason if result else "within_budget",
        "context_bytes_upper_bound_before": before,
        "context_bytes_upper_bound_after": result.after_tokens if result else before,
        "auxiliary_model_attempts": physical_calls, "reported_provider_cost": None,
        "complete_cost_known": False,
    }


async def _ready(value=None):
    return {"authorized": True, "authority": "synthetic-fixed"} if value is None else value


def _hex(value, size):
    return type(value) is str and re.fullmatch(rf"[a-f0-9]{{{size}}}", value) is not None


def _label(value, maximum=128):
    return (type(value) is str and 0 < len(value) <= maximum and "://" not in value
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", value) is not None)


def _number(value, maximum=2**63 - 1):
    return type(value) is int and 0 <= value <= maximum


def _amount(value, maximum=Decimal("1e30"), precision=18):
    if type(value) is not str or not value or len(value) > 64:
        raise ValueError
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError from None
    if not amount.is_finite() or not 0 <= amount <= maximum or amount.as_tuple().exponent < -precision:
        raise ValueError
    return amount


def _uuid(value):
    if type(value) is not str or len(value) != 36:
        raise ValueError
    parsed = UUID(value)
    if str(parsed) != value or parsed.int == 0:
        raise ValueError
    return value


def _route(value):
    if (type(value) is not dict or set(value) != {"provider", "model", "endpoint_digest", "configuration_digest"}
            or not _label(value["provider"]) or not _label(value["model"])
            or not _hex(value["endpoint_digest"], 64) or not _hex(value["configuration_digest"], 64)):
        raise ValueError
    return value


def _scope(candidate):
    return {"candidate": candidate, "implementation_digest": implementation_identity(),
            "workflows_digest": _identity(load_workflows()), "scoring": SCORING,
            "scoring_digest": _identity(SCORING_RULES)}


def _envelope(value, format_name, scope, extra_fields):
    if (type(value) is not dict or set(value) != {*_SCOPE_FIELDS, "format", "receipt_digest", *extra_fields}
            or value["format"] != format_name or not _hex(value["receipt_digest"], 64)
            or any(value[field] != scope[field] for field in _SCOPE_FIELDS)):
        raise ValueError


def _usage(value):
    if (type(value) is not dict or set(value) != {"prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens"}
            or any(not _number(count) for count in value.values())
            or value["total_tokens"] != value["prompt_tokens"] + value["completion_tokens"]
            or value["cached_tokens"] > value["prompt_tokens"]):
        raise ValueError
    return value


def _moment(value):
    if (type(value) is not str or len(value) > 64
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value) is None):
        raise ValueError
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _prices(value, measured_at):
    from datetime import date

    if (type(value) is not dict or set(value) != {"input_per_million", "output_per_million", "cached_input_per_million",
                                                "date", "currency", "source_digest"}
            or not _hex(value["source_digest"], 64) or type(value["currency"]) is not str
            or re.fullmatch(r"[A-Z]{3}", value["currency"]) is None
            or type(value["date"]) is not str):
        raise ValueError
    when = date.fromisoformat(value["date"])
    measured = _moment(measured_at)
    if when.isoformat() != value["date"] or when > measured.date():
        raise ValueError
    return {key: _amount(value[key], Decimal("1e9"), 12) for key in
            ("input_per_million", "output_per_million", "cached_input_per_million")}


def _attempt_cost(attempt, rates, route_digest, pricing_digest, previous, seen, receipts):
    if (type(attempt) is not dict or set(attempt) != {"attempt_id", "kind", "purpose", "outcome", "retry_of", "usage",
                                                   "provider_route_digest", "pricing_digest", "receipt_digest"}
            or attempt["kind"] not in {"direct", "auxiliary"} or not _label(attempt["purpose"], 64)
            or attempt["outcome"] not in {"success", "failure", "cancelled"}
            or attempt["provider_route_digest"] != route_digest or attempt["pricing_digest"] != pricing_digest
            or not _hex(attempt["receipt_digest"], 64)):
        raise ValueError
    identity = _uuid(attempt["attempt_id"])
    if identity in seen or attempt["receipt_digest"] in receipts:
        raise ValueError
    if attempt["retry_of"] is not None and previous.get(_uuid(attempt["retry_of"])) not in {"failure", "cancelled"}:
        raise ValueError
    seen.add(identity)
    previous[identity] = attempt["outcome"]
    receipts.add(attempt["receipt_digest"])
    usage = _usage(attempt["usage"])
    with localcontext() as context:
        context.prec = 64
        return ((usage["prompt_tokens"] - usage["cached_tokens"]) * rates["input_per_million"]
                + usage["cached_tokens"] * rates["cached_input_per_million"]
                + usage["completion_tokens"] * rates["output_per_million"]) / Decimal(1_000_000)


def _measurements(value, scope, provider_route, promoted_control):
    _envelope(value, MEASUREMENTS_FORMAT, scope,
              {"measurement_kind", "provider_route", "complete_cost_definition", "promoted_control", "pricing",
               "pricing_digest", "measured_at", "records", "attempt_inventory_digest", "repetitions"})
    route = _route(provider_route)
    if (value["measurement_kind"] != "real_provider" or value["provider_route"] != route
            or value["complete_cost_definition"] != COMPLETE_COST_DEFINITION or value["promoted_control"] != promoted_control
            or type(value["repetitions"]) is not int or value["repetitions"] != SCORING_RULES["repetitions"]
            or type(value["measured_at"]) is not str or len(value["measured_at"]) > 64
            or value["pricing_digest"] != _identity(value["pricing"])
            or type(value["records"]) is not list or len(value["records"]) != 96):
        raise ValueError
    rates = _prices(value["pricing"], value["measured_at"])
    expected = {(item["id"], control): item for item in load_workflows() for control in CONTROLS}
    observed, seen, receipts, inventory = set(), set(), set(), []
    costs = {control: Decimal(0) for control in CONTROLS}
    accepted = {control: 0 for control in CONTROLS}
    for record in value["records"]:
        if type(record) is not dict or set(record) != {"workflow", "split", "control", "source_identity", "task_identity", "quality_passed",
                "boundary_passed", "failure_preservation_passed", "accepted_task", "complete_cost_known",
                "reported_total_cost", "auxiliary_model_attempts", "attempts", "task_result", "source_operations",
                "recall_bytes", "non_model_cost", "non_model_cost_known", "operation_receipt_digest"}:
            raise ValueError
        pair = record["workflow"], record["control"]
        if pair not in expected or pair in observed:
            raise ValueError
        observed.add(pair)
        workflow = expected[pair]
        source = workflow["source"]
        task_result = {"facts": workflow["facts"], "outcome": workflow["expected"]["outcome"],
                       "source_refusal_reason": workflow["expected"]["packing_unavailable_reason"] if CONTROLS[pair[1]][0] else None}
        if (record["split"] != workflow["split"] or record["source_identity"] != _identity(source)
                or record["task_identity"] != _identity(workflow["task"])
                or type(record["task_result"]) is not dict or record["task_result"] != task_result
                or type(record["source_operations"]) is not int or record["source_operations"] != 1
                or not _number(record["recall_bytes"]) or not _hex(record["operation_receipt_digest"], 64)
                or any(record[field] is not True for field in ("quality_passed", "boundary_passed", "failure_preservation_passed",
                                                              "accepted_task", "complete_cost_known", "non_model_cost_known"))
                or type(record["attempts"]) is not list or not 1 <= len(record["attempts"]) <= 1024
                or not _number(record["auxiliary_model_attempts"], 1024)):
            raise ValueError
        total, auxiliary, previous = _amount(record["non_model_cost"]), 0, {}
        for attempt in record["attempts"]:
            amount = _attempt_cost(attempt, rates, _identity(route), value["pricing_digest"], previous, seen, receipts)
            with localcontext() as context:
                context.prec = 64
                total += amount
            auxiliary += attempt["kind"] == "auxiliary"
            inventory.append([*pair, attempt["attempt_id"]])
        if total != _amount(record["reported_total_cost"]) or auxiliary != record["auxiliary_model_attempts"]:
            raise ValueError
        if workflow["split"] == "held_out":
            with localcontext() as context:
                context.prec = 64
                costs[pair[1]] += total
            accepted[pair[1]] += 1
    if observed != set(expected) or value["attempt_inventory_digest"] != _identity(sorted(inventory)):
        raise ValueError
    with localcontext() as context:
        context.prec = 64
        averages = {control: costs[control] / accepted[control] for control in CONTROLS}
        baseline = averages["baseline"]
        candidate = averages[promoted_control]
        savings = None if baseline == 0 else (baseline - candidate) / baseline
        target_passed = baseline > 0 and costs[promoted_control] * accepted["baseline"] * 10 <= costs["baseline"] * accepted[promoted_control] * 9
    return {"currency": value["pricing"]["currency"], "pricing_date": value["pricing"]["date"],
            "cost_per_accepted_task": {control: str(averages[control]) for control in CONTROLS},
            "accepted_tasks": accepted, "saving_ratio": str(savings) if savings is not None else None,
            "target_passed": target_passed}


def _formative(value, scope):
    _envelope(value, FORMATIVE_FORMAT, scope, {"synthetic_sources", "representative_users", "participants"})
    if (value["synthetic_sources"] is not True or value["representative_users"] is not True
            or type(value["participants"]) is not list or len(value["participants"]) != 5):
        raise ValueError
    seen, receipts, successes, totals = set(), {value["receipt_digest"]}, 0, {key: 0 for key in ("corrections", "repeated_retrievals", "clarification_turns", "completed")}
    for item in value["participants"]:
        if (type(item) is not dict or set(item) != {"participant", "distinguished_preview", "recovered_omitted_fact",
                "without_assistance", "completed", "corrections", "repeated_retrievals", "clarification_turns", "receipt_digest"}
                or not _label(item["participant"], 64) or item["participant"] in seen
                or not _hex(item["receipt_digest"], 64)
                or item["receipt_digest"] in receipts
                or any(type(item[field]) is not bool for field in ("distinguished_preview", "recovered_omitted_fact", "without_assistance", "completed"))
                or any(not _number(item[field], 10_000) for field in ("corrections", "repeated_retrievals", "clarification_turns"))):
            raise ValueError
        seen.add(item["participant"])
        receipts.add(item["receipt_digest"])
        successes += all(item[field] for field in ("distinguished_preview", "recovered_omitted_fact", "without_assistance", "completed"))
        for field in totals:
            totals[field] += item[field]
    return {"participants": 5, "unaided_successes": successes, **totals, "target_passed": successes >= 4}


def _qualified(value, scope, provider_route, measured_at):
    _envelope(value, QUALIFICATION_FORMAT, scope,
              {"provider_route_digest", "qualified_at", "ordinary_gates_passed", "ordinary_gate_receipt_digest", "checks", "live_clients"})
    if (value["provider_route_digest"] != _identity(_route(provider_route))
            or not _hex(value["ordinary_gate_receipt_digest"], 64)
            or type(value["ordinary_gates_passed"]) is not bool
            or type(value["checks"]) is not dict or set(value["checks"]) != DETERMINISTIC_CHECKS
            or type(value["live_clients"]) is not list or len(value["live_clients"]) != 6):
        raise ValueError
    moment = _moment(value["qualified_at"])
    if measured_at is not None and moment < _moment(measured_at):
        raise ValueError
    seen, receipts, passed = set(), {value["receipt_digest"], value["ordinary_gate_receipt_digest"]}, value["ordinary_gates_passed"]
    if len(receipts) != 2:
        raise ValueError
    for check in value["checks"].values():
        if (type(check) is not dict or set(check) != {"passed", "receipt_digest"}
                or type(check["passed"]) is not bool or not _hex(check["receipt_digest"], 64)
                or check["receipt_digest"] in receipts):
            raise ValueError
        receipts.add(check["receipt_digest"])
        passed = passed and check["passed"]
    for item in value["live_clients"]:
        if (type(item) is not dict or set(item) != {"client", "candidate", "passed", "states", "dispatch_verified", "receipt_digest"}
                or item["client"] not in CLIENTS or item["client"] in seen or item["candidate"] != scope["candidate"]
                or type(item["passed"]) is not bool or type(item["dispatch_verified"]) is not bool
                or not _hex(item["receipt_digest"], 64)
                or item["receipt_digest"] in receipts
                or type(item["states"]) is not dict or set(item["states"]) != SOURCE_STATES
                or any(type(state) is not bool for state in item["states"].values())):
            raise ValueError
        seen.add(item["client"])
        receipts.add(item["receipt_digest"])
        passed = passed and item["passed"] and item["dispatch_verified"] and all(item["states"].values())
    return passed and seen == CLIENTS


def promotion(runs, *, candidate=None, provider_route=None, promoted_control="combined",
              provider_measurements=None, formative=None, qualification=None):
    failed, missing, costs, study = [], [], None, None
    selected = load_workflows()
    expected = {(item["id"], control): item for item in selected for control in CONTROLS}
    if type(runs) is not list or len(runs) != 96:
        failed.append("paired_workflows_incomplete")
    observed = set()
    for run in runs if type(runs) is list and len(runs) <= 96 else []:
        if type(run) is not dict:
            failed.append("paired_workflows_invalid")
            continue
        try:
            pair = run.get("workflow"), run.get("control")
            if pair not in expected or pair in observed:
                raise ValueError
            observed.add(pair)
            workflow = expected[pair]
            source = workflow["source"]
            if (run.get("split") != workflow["split"] or run.get("source_identity") != _identity(source)
                    or run.get("task_identity") != _identity(workflow["task"])):
                raise ValueError
        except (TypeError, ValueError):
            failed.append("paired_workflows_invalid")
        passed = all(run.get(field) is True for field in ("quality_passed", "scenario_passed", "failure_preservation_passed"))
        if not passed:
            failed.append("held_out_regression" if run.get("split") == "held_out" else "deterministic_regression")
        if run.get("exact_reconstruction") is False:
            failed.append("evidence_integrity_regression")
        if run.get("boundary_passed") is not True:
            failed.append("boundary_regression")
    if observed != set(expected):
        failed.append("paired_workflows_incomplete")
    if not _hex(candidate, 40) or type(promoted_control) is not str or promoted_control not in {"packing", "compaction", "combined"}:
        failed.append("evaluation_binding_invalid")
    scope = _scope(candidate)
    for evidence, name, validate in (
        (provider_measurements, "real_provider_measurements", lambda: _measurements(provider_measurements, scope, provider_route, promoted_control)),
        (formative, "five_user_formative_evidence", lambda: _formative(formative, scope)),
        (qualification, "product_and_client_qualification", lambda: _qualified(qualification, scope, provider_route,
            provider_measurements.get("measured_at") if type(provider_measurements) is dict else None)),
    ):
        if evidence is None:
            missing.append(name)
            continue
        try:
            result = validate()
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            failed.append(f"{name}_invalid")
            continue
        if name == "real_provider_measurements":
            costs = result
            if not costs["target_passed"]:
                failed.append("complete_cost_target_not_met")
        elif name == "five_user_formative_evidence":
            study = result
            if not study["target_passed"]:
                failed.append("formative_target_not_met")
        elif result is not True:
            failed.append("product_qualification_failed")
    return {"eligible": not failed and not missing, "failed": list(dict.fromkeys(failed)), "missing": missing,
            "promoted_control": promoted_control if type(promoted_control) is str and promoted_control in CONTROLS else None,
            "evidence_validation": "candidate-bound supplied receipts; actual execution and observations require external verification",
            "cost_comparison": costs, "formative_summary": study,
            "defaults_changed": False}


async def evaluate(*, candidate: str, workflows=None, provider_route=None, promoted_control="combined",
                   provider_measurements=None, formative=None, qualification=None):
    if type(candidate) is not str or re.fullmatch(r"[a-f0-9]{40}", candidate) is None:
        raise ValueError("evaluation_candidate_invalid")
    selected = load_workflows() if workflows is None else workflows
    _validate(selected)
    if _identity(selected) != _identity(load_workflows()):
        raise ValueError("evaluation_workflows_changed")
    implementation = implementation_identity()
    runs = [await _run(workflow, control) for workflow in selected for control in CONTROLS]
    if implementation_identity() != implementation:
        raise ValueError("evaluation_candidate_changed")
    try:
        measurement_route = dict(_route(provider_route))
    except ValueError:
        measurement_route = None
    return {
        "candidate": candidate, "implementation_digest": implementation,
        "workflows_digest": _identity(selected), "scoring": SCORING, "scoring_digest": _identity(SCORING_RULES),
        "provider_route": PROVIDER_ROUTE, "measurement_kind": "synthetic_capability",
        "measurement_provider_route": measurement_route,
        "runs": runs, "promotion": promotion(runs, candidate=candidate, provider_route=provider_route,
                                               promoted_control=promoted_control, provider_measurements=provider_measurements,
                                               formative=formative, qualification=qualification),
    }


def _unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError


def _read_measurements(path):
    limit = 16 * 1024 * 1024
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                raise ValueError
            content = source.read(limit + 1)
        if len(content) > limit:
            raise ValueError
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_fields, parse_constant=_invalid_constant)
        if (type(value) is not dict or not set(value) <= {"provider_route", "promoted_control", "provider_measurements",
                                                       "formative", "qualification"}):
            raise ValueError
        return value
    except (OSError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("evaluation_measurements_invalid") from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--measurements", type=Path)
    args = parser.parse_args()
    evidence = _read_measurements(args.measurements) if args.measurements else {}
    report = asyncio.run(evaluate(candidate=args.candidate, **evidence))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
