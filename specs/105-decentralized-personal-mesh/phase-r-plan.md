# Phase R (Reliability) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop losing chat turns to stalled models and malformed tool arguments in the existing Python loop.

**Architecture:** Four small modules hold the policy: model-call deadlines, recovery after a failed call, tool-argument repair, and a repeat guard. `backend/orchestrator/orchestrator.py` calls them at five existing seams. There is no schema, wire-protocol or client change.

**Tech Stack:** Python 3.11, the `openai` SDK, `httpx` 0.28, `jsonschema` 4.25.1 (already pinned), pytest with `asyncio_mode = auto`.

**Spec:** [spec.md](spec.md) sections 10.2 and 10.3, and [plan.md](plan.md) "Phase R". Line numbers below were read at AstralDeep `a3eb724a`. Re-find each anchor by the quoted text if `main` has moved.

## Global Constraints

- Python 3.11 compatible. Ruff target `py311`.
- Each new source file opens with a header of at most three sentences saying what it does and how it connects to other files. No other comments or docstrings. No spec, task or issue numbers in source.
- Tests cover the golden path, edge cases, denials and failures. Changed Python lines keep at least 90% coverage (CI measures this. The container has no `pytest-cov`).
- Required tests are deterministic: no live provider, no wall-clock performance bounds. Deadlines are tested by patching in small values. `asyncio.wait_for(..., 5)` appears in tests only as a guard against hanging.
- No new dependency.
- Never log or audit provider response bodies, tool arguments or model text. Existing tests enforce this with sentinel strings. Keep them passing.
- Work on a branch cut from `origin/main` (suggested name `feat/105-phase-r-reliability`), in its own worktree. Local commits only: no push, no PR. No Claude co-author or "Generated with" lines.

## Scope

In this plan:

| Spec item | Task |
|---|---|
| Separate connect, first-token, idle and attempt deadlines (10.2) | 2, 3 |
| Never re-run a request once text has reached the user. Continue from it (10.2) | 4 |
| Honor `retry-after`. A per-turn fallback chain (10.2) | 5 |
| Repair, validate, regenerate once, then a precise error (10.3 steps 3 to 6) | 6, 7 |
| Count identical failing calls: warn at two, block at three, end at six (10.3 steps 7 and 8) | 8 |

Not in this plan, with the reason:

