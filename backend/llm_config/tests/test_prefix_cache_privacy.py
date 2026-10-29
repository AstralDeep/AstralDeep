"""Regression probes for complete-prefix approvals and stale cache decisions. They exercise policy behavior without provider calls."""

import copy
from dataclasses import replace

import pytest

from llm_config.prompt_prefix_cache import (
    NATIVE_MESSAGES_BASE_URL,
    ApprovedPrefix,
    PromptCachePolicy,
    apply_cache_markers,
    canonical_digest,
    canonical_tools_digest,
)


def test_known_tool_name_does_not_approve_owner_private_description():
    policy = PromptCachePolicy((ApprovedPrefix("public-core", canonical_digest("public instructions")),))
    decision = policy.evaluate(
        enabled=True, base_url=NATIVE_MESSAGES_BASE_URL, model="claude-sonnet-4-5",
        static_prefix="public instructions", system_content="public instructions",
        tool_defs=[{"type": "function", "function": {
            "name": "graph_data", "description": "OWNER PRIVATE NOTE: patient secret",
            "parameters": {"type": "object", "properties": {}},
        }}],
        prefix_token_estimator=lambda *_: 5000,
    )
    assert decision.enabled is False


def test_cached_decision_cannot_mark_changed_tool_bytes():
    policy = PromptCachePolicy((ApprovedPrefix("public-core", canonical_digest("public instructions")),))
    decision = policy.evaluate(
        enabled=True, base_url=NATIVE_MESSAGES_BASE_URL, model="claude-sonnet-4-5",
        static_prefix="public instructions", system_content="public instructions",
        prefix_token_estimator=lambda *_: 5000,
    )
    payload = {
        "system": [{"type": "text", "text": "public instructions"}],
        "tools": [{"name": "graph_data", "description": "owner private description", "input_schema": {}}],
    }
    marked = apply_cache_markers(payload, decision, policy=policy)
    assert "cache_control" not in marked["system"][0]


def test_offline_cache_simulation_does_not_share_owner_entries():
    from llm_config.prefix_cache_synthetic import SyntheticRequest, simulate

    report = simulate([
        SyntheticRequest(0, "owner-a", "public", 2000, 100, 50, True),
        SyntheticRequest(1, "owner-b", "public", 2000, 100, 50, True),
    ], fixture_id="owner-bound")
    assert report.hits == 0
    assert report.writes == 2


def test_missing_cache_usage_does_not_claim_known_prompt_usage():
    from llm_config.native_messages import translate_native_response

    response = translate_native_response({
        "content": [{"type": "text", "text": "offline response"}],
        "usage": {"input_tokens": 100, "output_tokens": 20},
    })
    assert response.usage.prompt_tokens is None
    assert response.usage.total_tokens is None

PUBLIC_CORE = "public instructions"
PUBLIC_DIGEST = "b02af0591ad79b3be3447bad3c79e6a3856252906649509c735c779f9ab2bc6c"
PUBLIC_TOOLS = [{"name": "graph_data", "description": "Public plotting tool.",
                 "input_schema": {"type": "object", "properties": {}}}]
PUBLIC_TOOLS_DIGEST = "dd0d90a5297c7e418ba44e20f56da2fb399672a7da069bb86c9d98582170f131"


def public_policy(**kwargs):
    return PromptCachePolicy((ApprovedPrefix("public-core", PUBLIC_DIGEST, PUBLIC_TOOLS_DIGEST),), **kwargs)


def evaluate(policy=None, **kwargs):
    values = dict(enabled=True, base_url=NATIVE_MESSAGES_BASE_URL, model="claude-sonnet-4-5",
                  static_prefix=PUBLIC_CORE, system_content=PUBLIC_CORE + "\nprivate owner details",
                  tool_defs=copy.deepcopy(PUBLIC_TOOLS), prefix_token_estimator=lambda *_: 5000)
    values.update(kwargs)
    return (policy or public_policy()).evaluate(**values)


def public_payload():
    return {"model": "claude-sonnet-4-5", "tools": copy.deepcopy(PUBLIC_TOOLS),
            "system": [{"type": "text", "text": PUBLIC_CORE},
                       {"type": "text", "text": "owner private clinical details"}],
            "messages": [{"role": "user", "content": "owner private message"}]}


def test_approved_complete_prefix_marks_only_public_bytes_and_does_not_mutate_input():
    policy = public_policy()
    decision = evaluate(policy)
    assert decision.enabled
    payload = public_payload()
    marked = apply_cache_markers(payload, decision, policy=policy)
    assert marked["system"][0] == {"type": "text", "text": PUBLIC_CORE, "cache_control": {"type": "ephemeral"}}
    assert marked["tools"][0]["cache_control"] == {"type": "ephemeral"}
    assert marked["system"][1] == payload["system"][1]
    assert marked["messages"] == payload["messages"]
    assert "cache_control" not in payload["tools"][0]
    assert canonical_tools_digest(PUBLIC_TOOLS) == PUBLIC_TOOLS_DIGEST


