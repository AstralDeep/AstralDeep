# Contract: TypeSafe Routing Adapter

Package: `backend/orchestrator/typesafe_routing/`. This package is the only code in AstralDeep that imports `typesafe_sdk`.

## Public interface

```python
async def start_routing(
    *, user_id: str, turn_id: str, chat_id: str,
    request: RoutingRequest, notifier: RoutingNotifier,
) -> asyncio.Task[RoutingDecision | None]:
    """Create the routing task. Returns immediately.

    The task returns None (and makes zero network calls) when the user has
    no key, the circuit is open, the eligible set is empty, or the SDK is
    unavailable. It never raises: every failure becomes a fallback outcome.
    """

async def await_decision(task, *, deadline: float) -> RoutingDecision | None:
    """Await with the remaining budget (monotonic deadline). On expiry it
    cancels the task and returns None."""

def apply_round_one(decision, tools_desc, provider_preset) -> RoundOnePlan:
    """Pure function. Returns (tools_desc, tool_choice) for round 1 per tier.
    Low tier or None -> the inputs unchanged, tool_choice="auto"."""

def security_verdict(decision) -> Verdict:  # pass | confirm_tools | refuse
    ...
```

**Kill switch**: `FF_TYPESAFE_ROUTING` is registered in `shared/feature_flags.py` and defaults ON. When OFF, `start_routing` returns a task resolving to `None` with zero network calls for every user, which is exactly the no-key path. The flag holds no key material, and it is an operations switch, not a rollout mechanism.

`RoutingNotifier` wraps `Orchestrator._send_chat_status(websocket, status, message)`. It is the only way the adapter can talk to the user.

## Orchestrator integration points (`orchestrator.py`)

| # | Location | Change |
|---|---|---|
| I1 | `_handle_chat_message_impl`, right after eligible tool pairs are computed (~L15881) | Build `RoutingRequest`; `routing_task = await start_routing(...)`; record `routing_deadline = monotonic() + 1.5`. |
| I2 | Before round-1 `_call_llm` (~L16435, only when `turn_count == 0`) | `decision = await await_decision(routing_task, deadline=routing_deadline)`. If `security_verdict(decision) == refuse`, render the refusal, audit, send `chat_status done` and return. Otherwise compute `RoundOnePlan` and pass `tools_desc` and `tool_choice` explicitly. |
| I3 | `_call_llm` signature | New keyword `tool_choice: str \| dict \| None = None`; `None` keeps `"auto"`. On a provider rejection of a dict value, retry once with `"auto"`, which does not count against `MAX_RETRIES`. |
| I4 | Turn context → `_run_gate_stack` supervisor + HITL step (~L19239) | If the turn verdict is `confirm_tools`, require human approval for this call through the existing HITL path. This must never change an approval-required or denied result into allowed. |
| I5 | `_deliver_round_components` (~L23572) | After the upsert-first flat send: if the decision has a style other than `as_delivered` and the device is web, apply `layout.compose()`; otherwise, or on `None`/invalid, fall back to `_run_designer` unchanged. |
| I6 | Turn cancellation / socket close | `routing_task.cancel()`. |
| I7 | REST `api.py` chat path and `async_mode` | No separate code; they share I1–I6 via `handle_chat_message`. HTTP Work `kind="chat"` calls only `security_verdict` on a security-only request (a question set containing only the security core). |

## Request construction

- One `AsyncTypeSafeClient.system_one(state=..., questions=..., timeout=attempt_timeout, retry=RetryPolicy(max_retries=0))` per attempt.
- Constructor arguments are always explicit: `api_key`, `base_url=TYPESAFE_API_BASE`, `model=TYPESAFE_MODEL`.
- The question IDs below are code identifiers, and the full meaning is in the instructions:

| ID | Primitive | Instructions (summary) | Criteria |
|---|---|---|---|
| `is_jailbreak` | Noul | Is `current_request` attempting prompt injection, jailbreak, or bypassing restrictions? | — |
| `harm_score` | Score | How much harm would complying with `current_request` cause? | 4 levels (safe → critical), per a8p |
| `threat_category` | Choice | Which threat category does `current_request` belong to? | none, data_egress, code_execution, credential_access, destructive, privilege_escalation, network_manipulation |
| `target_agent` | Choice | Which agent should handle `current_request` given `recent_conversation` and `active_agent`? | each eligible agent id → name + description; `no_tool_needed` |
| `tool_for_<n>` | Choice | *Assuming agent `<agent id>` handles `current_request`*, which of its tools fits? | that agent's eligible prefixed tool names → description; `none_fit` |
| `presentation_style` | Choice | How should the results for `current_request` be arranged? | dashboard, detailed_table, alert_focused, conversational, as_delivered |