- **Deadlines learned from observed latency, and health tracked per model.** They need state that outlives a call. They belong with the kernel's provider layer (plan.md phase 3).
- **A retry budget across calls, and the one-hour budget for an unreachable network.** Each call is already bounded by attempts times chain length. The long budget is for a device that goes offline, not a server.
- **Strict schemas on the first tool request, and grammar-defined free-form tools (10.3 step 1).** Both depend on the native provider adapters (Deep #331, #326). Strict schemas are used here only in the regeneration step.
- **Progressive parsing of streamed arguments (10.3 step 2).** Arguments are already accumulated and parsed at the end of the call. A stall mid-arguments is retried (task 3).
- **Configuring the fallback chain in the product.** A user has one model config, so that needs a Plane column and client UI. The chain is set by environment variable here.
- **The native Anthropic client's timeout.** `backend/llm_config/native_provider.py:79` enforces a 60-second total as part of its binding contract. Raising it is a separate change.
- **Malformed responses stay non-retryable.** `backend/tests/test_call_llm_wave0.py::test_malformed_choices_fail_once_before_success_audit` pins that on purpose.
- **Unparseable arguments in replayed history.** Already handled by `_history_assistant_message` (`orchestrator.py:942`) and its test.

## Review Focus

Each line is a case the spec implies and a person would hit. The test that pins it is named in the task that owns the code.

1. **A provider that sends keep-alive chunks but no tokens.** The turn must still time out. Task 3, `test_heartbeat_chunks_without_progress_still_stall`.
2. **A stall while tool-call arguments are streaming.** The call must be retried whole and a partial call must never be dispatched. Task 3, `test_stall_mid_tool_arguments_retries_and_never_returns_a_partial_call`.
3. **Stop pressed during a stalled stream.** It must return at once, not after the idle deadline. Task 3, `test_cancel_during_stall_returns_at_once`.
4. **A repair that would change meaning.** `"007"` for a string field stays `"007"`, and truncated JSON is never completed. Task 6, `test_repair_never_guesses` and `test_truncated_json_is_not_completed`.
5. **A fallback model that rejects a parameter the primary accepted.** The unsupported-parameter cache is keyed by model and must be re-read. Task 5, `test_fallback_rereads_unsupported_params_for_the_new_model`.

## Commands

Backend source is baked into the image, so sync before each run. From the worktree root, define once:

```bash
t() { make sync-backend >/dev/null && docker exec astraldeep bash -c "cd /app/backend && python -m pytest -q $*"; }
```

Every "Run" below uses `t`. Lint with `ruff check <paths>` from the worktree root.

## File Structure

| File | Responsibility |
|---|---|
| Create `backend/tests/helpers/llm_faults.py` | A scripted fake model provider and a bare orchestrator stub, shared by every test in this plan |
| Create `backend/llm_config/deadlines.py` | The deadline profile, its environment overrides, and the pure function that says how long a stream may wait |
| Create `backend/orchestrator/llm_recovery.py` | After a failed model call: how long to wait, and which model to try next |
| Create `backend/orchestrator/tool_arguments.py` | Turn the model's raw tool arguments into validated arguments or a precise error |
| Create `backend/orchestrator/repeat_guard.py` | Count identical failing calls within a turn |
| Modify `backend/llm_config/client_factory.py` | Build the OpenAI client with the deadline profile |
| Modify `backend/orchestrator/orchestrator.py` | Call the four modules at the model-call, stream, dispatch and chat-loop seams |

---

### Task 1: Scripted fake model provider

**Files:**
- Create: `backend/tests/helpers/llm_faults.py`
- Test: `backend/tests/test_llm_faults.py`

**Interfaces:**
- Consumes: nothing.
- Produces, all importable from `tests.helpers.llm_faults`:
  - `content_chunk(text: str)`, `tool_chunk(index=0, call_id=None, name=None, arguments=None)`, `finish_chunk(reason="stop")`, `empty_chunk()`: stream chunks shaped like the ones in `backend/tests/test_llm_streaming.py:36-49`. `empty_chunk()` has `choices=[]`.
  - Steps, one consumed per `create()` call, the last one repeating:
    - `reply(text: str)`: a normal answer. Streaming yields one content chunk then `finish_chunk("stop")`.
    - `stream(*chunks, finish="stop")`: yields the chunks then `finish_chunk(finish)`. Non-streaming returns their joined content.
    - `fail(error: BaseException)`: raises `error`.
    - `status(code: int, retry_after: float | None = None)`: raises `StatusError`, which has `status_code` and `response.headers["retry-after"]`.
    - `stall(after: int = 0, *chunks)`: yields the first `after` chunks, then blocks until `release` is set.
    - `drop(after: int, *chunks)`: yields `after` chunks, then raises `ConnectionError("dropped")`.
    - `heartbeat_only()`: yields `empty_chunk()` every 5 ms until `release` is set.
  - `FaultyCompletions(script=None, *, by_model=None)` with `calls: list[dict]`, `release: threading.Event`, and `create(**kwargs)`. `by_model` maps a model name to its own script. A blocked step gives up after 5 seconds with `TimeoutError` so a broken test cannot hang the suite.
  - `bare_orch(completions, *, model="m1", base_url="https://ep/v1")`: an `Orchestrator.__new__` stub like `_bare_orch` in `test_llm_streaming.py:78-112`, plus `orch._audits` (keyword arguments of each `_record_llm_call`) and `orch._rendered` (each component list passed to `send_ui_render`). `orch._sent_frames` is kept.
  - `no_retry_sleep(monkeypatch) -> list[float]`: replaces `orchestrator.orchestrator.asyncio.sleep` and returns the list of requested delays.
  - `tiny_deadlines(monkeypatch, *, first_token=0.05, idle=0.05, attempt=2.0)`: patches `llm_config.deadlines.deadlines_for` to return `LLMDeadlines(0.05, first_token, idle, attempt)`. It imports `LLMDeadlines` lazily, so task 1 does not depend on task 2.

- [ ] **Step 1: Write the failing test**

```python
import threading
import pytest
from tests.helpers.llm_faults import (
    FaultyCompletions, StatusError, bare_orch, content_chunk, drop, fail, reply, stall, status, stream)


def test_steps_are_consumed_in_order_and_the_last_repeats():
    comp = FaultyCompletions([fail(ConnectionError("x")), reply("ok")])
    with pytest.raises(ConnectionError):
        comp.create(model="m1", messages=[])
    assert comp.create(model="m1", messages=[]).choices[0].message.content == "ok"
    assert comp.create(model="m1", messages=[]).choices[0].message.content == "ok"
    assert [c["model"] for c in comp.calls] == ["m1", "m1", "m1"]


def test_by_model_gives_each_model_its_own_script():
    comp = FaultyCompletions(by_model={"m1": [status(503)], "m2": [reply("two")]})
    with pytest.raises(StatusError) as caught:
        comp.create(model="m1", messages=[])
    assert caught.value.status_code == 503
    assert comp.create(model="m2", messages=[]).choices[0].message.content == "two"


def test_status_carries_retry_after():
    with pytest.raises(StatusError) as caught:
        FaultyCompletions([status(429, retry_after=7)]).create(model="m1", messages=[])
    assert caught.value.response.headers["retry-after"] == "7"


def test_stream_yields_chunks_then_a_finish_chunk():
    comp = FaultyCompletions([stream(content_chunk("a"), content_chunk("b"))])
    chunks = list(comp.create(model="m1", messages=[], stream=True))
    assert [c.choices[0].delta.content for c in chunks[:2]] == ["a", "b"]
    assert chunks[-1].choices[0].finish_reason == "stop"


def test_drop_raises_after_the_given_chunks():
    gen = FaultyCompletions([drop(1, content_chunk("a"))]).create(model="m1", messages=[], stream=True)
    assert next(gen).choices[0].delta.content == "a"
    with pytest.raises(ConnectionError):
        next(gen)


def test_stall_blocks_until_released():
    comp = FaultyCompletions([stall(1, content_chunk("a"))])
    gen = comp.create(model="m1", messages=[], stream=True)
    assert next(gen).choices[0].delta.content == "a"
    seen = []
    worker = threading.Thread(target=lambda: seen.extend(gen))
    worker.start()
    worker.join(0.05)
    assert worker.is_alive()
    comp.release.set()
    worker.join(2)
    assert not worker.is_alive() and seen == []


async def test_bare_orch_runs_a_plain_call():
    orch = bare_orch(FaultyCompletions([reply("hello")]))
    msg, _usage = await orch._call_llm(None, [{"role": "user", "content": "hi"}])
    assert msg.content == "hello"
    assert orch._audits[-1]["outcome"] == "success"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `t tests/test_llm_faults.py`
Expected: an import error for `tests.helpers.llm_faults`.

- [ ] **Step 3: Implement `backend/tests/helpers/llm_faults.py`** to the Interfaces block above. A step is a small object with `run(kwargs, release)`. `FaultyCompletions.create` picks the script (by `kwargs["model"]` when `by_model` is given), records `kwargs`, and calls the step.

- [ ] **Step 4: Run it and confirm it passes**

Run: `t tests/test_llm_faults.py`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/helpers/llm_faults.py backend/tests/test_llm_faults.py
git commit -m "Add a scripted fake model provider for reliability tests"
```

---

### Task 2: Deadline profile

**Files:**
- Create: `backend/llm_config/deadlines.py`
- Modify: `backend/llm_config/client_factory.py:104-112` (the `kwargs = {"base_url": ...}` block in `build_llm_client`)
- Modify: `backend/llm_config/tests/test_client_factory.py:103-107` (`test_default_timeout_is_bounded_and_sdk_retries_are_disabled`)
- Test: `backend/tests/test_llm_deadlines.py`

**Interfaces:**
- Consumes: `classify_endpoint(base_url).local` from `backend/llm_config/local_endpoint.py:85`.
- Produces:
  - `LLMDeadlines(connect: float, first_token: float, idle: float, attempt: float)`, a frozen slotted dataclass, with `http_timeout() -> httpx.Timeout` returning `httpx.Timeout(connect=connect, read=idle, write=WRITE_TIMEOUT_SECONDS, pool=connect)`.
  - `WRITE_TIMEOUT_SECONDS = 30.0`, `REMOTE = LLMDeadlines(10.0, 60.0, 120.0, 600.0)`, `LOCAL = LLMDeadlines(10.0, 180.0, 300.0, 600.0)`.
  - `deadlines_for(base_url: str | None) -> LLMDeadlines`: `LOCAL` when the endpoint classifies as local, else `REMOTE`, then environment overrides.
  - `stream_wait(deadlines: LLMDeadlines, *, now: float, started: float, last_progress: float, progressed: bool) -> tuple[float, str]`: seconds the stream reader may wait, never negative, and which deadline that is: `"first_token"`, `"idle"` or `"attempt"`.
- Environment overrides, each a number of seconds clamped to 1 through 3600, ignored when not a number: `LLM_CONNECT_TIMEOUT_SECONDS`, `LLM_FIRST_TOKEN_TIMEOUT_SECONDS`, `LLM_IDLE_TIMEOUT_SECONDS`, `LLM_ATTEMPT_TIMEOUT_SECONDS`.

- [ ] **Step 1: Write the failing test**

```python
import types
import pytest
from llm_config.client_factory import build_llm_client
from llm_config.deadlines import LOCAL, REMOTE, LLMDeadlines, deadlines_for, stream_wait
from llm_config.types import CredentialSource

ENV = ("LLM_CONNECT_TIMEOUT_SECONDS", "LLM_FIRST_TOKEN_TIMEOUT_SECONDS",
       "LLM_IDLE_TIMEOUT_SECONDS", "LLM_ATTEMPT_TIMEOUT_SECONDS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def test_remote_and_local_defaults():
    assert deadlines_for("https://api.example.com/v1") == LLMDeadlines(10.0, 60.0, 120.0, 600.0)
    assert deadlines_for("http://127.0.0.1:11434/v1") == LLMDeadlines(10.0, 180.0, 300.0, 600.0)
    assert deadlines_for("http://192.168.1.5:8000/v1") == LOCAL
    assert deadlines_for(None) == REMOTE


@pytest.mark.parametrize("raw, expected", [("5", 5.0), ("0", 1.0), ("99999", 3600.0), ("abc", 120.0), ("", 120.0)])
def test_env_override_is_clamped_or_ignored(monkeypatch, raw, expected):
    monkeypatch.setenv("LLM_IDLE_TIMEOUT_SECONDS", raw)
    assert deadlines_for("https://api.example.com/v1").idle == expected


def test_http_timeout_shape():
    timeout = REMOTE.http_timeout()
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (10.0, 120.0, 30.0, 10.0)


def test_stream_wait_picks_the_nearest_deadline():
    assert stream_wait(REMOTE, now=1.0, started=0.0, last_progress=0.0, progressed=False) == (59.0, "first_token")
    assert stream_wait(REMOTE, now=100.0, started=0.0, last_progress=90.0, progressed=True) == (110.0, "idle")
    assert stream_wait(REMOTE, now=595.0, started=0.0, last_progress=594.0, progressed=True) == (5.0, "attempt")
    assert stream_wait(REMOTE, now=700.0, started=0.0, last_progress=699.0, progressed=True) == (0.0, "attempt")


def test_build_llm_client_uses_the_profile_unless_a_timeout_is_given():
    cfg = types.SimpleNamespace(api_key="k", base_url="https://api.example.com/v1", model="m")
    client, _, _ = build_llm_client(cfg, CredentialSource.USER)
    assert client.timeout == REMOTE.http_timeout()
    assert client.max_retries == 0
    explicit, _, _ = build_llm_client(cfg, CredentialSource.USER, timeout=10.0)
    assert explicit.timeout == 10.0
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `t tests/test_llm_deadlines.py`
Expected: an import error for `llm_config.deadlines`.

- [ ] **Step 3: Implement `backend/llm_config/deadlines.py`** to the Interfaces block. `stream_wait` returns the smaller of two remainders: the phase deadline (`first_token` before any progress, `idle` after) minus time since `last_progress`, and `attempt` minus time since `started`. On a tie the phase deadline wins. Clamp at zero.

- [ ] **Step 4: Change `build_llm_client`** so the OpenAI client's `timeout` is `deadlines_for(config.base_url).http_timeout()` when `timeout is None`, and the given float otherwise. Leave the native branch (`client_factory.py:95-103`) and `DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS` as they are. The native client still uses that constant.

- [ ] **Step 5: Update the existing module test** at `backend/llm_config/tests/test_client_factory.py:103-107`. Rename it `test_default_timeout_is_the_deadline_profile_and_sdk_retries_are_disabled` and replace its timeout assertion with:

```python
        assert client.timeout == deadlines_for(_cfg().base_url).http_timeout()
        assert client.max_retries == 0
```

Import `deadlines_for` from `llm_config.deadlines`. Keep the `DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS` import only if another test in the file still uses it.

- [ ] **Step 6: Run both suites and confirm they pass**

Run: `t tests/test_llm_deadlines.py llm_config/tests/test_client_factory.py`
Expected: all passed.

- [ ] **Step 7: Commit**

```bash
git add backend/llm_config/deadlines.py backend/llm_config/client_factory.py \
        backend/llm_config/tests/test_client_factory.py backend/tests/test_llm_deadlines.py
git commit -m "Give model calls separate connect, first-token, idle and attempt deadlines"
```

---

### Task 3: Stream watchdog

**Files:**
- Modify: `backend/orchestrator/orchestrator.py`
  - near line 190, next to `_LLMMalformedResponseError`: add the exception
  - the import block: `from llm_config import deadlines as llm_deadlines`
  - `_call_llm_guarded`, the streaming block at lines 17903-17933
  - `_call_llm_streamed`, lines 18219-18408
- Test: `backend/tests/test_llm_stream_watchdog.py`

**Interfaces:**
- Consumes: `llm_deadlines.deadlines_for`, `llm_deadlines.stream_wait` (task 2). The fake provider (task 1).
- Produces:
  - `class _LLMStreamStalledError(TimeoutError)` with attribute `phase: str`. Being a `TimeoutError`, `_safe_llm_error_metadata` already classes it `transport_error` and retryable.
  - `_call_llm_streamed(..., deadlines: Optional[LLMDeadlines] = None)`. `None` keeps today's unbounded wait, which the three existing direct callers in tests rely on.
- Always call the module function as `llm_deadlines.deadlines_for(...)`, never through a `from` import, so tests can patch it.

- [ ] **Step 1: Write the failing test**

```python
import asyncio
import pytest
from tests.helpers.llm_faults import (
    FaultyCompletions, bare_orch, heartbeat_only, no_retry_sleep, reply, stall, stream, tiny_deadlines, tool_chunk)

USER = [{"role": "user", "content": "hi"}]


@pytest.fixture
def faults(monkeypatch):
    made = []

    def build(script):
        comp = FaultyCompletions(script)
        made.append(comp)
        return comp

    tiny_deadlines(monkeypatch)
    no_retry_sleep(monkeypatch)
    yield build
    for comp in made:
        comp.release.set()


async def call(orch, **kwargs):
    return await asyncio.wait_for(
        orch._call_llm(object(), USER, allow_stream=True, stream_chat_id="chat-1", **kwargs), 5)


async def test_stall_before_first_token_is_retried_as_a_stream(faults):
    comp = faults([stall(), reply("ok")])
    msg, _usage = await call(bare_orch(comp))
    assert msg.content == "ok"
    assert [c.get("stream") for c in comp.calls] == [True, True]


async def test_stall_mid_tool_arguments_retries_and_never_returns_a_partial_call(faults):
    comp = faults([
        stall(1, tool_chunk(0, "call_a", "get_weather", '{"city": "Ro')),
        stream(tool_chunk(0, "call_a", "get_weather", '{"city": "Rome"}'), finish="tool_calls"),
    ])
    msg, _usage = await call(bare_orch(comp))
    assert msg.tool_calls[0].function.arguments == '{"city": "Rome"}'
    assert len(comp.calls) == 2


async def test_heartbeat_chunks_without_progress_still_stall(faults):
    comp = faults([heartbeat_only(), reply("ok")])
    msg, _usage = await call(bare_orch(comp))
    assert msg.content == "ok"
    assert len(comp.calls) == 2


async def test_a_stall_on_every_attempt_fails_as_a_transport_error(faults):
    comp = faults([stall()])
    orch = bare_orch(comp)
    assert await call(orch) == (None, None)
    assert len(comp.calls) == orch.MAX_RETRIES == 3
    assert all(c.get("stream") is True for c in comp.calls)
    assert orch._audits[-1]["upstream_error_class"] == "transport_error"


async def test_cancel_during_stall_returns_at_once(faults):
    comp = faults([stall()])
    orch = bare_orch(comp)
    turn = asyncio.create_task(orch._call_llm(object(), USER, allow_stream=True, stream_chat_id="chat-1"))
    await asyncio.sleep(0.01)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, 1)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `t tests/test_llm_stream_watchdog.py`
