"""Verifies bounded operator context budgets against exact owner/provider identities.
The host rechecks this immutable binding before context and physical model dispatch.
"""

from dataclasses import FrozenInstanceError
import json
import os

import pytest

from orchestrator.context_budget import ContextBudgetUnavailable, load_budget


ROUTE = {"owner_id": "owner-a", "provider": "custom", "base_url": "https://provider.invalid/v1", "model": "model-a"}
ENTRY = {**ROUTE, "context_tokens": 32_768, "max_output_tokens": 4_096, "output_parameter": "max_tokens"}


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_exact_route_loads_immutable_bounded_context(tmp_path):
    path = write(tmp_path / "budgets.json", [ENTRY])
    budget = load_budget(path, **ROUTE)
    assert budget.context_tokens == 32_768
    assert budget.max_output_tokens == 4_096
    assert budget.output_parameter == "max_tokens"
    assert len(budget.fingerprint) == 64
    with pytest.raises(FrozenInstanceError):
        budget.max_output_tokens = 8_192


def test_unrelated_qualified_route_does_not_change_selected_fingerprint(tmp_path):
    path = write(tmp_path / "budgets.json", [ENTRY])
    selected = load_budget(path, **ROUTE)
    write(path, [{**ENTRY, "owner_id": "owner-b"}, dict(reversed(list(ENTRY.items())))])
    assert load_budget(path, **ROUTE) == selected


@pytest.mark.parametrize("field,value", [
    ("owner_id", "owner-b"), ("provider", "CUSTOM"),
    ("base_url", "https://provider.invalid/v1/"), ("model", "model-b"),
])
def test_foreign_or_normalized_alias_routes_fail_closed(tmp_path, field, value):
    path = write(tmp_path / "budgets.json", [ENTRY])
    with pytest.raises(ContextBudgetUnavailable, match="^context_budget_unavailable$"):
        load_budget(path, **{**ROUTE, field: value})


def test_ambiguous_and_missing_bindings_never_choose_a_fallback(tmp_path):
    path = tmp_path / "budgets.json"
    for entries in ([], [ENTRY, ENTRY], [{**ENTRY, "owner_id": "other-owner"}]):
        write(path, entries)
        with pytest.raises(ContextBudgetUnavailable):
            load_budget(path, **ROUTE)


def test_operator_edit_changes_the_binding_fingerprint(tmp_path):
    path = write(tmp_path / "budgets.json", [ENTRY])
    before = load_budget(path, **ROUTE)
    write(path, [{**ENTRY, "output_parameter": "max_completion_tokens", "max_output_tokens": 1_024}])
    after = load_budget(path, **ROUTE)
    assert after.fingerprint != before.fingerprint
    assert after.output_parameter == "max_completion_tokens"
    assert after.max_output_tokens == 1_024


@pytest.mark.parametrize("field,value", [
    ("context_tokens", True), ("context_tokens", 1_023), ("context_tokens", 2_000_001),
    ("context_tokens", "32768"), ("max_output_tokens", True), ("max_output_tokens", 0),
    ("max_output_tokens", 8_193), ("max_output_tokens", 32_768),
    ("output_parameter", "max_tokens_from_user"), ("output_parameter", []),
    ("owner_id", None), ("provider", ""), ("base_url", "x" * 2_049),
    ("model", "🌌" * 513), ("model", "\ud800"),
])
def test_invalid_values_reject_the_entire_operator_document(tmp_path, field, value):
    path = write(tmp_path / "budgets.json", [{**ENTRY, field: value}])
    with pytest.raises(ContextBudgetUnavailable, match="^context_budget_unavailable$"):
        load_budget(path, **ROUTE)


@pytest.mark.parametrize("field,value", [("owner_id", " \t"), ("provider", "cus\ntom"), ("model", "model\x7fa")])
def test_blank_and_control_bearing_identities_are_not_qualified_routes(tmp_path, field, value):
    path = write(tmp_path / "budgets.json", [{**ENTRY, field: value}])
    with pytest.raises(ContextBudgetUnavailable):
        load_budget(path, **{**ROUTE, field: value})


@pytest.mark.parametrize("document", [None, {}, [None], [{key: value for key, value in ENTRY.items() if key != "model"}], [{**ENTRY, "api_key": "excluded"}]])
def test_malformed_or_extra_configuration_fields_fail_closed(tmp_path, document):
    path = write(tmp_path / "budgets.json", document)
    with pytest.raises(ContextBudgetUnavailable) as caught:
        load_budget(path, **ROUTE)
    assert "excluded" not in str(caught.value)


def test_an_invalid_unrelated_entry_does_not_get_silently_ignored(tmp_path):
    path = write(tmp_path / "budgets.json", [ENTRY, {**ENTRY, "owner_id": "other", "context_tokens": 0}])
    with pytest.raises(ContextBudgetUnavailable):
        load_budget(path, **ROUTE)


def test_duplicate_json_fields_are_rejected_before_binding(tmp_path):
    path = tmp_path / "budgets.json"
    path.write_text(json.dumps([ENTRY]).replace('"owner_id": "owner-a"', '"owner_id": "owner-a", "owner_id": "owner-a"'))
    with pytest.raises(ContextBudgetUnavailable):
        load_budget(path, **ROUTE)


def test_missing_directory_and_nonregular_policy_files_fail_closed(tmp_path):
    for path in (tmp_path / "missing.json", tmp_path):
        with pytest.raises(ContextBudgetUnavailable):
            load_budget(path, **ROUTE)
    pipe = tmp_path / "policy-pipe"
    os.mkfifo(pipe)
    with pytest.raises(ContextBudgetUnavailable):
        load_budget(pipe, **ROUTE)


@pytest.mark.parametrize("body", [b"[invalid", b"\xff", b"[" * 1_100 + b"]" * 1_100])
def test_unreadable_policy_encoding_json_and_recursion_fail_closed(tmp_path, body):
    path = tmp_path / "budgets.json"
    path.write_bytes(body)
    with pytest.raises(ContextBudgetUnavailable):
        load_budget(path, **ROUTE)


def test_file_and_entry_capacity_boundaries_are_enforced(tmp_path):
    path = tmp_path / "budgets.json"
    payload = json.dumps([ENTRY]).encode()
    path.write_bytes(payload + b" " * (1024 * 1024 - len(payload)))
    assert load_budget(path, **ROUTE).context_tokens == ENTRY["context_tokens"]
    path.write_bytes(payload + b" " * (1024 * 1024 + 1 - len(payload)))
    with pytest.raises(ContextBudgetUnavailable):
        load_budget(path, **ROUTE)
    write(path, [{**ENTRY, "owner_id": f"other-{index}"} for index in range(256)] + [ENTRY])
    with pytest.raises(ContextBudgetUnavailable):
        load_budget(path, **ROUTE)


def test_smallest_and_largest_qualified_bounds_load(tmp_path):
    path = tmp_path / "budgets.json"
    for window, output in ((1_024, 512), (2_000_000, 8_192)):
        write(path, [{**ENTRY, "context_tokens": window, "max_output_tokens": output}])
        budget = load_budget(path, **ROUTE)
        assert (budget.context_tokens, budget.max_output_tokens) == (window, output)
