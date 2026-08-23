"""FF_MODEL_ROUTER audit fidelity — the ``llm_call`` audit row and the
capability-probe cache both name the model that was ACTUALLY requested when
the router re-tiers a SYSTEM call, and the routed selection is logged
(low cardinality: tier + model only). Pure Python, no DB, no LLM."""
from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from llm_config import audit_events  # noqa: E402
from llm_config.types import CredentialSource, LLMUnavailable  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402
from tests.test_call_llm_wave0 import _FakeClient, _FakeCompletions  # noqa: E402

pytestmark = pytest.mark.asyncio

_CONFIDENT = "A sufficiently confident and complete answer to the question here."


def _orch(completions, *, source=CredentialSource.SYSTEM):
    orch = Orchestrator.__new__(Orchestrator)
    orch._llm_unsupported_params = {}
    orch.llm_reasoning_effort = None
    orch.audit_recorder = types.SimpleNamespace(record=AsyncMock())
    orch._CredentialSource = CredentialSource
    orch._LLMUnavailable = LLMUnavailable
    orch._llm_audit_principals = lambda ws: ("u", "p")
    resolved = types.SimpleNamespace(model="default-model", base_url="https://ep/v1")

    async def _resolve(ws):
        return (_FakeClient(completions), source, resolved)

    orch._resolve_llm_client_for = _resolve
    # The REAL audit writer — the assertion is on the persisted row.
    orch._record_llm_call = audit_events.record_llm_call

    async def _noop(*a, **k):
        return None

    orch._record_llm_unconfigured = _noop
    orch._emit_llm_usage_report = _noop
    return orch


def _router_on(monkeypatch):
    monkeypatch.setenv("FF_LLM_STREAMING", "false")
    monkeypatch.setenv("FF_MODEL_ROUTER", "true")
    monkeypatch.setenv("MODEL_TIERS",
                       '{"small":"tiny-8b","medium":"mid-70b","large":"big"}')


async def test_audit_row_names_routed_model_for_system_call(monkeypatch, caplog):
    _router_on(monkeypatch)
    comp = _FakeCompletions(content=_CONFIDENT)
    orch = _orch(comp)
    with caplog.at_level(logging.INFO):
        msg, _ = await orch._call_llm(None, [{"role": "user", "content": "hi"}],
                                      feature="tool_dispatch")
    assert msg is not None
    requested = comp.calls[0]["model"]
    assert requested != "default-model"  # the router re-tiered this call
    ev = orch.audit_recorder.record.await_args.args[0]
    assert ev.event_class == "llm_call"
    assert ev.inputs_meta["model"] == requested
    assert ev.inputs_meta["configured_model"] == "default-model"
    routed = [r.getMessage() for r in caplog.records
              if "model_router: routed" in r.getMessage()]
    assert routed and requested in routed[0]


async def test_audit_row_unchanged_when_router_off(monkeypatch):
    monkeypatch.setenv("FF_LLM_STREAMING", "false")
    monkeypatch.setenv("FF_MODEL_ROUTER", "false")
    comp = _FakeCompletions(content="ok")
    orch = _orch(comp)
    await orch._call_llm(None, [{"role": "user", "content": "hi"}],
                         feature="tool_dispatch")
    ev = orch.audit_recorder.record.await_args.args[0]
    assert ev.inputs_meta["model"] == "default-model"
    assert "configured_model" not in ev.inputs_meta


async def test_user_credential_is_never_re_tiered(monkeypatch):
    _router_on(monkeypatch)
    comp = _FakeCompletions(content=_CONFIDENT)
    orch = _orch(comp, source=CredentialSource.USER)
    await orch._call_llm(None, [{"role": "user", "content": "hi"}],
                         feature="tool_dispatch")
    assert comp.calls[0]["model"] == "default-model"
    ev = orch.audit_recorder.record.await_args.args[0]
    assert ev.inputs_meta["model"] == "default-model"
    assert "configured_model" not in ev.inputs_meta


async def test_probe_cache_keyed_on_routed_model(monkeypatch):
    """A routed tier's param rejection is remembered under the ROUTED model,
    not the default one."""
    _router_on(monkeypatch)

    def fail_on(kw):
        return ("400 unknown parameter: reasoning_effort"
                if "reasoning_effort" in kw else None)

    comp = _FakeCompletions(fail_on=fail_on, content=_CONFIDENT)
    orch = _orch(comp)
    msg, _ = await orch._call_llm(None, [{"role": "user", "content": "hi"}],
                                  feature="tool_dispatch", reasoning_effort="high")
    assert msg is not None
    requested = comp.calls[0]["model"]
    assert requested != "default-model"
    assert ("https://ep/v1", requested) in orch._llm_unsupported_params
    assert ("https://ep/v1", "default-model") not in orch._llm_unsupported_params


async def test_failure_row_names_routed_model(monkeypatch):
    _router_on(monkeypatch)
    comp = _FakeCompletions(fail_on=lambda kw: "500 boom")
    orch = _orch(comp)
    monkeypatch.setattr("orchestrator.orchestrator.asyncio.sleep", AsyncMock())
    msg, _ = await orch._call_llm(None, [{"role": "user", "content": "hi"}],
                                  feature="tool_dispatch")
    assert msg is None
    ev = orch.audit_recorder.record.await_args.args[0]
    assert ev.outcome == "failure"
    assert ev.inputs_meta["model"] == comp.calls[0]["model"]
    assert ev.inputs_meta["model"] != "default-model"