Expected: the stall tests fail on the 5-second hang guard, because nothing times the stream out.

- [ ] **Step 3: Add `_LLMStreamStalledError`** next to `_LLMMalformedResponseError`, storing `phase` and passing it to `TimeoutError.__init__`.

- [ ] **Step 4: Bound the wait in `_call_llm_streamed`.** Replace `kind, payload = await queue.get()` (line 18300) with the block below, and set `last_progress, progressed = loop.time(), True` wherever a chunk carries a content delta or a tool-call delta. An empty chunk is not progress.

```python
                if deadlines is None:
                    kind, payload = await queue.get()
                else:
                    wait, phase = llm_deadlines.stream_wait(
                        deadlines, now=loop.time(), started=started,
                        last_progress=last_progress, progressed=progressed)
                    try:
                        kind, payload = await asyncio.wait_for(queue.get(), wait)
                    except asyncio.TimeoutError:
                        raise _LLMStreamStalledError(phase) from None
```

Initialize `started = last_progress = loop.time()` and `progressed = False` before the loop.

- [ ] **Step 5: Add an `except _LLMStreamStalledError:` branch** before the generic `except Exception:` at line 18385. It does what the `CancelledError` branch does (set `abandoned`, cancel `pump_task`, gather it, send the terminal narrative frame if any text was emitted) and re-raises. Cancelling the task returns at once. The worker thread ends on the client's own read timeout.

- [ ] **Step 6: Pass the deadlines and stop falling back on a stall.** In `_call_llm_guarded`, compute `deadlines = llm_deadlines.deadlines_for(getattr(resolved, "base_url", None))` after the client is resolved, pass `deadlines=deadlines` to both `_call_llm_streamed` calls (lines 17908 and 17915), and add `except _LLMStreamStalledError: raise` above `except Exception as _stream_exc:` (line 17919). A stalled provider is retried as a stream by the outer loop. It is not asked again without streaming in the same attempt.

- [ ] **Step 7: Run the new and the existing stream suites**

Run: `t tests/test_llm_stream_watchdog.py tests/test_llm_streaming.py tests/test_llm_streaming_continuation.py tests/test_goal_buffered_transport.py tests/test_call_llm_wave0.py`
Expected: all passed.

- [ ] **Step 8: Commit**

```bash
git add backend/orchestrator/orchestrator.py backend/tests/test_llm_stream_watchdog.py
git commit -m "Time out a model stream that stops making progress"
```

---

### Task 4: No re-run after text has reached the user

**Files:**
- Modify: `backend/orchestrator/orchestrator.py`: `_call_llm_streamed` error branches. `_call_llm_guarded` streaming block. One new method after `_call_llm_guarded`
- Modify: `backend/tests/test_llm_streaming.py:168-186` (`test_mid_stream_error_falls_back_to_non_streaming`)
- Test: `backend/tests/test_llm_stream_interrupted.py`

**Interfaces:**
- Consumes: `_LLMStreamStalledError` (task 3). `tracked_provider` and `create_completion` already in `_call_llm_guarded`.
- Produces:
  - `class _LLMStreamInterrupted(RuntimeError)` with attribute `partial: str`.
  - `async def _continue_interrupted_stream(self, websocket, kwargs: Dict[str, Any], partial: str, invoke) -> Any`, where `invoke(request)` performs one non-streaming completion and returns the provider response. It returns a response object with `choices[0].message`, `choices[0].finish_reason` and `usage`.
  - A message attribute `_astral_incomplete = True` when the continuation failed.
  - Module constants, exact text:
    - `_CONTINUE_PROMPT = "Continue your previous message from exactly where it stopped. Do not repeat any of it and do not restart."`
    - `_INCOMPLETE_NOTICE = "The model connection dropped before this answer finished. The text above may be incomplete."`

**Rule.** "Visible" means narrative frames were sent: `emitted` is true and `_GOAL_BUFFERED_RESPONSE` is false. A stream that fails while visible is never re-run. A stream that fails before anything was visible behaves as today.

The visible text in these tests ends with a newline on purpose. `markdown_safe_prefix_len` (`backend/orchestrator/stream_manager.py:264`) always releases a completed line, so the frame is certain to be sent.

- [ ] **Step 1: Write the failing tests** in `backend/tests/test_llm_stream_interrupted.py`:

```python
import asyncio
import pytest
from orchestrator.orchestrator import _GOAL_BUFFERED_RESPONSE, _INCOMPLETE_NOTICE
from tests.helpers.llm_faults import (
    FaultyCompletions, bare_orch, content_chunk, drop, fail, no_retry_sleep, reply, stall, tiny_deadlines)

USER = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    tiny_deadlines(monkeypatch)
    no_retry_sleep(monkeypatch)


async def call(orch, **kwargs):
    return await asyncio.wait_for(
        orch._call_llm(object(), USER, allow_stream=True, stream_chat_id="chat-1", **kwargs), 5)


async def test_drop_after_visible_text_continues_without_rerunning():
    comp = FaultyCompletions([drop(1, content_chunk("First line.\n")), reply("Second line.")])
    msg, _usage = await call(bare_orch(comp))
    assert msg.content == "First line.\nSecond line."
    assert len(comp.calls) == 2
    assert "stream" not in comp.calls[1]
    assert comp.calls[1]["messages"][:1] == USER
    assert comp.calls[1]["messages"][-2] == {"role": "assistant", "content": "First line.\n"}
    assert comp.calls[1]["messages"][-1]["role"] == "user"
    assert not getattr(msg, "_astral_incomplete", False)


async def test_stall_after_visible_text_continues():
    comp = FaultyCompletions([stall(1, content_chunk("First line.\n")), reply("Second line.")])
    try:
        msg, _usage = await call(bare_orch(comp))
    finally:
        comp.release.set()
    assert msg.content == "First line.\nSecond line."


async def test_failed_continuation_keeps_the_partial_and_says_so():
    comp = FaultyCompletions([drop(1, content_chunk("First line.\n")), fail(ConnectionError("down"))])
    orch = bare_orch(comp)
    msg, _usage = await call(orch)
    assert msg.content == "First line.\n"
    assert msg._astral_incomplete is True
    assert len(comp.calls) == 2
    assert orch._rendered[-1][0]["message"] == _INCOMPLETE_NOTICE
    assert orch._rendered[-1][0]["variant"] == "warning"


async def test_continuation_keeps_the_tools():
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}]
    comp = FaultyCompletions([drop(1, content_chunk("First line.\n")), reply("Second line.")])
    await call(bare_orch(comp), tools_desc=tools)
    assert comp.calls[1]["tools"] == tools


async def test_buffered_goal_stream_is_retried_not_continued():
    token = _GOAL_BUFFERED_RESPONSE.set(True)
    try:
        comp = FaultyCompletions([drop(1, content_chunk("Hello")), reply("full answer")])
        msg, _usage = await call(bare_orch(comp))
    finally:
        _GOAL_BUFFERED_RESPONSE.reset(token)
    assert msg.content == "full answer"
    assert comp.calls[1]["messages"] == USER
```

