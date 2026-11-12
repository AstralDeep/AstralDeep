"""Tests for retry delay, the per-turn fallback chain, and their wiring into the model call."""

import pytest
from orchestrator import llm_recovery
from tests.helpers.llm_faults import FaultyCompletions, bare_orch, no_retry_sleep, reply, status

USER = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_MODELS", raising=False)
    monkeypatch.setattr("orchestrator.model_router.router_enabled", lambda: False)
    llm_recovery.reset_turn()
    yield
    llm_recovery.reset_turn()


def models(comp):
    return [c["model"] for c in comp.calls]


def test_fallback_models_parses_the_list(monkeypatch):
    assert llm_recovery.fallback_models("m1") == []
    monkeypatch.setenv("LLM_FALLBACK_MODELS", " m2, m1 ,m2,,m3,m4,m5")
    assert llm_recovery.fallback_models("m1") == ["m2", "m3", "m4"]


@pytest.mark.parametrize("code, cls, expected", [
    (503, "other", True), (429, "rate_limit", True), (None, "transport_error", True),
    (404, "model_not_found", True), (401, "auth_failed", False), (403, "auth_failed", False),
    (400, "other", False), (None, "other", False)])
def test_fallback_eligible(code, cls, expected):
    assert llm_recovery.fallback_eligible(code, cls) is expected


def test_retry_delay():
    assert llm_recovery.retry_delay(1, None, 1.0) == 1.0
    assert llm_recovery.retry_delay(3, None, 1.0) == 4.0
    assert llm_recovery.retry_delay(9, None, 1.0) == 8.0
    assert llm_recovery.retry_delay(1, 12.0, 1.0) == 12.0
    assert llm_recovery.retry_delay(1, 500.0, 1.0) == 60.0


async def test_no_chain_configured_behaves_as_before(monkeypatch):
    no_retry_sleep(monkeypatch)
    comp = FaultyCompletions([status(503)])
    assert await bare_orch(comp)._call_llm(None, USER) == (None, None)
    assert models(comp) == ["m1", "m1", "m1"]


async def test_falls_back_after_retries_are_exhausted(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "m2")
    no_retry_sleep(monkeypatch)
    comp = FaultyCompletions(by_model={"m1": [status(503)], "m2": [reply("ok")]})
    orch = bare_orch(comp)
    msg, _usage = await orch._call_llm(None, USER)
    assert msg.content == "ok"
    assert models(comp) == ["m1", "m1", "m1", "m2"]
    assert [(a["routed_model"], a["outcome"]) for a in orch._audits] == [("m1", "failure"), ("m2", "success")]


async def test_model_not_found_falls_back_without_retrying(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "m2")
    no_retry_sleep(monkeypatch)
    comp = FaultyCompletions(by_model={"m1": [status(404)], "m2": [reply("ok")]})
    msg, _usage = await bare_orch(comp)._call_llm(None, USER)
    assert msg.content == "ok" and models(comp) == ["m1", "m2"]


@pytest.mark.parametrize("code", [400, 401, 403])
async def test_auth_and_bad_request_never_fall_back(monkeypatch, code):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "m2")
    no_retry_sleep(monkeypatch)
    comp = FaultyCompletions(by_model={"m1": [status(code)], "m2": [reply("ok")]})
    assert await bare_orch(comp)._call_llm(None, USER) == (None, None)
    assert models(comp) == ["m1"]


async def test_fallback_sticks_for_the_turn_and_resets_with_it(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "m2")
    no_retry_sleep(monkeypatch)
    comp = FaultyCompletions(by_model={"m1": [status(503)], "m2": [reply("ok")]})
    orch = bare_orch(comp)
    await orch._call_llm(None, USER)
    await orch._call_llm(None, USER)
    assert models(comp)[-1] == "m2" and models(comp).count("m1") == 3
    llm_recovery.reset_turn()
    await orch._call_llm(None, USER)
    assert models(comp).count("m1") == 6


async def test_chain_exhausted_returns_none(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "m2,m3")
    no_retry_sleep(monkeypatch)
    comp = FaultyCompletions([status(503)])
    assert await bare_orch(comp)._call_llm(None, USER) == (None, None)
    assert models(comp) == ["m1"] * 3 + ["m2"] * 3 + ["m3"] * 3


async def test_fallback_rereads_unsupported_params_for_the_new_model(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "m2")
    no_retry_sleep(monkeypatch)
    comp = FaultyCompletions(by_model={"m1": [status(503)], "m2": [reply("ok")]})
    orch = bare_orch(comp)
    orch._llm_unsupported_params = {("https://ep/v1", "m2"): {"reasoning_effort"}}
    await orch._call_llm(None, USER, reasoning_effort="low")
    assert comp.calls[0]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in comp.calls[-1]


async def test_notice_is_sent_once_when_the_fallback_engages(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "m2")
    no_retry_sleep(monkeypatch)
    comp = FaultyCompletions(by_model={"m1": [status(503)], "m2": [reply("ok")]})
    orch = bare_orch(comp)
    await orch._call_llm(object(), USER)
    await orch._call_llm(object(), USER)
    notices = [r[0]["message"] for r in orch._rendered]
    assert notices == ["m1 did not respond. This turn is using m2."]


async def test_retry_after_sets_the_delay(monkeypatch):
    delays = no_retry_sleep(monkeypatch)
    comp = FaultyCompletions([status(429, retry_after=7), reply("ok")])
    msg, _usage = await bare_orch(comp)._call_llm(None, USER)
    assert msg.content == "ok" and delays == [7.0]


@pytest.mark.parametrize("headers, expected", [
    ({"retry-after": "7"}, 7.0), ({"retry-after": "-1"}, None), ({"retry-after": "inf"}, None),
    ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, None), ({}, None)])
def test_retry_after_seconds(headers, expected):
    error = Exception("x")
    error.response = type("R", (), {"headers": headers})()
    assert llm_recovery.retry_after_seconds(error) == expected


def test_retry_after_seconds_without_a_response():
    assert llm_recovery.retry_after_seconds(Exception("x")) is None
