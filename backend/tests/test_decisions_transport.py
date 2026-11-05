"""Exercises custom System One requests, typed answers, and failure boundaries through the approved egress seam."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from orchestrator.typesafe_routing import custom
from orchestrator.typesafe_routing.client import load_sdk
from shared import external_http

MODEL = "bespokelabs/Bespoke-Nimble-9B"
BASE = "https://factory.example/v1"


def _response(answers, status=200):
    return SimpleNamespace(status_code=status, content=json.dumps({"model": MODEL, "answers": answers}).encode())


@pytest.fixture
def endpoint(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _response({"ready": {"type": "noul", "noul": 0.9}})

    monkeypatch.setattr(external_http, "request", request)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["https://factory.example", "https://factory.example/v1", "https://factory.example/v1/"])
async def test_keyless_system_one_uses_versioned_endpoint_and_approved_egress(endpoint, base):
    client = custom.CustomDecisionClient(base, MODEL)
    response = await client.system_one(api_key="", state={"message": "hello"},
        questions={"ready": load_sdk().noul(instructions="Is this a greeting?")}, timeout=0.4)
    method, url, options = endpoint[0]
    assert method == "POST"
    assert url == "https://factory.example/v1/systemone"
    assert options["api_key"] == ""
    assert options["json_body"] == {"model": MODEL, "state": {"message": "hello"},
        "questions": {"ready": {"type": "noul", "instructions": "Is this a greeting?"}}}
    assert options["allow_redirects"] is False
    assert options["trust_environment"] is False
    assert options["max_response_bytes"] == 2 * 1024 * 1024
    assert response.nouls["ready"].noul == 0.9


@pytest.mark.asyncio
async def test_supplied_key_and_all_question_types_are_preserved(monkeypatch, endpoint):
    sdk = load_sdk()

    def request(method, url, **kwargs):
        endpoint.append((method, url, kwargs))
        return _response({"flag": {"type": "noul", "noul": 0.1},
            "route": {"type": "choice", "choice": "a", "confidence": 0.9,
                "probabilities": {"a": 0.9, "b": 0.1}},
            "risk": {"type": "score", "score": 0.2}})

    monkeypatch.setattr(external_http, "request", request)
    client = custom.CustomDecisionClient(BASE, MODEL)
    response = await client.system_one(api_key="test-key", state="text", questions={
        "flag": sdk.noul(), "route": sdk.choice(criteria={"a": None, "b": "other"}),
        "risk": sdk.score(criteria=["safe", "risky"])}, timeout=1)
    assert endpoint[0][2]["api_key"] == "test-key"
    assert endpoint[0][2]["json_body"]["questions"]["route"]["criteria"] == {"a": None, "b": "other"}
    assert response.choices["route"].choice == "a"
    assert response.scores["risk"].score == 0.2


@pytest.mark.parametrize("base", ["file:///etc/passwd", "http://factory.example/v1", "https://user:pass@factory.example", "https://factory.example?key=secret", "https://factory.example/#secret", "https://factory.example/\n", "", "https://factory.example:0"])
def test_unsafe_endpoint_is_rejected_before_dispatch(base):
    with pytest.raises(custom.CustomDecisionError):
        custom.CustomDecisionClient(base, MODEL)


@pytest.mark.asyncio
async def test_http_provider_is_refused_at_runtime_without_egress(monkeypatch):
    def request(*args, **kwargs):
        pytest.fail("an HTTP decisions URL reached external egress")

    monkeypatch.setattr(external_http, "request", request)
    with pytest.raises(custom.CustomDecisionError, match="HTTPS"):
        await custom.probe_custom(base_url="http://factory.example/v1", model=MODEL,
            api_key="test-only-key")


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [None, {"type": "noul", "noul": float("nan")},
    {"type": "noul", "noul": 1.1}, {"type": "choice", "choice": "yes", "confidence": 1}])
async def test_missing_malformed_or_wrong_type_answer_is_rejected(monkeypatch, answer):
    monkeypatch.setattr(external_http, "request", lambda *a, **k: _response({"ready": answer} if answer else {}))
    with pytest.raises(custom.CustomDecisionError, match="valid System One"):
        await custom.CustomDecisionClient(BASE, MODEL).system_one(api_key="", state="hello",
            questions={"ready": {"type": "noul"}}, timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,status", [(external_http.AuthFailedError("secret body"), 401),
    (external_http.RateLimitedError("secret body"), 429),
    (external_http.ServiceUnreachableError("secret URL"), 503),
    (external_http.BadRequestError("secret body"), 400),
    (external_http.EgressBlockedError("secret URL"), None)])
async def test_http_errors_keep_status_without_provider_body(monkeypatch, error, status):
    def request(*args, **kwargs):
        raise error

    monkeypatch.setattr(external_http, "request", request)
    with pytest.raises(custom.CustomDecisionError) as failure:
        await custom.CustomDecisionClient(BASE, MODEL).system_one(api_key="", state="hello",
            questions={"ready": {"type": "noul"}}, timeout=1)
    assert failure.value.status_code == status
    assert "secret" not in str(failure.value)


@pytest.mark.asyncio
async def test_deadline_cancels_wait_and_is_retryable(monkeypatch):
    async def thread(*args, **kwargs):
        await asyncio.Future()

    monkeypatch.setattr(asyncio, "to_thread", thread)
    with pytest.raises(custom.CustomDecisionError) as failure:
        await custom.CustomDecisionClient(BASE, MODEL).system_one(api_key="", state="hello",
            questions={"ready": {"type": "noul"}}, timeout=0.001)
    assert failure.value.status_code == 408


@pytest.mark.asyncio
async def test_probe_sends_minimal_system_one_question_and_closes(endpoint):
    await custom.probe_custom(base_url=BASE, model=MODEL)
    assert endpoint[0][2]["json_body"]["questions"]["ready"]["type"] == "noul"
    assert endpoint[0][2]["json_body"]["model"] == MODEL
    client = custom.CustomDecisionClient(BASE, MODEL)
    await client.aclose()
    with pytest.raises(custom.CustomDecisionError, match="closed"):
        await client.system_one(api_key="", state="hello", questions={"ready": {"type": "noul"}}, timeout=1)


@pytest.mark.asyncio
async def test_large_payload_is_rejected_without_egress(endpoint):
    with pytest.raises(custom.CustomDecisionError, match="too large"):
        await custom.CustomDecisionClient(BASE, MODEL).system_one(api_key="", state="x" * (1024 * 1024),
            questions={"ready": {"type": "noul"}}, timeout=1)
    assert endpoint == []