- [ ] **Step 2: Replace the existing test** `test_mid_stream_error_falls_back_to_non_streaming` in `backend/tests/test_llm_streaming.py` with two. Keep the sentinel check in both.

```python
async def test_mid_stream_error_before_visible_text_falls_back_to_non_streaming(caplog):
    sentinel = "SENTINEL_STREAM_BODY_MUST_NOT_ESCAPE"
    comp = _FakeCompletions(chunks=[_content_chunk("partial ")], raise_after=0,
                            content="recovered", error_message=sentinel)
    orch = _bare_orch(comp)
    msg, _usage = await orch._call_llm(
        object(), [{"role": "user", "content": "hi"}], allow_stream=True, stream_chat_id="chat-1")
    assert msg.content == "recovered"
    assert comp.calls[0].get("stream") is True
    assert "stream" not in comp.calls[-1]
    assert _stream_frames(orch) == []
    assert sentinel not in caplog.text


async def test_mid_stream_error_after_visible_text_continues_without_rerun(caplog):
    sentinel = "SENTINEL_STREAM_BODY_MUST_NOT_ESCAPE"
    comp = _FakeCompletions(chunks=[_content_chunk("First line.\n"), _content_chunk("lost")], raise_after=1,
                            content="Second line.", error_message=sentinel)
    orch = _bare_orch(comp)
    msg, _usage = await orch._call_llm(
        object(), [{"role": "user", "content": "hi"}], allow_stream=True, stream_chat_id="chat-1")
    assert msg.content == "First line.\nSecond line."
    assert len(comp.calls) == 2
    assert _stream_frames(orch)[-1]["terminal"] is True
    assert sentinel not in caplog.text
```

- [ ] **Step 3: Run and confirm they fail**

Run: `t tests/test_llm_stream_interrupted.py tests/test_llm_streaming.py`
Expected: the new tests fail (`_INCOMPLETE_NOTICE` missing, then content mismatches). The unchanged tests pass.

- [ ] **Step 4: Raise `_LLMStreamInterrupted` from `_call_llm_streamed`.** In the stall branch (task 3) and the generic `except Exception:` branch, after the cleanup they already do: when `emitted and not buffer_narrative`, set `_NARRATIVE_STREAMED.set(True)` and `raise _LLMStreamInterrupted("".join(content_parts)) from None`. Otherwise re-raise as today. Leave the `CancelledError` branch alone.

- [ ] **Step 5: Implement `_continue_interrupted_stream`.** Build `request = {**kwargs, "messages": [*kwargs["messages"], {"role": "assistant", "content": partial}, {"role": "user", "content": _CONTINUE_PROMPT}]}` and call `invoke(request)` once.
  - On success, set the returned message's `content` to `partial + (content or "")` and return the response.
  - On any exception other than `asyncio.CancelledError`: log the exception class only. If `websocket is not None`, send `Alert(message=_INCOMPLETE_NOTICE, variant="warning").to_dict()` with `self.send_ui_render(websocket, [...], target="chat")`. Build the message with `self._assemble_streamed_message(partial, {})`, set `_astral_incomplete` on it with `object.__setattr__`, and return a response with `finish_reason="length"` and `usage=None`.

- [ ] **Step 6: Use it in `_call_llm_guarded`.** Above the `except _LLMStreamStalledError: raise` added in task 3, add:

```python
                    except _LLMStreamInterrupted as interrupted:
                        response = await self._continue_interrupted_stream(
                            websocket, kwargs, interrupted.partial,
                            lambda request: tracked_provider(
                                lambda: create_completion(client, **request), request))
```

The response then flows through the existing checks, audit and return.

- [ ] **Step 7: Run and confirm they pass**

Run: `t tests/test_llm_stream_interrupted.py tests/test_llm_streaming.py tests/test_llm_stream_watchdog.py tests/test_call_llm_wave0.py`
Expected: all passed.

- [ ] **Step 8: Commit**

```bash
git add backend/orchestrator/orchestrator.py backend/tests/test_llm_stream_interrupted.py backend/tests/test_llm_streaming.py
git commit -m "Continue an interrupted answer instead of re-running it"
```

---

### Task 5: Retry delay and the fallback chain

**Files:**
- Create: `backend/orchestrator/llm_recovery.py`
- Modify: `backend/orchestrator/orchestrator.py`: import `llm_recovery` where `model_router` is imported. `_call_llm_guarded` after the router block (line 17833), the give-up branch (line 18088) and the backoff line (18111). The chat-turn setup at `denial_tracker: Dict[str, int] = {}` (line 15905)
- Test: `backend/tests/test_llm_recovery.py`

**Interfaces:**
- Consumes: `_SafeLLMErrorMetadata.status_code` and `.upstream_error_class` (`orchestrator.py:179`). `_probe_state` inside `_call_llm_guarded`.
- Produces, in `orchestrator.llm_recovery`:
  - `MAX_FALLBACKS = 3`, `RETRY_AFTER_CAP_SECONDS = 60.0`.
  - `fallback_models(primary: str) -> list[str]`: the comma-separated names in `LLM_FALLBACK_MODELS`, stripped, with blanks, duplicates and `primary` removed, at most `MAX_FALLBACKS`. Unset means `[]`, which leaves today's behavior unchanged.
  - `fallback_eligible(status_code: int | None, upstream_error_class: str) -> bool`: true for `transport_error`, `rate_limit`, `model_not_found`, and any status 500 through 599. False otherwise, so 400, 401 and 403 never fall back.
  - `retry_after_seconds(exc: BaseException) -> float | None`: `exc.response.headers["retry-after"]` as a non-negative number of seconds. `None` for anything else, including an HTTP date.
  - `retry_delay(attempt: int, retry_after: float | None, jitter: float) -> float`: `min(retry_after, RETRY_AFTER_CAP_SECONDS)` when `retry_after` is given, else `min(2 ** (attempt - 1), 8) * jitter`.
  - `TURN_MODEL: contextvars.ContextVar[str | None]` (default `None`) and `reset_turn() -> None`.
  - `fallback_notice(primary: str, fallback: str) -> str` returning exactly `f"{primary} did not respond. This turn is using {fallback}."`.
- The chain applies only to a plain call: no harness binding, no persistent dispatch, not the native client.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `t tests/test_llm_recovery.py`
Expected: an import error for `orchestrator.llm_recovery`.

- [ ] **Step 3: Implement `backend/orchestrator/llm_recovery.py`** to the Interfaces block.

- [ ] **Step 4: Pin and build the chain in `_call_llm_guarded`.** After the router block ends (line 17833) and before `cap_key, extra_kwargs = _probe_state(call_model)` (line 17844):

```python
        plain_call = harness_binding is None and persistent_dispatch is None and not native_call
        chain = llm_recovery.fallback_models(resolved.model) if plain_call else []
        pinned = llm_recovery.TURN_MODEL.get()
        if pinned in chain:
            chain = chain[chain.index(pinned) + 1:]
            call_model = pinned
```

- [ ] **Step 5: Fall back before giving up.** At the top of the branch `if not error.retryable or attempt == attempt_limit:` (line 18088), before the existing body:

```python
                    if chain and llm_recovery.fallback_eligible(error.status_code, error.upstream_error_class):
                        failed_model, call_model = call_model, chain.pop(0)
                        await self._record_llm_call(
                            self.audit_recorder, actor_user_id=actor_user_id, auth_principal=auth_principal,
                            feature=feature, credential_source=source, resolved=resolved,
                            routed_model=failed_model, total_tokens=None, outcome="failure",
                            upstream_error_class=error.upstream_error_class)
                        first_switch = llm_recovery.TURN_MODEL.get() is None
                        llm_recovery.TURN_MODEL.set(call_model)
                        cap_key, extra_kwargs = _probe_state(call_model)
                        attempt = 0
                        if first_switch and websocket is not None:
                            await self.send_ui_render(websocket, [Alert(
                                message=llm_recovery.fallback_notice(resolved.model, call_model),
                                variant="info").to_dict()], target="chat")
                        continue
```

- [ ] **Step 6: Use `retry_delay`.** Replace line 18111 with `backoff = llm_recovery.retry_delay(attempt, llm_recovery.retry_after_seconds(e), random.uniform(0.8, 1.2))`.

- [ ] **Step 7: Reset per chat turn.** Next to `denial_tracker: Dict[str, int] = {}` (line 15905), call `llm_recovery.reset_turn()`.