@pytest.mark.parametrize("field,value", [
    ("description", "OWNER PRIVATE NOTE: patient secret"),
    ("name", "owner_skill"),
    ("input_schema", {"type": "object", "properties": {"owner_secret": {"const": "patient secret"}}}),
    ("private_metadata", "patient secret"),
])
def test_definition_changes_deny_cached_prefix_even_with_approved_tool_name(field, value):
    tools = copy.deepcopy(PUBLIC_TOOLS)
    tools[0][field] = value
    assert not evaluate(tool_defs=tools).enabled
    payload = public_payload()
    payload["tools"] = tools
    marked = apply_cache_markers(payload, evaluate(), policy=public_policy())
    assert "cache_control" not in marked["system"][0]
    assert "cache_control" not in marked["tools"][0]


@pytest.mark.parametrize("tools", [False, 1, "tools", {}, [None], [{"function": None}],
    [{"name": "n", "input_schema": {}}], [{"name": "n", "input_schema": {"type": "array"}}],
    [{"name": "n", "input_schema": {"type": "object", "properties": []}}],
    [{"name": "n", "input_schema": {"type": "object"}, "description": float("nan")}],
    [{"name": "n", "input_schema": {"type": "object"}, "description": {1: "private"}}],
    [{"name": "n", "input_schema": {"type": "object"}, "description": object()}],
    [{"name": "n", "input_schema": {"type": "object"}, "cache_control": {"type": "ephemeral"}}],
    [{"name": "n", "input_schema": {"type": "object"}, "description": "x" * 65_537}],
])
def test_malformed_or_unbounded_tools_never_qualify(tools):
    assert canonical_tools_digest(tools) is None
    assert not evaluate(tool_defs=tools).enabled


def test_duplicate_and_recursive_definitions_fail_closed():
    assert canonical_tools_digest(PUBLIC_TOOLS * 2) is None
    recursive = copy.deepcopy(PUBLIC_TOOLS)
    recursive[0]["self"] = recursive
    assert canonical_tools_digest(recursive) is None
    assert not evaluate(tool_defs=recursive).enabled


def test_complete_list_digest_rejects_added_unapproved_tool_and_reordered_tools():
    tools = PUBLIC_TOOLS + [{"name": "other", "description": "public", "input_schema": {"type": "object"}}]
    assert not evaluate(tool_defs=tools).enabled
    policy = PromptCachePolicy((ApprovedPrefix("two-public-tools", PUBLIC_DIGEST, canonical_tools_digest(tools)),))
    assert evaluate(policy, tool_defs=tools).enabled
    assert not evaluate(policy, tool_defs=list(reversed(tools))).enabled


def test_canonical_digest_ignores_key_order_but_preserves_full_values():
    tools = [{"input_schema": {"properties": {}, "type": "object"},
              "description": "Public plotting tool.", "name": "graph_data"}]
    assert evaluate(tool_defs=tools).enabled
    assert canonical_tools_digest(tools) == PUBLIC_TOOLS_DIGEST
    assert canonical_tools_digest(tuple(tools)) == PUBLIC_TOOLS_DIGEST


@pytest.mark.parametrize("value", [None, -1, True, 1.5, "5000"])
def test_missing_or_malformed_admission_keeps_cache_disabled(value):
    estimator = None if value is None else lambda *_: value
    decision = evaluate(prefix_token_estimator=estimator)
    assert not decision.enabled
    assert decision.reason == "admission_uncertain"


def test_failed_admission_and_below_minimum_remain_uncached():
    def failure(*_):
        raise RuntimeError("native accounting unavailable")
    assert evaluate(prefix_token_estimator=failure).reason == "admission_uncertain"
    assert evaluate(prefix_token_estimator=lambda *_: 1000).reason == "below_minimum"
    decision = evaluate(model="claude-opus-4-1", prefix_token_estimator=lambda *_: 1500)
    assert decision.reason == "below_minimum"
    assert decision.minimum_prefix_tokens == 2048


@pytest.mark.parametrize("changes,reason", [
    ({"enabled": False}, "policy_disabled"), ({"enabled": "true"}, "policy_disabled"),
    ({"base_url": "https://proxy.example/v1"}, "profile_not_native"),
    ({"model": None}, "profile_not_native"), ({"static_prefix": ""}, "no_approved_prefix"),
    ({"static_prefix": None}, "no_approved_prefix"), ({"system_content": None}, "prefix_mismatch"),
    ({"system_content": "private before public instructions"}, "prefix_mismatch"),
    ({"static_prefix": "public instructions changed"}, "prefix_mismatch"),
    ({"tool_defs": []}, "prefix_not_fully_approved"),
])
def test_denial_keeps_cache_markers_out(changes, reason):
    decision = evaluate(**changes)
    assert not decision.enabled and decision.reason == reason
    assert apply_cache_markers(public_payload(), decision, policy=public_policy()) == public_payload()


