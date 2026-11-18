"""Exercises canonical clinical placement with the normal host graph and durable context accounting.
Synthetic IAM and bounded provider transports remain developer tests, not clinical or staging qualification.
"""

import asyncio
import importlib
import importlib.util
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agents.fhir.fhir_agent import FhirAgent
from agents.fhir.tests.test_extraction import NOTE, NOTE_ID, golden_payload
from orchestrator.evidence_context import EvidenceContext
from shared.feature_flags import flags
from shared.isolated_http import IsolatedHttpResponse
from shared.protocol import RegisterAgent


def clinical_module():
    assert importlib.util.find_spec("orchestrator.clinical_extraction") is not None
    return importlib.import_module("orchestrator.clinical_extraction")


@pytest.fixture
async def host(orchestrator_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "clinical-test-private-binding-" + uuid4().hex)
    for name in (
        "fhir",
        "clinical_note_extract",
        "inprocess_agents",
        "observation_packing",
    ):
        monkeypatch.setitem(flags._flags, name, True)
    monkeypatch.setitem(flags._flags, "user_skills", False)
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "agent.pem"))
    agent_api_key = "clinical-host-registration-" + uuid4().hex
    monkeypatch.setenv("AGENT_API_KEY", agent_api_key)
    monkeypatch.delenv("DEFAULT_AGENT_OWNER", raising=False)
    orch = await asyncio.to_thread(orchestrator_factory)
    agent = FhirAgent(port=0)
    await orch.register_agent(None, RegisterAgent(agent_card=agent.card, api_key=agent_api_key))
    orch.local_agents[agent.agent_id] = agent
    orch._evidence_context = EvidenceContext(orch)
    return SimpleNamespace(orch=orch, agent=agent)


@pytest.mark.asyncio
async def test_canonical_host_service_registers_exact_fhir_implementation(host):
    module = clinical_module()
    assert module.register(host.orch) is True
    assert module.current(host.orch) is True
    host.orch.local_agents["fhir-1"] = object()
    assert module.current(host.orch) is False


@pytest.mark.asyncio
async def test_clinical_only_mode_refuses_before_capture_or_send(host, monkeypatch):
    module = clinical_module()
    assert module.register(host.orch)
    monkeypatch.setitem(flags._flags, "observation_packing", False)
    monkeypatch.setitem(flags._flags, "safe_compaction", False)
    calls = []

    async def capture(_):
        calls.append("capture")

    monkeypatch.setattr(host.orch._llm_store, "capture_user", capture)
    response = await module.invoke(
        host.orch,
        object(),
        {"note_text": NOTE},
        owner="unused",
        chat=str(uuid4()),
        timeout=60,
    )
    assert response.error["code"] == "EXTRACT_CONTEXT_UNAVAILABLE"
    assert calls == []


def wire_response(note=NOTE, note_id=NOTE_ID, **changes):
    value = {
        "id": "synthetic-completion",
        "object": "chat.completion",
        "created": 1,
        "model": "synthetic-clinical-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(golden_payload(note, note_id)),
                },
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50},
    }
    value.update(changes)
    return IsolatedHttpResponse(
        200,
        json.dumps(value).encode(),
        "https://clinical.invalid/v1/chat/completions",
        "application/json",
    )


@pytest.mark.parametrize("wrapped", ["```json\n%s\n```", "<think>ignore</think>%s"])
def test_wire_envelope_requires_strict_json(wrapped):
    module = clinical_module()
    response = wire_response()
    altered = IsolatedHttpResponse(
        200,
        (wrapped % response.body.decode()).encode(),
        response.final_url,
        response.content_type,
    )
    with pytest.raises(module.ClinicalUnavailable, match="EXTRACT_RESPONSE_INVALID"):
        module.parse_response(altered, "synthetic-clinical-model", 2048, 32768)


async def revoke(state, loss, monkeypatch):
    from datetime import UTC, datetime

    orch = state.orch
    if loss == "config":
        await orch._llm_store.set(
            state.owner,
            provider="custom",
            base_url="https://clinical.invalid/v1",
            model="synthetic-clinical-model",
            api_key="synthetic-rotated-key",
        )
    elif loss == "consent":

        def withdraw():
            runtime = orch.runtime_composition.plane.runtime
            with runtime.transaction() as tx:
                runtime.repositories.preferences.data_sharing.acknowledge(
                    tx,
                    owner_id=state.owner,
                    notice_version="withdrawn",
                    at=datetime.now(UTC),
                )

        await asyncio.to_thread(withdraw)
    elif loss == "permission":
        await asyncio.to_thread(
            orch.tool_permissions.set_agent_scopes,
            state.owner,
            "fhir-1",
            {"tools:read": False},
        )
    elif loss == "registration":
        orch.ui_sessions[state.socket] = dict(orch.ui_sessions[state.socket])
    elif loss == "connection":
        state.connection.closing = True
    elif loss == "flag":
        monkeypatch.setitem(flags._flags, "clinical_note_extract", False)
    elif loss == "context_flag":
        monkeypatch.setitem(flags._flags, "observation_packing", False)
    elif loss == "key":
        monkeypatch.setenv(
            "AUDIT_HMAC_SECRET", "synthetic-other-binding-secret-0123456789abcdef"
        )
    elif loss == "private_hosts":
        monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "localhost")
    elif loss == "service":
        orch._clinical_extraction = object()
    elif loss == "session":
        credential = state.origin.credential
        await asyncio.to_thread(orch.web_sessions.delete, credential.session_id)