- [ ] **Step 8: Run the new and existing suites**

Run: `t tests/test_llm_recovery.py tests/test_call_llm_wave0.py tests/test_model_router.py tests/test_model_router_audit.py`
Expected: all passed. The existing retry-budget tests still see three attempts because no chain is configured.

- [ ] **Step 9: Commit**

```bash
git add backend/orchestrator/llm_recovery.py backend/orchestrator/orchestrator.py backend/tests/test_llm_recovery.py
git commit -m "Honor retry-after and fall back to a configured model for the rest of a turn"
```

---

### Task 6: Tool-argument pipeline

**Files:**
- Create: `backend/orchestrator/tool_arguments.py`
- Test: `backend/tests/test_tool_arguments.py`

**Interfaces:**
- Consumes: `jsonschema.Draft202012Validator`.
- Produces:
  - `ArgumentError(code: str, field: str, message: str, example: dict | None)`, frozen. `code` is `"invalid_json"`, `"not_an_object"` or `"schema_mismatch"`. `field` is a slash-separated path, `""` for the whole object.
  - `ArgumentResult(args: dict | None, repaired: bool, error: ArgumentError | None)`, frozen.
  - `prepare_arguments(raw: str | dict | None, schema: dict | None) -> ArgumentResult`.
  - `example_for(schema: dict) -> dict`: one entry per required property: the first `enum` value, else the first of `examples`, else `default`, else by type: `"<string>"`, `0`, `0`, `False`, `[]`, `{}`.
  - `model_error_text(tool_name: str, error: ArgumentError) -> str` returning exactly `f"The arguments for '{tool_name}' were not run: {error.message} Send the call again with arguments like {json.dumps(error.example)}."`, without the last sentence when `example` is `None`.

**The pipeline, in order.** This is the algorithm. Tests pin each rule.

1. `None`, `""` or whitespace becomes `{}`. A `dict` is used as given.
2. Parse with `json.loads(raw, strict=False)`. This accepts raw control characters inside strings and is not a repair.
3. If that fails, apply each repair in order to the text so far (they accumulate), re-parsing after each, and stop at the first that parses:
   a. strip a surrounding Markdown code fence.
   b. take the first balanced `{...}` block (the logic of `Orchestrator._extract_json_block`, `orchestrator.py:18541`).
   c. remove a comma that directly precedes `}` or `]`, outside strings.
   d. replace the bare words `True`, `False`, `None` with `true`, `false`, `null`, outside strings.
   Never append a missing quote, bracket or brace. If nothing parses, the error is `invalid_json`, and `message` is the first line of the decoder's error for the original text.
4. If the parsed value is a string that itself parses to an object, use that object.
5. If the value is not an object, the error is `not_an_object`.
6. If `schema` is a dict that passes `Draft202012Validator.check_schema`, coerce only where the schema declares the type, recursing into `properties` and `items`:
   - string to integer when it matches `-?\d+`. String to number when `float()` accepts it and it is not an integer field.
   - `"true"` or `"false"` to boolean.
   - A string that parses to an object or array, when the schema says object or array.
   - An integer to a string, when the schema says string.
   When the schema sets `additionalProperties` to `false`, drop properties it does not declare.
   Then validate. Report the first error ordered by path then validator: `required` gives `missing required field '<name>'.` with `field` set to that name. `type` gives `field '<path>' must be <type>.`. `enum` gives `field '<path>' must be one of <json list>.`. Anything else gives the validator's message cut to 200 characters. `example` is `example_for(schema)`.
7. `repaired` is true when step 3, 4 or 6 changed anything.
8. A schema that is not a dict, or fails `check_schema`, is skipped: a broken schema never fails a call.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from orchestrator.tool_arguments import (
    ArgumentError, ArgumentResult, example_for, model_error_text, prepare_arguments)

SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"}, "days": {"type": "integer"}, "ratio": {"type": "number"},
        "metric": {"type": "boolean"}, "unit": {"type": "string", "enum": ["c", "f"]},
        "zip": {"type": "string"}, "filters": {"type": "object"},
    },
    "required": ["city"],
}


def test_valid_json_passes_untouched():
    assert prepare_arguments('{"city": "Rome"}', SCHEMA) == ArgumentResult({"city": "Rome"}, False, None)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_empty_becomes_an_empty_object(raw):
    assert prepare_arguments(raw, None) == ArgumentResult({}, False, None)


def test_a_dict_is_used_as_given():
    assert prepare_arguments({"city": "Rome"}, SCHEMA).args == {"city": "Rome"}


def test_raw_control_characters_are_accepted_without_repair():
    assert prepare_arguments('{"city": "a\nb"}', None) == ArgumentResult({"city": "a\nb"}, False, None)


@pytest.mark.parametrize("raw", [
    '{"city": "Rome",}',
    '```json\n{"city": "Rome"}\n```',
    'Here you go: {"city": "Rome"} thanks',
    '"{\\"city\\": \\"Rome\\"}"',
])
def test_mechanical_repairs(raw):
    assert prepare_arguments(raw, SCHEMA) == ArgumentResult({"city": "Rome"}, True, None)


def test_python_literals_are_repaired():
    result = prepare_arguments('{"city": "Rome", "metric": True, "filters": None}', None)
    assert result == ArgumentResult({"city": "Rome", "metric": True, "filters": None}, True, None)


def test_truncated_json_is_not_completed():
    result = prepare_arguments('{"city": "Rome", "zip": "405', SCHEMA)
    assert result.args is None and result.error.code == "invalid_json"


def test_a_non_object_is_refused():
    assert prepare_arguments("[1, 2]", SCHEMA).error.code == "not_an_object"


def test_coercion_follows_the_schema():
    raw = '{"city": "Rome", "days": "3", "ratio": "0.5", "metric": "true", "zip": 40506, "filters": "{\\"a\\": 1}"}'
    assert prepare_arguments(raw, SCHEMA) == ArgumentResult(
        {"city": "Rome", "days": 3, "ratio": 0.5, "metric": True, "zip": "40506", "filters": {"a": 1}}, True, None)


def test_repair_never_guesses():
    assert prepare_arguments('{"city": "007"}', SCHEMA) == ArgumentResult({"city": "007"}, False, None)
    assert prepare_arguments('{"city": "Rome", "days": "3.5"}', SCHEMA).error.field == "days"
    assert prepare_arguments('{"city": "Rome", "days": "3"}', None).args == {"city": "Rome", "days": "3"}


def test_extras_are_dropped_only_when_the_schema_forbids_them():
    closed = {**SCHEMA, "additionalProperties": False}
    assert prepare_arguments('{"city": "Rome", "x": 1}', closed) == ArgumentResult({"city": "Rome"}, True, None)
    assert prepare_arguments('{"city": "Rome", "x": 1}', SCHEMA).args == {"city": "Rome", "x": 1}


def test_missing_required_field():
    error = prepare_arguments("{}", SCHEMA).error
    assert error == ArgumentError("schema_mismatch", "city", "missing required field 'city'.", {"city": "<string>"})


def test_enum_and_type_messages():
    assert prepare_arguments('{"city": "Rome", "unit": "k"}', SCHEMA).error.message == \
        "field 'unit' must be one of [\"c\", \"f\"]."
    assert prepare_arguments('{"city": ["Rome"]}', SCHEMA).error.message == "field 'city' must be string."


def test_a_broken_schema_never_fails_a_call():
    assert prepare_arguments('{"a": 1}', {"type": "nonsense"}).args == {"a": 1}
    assert prepare_arguments('{"a": 1}', "not a schema").args == {"a": 1}


def test_example_for_uses_enum_then_type():
    schema = {"type": "object", "required": ["unit", "days", "tags"],
              "properties": {"unit": {"enum": ["c", "f"]}, "days": {"type": "integer"}, "tags": {"type": "array"}}}
    assert example_for(schema) == {"unit": "c", "days": 0, "tags": []}


def test_model_error_text():
    error = ArgumentError("schema_mismatch", "city", "missing required field 'city'.", {"city": "<string>"})
    assert model_error_text("get_weather", error) == (
        "The arguments for 'get_weather' were not run: missing required field 'city'. "
        "Send the call again with arguments like {\"city\": \"<string>\"}.")
    bare = ArgumentError("invalid_json", "", "Expecting value: line 1 column 1 (char 0)", None)
    assert model_error_text("get_weather", bare) == (
        "The arguments for 'get_weather' were not run: Expecting value: line 1 column 1 (char 0)")
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `t tests/test_tool_arguments.py`
Expected: an import error for `orchestrator.tool_arguments`.

- [ ] **Step 3: Implement `backend/orchestrator/tool_arguments.py`** to the Interfaces block and the eight pipeline rules. Keep each repair a separate small function so a reader can see the order.

- [ ] **Step 4: Run it and confirm it passes**