@pytest.mark.parametrize("entry", [ApprovedPrefix("", PUBLIC_DIGEST),
    ApprovedPrefix("public", "x" * 64), ApprovedPrefix("public", PUBLIC_DIGEST, "bad"), None])
def test_malformed_approval_records_are_rejected(entry):
    with pytest.raises(ValueError, match="malformed approved"):
        PromptCachePolicy((entry,))


@pytest.mark.parametrize("minimum", [True, -1, "1000"])
def test_malformed_minimum_is_rejected(minimum):
    with pytest.raises(ValueError, match="malformed minimum"):
        public_policy(minimum_prefix_tokens=minimum)


def test_policy_revision_binds_complete_entries_and_admission_and_invalidates_decisions():
    entries = (ApprovedPrefix("public", PUBLIC_DIGEST, PUBLIC_TOOLS_DIGEST),
               ApprovedPrefix("another", "a" * 64, "b" * 64))
    policy = PromptCachePolicy(entries)
    assert policy.policy_revision == PromptCachePolicy(tuple(reversed(entries))).policy_revision
    assert policy.policy_revision != PromptCachePolicy((replace(entries[0], tools_digest="b" * 64), entries[1])).policy_revision
    changed_minimum = public_policy(minimum_prefix_tokens=100)
    assert changed_minimum.policy_revision != public_policy().policy_revision
    assert apply_cache_markers(public_payload(), evaluate(), policy=changed_minimum) == public_payload()
    assert apply_cache_markers(public_payload(), evaluate(), policy=PromptCachePolicy()) == public_payload()
    with pytest.raises(ValueError):
        PromptCachePolicy((entries[0], entries[0]))


@pytest.mark.parametrize("change", ["model", "first_block", "extra_first_field", "no_system"])
def test_changed_final_payload_cannot_reuse_approved_decision(change):
    payload = public_payload()
    if change == "model":
        payload["model"] = "different-model"
    elif change == "first_block":
        payload["system"].insert(0, {"type": "text", "text": "private context"})
    elif change == "extra_first_field":
        payload["system"][0]["private"] = "owner"
    else:
        del payload["system"]
    assert apply_cache_markers(payload, evaluate(), policy=public_policy()) == payload


def test_forged_static_core_and_imported_markers_do_not_bypass_final_validation():
    policy = public_policy()
    payload = public_payload()
    decision = replace(evaluate(), static_core="private core")
    payload["system"][0]["text"] = "private core"
    assert apply_cache_markers(payload, decision, policy=policy) == payload
    poisoned = public_payload()
    poisoned["cache_control"] = {"type": "ephemeral"}
    poisoned["system"][1]["cache_control"] = {"type": "ephemeral"}
    poisoned["messages"][0]["cache_control"] = {"type": "ephemeral"}
    marked = apply_cache_markers(poisoned, evaluate(), policy=policy)
    assert "cache_control" not in marked
    assert "cache_control" not in marked["system"][1]
    assert "cache_control" not in marked["messages"][0]
    assert marked["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_no_tools_approval_cannot_cover_tools_added_before_system_marker():
    policy = PromptCachePolicy((ApprovedPrefix("system-only", PUBLIC_DIGEST),))
    decision = evaluate(policy, tool_defs=None)
    assert decision.enabled
    payload = public_payload()
    assert apply_cache_markers(payload, decision, policy=policy) == payload
    del payload["tools"]
    marked = apply_cache_markers(payload, decision, policy=policy)
    assert marked["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in marked["system"][1]


@pytest.mark.parametrize("changed", [{"provider": "other"}, {"model": "other"}, {"config_revision": "next"}])
def test_simulated_entries_invalidate_on_provider_model_or_config_changes(changed):
    from llm_config.prefix_cache_synthetic import SyntheticRequest, simulate

    first = SyntheticRequest(0, "owner-a", "public", 2000, 100, 50, True)
    second = replace(first, at_seconds=1, **changed)
    report = simulate([first, second], fixture_id="configuration-bound")
    assert report.hits == 0 and report.writes == 2


def test_prefix_cache_flag_remains_default_off(monkeypatch):
    from shared.feature_flags import FeatureFlags

    monkeypatch.delenv("FF_PROMPT_PREFIX_CACHE", raising=False)
    assert not FeatureFlags().is_enabled("prompt_prefix_cache")


def test_offline_simulation_refreshes_ttl_on_a_read():
    from llm_config.prefix_cache_synthetic import SyntheticRequest, simulate

    report = simulate([
        SyntheticRequest(0, "owner-a", "public", 2000, 100, 50, True),
        SyntheticRequest(250, "owner-a", "public", 2000, 100, 50, True),
        SyntheticRequest(500, "owner-a", "public", 2000, 100, 50, True),
    ], fixture_id="refresh-on-read")
    assert report.hits == 2 and report.writes == 1 and report.expired == 0


def test_malformed_decision_cannot_raise_or_mark_private_payload():
    payload = public_payload()
    payload["system"][0]["text"] = None
    marked = apply_cache_markers(payload, replace(evaluate(), static_core=None), policy=public_policy())
    assert marked == payload