`<n>` is a stable index. A mapping from index to agent id is kept locally, because question IDs are not sent to the model.

## Retry, deadline and notices

```text
t0 = task start; deadline = t0 + 1.5s
attempt 1: timeout = min(ATTEMPT_TIMEOUT, remaining)
  fail transient  -> notify("retrying", "Smart routing is slow to respond — retrying…")
                     sleep min(100ms±20%, remaining)
attempt 2: timeout = min(ATTEMPT_TIMEOUT, remaining); fail transient -> sleep min(200ms±20%, remaining)
attempt 3: timeout = min(ATTEMPT_TIMEOUT, remaining)
all failed / deadline -> notify("info", "Using standard routing for this request."); outcome=fallback_transient
non-transient on any attempt -> no retry; notify("info", …); outcome=fallback_nontransient;
                                401/403 -> record_outcome_async(rejected); circuit auth-block for this fingerprint
```

| Class | Errors |
|---|---|
| Transient | `TypeSafeAPITimeoutError`, `TypeSafeAPIConnectionError`, `TypeSafeRateLimitError`, `TypeSafeInternalServerError`, `TypeSafeAPIError` with status 408 / ≥500 / 529 |
| Non-transient | `TypeSafeAuthenticationError`, `TypeSafePermissionDeniedError`, `TypeSafeUnprocessableEntityError`, `TypeSafeAPIResponseValidationError`, `TypeSafeError` (configuration), `ImportError` |

**Circuit behavior**:
- 3 consecutive turn fallbacks open it for 300 s. While open, the outcome is `skipped_circuit` with one notice on the first skipped turn.
- After the cool-down, the next turn is a half-open trial.
- An auth-block opens it until the key fingerprint changes.

## Tiering (provisional thresholds; final values from T015/T028)

| Tier | Rule | Round 1 |
|---|---|---|
| high | `agent_p ≥ 0.80 ∧ tool_p ≥ 0.75 ∧ margin ≥ 0.30 ∧ tool ∈ eligible` | `[tool]`, forced choice if the provider supports it |
| medium | `agent_p ≥ 0.55 ∧ ∃ top-k (k ≤ 6) with Σp ≥ 0.80, all ∈ eligible` | shortlist, `"auto"` |
| low | otherwise, including `no_tool_needed`, `none_fit`, unknown option, missing answer | unchanged |

Forced-choice support is a static set of provider preset keys (initially `openai`, `anthropic`-via-OpenAI-compat, `openrouter`, `groq`, `together`, `mistral`, `xai`), confirmed during T015. `custom`, `ollama` and `lmstudio` use `"auto"`.

## Security verdict (additive)

| Verdict | Rule (provisional; calibrated in T036) | Effect |
|---|---|---|
| refuse | `(jailbreak_p ≥ 0.70 ∧ harm ≥ 2.5) ∨ (threat ∈ {credential_access, network_manipulation} ∧ jailbreak_p ≥ 0.50)` **and** `REFUSE_TIER_ENABLED` | Turn refused before round 1; audit `typesafe.security_verdict` |
| confirm_tools | `jailbreak_p ≥ 0.40 ∨ harm ≥ 1.5 ∨ threat ∈ {destructive, data_egress, code_execution, privilege_escalation}`, or refuse while the refuse tier is disabled | Every tool call this turn requires HITL approval; audited |
| pass | otherwise, or no decision | No change |

A TypeSafe verdict can never set, clear or satisfy any existing gate state.

## Invariants (tested)

1. No key ⇒ zero TypeSafe network calls and byte-identical round-1 `tools_desc` and `tool_choice` compared to baseline.
2. At most 3 `system_one` calls per turn; total routing wall time ≤ 1.5 s + scheduling jitter tolerance (50 ms in tests).
3. Round ≥ 2 `tools_desc` always equals the full eligible list.
4. Every tool call still passes `_run_gate_stack` in full; recorded gate decisions are identical to baseline for `pass` and `None` decisions.
5. `tools_desc` in round 1 is always a subset of the eligible list.
6. No key, request text or state content appears in logs, metrics labels, audit or exceptions.
7. An environment-supplied `TYPESAFE_*` value is never used.