Run: `t tests/test_tool_arguments.py`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add backend/orchestrator/tool_arguments.py backend/tests/test_tool_arguments.py
git commit -m "Add a repair-and-validate pipeline for tool arguments"
```

---

### Task 7: Prepare tool calls before they are recorded and dispatched

**Files:**
- Modify: `backend/orchestrator/orchestrator.py`
  - import `prepare_arguments`, `model_error_text` and `ArgumentError` from `orchestrator.tool_arguments`
  - new methods next to `_tool_accepts_context_arg` (line 19062)
  - the chat loop, directly above `history_assistant_message = _history_assistant_message(llm_msg)` (line 16517)
  - `execute_single_tool`, directly after `raw_arguments = tool_call.function.arguments` (line 19939)
  - `execute_parallel_tools`, the per-call block at lines 20825-20844
- Test: `backend/tests/test_tool_argument_dispatch.py`

**Interfaces:**
- Consumes: `prepare_arguments`, `model_error_text`, `ArgumentError` (task 6). `_call_llm_json` (`orchestrator.py:18511`). `current_dispatch` from `persistent_agents.dispatch_context`.
- Produces:
  - `_tool_input_schema(self, agent_id: Optional[str], tool_name: str) -> Optional[dict]`: the matching skill's `input_schema` from `self.agent_cards`, matched by `skill.id` or `skill.name` as `_tool_accepts_context_arg` does. `None` when there is no agent, card, skill or dict schema.
  - `async def _regenerate_tool_arguments(self, websocket, tool_name: str, schema: Optional[dict], raw: Any, error: ArgumentError) -> Optional[dict]`.
  - `async def _prepare_tool_calls(self, websocket, tool_calls, tool_to_agent: Dict, tool_to_unqualified: Optional[Dict[str, str]]) -> None`.
  - `_invalid_arguments_response(self, tool_name: str, error: ArgumentError) -> MCPResponse` with `error = {"code": "invalid_tool_arguments", "message": model_error_text(tool_name, error), "retryable": True, "effect_state": "not_started", "data": {"field": error.field, "reason": error.code}}`.
  - A tool-call attribute `_astral_argument_error: ArgumentError`, set with `object.__setattr__`, read with `getattr(..., None)`.

**Why a pre-pass.** The assistant message is copied into history at line 16517, before dispatch. Repairing at dispatch would leave history showing `{}` for a call that then ran with real arguments. Repairing first means history, the goal harness and the tool trace all see the arguments that ran.

**Rules for `_prepare_tool_calls`,** for each call:
- Resolve `tool_name` and `agent_id` as `execute_single_tool` does at lines 19924-19929.
- Skip `computer-use-1` / `write_file`. Its own size and schema checks at lines 19955-20041 stay as they are.
- `result = prepare_arguments(call.function.arguments, self._tool_input_schema(agent_id, tool_name))`.
- On an error, try `_regenerate_tool_arguments` once. If it returns `None`, set `_astral_argument_error` and leave the arguments untouched.
- When arguments were repaired or regenerated, write `json.dumps(args, ensure_ascii=False)` back to `call.function.arguments` and log `tool_arg_repaired tool=%s kind=%s` with kind `repaired` or `regenerated`. Never log values.

**Rules for `_regenerate_tool_arguments`.** Return `None` without a model call when `schema` is not a dict, when `raw` is not a string or is longer than 8,000 characters, or when `current_dispatch()` is not `None`. Otherwise call, once:

```python
        result = await self._call_llm_json(
            websocket,
            [{"role": "system", "content": _ARGUMENT_REPAIR_PROMPT},
             {"role": "user", "content": json.dumps(
                 {"tool": tool_name, "problem": error.message, "original_arguments": raw})}],
            schema=schema, schema_name="tool_arguments", temperature=0, feature="tool_argument_repair")
```

with the module constant, exact text:

`_ARGUMENT_REPAIR_PROMPT = "You repair the arguments of one tool call. Reply with one JSON object that matches the schema. Use only values that appear in the original arguments. If a required value is not there, reply with {}."`

Accept the result only if it is a dict and `prepare_arguments(result, schema)` has no error. Return those arguments. Any exception other than `asyncio.CancelledError` returns `None`.

- [ ] **Step 1: Write the failing test**

```python
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from orchestrator.orchestrator import Orchestrator
from orchestrator.tool_arguments import ArgumentError, model_error_text

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
          "required": ["city"]}


def call(tool, arguments):
    return SimpleNamespace(id="c1", function=SimpleNamespace(name=tool, arguments=arguments))


def bare(regenerated=None):
    orch = Orchestrator.__new__(Orchestrator)
    skill = SimpleNamespace(id="get_weather", name="get_weather", input_schema=SCHEMA)
    orch.agent_cards = {"weather-1": SimpleNamespace(skills=[skill])}
    orch._call_llm_json = AsyncMock(return_value=regenerated)
    return orch


async def prepare(orch, tc, agent="weather-1"):
    await orch._prepare_tool_calls(None, [tc], {tc.function.name: agent}, None)
    return tc


async def test_repaired_arguments_are_written_back():
    orch = bare()
    tc = await prepare(orch, call("get_weather", '{"city": "Rome",}'))
    assert tc.function.arguments == '{"city": "Rome"}'
    assert getattr(tc, "_astral_argument_error", None) is None
    orch._call_llm_json.assert_not_awaited()


async def test_schema_coercion_is_written_back():
    tc = await prepare(bare(), call("get_weather", '{"city": "Rome", "days": "3"}'))
    assert json.loads(tc.function.arguments) == {"city": "Rome", "days": 3}


async def test_valid_arguments_are_left_byte_for_byte():
    tc = await prepare(bare(), call("get_weather", '{"city":  "Rome"}'))
    assert tc.function.arguments == '{"city":  "Rome"}'


async def test_regeneration_is_used_once_when_repair_fails():
    orch = bare(regenerated={"city": "Rome"})
    tc = await prepare(orch, call("get_weather", "city=Rome"))
    assert json.loads(tc.function.arguments) == {"city": "Rome"}
    orch._call_llm_json.assert_awaited_once()
    assert orch._call_llm_json.await_args.kwargs["schema"] == SCHEMA
    assert orch._call_llm_json.await_args.kwargs["feature"] == "tool_argument_repair"


async def test_failed_regeneration_marks_the_call_and_keeps_the_raw_text():
    tc = await prepare(bare(regenerated=None), call("get_weather", "city=Rome"))
    assert tc.function.arguments == "city=Rome"
    assert tc._astral_argument_error.code == "invalid_json"


async def test_regenerated_arguments_that_fail_the_schema_are_rejected():
    tc = await prepare(bare(regenerated={"city": ["Rome"]}), call("get_weather", "{}"))
    assert tc._astral_argument_error.code == "schema_mismatch"


async def test_write_file_is_left_to_its_own_checks():
    orch = bare()
    tc = await prepare(orch, call("write_file", '{"path": "/x",'), agent="computer-use-1")
    assert tc.function.arguments == '{"path": "/x",' and not hasattr(tc, "_astral_argument_error")
    orch._call_llm_json.assert_not_awaited()


async def test_a_tool_without_a_schema_is_repaired_but_never_regenerated():
    orch = bare(regenerated={"city": "Rome"})
    fixed = await prepare(orch, call("mystery", '{"a": 1,}'), agent=None)
    assert fixed.function.arguments == '{"a": 1}'
    broken = await prepare(orch, call("mystery", "a=1"), agent=None)
    assert broken._astral_argument_error.code == "invalid_json"
    orch._call_llm_json.assert_not_awaited()


async def test_oversized_raw_arguments_are_not_sent_for_regeneration():
    orch = bare(regenerated={"city": "Rome"})
    tc = await prepare(orch, call("get_weather", "x" * 8001))
    assert tc._astral_argument_error.code == "invalid_json"
    orch._call_llm_json.assert_not_awaited()


@pytest.fixture
def orch(orchestrator_factory, monkeypatch):
    monkeypatch.setenv("DELEGATION_REQUIRED", "false")
    built = orchestrator_factory()
    built.audit_recorder = MagicMock()
    built.audit_recorder.record = AsyncMock()
    built.send_ui_render = AsyncMock()
    built.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    built.tool_permissions.get_tool_scope = MagicMock(return_value="tools:read")
    built.tool_permissions.get_tool_scope_map = MagicMock(
        side_effect=lambda agent_id: {"t1": "tools:read"} if agent_id == "a1" else {})
    built._map_file_paths = lambda cid, a, **k: a
    built.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    built.local_agents["a1"] = MagicMock()
    return built


async def test_a_marked_call_is_refused_the_same_way_on_both_paths(orch):
    error = ArgumentError("schema_mismatch", "city", "missing required field 'city'.", {"city": "<string>"})
    marked = call("t1", "{}")
    object.__setattr__(marked, "_astral_argument_error", error)
    single = await orch.execute_single_tool(MagicMock(), marked, {"t1": "a1"}, "c1", user_id="u1")
    parallel = (await orch.execute_parallel_tools(MagicMock(), [marked], {"t1": "a1"}, "c1", user_id="u1"))[0]
    for response in (single, parallel):
        assert response.error["code"] == "invalid_tool_arguments"
        assert response.error["message"] == model_error_text("t1", error)
        assert response.error["effect_state"] == "not_started"
        assert response.error["data"] == {"field": "city", "reason": "schema_mismatch"}


