"""033 Wave-0 — _call_llm optional enhancement params (C-N14 + C-U12).

Covers the reasoning-budget knob and enforced-structured-output plumbing
threaded through ``Orchestrator._call_llm``, plus the capability-probe
fallback that keeps a plainer OpenAI-compatible endpoint working when it
rejects either param. Pure Python — a bare ``Orchestrator.__new__`` stub with
a fake completions client; no DB, no socket, no real LLM.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from llm_config.types import CredentialSource, LLMUnavailable  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402

pytestmark = pytest.mark.asyncio


class _Msg:
    def __init__(self, content="ok"):
        self.content = content
        self.tool_calls = None


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content="ok"):
        self.choices = [_Choice(content)]
        self.usage = types.SimpleNamespace(total_tokens=10)


class _StatusError(Exception):
    def __init__(self, status_code, body="provider response body"):
        super().__init__(body)
        self.status_code = status_code


class _FakeCompletions:
    """Records every create() kwargs; behavior driven by ``fail_on``.

    ``fail_on`` is a callable(kwargs) -> Optional[str]; returning a string
    raises an Exception with that message, returning None succeeds.
    """

    def __init__(self, fail_on=None, content="ok"):
        self.calls = []
        self._fail_on = fail_on or (lambda kw: None)
        self._content = content

    def create(self, **kwargs):
        self.calls.append(kwargs)
        failure = self._fail_on(kwargs)
        if isinstance(failure, BaseException):
            raise failure
        if failure:
            raise Exception(failure)
        return _Resp(self._content)


class _FakeClient:
    def __init__(self, completions):
        self.chat = types.SimpleNamespace(completions=completions)


def _bare_orch(completions, *, default_effort=None):
    orch = Orchestrator.__new__(Orchestrator)
    orch._llm_unsupported_params = {}
    orch.llm_reasoning_effort = default_effort
    orch.audit_recorder = None
    orch._CredentialSource = CredentialSource
    orch._LLMUnavailable = LLMUnavailable
    resolved = types.SimpleNamespace(model="m1", base_url="https://ep/v1")

    orch._llm_audit_principals = lambda ws: ("u", "p")

    async def _resolve(ws):
        # Feature 054: _resolve_llm_client_for is async and resolves either
        # the caller's persisted record (USER) or the admin system record
        # (SYSTEM) — websocket=None here is a system-context call.
        return (_FakeClient(completions), CredentialSource.SYSTEM, resolved)

    orch._resolve_llm_client_for = _resolve

    async def _noop(*a, **k):
        return None

    audits = []

    async def _record(*_args, **kwargs):
        audits.append(kwargs)

    orch._record_llm_call = _record
    orch._record_llm_unconfigured = _noop
    orch._emit_llm_usage_report = _noop
    orch._audits = audits
    return orch


# --------------------------------------------------------------------------
# C-U12 — reasoning-budget knob
# --------------------------------------------------------------------------

async def test_reasoning_effort_passed_when_set():
    comp = _FakeCompletions()
    orch = _bare_orch(comp)
    msg, _ = await orch._call_llm(None, [{"role": "user", "content": "hi"}],
                                  reasoning_effort="high")
    assert msg is not None
    assert comp.calls[0]["reasoning_effort"] == "high"


async def test_reasoning_effort_global_default_applies():
    comp = _FakeCompletions()
    orch = _bare_orch(comp, default_effort="low")
    await orch._call_llm(None, [{"role": "user", "content": "hi"}])
    assert comp.calls[0]["reasoning_effort"] == "low"


async def test_per_call_effort_overrides_default():
    comp = _FakeCompletions()
    orch = _bare_orch(comp, default_effort="low")
    await orch._call_llm(None, [{"role": "user", "content": "hi"}],
                         reasoning_effort="high")
    assert comp.calls[0]["reasoning_effort"] == "high"


async def test_invalid_effort_not_sent():
    comp = _FakeCompletions()
    orch = _bare_orch(comp, default_effort="ludicrous")
    await orch._call_llm(None, [{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in comp.calls[0]


async def test_no_effort_means_no_param():
    comp = _FakeCompletions()
    orch = _bare_orch(comp)
    await orch._call_llm(None, [{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in comp.calls[0]


# --------------------------------------------------------------------------
# Capability-probe fallback (shared by both params)
# --------------------------------------------------------------------------

async def test_unsupported_effort_is_stripped_and_retried():
    # First call (with reasoning_effort) 400s; the retry without it succeeds.
    def fail_on(kw):
        return "400 unknown parameter: reasoning_effort" if "reasoning_effort" in kw else None

    comp = _FakeCompletions(fail_on=fail_on)
    orch = _bare_orch(comp)
    msg, _ = await orch._call_llm(None, [{"role": "user", "content": "hi"}],
                                  reasoning_effort="high")
    assert msg is not None                      # the call ultimately succeeds
    assert len(comp.calls) == 2                 # one rejected, one clean retry
    assert "reasoning_effort" not in comp.calls[1]
    # remembered for this (base_url, model) so future calls skip it
    assert "reasoning_effort" in orch._llm_unsupported_params[("https://ep/v1", "m1")]


async def test_remembered_param_not_resent_on_next_call():
    def fail_on(kw):
        return "400 unsupported parameter response_format" if "response_format" in kw else None

    comp = _FakeCompletions(fail_on=fail_on)
    orch = _bare_orch(comp)
    await orch._call_llm_json(None, [{"role": "user", "content": "hi"}])
    n_after_first = len(comp.calls)
    # second structured call: response_format already known-unsupported → never sent
    await orch._call_llm_json(None, [{"role": "user", "content": "hi"}])
    assert "response_format" not in comp.calls[-1]
    # only ONE extra call (no second rejection round-trip)
    assert len(comp.calls) == n_after_first + 1


async def test_strip_retry_does_not_consume_real_retry_budget(monkeypatch):
    # The enhancement param is rejected once, then a transient error happens
    # on every clean attempt — we should still get the full MAX_RETRIES (3)
    # clean attempts, i.e. 1 rejected + 3 transient = 4 total calls.
    state = {"n": 0}

    def fail_on(kw):
        if "reasoning_effort" in kw:
            return "400 unknown parameter reasoning_effort"
        state["n"] += 1
        return _StatusError(503)

    comp = _FakeCompletions(fail_on=fail_on)
    orch = _bare_orch(comp)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(
        "orchestrator.orchestrator.asyncio.sleep",
        no_sleep,
    )
    msg, _ = await orch._call_llm(None, [{"role": "user", "content": "hi"}],
                                  reasoning_effort="high")
    assert msg is None
    assert state["n"] == Orchestrator.MAX_RETRIES   # 3 real attempts preserved


async def test_transient_error_not_misread_as_param_problem():
    # A 503 with an active enhancement param must NOT strip the param.
    assert Orchestrator._llm_unsupported_extras(
        "503 Service Unavailable", {"reasoning_effort": "high"}) == set()


async def test_named_param_only_drops_that_param():
    drop = Orchestrator._llm_unsupported_extras(
        "400 unrecognized parameter: response_format",
        {"response_format": {}, "reasoning_effort": "high"})
    assert drop == {"response_format"}


async def test_generic_400_drops_all_extras():
    drop = Orchestrator._llm_unsupported_extras(
        "400 Bad Request: unsupported parameter in input",
        {"response_format": {}, "reasoning_effort": "high"})
    assert drop == {"response_format", "reasoning_effort"}


# --------------------------------------------------------------------------
# C-N14 — enforced structured output
# --------------------------------------------------------------------------

async def test_call_llm_json_object_request_and_parse():
    comp = _FakeCompletions(content='{"a": 1, "b": "two"}')
    orch = _bare_orch(comp)
    out = await orch._call_llm_json(None, [{"role": "user", "content": "hi"}])
    assert out == {"a": 1, "b": "two"}
    assert comp.calls[0]["response_format"] == {"type": "json_object"}


async def test_call_llm_json_schema_strict_passthrough():
    comp = _FakeCompletions(content='{"ok": true}')
    orch = _bare_orch(comp)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    out = await orch._call_llm_json(None, [{"role": "user", "content": "hi"}],
                                    schema=schema, schema_name="thing")
    assert out == {"ok": True}
    rf = comp.calls[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "thing"
    assert rf["json_schema"]["schema"] == schema


async def test_call_llm_json_tolerates_fenced_block():
    comp = _FakeCompletions(content='```json\n{"x": 42}\n```')
    orch = _bare_orch(comp)
    out = await orch._call_llm_json(None, [{"role": "user", "content": "hi"}])
    assert out == {"x": 42}


async def test_call_llm_json_returns_none_on_garbage():
    comp = _FakeCompletions(content="this is not json at all")
    orch = _bare_orch(comp)
    out = await orch._call_llm_json(None, [{"role": "user", "content": "hi"}])
    assert out is None  # caller keeps its own repair/fallback path


async def test_call_llm_json_falls_back_when_format_unsupported():
    # response_format 400s; retry without it still returns parseable content.
    def fail_on(kw):
        return "400 response_format is not supported" if "response_format" in kw else None

    comp = _FakeCompletions(fail_on=fail_on, content='{"y": 9}')
    orch = _bare_orch(comp)
    out = await orch._call_llm_json(None, [{"role": "user", "content": "hi"}])
    assert out == {"y": 9}
    assert len(comp.calls) == 2
    assert "response_format" not in comp.calls[1]


# --------------------------------------------------------------------------
# Provider failure disposition, bounded retry, and redaction
# --------------------------------------------------------------------------


def _disable_retry_sleep(monkeypatch):
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(
        "orchestrator.orchestrator.asyncio.sleep",
        no_sleep,
    )


async def test_http_400_fails_once_and_never_logs_or_audits_body(
    monkeypatch,
    caplog,
):
    sentinel = "SENTINEL_PROVIDER_BODY_MUST_NOT_ESCAPE"
    comp = _FakeCompletions(
        fail_on=lambda _kwargs: _StatusError(400, sentinel),
    )
    orch = _bare_orch(comp)
    _disable_retry_sleep(monkeypatch)

    msg, usage = await orch._call_llm(
        None,
        [{"role": "user", "content": "hi"}],
    )

    assert (msg, usage) == (None, None)
    assert len(comp.calls) == 1
    assert [audit["outcome"] for audit in orch._audits] == ["failure"]
    assert orch._audits[0]["upstream_error_class"] == "other"
    assert sentinel not in caplog.text
    assert sentinel not in repr(orch._audits)


async def test_unknown_nontransient_exception_fails_once(monkeypatch):
    comp = _FakeCompletions(
        fail_on=lambda _kwargs: RuntimeError("unknown provider failure"),
    )
    orch = _bare_orch(comp)
    _disable_retry_sleep(monkeypatch)

    msg, usage = await orch._call_llm(
        None,
        [{"role": "user", "content": "hi"}],
    )

    assert (msg, usage) == (None, None)
    assert len(comp.calls) == 1
    assert orch._audits[0]["upstream_error_class"] == "other"


async def test_failure_metadata_does_not_stringify_provider_body(monkeypatch):
    class BodyStringForbidden(RuntimeError):
        def __str__(self):
            raise AssertionError("provider body was stringified")

    comp = _FakeCompletions(
        fail_on=lambda _kwargs: BodyStringForbidden(),
    )
    orch = _bare_orch(comp)
    _disable_retry_sleep(monkeypatch)

    msg, usage = await orch._call_llm(
        None,
        [{"role": "user", "content": "hi"}],
    )

    assert (msg, usage) == (None, None)
    assert len(comp.calls) == 1
    assert orch._audits[0]["upstream_error_class"] == "other"


async def test_malformed_choices_fail_once_before_success_audit(monkeypatch):
    class MalformedCompletions:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return types.SimpleNamespace(choices=[], usage=None)

    comp = MalformedCompletions()
    orch = _bare_orch(comp)
    _disable_retry_sleep(monkeypatch)

    msg, usage = await orch._call_llm(
        None,
        [{"role": "user", "content": "hi"}],
    )

    assert (msg, usage) == (None, None)
    assert len(comp.calls) == 1
    assert [audit["outcome"] for audit in orch._audits] == ["failure"]
    assert orch._audits[0]["upstream_error_class"] == "other"


@pytest.mark.parametrize(
    ("status_code", "audit_class"),
    [
        (408, "other"),
        (409, "other"),
        (425, "other"),
        (429, "rate_limit"),
        (500, "other"),
        (503, "other"),
        (599, "other"),
    ],
)
async def test_proven_transient_http_statuses_use_bounded_retry_budget(
    monkeypatch,
    status_code,
    audit_class,
):
    comp = _FakeCompletions(
        fail_on=lambda _kwargs: _StatusError(status_code),
    )
    orch = _bare_orch(comp)
    _disable_retry_sleep(monkeypatch)

    msg, usage = await orch._call_llm(
        None,
        [{"role": "user", "content": "hi"}],
    )

    assert (msg, usage) == (None, None)
    assert len(comp.calls) == orch.MAX_RETRIES == 3
    assert len(orch._audits) == 1
    assert orch._audits[0]["upstream_error_class"] == audit_class


@pytest.mark.parametrize("error_type", [TimeoutError, ConnectionError])
async def test_transport_exception_uses_bounded_retry_budget(
    monkeypatch,
    error_type,
):
    comp = _FakeCompletions(
        fail_on=lambda _kwargs: error_type("provider transport failure"),
    )
    orch = _bare_orch(comp)
    _disable_retry_sleep(monkeypatch)

    msg, usage = await orch._call_llm(
        None,
        [{"role": "user", "content": "hi"}],
    )

    assert (msg, usage) == (None, None)
    assert len(comp.calls) == orch.MAX_RETRIES == 3
    assert orch._audits[0]["upstream_error_class"] == "transport_error"


async def test_internal_html_maintenance_marker_is_transient_and_redacted(
    monkeypatch,
    caplog,
):
    sentinel = "SENTINEL_MAINTENANCE_BODY_MUST_NOT_ESCAPE"
    comp = _FakeCompletions(content=f"<html>{sentinel}</html>")
    orch = _bare_orch(comp)
    _disable_retry_sleep(monkeypatch)

    msg, usage = await orch._call_llm(
        None,
        [{"role": "user", "content": "hi"}],
    )

    assert (msg, usage) == (None, None)
    assert len(comp.calls) == orch.MAX_RETRIES == 3
    assert orch._audits[0]["upstream_error_class"] == "other"
    assert sentinel not in caplog.text
    assert sentinel not in repr(orch._audits)