@pytest.mark.parametrize(
    "change",
    [
        {"model": "foreign"},
        {"object": "text_completion"},
        {"id": ""},
        {"created": True},
        {"choices": []},
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "{}"},
                }
            ]
        },
        {
            "choices": [
                {
                    "index": True,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "{}"},
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "{}", "tool_calls": []},
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "{}", "refusal": "no"},
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": [], "annotations": []},
                }
            ]
        },
        {"usage": None},
        {"usage": {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2}},
        {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3}},
        {"usage": {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0}},
        {
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 3000,
                "total_tokens": 3020,
            }
        },
        {
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 30,
                "total_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 21},
            }
        },
        {
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 30,
                "total_tokens": 50,
                "completion_tokens_details": {"other": 1},
            }
        },
        {"other": "unsupported"},
    ],
)
def test_strict_wire_shape_and_usage_deny_incomplete_or_unsupported_results(change):
    module = clinical_module()
    with pytest.raises(module.ClinicalUnavailable, match="EXTRACT_RESPONSE_INVALID"):
        module.parse_response(
            wire_response(**change), "synthetic-clinical-model", 2048, 32768
        )


@pytest.mark.parametrize(
    "body",
    [
        b'{"choices":[],"choices":[]}',
        b'{"x":NaN}',
        b'{"x":1e999}',
        b"[]",
        b"\xff",
        b'{"x":"\\ud800"}',
        b'{"x":' + b"[" * 20 + b"0" + b"]" * 20 + b"}",
    ],
)
def test_wire_json_duplicate_nonfinite_encoding_and_depth_fail_closed(body):
    module = clinical_module()
    with pytest.raises(module.ClinicalUnavailable, match="EXTRACT_RESPONSE_INVALID"):
        module.parse_response(
            IsolatedHttpResponse(
                200, body, "https://clinical.invalid", "application/json"
            ),
            "synthetic-clinical-model",
            2048,
            32768,
        )


@pytest.mark.parametrize(
    "content",
    [
        "```json\n{}\n```",
        "<think>ignore</think>{}",
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":1e999}',
        "[]",
        "not json",
    ],
)
def test_proposal_content_is_strict_json_before_field_evaluation(content):
    module = clinical_module()
    response = wire_response(
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ]
    )
    with pytest.raises(
        module.ClinicalUnavailable, match="EXTRACT_RESPONSE_INVALID"
    ) as caught:
        module.parse_response(response, "synthetic-clinical-model", 2048, 32768)
    assert caught.value.usage["total_tokens"] == 50


@pytest.mark.asyncio
async def test_normal_registration_installs_only_the_canonical_host_service(
    orchestrator_factory, monkeypatch, tmp_path
):
    from orchestrator import local_agents

    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda: [])
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "registered-agent.pem"))
    # Ambient runtime config must not override this suite's dev-posture registration.
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.delenv("DEFAULT_AGENT_OWNER", raising=False)
    for name in ("fhir", "inprocess_agents"):
        monkeypatch.setitem(flags._flags, name, True)
    for name in (
        "remote_compute",
        "computer_use",
        "terminal_shell",
        "gaiakeep",
        "cresco",
        "observation_packing",
        "safe_compaction",
    ):
        monkeypatch.setitem(flags._flags, name, False)
    orch = await asyncio.to_thread(orchestrator_factory)
    registered = await local_agents.register_built_ins(orch)
    assert registered == ["fhir-1"] and clinical_module().current(orch)


def timing_metadata():
    return {
        "cache_n": 19,
        "prompt_n": 1,
        "prompt_ms": 2.0,
        "prompt_per_token_ms": 2.0,
        "prompt_per_second": 500.0,
        "predicted_n": 30,
        "predicted_ms": 3.0,
        "predicted_per_token_ms": 0.1,
        "predicted_per_second": 10000.0,
    }


def test_native_provider_timing_metadata_preserves_content_and_usage():
    from orchestrator.clinical_extraction import parse_response

    parsed = parse_response(
        wire_response(timings=timing_metadata()),
        "synthetic-clinical-model",
        2048,
        32768,
    )
    assert json.loads(parsed.content) == golden_payload(NOTE, NOTE_ID)
    assert parsed.usage == {
        "prompt_tokens": 20,
        "completion_tokens": 30,
        "total_tokens": 50,
        "cached_tokens": None,
    }


@pytest.mark.parametrize(
    "timings",
    [
        None,
        [],
        {},
        timing_metadata() | {"unrecognized": 1},
        timing_metadata() | {"cache_n": True},
        timing_metadata() | {"prompt_n": 1.0},
        timing_metadata() | {"prompt_n": -1},
        timing_metadata() | {"cache_n": 20},
        timing_metadata() | {"predicted_n": 29},
        timing_metadata() | {"predicted_ms": "3"},
        timing_metadata() | {"predicted_ms": float("nan")},
        timing_metadata() | {"predicted_ms": float("inf")},
        timing_metadata() | {"predicted_ms": 1e100},
    ],
)
def test_native_provider_timing_metadata_refuses_malformed_or_inconsistent_values(
    timings,
):
    from orchestrator.clinical_extraction import ClinicalUnavailable, parse_response

    with pytest.raises(ClinicalUnavailable, match="EXTRACT_RESPONSE_INVALID"):
        parse_response(
            wire_response(timings=timings), "synthetic-clinical-model", 2048, 32768
        )