async def test_parallel_path_accepts_raw_control_characters(orch):
    results = await orch.execute_parallel_tools(
        MagicMock(), [call("t1", '{"q": "a\nb"}')], {"t1": "ghost"}, "c1", user_id="u1")
    assert "not valid JSON" not in ((results[0].error or {}).get("message", "") if results[0] else "")
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `t tests/test_tool_argument_dispatch.py`
Expected: `AttributeError: ... has no attribute '_prepare_tool_calls'`.

- [ ] **Step 3: Implement `_tool_input_schema`, `_regenerate_tool_arguments`, `_prepare_tool_calls` and `_invalid_arguments_response`** to the Interfaces block and the rules above.

- [ ] **Step 4: Call the pre-pass in the chat loop.** The narrative stream context is reset at line 16379 (`_NARRATIVE_STREAM_CHAT.reset(_stream_token)`), so a regeneration call made after that point cannot stream text to the user. Keep the pre-pass after that reset. Directly above line 16517:

```python
                    await self._prepare_tool_calls(websocket, llm_msg.tool_calls, tool_to_agent, tool_to_unqualified)
```

- [ ] **Step 5: Refuse a marked call on the sequential path.** In `execute_single_tool`, directly after `raw_arguments = tool_call.function.arguments`:

```python
        argument_error = getattr(tool_call, "_astral_argument_error", None)
        if argument_error is not None and not is_computer_write:
            refused = self._invalid_arguments_response(tool_name, argument_error)
            await self.send_ui_render(websocket, [Alert(message=refused.error["message"], variant="error").to_dict()])
            return refused
```

Leave the existing `except json.JSONDecodeError` branch in place. It still serves callers that do not run the pre-pass.

- [ ] **Step 6: Do the same on the parallel path and match the parser.** In `execute_parallel_tools`, before `try: args = json.loads(...)` (line 20832), add the same check using the `_arg_err` pattern already there (a coroutine that returns the response, appended to `prepared`, then `continue`), with no alert, as today. Change line 20833 to `json.loads(tc.function.arguments, strict=False)` so both paths parse alike.

- [ ] **Step 7: Run the new and the neighbouring suites**

Run: `t tests/test_tool_argument_dispatch.py tests/test_dispatch_parity.py tests/test_tool_feedback.py tests/test_history_assistant_message_104.py tests/test_goal_progress.py`
Expected: all passed.

- [ ] **Step 8: Commit**

```bash
git add backend/orchestrator/orchestrator.py backend/tests/test_tool_argument_dispatch.py
git commit -m "Repair or regenerate tool arguments before a call is recorded and dispatched"
```

---

### Task 8: Repeat guard for every turn

**Files:**
- Create: `backend/orchestrator/repeat_guard.py`
- Modify: `backend/orchestrator/goal_progress.py`: one public static method next to `is_approval_pending` (line 131)
- Modify: `backend/orchestrator/orchestrator.py`
  - import `repeat_guard` where `model_router` is imported
  - chat-turn setup at `denial_tracker: Dict[str, int] = {}` (line 15905)
  - `execute_single_tool`, after the check added in task 7
  - `execute_parallel_tools`, after the check added in task 7
  - a new method `_apply_repeat_guard`, and its call in the chat loop directly after `self._append_tool_images(messages, llm_msg.tool_calls, tool_results)` (line 16707)
- Test: `backend/tests/test_repeat_guard.py`

**Interfaces:**
- Consumes: `GoalProgress.is_approval_pending` (`backend/orchestrator/goal_progress.py:131`). `REPEATED_FAILURES_REASON` (`backend/orchestrator/goal_execution.py:42`).
- Produces, in `orchestrator.repeat_guard`:
  - `WARN_AT = 2`, `BLOCK_AT = 3`, `STOP_AT = 6`.
  - `class RepeatGuard` with `observe(tool_name: str, raw_arguments: str | dict | None, failed: bool) -> str` returning `"ok"`, `"warn"`, `"block"` or `"stop"`, and `blocked(tool_name, raw_arguments) -> bool`.
  - `start_turn() -> RepeatGuard` (installs a fresh guard in a context variable) and `current() -> RepeatGuard | None`.
  - `GoalProgress.has_error(result: object) -> bool`, a static method returning `bool(GoalProgress._unwrap(result)[1])`. A tool can report failure in `error`, inside `result`, or as an error alert component. `_unwrap` (line 680) already reads all three.
  - `is_failure(result: object) -> bool`: true when `result is None` or `GoalProgress.has_error(result)`, unless `GoalProgress.is_approval_pending(result)`.
  - `warn_text(name: str) -> str` returning exactly `f"The last call to '{name}' failed twice with the same arguments. Do not send it again unchanged. Fix the arguments, use a different tool, or explain what is blocking you."`
  - `blocked_text(name: str) -> str` returning exactly `f"This exact call to '{name}' already failed twice and was not run again. Change the arguments or the approach."`
  - `STOP_NOTICE = "Stopped because the same failing action kept being repeated. Nothing further was run."`
- Produces, on `Orchestrator`: `_apply_repeat_guard(self, tool_calls, tool_results, tool_to_unqualified, messages) -> bool`, true when the turn must stop.

**Semantics.**
- A call's identity is the tool name plus its arguments. Parseable arguments are compared as canonical JSON (keys sorted), so key order and whitespace do not matter. Unparseable arguments are compared as stripped raw text.
- `observe` with `failed=False` clears the count for that call and returns `"ok"`.
- `observe` with `failed=True` adds one. The count after adding decides: 1 is `"ok"`, 2 is `"warn"`, 3 through 5 is `"block"`, 6 or more is `"stop"`.
- `blocked` is true once a call has failed `WARN_AT` times. Dispatch refuses it without running it, and that refusal is observed as another failure. So an identical failing call is dispatched at most twice.
- Tool names are the unqualified names on both sides.

- [ ] **Step 1: Write the failing test**

```python
from types import SimpleNamespace
from orchestrator import repeat_guard
from orchestrator.orchestrator import Orchestrator
from orchestrator.repeat_guard import RepeatGuard
from shared.protocol import MCPResponse

ARGS = '{"path": "/tmp/x"}'


def call(name, arguments):
    return SimpleNamespace(id="c", function=SimpleNamespace(name=name, arguments=arguments))


def test_two_failures_warn_then_block_then_stop():
    guard = RepeatGuard()
    assert not guard.blocked("read_file", ARGS)
    verdicts = [guard.observe("read_file", ARGS, True) for _ in range(6)]
    assert verdicts == ["ok", "warn", "block", "block", "block", "stop"]
    assert guard.blocked("read_file", ARGS)


def test_blocked_only_after_the_second_failure():
    guard = RepeatGuard()
    guard.observe("read_file", ARGS, True)
    assert not guard.blocked("read_file", ARGS)
    guard.observe("read_file", ARGS, True)
    assert guard.blocked("read_file", ARGS)


def test_a_success_clears_the_count():
    guard = RepeatGuard()
    guard.observe("read_file", ARGS, True)
    guard.observe("read_file", ARGS, True)
    assert guard.observe("read_file", ARGS, False) == "ok"
    assert not guard.blocked("read_file", ARGS)


def test_identity_ignores_key_order_and_whitespace_but_not_values():
    guard = RepeatGuard()
    guard.observe("t", '{"a":1,"b":2}', True)
    assert guard.observe("t", '{ "b": 2, "a": 1 }', True) == "warn"
    assert guard.observe("t", '{"a":1,"b":3}', True) == "ok"
    assert guard.observe("other", '{"a":1,"b":2}', True) == "ok"


def test_unparseable_arguments_are_compared_as_text():
    guard = RepeatGuard()
    guard.observe("t", "city=Rome", True)
    assert guard.observe("t", " city=Rome ", True) == "warn"


def test_start_turn_installs_a_fresh_guard():
    first = repeat_guard.start_turn()
    assert repeat_guard.current() is first
    assert repeat_guard.start_turn() is not first


def test_is_failure():
    assert repeat_guard.is_failure(None)
    assert repeat_guard.is_failure(MCPResponse(error={"message": "boom"}))
    assert repeat_guard.is_failure(MCPResponse(result={"error": {"message": "boom"}}))
    assert repeat_guard.is_failure(MCPResponse(
        ui_components=[{"type": "alert", "variant": "error", "message": "boom"}]))
    assert not repeat_guard.is_failure(MCPResponse(result={"ok": True}))
    assert not repeat_guard.is_failure(MCPResponse(result={"status": "confirmation_required"}))


def test_apply_repeat_guard_warns_once_then_stops_the_turn():
    orch = Orchestrator.__new__(Orchestrator)
    repeat_guard.start_turn()
    failed = MCPResponse(error={"message": "boom"})
    messages = []
    stops = [orch._apply_repeat_guard([call("read_file", ARGS)], [failed], None, messages) for _ in range(6)]
    assert stops == [False, False, False, False, False, True]
    assert [m["content"] for m in messages] == [repeat_guard.warn_text("read_file")]


def test_apply_repeat_guard_without_a_guard_does_nothing():
    repeat_guard._CURRENT.set(None)
    orch = Orchestrator.__new__(Orchestrator)
    assert orch._apply_repeat_guard([call("t", "{}")], [None], None, []) is False
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `t tests/test_repeat_guard.py`
Expected: an import error for `orchestrator.repeat_guard`.

- [ ] **Step 3: Implement `backend/orchestrator/repeat_guard.py`** to the Interfaces block, and add `GoalProgress.has_error`. Name the context variable `_CURRENT` with default `None`.

- [ ] **Step 4: Implement `Orchestrator._apply_repeat_guard`.** If `repeat_guard.current()` is `None`, return `False`. Otherwise, for each call with its result by position (a missing result is `None`), call `observe` with the unqualified name, the call's arguments and `repeat_guard.is_failure(result)`. For each `"warn"`, append `{"role": "system", "content": repeat_guard.warn_text(name)}` to `messages`. Return `True` if any verdict is `"stop"`.

- [ ] **Step 5: Start a guard per chat turn.** Next to `denial_tracker: Dict[str, int] = {}` (line 15905), call `repeat_guard.start_turn()`.

- [ ] **Step 6: Refuse a blocked call on both dispatch paths.** In `execute_single_tool`, directly after the argument-error check from task 7:

```python
        guard = repeat_guard.current()
        if guard is not None and guard.blocked(tool_name, raw_arguments):
            return MCPResponse(error={
                "code": "repeated_call_blocked", "message": repeat_guard.blocked_text(tool_name),
                "retryable": False, "effect_state": "not_started"})
```

Add the same check in `execute_parallel_tools` after its argument-error check, using the `_arg_err` pattern and `tc.function.arguments`.

- [ ] **Step 7: Apply it in the chat loop.** Directly after `self._append_tool_images(messages, llm_msg.tool_calls, tool_results)`:

```python
                    if self._apply_repeat_guard(llm_msg.tool_calls, tool_results, tool_to_unqualified, messages):
                        if goal_progress is not None:
                            from orchestrator.goal_execution import REPEATED_FAILURES_REASON
                            goal_stop_reason = REPEATED_FAILURES_REASON
                            break
                        await self.send_ui_render(websocket, [
                            Alert(message=repeat_guard.STOP_NOTICE, variant="warning").to_dict()], target="chat")
                        await self._safe_send(websocket, json.dumps(
                            {"type": "chat_status", "status": "done", "message": ""}))
                        break
```

This follows the shape of the existing "All tools denied" exit at lines 16753-16763.

- [ ] **Step 8: Add a dispatch test** to `backend/tests/test_repeat_guard.py`. Copy the `orch` fixture from task 7's test file into this one, and import `pytest`, `AsyncMock` and `MagicMock`:

```python
async def test_a_blocked_call_is_not_dispatched_on_either_path(orch):
    guard = repeat_guard.start_turn()
    guard.observe("t1", "{}", True)
    guard.observe("t1", "{}", True)
    tc = call("t1", "{}")
    single = await orch.execute_single_tool(MagicMock(), tc, {"t1": "a1"}, "c1", user_id="u1")
    parallel = (await orch.execute_parallel_tools(MagicMock(), [tc], {"t1": "a1"}, "c1", user_id="u1"))[0]
    for response in (single, parallel):
        assert response.error["code"] == "repeated_call_blocked"
        assert response.error["message"] == repeat_guard.blocked_text("t1")
    assert orch.local_agents["a1"].mock_calls == []
```

- [ ] **Step 9: Run the new and the neighbouring suites**

Run: `t tests/test_repeat_guard.py tests/test_dispatch_parity.py tests/test_goal_progress.py tests/test_goal_progress_checkpoint_loop.py tests/test_tool_argument_dispatch.py`
Expected: all passed.

- [ ] **Step 10: Commit**

```bash
git add backend/orchestrator/repeat_guard.py backend/orchestrator/goal_progress.py \
        backend/orchestrator/orchestrator.py backend/tests/test_repeat_guard.py
git commit -m "Stop a turn that keeps repeating the same failing call"
```

---

### Task 9: Exit criteria and final checks

**Files:**
- Test: `backend/tests/test_reliability_exit_criteria.py`

**Interfaces:**
- Consumes: everything above. Produces nothing new. Each test is named for one exit criterion in [plan.md](plan.md) "Phase R", so the phase's exit can be audited from one file.

- [ ] **Step 1: Write the tests**

```python
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from orchestrator import llm_recovery, repeat_guard
from orchestrator.orchestrator import Orchestrator
from tests.helpers.llm_faults import (
    FaultyCompletions, bare_orch, content_chunk, fail, no_retry_sleep, reply, stall, status, tiny_deadlines)

USER = [{"role": "user", "content": "hi"}]
SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
          "required": ["city"]}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_MODELS", raising=False)
    tiny_deadlines(monkeypatch)
    no_retry_sleep(monkeypatch)
    llm_recovery.reset_turn()
    yield
    llm_recovery.reset_turn()


async def stream_call(orch):
    return await asyncio.wait_for(
        orch._call_llm(object(), USER, allow_stream=True, stream_chat_id="chat-1"), 5)


async def test_a_provider_that_never_sends_a_first_token_surfaces_within_the_deadline():
    comp = FaultyCompletions([stall()])
    try:
        assert await stream_call(bare_orch(comp)) == (None, None)
    finally:
        comp.release.set()
    assert [c.get("stream") for c in comp.calls] == [True, True, True]


async def test_a_stream_that_stalls_mid_answer_keeps_the_partial_text():
    comp = FaultyCompletions([stall(1, content_chunk("First line.\n")), fail(ConnectionError("down"))])
    try:
        msg, _usage = await stream_call(bare_orch(comp))
    finally:
        comp.release.set()
    assert msg.content == "First line.\n" and msg._astral_incomplete is True


@pytest.mark.parametrize("raw, expected, model_calls", [
    ('Sure: {"city": "Rome"}', {"city": "Rome"}, 0),
    ('{"city": "Ro\tme"}', {"city": "Ro\tme"}, 0),
    ('{"city": "Rome", "days": "3"}', {"city": "Rome", "days": 3}, 0),
    ("city=Rome", {"city": "Rome"}, 1),
])
async def test_each_malformed_argument_case_costs_at_most_one_extra_model_call(raw, expected, model_calls):
    orch = Orchestrator.__new__(Orchestrator)
    skill = SimpleNamespace(id="get_weather", name="get_weather", input_schema=SCHEMA)
    orch.agent_cards = {"weather-1": SimpleNamespace(skills=[skill])}
    orch._call_llm_json = AsyncMock(return_value={"city": "Rome"})
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="get_weather", arguments=raw))
    await orch._prepare_tool_calls(None, [tc], {"get_weather": "weather-1"}, None)
    assert json.loads(tc.function.arguments, strict=False) == expected
    assert orch._call_llm_json.await_count == model_calls


def test_no_identical_failing_call_is_dispatched_more_than_twice():
    guard = repeat_guard.start_turn()
    dispatched = 0
    for _ in range(6):
        if not guard.blocked("read_file", '{"path": "/x"}'):
            dispatched += 1
        guard.observe("read_file", '{"path": "/x"}', True)
    assert dispatched == 2


async def test_a_primary_model_outage_completes_on_the_fallback_with_a_notice(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "m2")
    comp = FaultyCompletions(by_model={"m1": [status(503)], "m2": [reply("answer")]})
    orch = bare_orch(comp)
    msg, _usage = await orch._call_llm(object(), USER)
    assert msg.content == "answer"
    assert orch._rendered[0][0]["message"] == "m1 did not respond. This turn is using m2."
```

- [ ] **Step 2: Run it and confirm it passes**

Run: `t tests/test_reliability_exit_criteria.py`
Expected: 8 passed.

- [ ] **Step 3: Run every suite this plan touched, and lint**

Run: `t tests/test_llm_faults.py tests/test_llm_deadlines.py tests/test_llm_stream_watchdog.py tests/test_llm_stream_interrupted.py tests/test_llm_recovery.py tests/test_tool_arguments.py tests/test_tool_argument_dispatch.py tests/test_repeat_guard.py tests/test_reliability_exit_criteria.py tests/test_llm_streaming.py tests/test_llm_streaming_continuation.py tests/test_goal_buffered_transport.py tests/test_call_llm_wave0.py tests/test_dispatch_parity.py tests/test_tool_feedback.py tests/test_goal_progress.py llm_config/tests/test_client_factory.py`
Expected: all passed.

Run: `ruff check backend/llm_config/deadlines.py backend/orchestrator/llm_recovery.py backend/orchestrator/tool_arguments.py backend/orchestrator/repeat_guard.py backend/orchestrator/orchestrator.py backend/tests/helpers/llm_faults.py`
Expected: no findings.

- [ ] **Step 4: Run the full backend suite once**

Run: `make sync-backend && make test-backend`
Expected: no new failures. About twenty memory-fixture failures in the module suites exist on `main` and are not this change. Compare against a run on `main` before blaming a failure on this branch.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_reliability_exit_criteria.py
git commit -m "Add one test per reliability exit criterion"
```

- [ ] **Step 6: Report.** State what is implemented and test-passing, and what is not yet verified:
  - Changed-line coverage has not been measured locally. CI measures it.
  - Nothing has been checked against a live model. The live checks are: a real stalled endpoint surfaces within the first-token deadline. A real mid-answer drop shows the continued answer once, with no duplicate text, on the Mac client. And `LLM_FALLBACK_MODELS` set to a second model on the same endpoint takes over with the notice.
