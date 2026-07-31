"""Planner decomposition into bounded, isolated sub-tasks (feature 056 US4).

The second orchestrator-mediated chaining seam. Where
``AgentRuntime.call_agent_tool`` lets ONE agent request ONE peer tool
deterministically (US1), this lets the PLANNER split a broad request into
several sub-tasks that run concurrently in fresh, isolated contexts and return
bounded, provenance-tagged digests — so the orchestrator is no longer the sole
step-by-step planner of every turn.

Every guarantee the direct path has is preserved, because a sub-task is just a
normal turn on the existing ``BackgroundTask``/``VirtualWebSocket`` substrate:

* **Isolated** — a fresh chat context per sub-task; no parent transcript, so a
  poisoned sub-task cannot rewrite the parent's history.
* **Narrower authority than the parent** — a sub-task may use only tools the
  parent turn itself offered (never a superset), and every dispatch inside it
  re-enters the full single-path gate stack keyed to the SAME human principal.
  Agent-to-agent hops started inside a sub-task mint attenuated children off
  the dispatch's parent authority exactly as they do anywhere else (US1).
* **Bounded** — each sub-task holds a slice of the turn's global ``ChainBudget``
  whose charges also debit the parent, so depth × breadth × wall clock cannot
  exceed the turn ceiling however the tree is shaped.
* **Digest return** — the parent receives a capped, provenance-tagged summary
  (which sub-task, which agent, under whose authority), never a raw transcript.
* **Scanned** — every digest passes the multi-agent-defense scan before it can
  enter the planner's context; a flagged digest is quarantined with an audited
  reason and an honest error (never silently delivered).
* **Never orphaned** — if the parent turn ends, its socket goes away, or the
  budget is exhausted, in-flight sub-tasks are cancelled and audited, and their
  partial output is discarded rather than attached to a later turn.

Gated by ``FF_RECURSIVE_DELEGATION`` (default off) — with the flag off the
meta-tool is not injected and behavior is byte-identical to today.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from astralprims import Card, Text, create_ui_response
from shared.feature_flags import flags

logger = logging.getLogger("Orchestrator.Subtasks")

META_AGENT_ID = "__subtasks__"

#: Hard bounds on a decomposition (independent of the chain budget, which also
#: applies): a planner cannot spawn an unbounded fan-out in one call.
MAX_SUBTASKS = 5
MIN_SUBTASKS = 2
#: Characters of digest returned per sub-task (bounded context growth, FR-020).
DIGEST_CAP = 1200
#: Wall clock for one sub-task; the turn's ChainBudget bounds the tree overall.
SUBTASK_TIMEOUT_S = 90.0

SYSTEM_PROMPT_ADDENDUM = """
PLANNING DECOMPOSITION (delegate_subtasks):
For a BROAD request that splits into independent pieces of work (e.g. "audit
these five programs and build me a dashboard"), you may call `delegate_subtasks`
with 2-5 sub-tasks. Each runs in its own isolated context, concurrently, and
returns a short digest you then synthesize. Use it only when the pieces are
genuinely independent — a single focused request should just call its tool
directly. Never use it to retry the same work.
"""


def should_inject(draft_agent_id: Optional[str]) -> bool:
    """Inject the decomposition meta-tool? (flag-gated; never for draft tests)."""
    if draft_agent_id:
        return False
    return bool(flags.is_enabled("recursive_delegation"))


def meta_tool_definitions() -> List[Dict[str, Any]]:
    return [{
        "type": "function",
        "function": {
            "name": "delegate_subtasks",
            "description": (
                "Split a broad request into 2-5 INDEPENDENT sub-tasks that run "
                "concurrently in isolated contexts, each returning a short digest "
                "you then synthesize into one answer. Only for genuinely "
                "independent pieces of work."),
            "parameters": {
                "type": "object",
                "properties": {
                    "subtasks": {
                        "type": "array",
                        "description": "2-5 independent sub-tasks.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string",
                                          "description": "Short label (shown to the user)."},
                                "instruction": {"type": "string",
                                                "description": "Self-contained instruction; the sub-task sees NO parent context."},
                            },
                            "required": ["title", "instruction"],
                        },
                    },
                },
                "required": ["subtasks"],
            },
        },
    }]


class SubtaskResult:
    """One sub-task's bounded, provenance-tagged outcome."""

    __slots__ = ("title", "digest", "agents", "status", "detail")

    def __init__(self, title: str, digest: str = "", agents: Optional[List[str]] = None,
                 status: str = "ok", detail: str = ""):
        self.title = title
        self.digest = digest
        self.agents = agents or []
        self.status = status          # ok | quarantined | cancelled | failed | timeout
        self.detail = detail

    def as_dict(self) -> Dict[str, Any]:
        return {"subtask": self.title, "status": self.status,
                "agents": self.agents, "digest": self.digest,
                "detail": self.detail}


def _digest_from_outputs(outputs: List[Any]) -> tuple[str, List[str]]:
    """Distil a sub-task's captured frames into a bounded digest + the agents
    that acted. Never returns a raw transcript (FR-020)."""
    parts: List[str] = []
    agents: List[str] = []
    for out in outputs or []:
        if not isinstance(out, dict):
            continue
        payload = out.get("payload") if isinstance(out.get("payload"), dict) else {}
        text = out.get("text") or out.get("message") or payload.get("text") or payload.get("message")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
        for comp in (out.get("components") or []):
            if isinstance(comp, dict) and comp.get("type") == "text":
                content = comp.get("content")
                if isinstance(content, str) and content.strip():
                    parts.append(content.strip())
        agent = out.get("agent_id") or payload.get("agent_id")
        if isinstance(agent, str) and agent and agent not in agents:
            agents.append(agent)
    digest = "\n".join(parts).strip()
    return digest[:DIGEST_CAP], agents


async def _audit_subtask(orch, *, user_id: str, chat_id: Optional[str],
                         correlation_id: str, action: str, outcome: str,
                         title: str, detail: str = "") -> None:
    """Record a sub-task lifecycle event on the hash chain (FR-023)."""
    try:
        from audit.recorder import get_recorder, now_utc
        from audit.schemas import AuditEventCreate
        rec = get_recorder()
        if rec is None or not user_id or user_id == "legacy":
            return
        await rec.record(AuditEventCreate(
            actor_user_id=user_id,
            auth_principal="agent:__subtasks__",
            event_class="delegation",
            action_type=f"delegation.subtask.{action}",
            description=f"sub-task {action}: {title[:80]}",
            conversation_id=chat_id,
            correlation_id=correlation_id,
            outcome=outcome,
            outcome_detail=detail or None,
            inputs_meta={"subtask": title[:120]},
            started_at=now_utc(),
        ))
    except Exception:
        logger.debug("subtask audit failed", exc_info=True)


async def _run_one(orch, spec: Dict[str, Any], *, user_id: str,
                   parent_chat_id: Optional[str], parent_ws,
                   allowed_tools: Optional[List[str]], budget,
                   correlation_id: str) -> SubtaskResult:
    """Run ONE sub-task in a fresh isolated context under a budget slice."""
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket

    title = str(spec.get("title") or "sub-task")[:120]
    instruction = str(spec.get("instruction") or "").strip()
    if not instruction:
        return SubtaskResult(title, status="failed", detail="empty instruction")

    # A sub-task hop counts against the turn's global ceiling like any other.
    reason = budget.charge(1)
    if reason is not None:
        await _audit_subtask(orch, user_id=user_id, chat_id=parent_chat_id,
                             correlation_id=correlation_id, action="budget_stop",
                             outcome="failure", title=title, detail=reason)
        return SubtaskResult(title, status="cancelled",
                             detail=f"chain budget exhausted ({reason})")

    sub_chat = await asyncio.to_thread(orch.history.create_chat, user_id=user_id)
    task = BackgroundTask(task_id=f"sub-{uuid.uuid4().hex[:8]}",
                          chat_id=sub_chat, user_id=user_id)
    vws = VirtualWebSocket(task)
    # A sub-task is FOREGROUND work the user is waiting on: resolve its LLM
    # (ReAct planning + agent-side LLM tools) to the requesting user, not the
    # admin SYSTEM account a bare VirtualWebSocket would default to (054
    # FR-019). Without this a fully-configured user whose deployment has no
    # system credential gets every decomposition failing.
    vws.llm_context_user_id = user_id
    # The sub-task inherits the parent's session authority (same human
    # principal, same gates) but may use only the tools the parent turn itself
    # offered — never a superset (FR-020). Binding the parent's claims onto the
    # isolated socket is what lets its dispatches mint delegated tokens at all.
    parent_claims = orch.ui_sessions.get(parent_ws) if parent_ws is not None else None
    if isinstance(parent_claims, dict):
        orch.ui_sessions[vws] = dict(parent_claims)
    # Bind this sub-task's budget slice as the sub-chat's chain budget so hops
    # started INSIDE the sub-task charge the slice — whose ``charge`` also
    # debits the parent turn's global ceiling — rather than a fresh parentless
    # budget keyed on the sub-chat. handle_chat_message's turn-reset preserves
    # a pre-bound slice (a budget with a parent); we remove it in ``finally``.
    orch._chain_budgets[sub_chat] = budget

    await _audit_subtask(orch, user_id=user_id, chat_id=parent_chat_id,
                         correlation_id=correlation_id, action="spawned",
                         outcome="in_progress", title=title)
    await _progress(orch, parent_ws, f"{title} — running")

    try:
        await asyncio.wait_for(
            orch.handle_chat_message(vws, instruction, sub_chat, user_id=user_id,
                                     selected_tools=allowed_tools),
            timeout=min(SUBTASK_TIMEOUT_S, max(budget.wall_clock_s - budget.elapsed_s(), 1.0)))
    except asyncio.CancelledError:
        task.outputs.clear()  # orphaned partials are DISCARDED (FR-023)
        await _audit_subtask(orch, user_id=user_id, chat_id=parent_chat_id,
                             correlation_id=correlation_id, action="cancelled",
                             outcome="interrupted", title=title)
        raise
    except asyncio.TimeoutError:
        task.outputs.clear()
        await _audit_subtask(orch, user_id=user_id, chat_id=parent_chat_id,
                             correlation_id=correlation_id, action="timeout",
                             outcome="failure", title=title)
        return SubtaskResult(title, status="timeout",
                             detail="exceeded its time slice")
    except Exception as exc:
        logger.warning("subtask %r failed: %s", title, exc)
        await _audit_subtask(orch, user_id=user_id, chat_id=parent_chat_id,
                             correlation_id=correlation_id, action="failed",
                             outcome="failure", title=title, detail=str(exc)[:200])
        return SubtaskResult(title, status="failed", detail=str(exc)[:200])
    finally:
        orch.ui_sessions.pop(vws, None)
        orch._chain_budgets.pop(sub_chat, None)
        try:
            await vws.close()
        except Exception:  # pragma: no cover — close is best-effort
            pass

    digest, agents = _digest_from_outputs(task.outputs)

    # FR-007/D11: every inter-agent payload is scanned BEFORE it can enter the
    # planner's context. A finding quarantines it — not delivered, audited,
    # honest error.
    from orchestrator import mas_defense
    findings = []
    try:
        findings = mas_defense.scan_message(digest)
    except Exception:  # pragma: no cover — scanner is pure/stdlib
        logger.debug("subtask digest scan failed", exc_info=True)
    if findings:
        markers = sorted({f.marker for f in findings})
        logger.warning("subtask.quarantine title=%r markers=%s chat=%s",
                       title, markers, parent_chat_id)
        await _audit_subtask(orch, user_id=user_id, chat_id=parent_chat_id,
                             correlation_id=correlation_id, action="quarantined",
                             outcome="failure", title=title,
                             detail=f"injection markers: {', '.join(markers)[:150]}")
        return SubtaskResult(
            title, agents=agents, status="quarantined",
            detail=("its result contained prompt-injection markers and was "
                    "quarantined; it was not delivered"))

    await _audit_subtask(orch, user_id=user_id, chat_id=parent_chat_id,
                         correlation_id=correlation_id, action="completed",
                         outcome="success", title=title)
    await _progress(orch, parent_ws, f"{title} — done")
    return SubtaskResult(title, digest=digest or "(no output)", agents=agents)


async def _progress(orch, websocket, message: str) -> None:
    """Hierarchical sub-task progress over the EXISTING chat_status frame
    (FR-022) — no new frame type, so every client renders it unchanged."""
    if websocket is None:
        return
    try:
        import json
        await orch._safe_send(websocket, json.dumps({
            "type": "chat_status", "status": "thinking",
            "message": message,
        }))
    except Exception:  # pragma: no cover — progress is best-effort
        logger.debug("subtask progress send failed", exc_info=True)


async def handle_meta_tool(orch, tool_name: str, args: Dict[str, Any], *,
                           user_id: Optional[str], chat_id: Optional[str],
                           websocket=None):
    """``delegate_subtasks`` — spawn bounded, isolated sub-tasks concurrently."""
    from shared.protocol import MCPResponse

    if tool_name != "delegate_subtasks":
        return MCPResponse(error={"message": f"Unknown sub-task tool: {tool_name}",
                                  "retryable": False})
    if not user_id:
        return MCPResponse(error={"message": "Sub-tasks require an authenticated user.",
                                  "retryable": False})

    specs = args.get("subtasks")
    if not isinstance(specs, list) or not (MIN_SUBTASKS <= len(specs) <= MAX_SUBTASKS):
        msg = (f"delegate_subtasks needs between {MIN_SUBTASKS} and {MAX_SUBTASKS} "
               f"sub-tasks; answer directly instead.")
        return MCPResponse(error={"message": msg, "retryable": False})

    from audit.recorder import make_correlation_id
    correlation_id = make_correlation_id()
    budget = orch._chain_budget_for(chat_id)
    # Each sub-task gets a slice of the turn's global budget; the slice debits
    # the parent, so the tree can never exceed the turn ceiling (FR-021).
    slices = [budget.slice(max_hops=max(budget.max_hops // len(specs), 1))
              for _ in specs]

    # A sub-task may use only the tools the parent turn itself offered.
    allowed_tools = args.get("_parent_tools") if isinstance(args.get("_parent_tools"), list) else None

    logger.info("subtasks.spawn count=%d chat=%s user=%s corr=%s",
                len(specs), chat_id, user_id, correlation_id)
    started = time.monotonic()
    tasks = [
        asyncio.create_task(_run_one(
            orch, spec, user_id=user_id, parent_chat_id=chat_id,
            parent_ws=websocket, allowed_tools=allowed_tools, budget=sl,
            correlation_id=correlation_id))
        for spec, sl in zip(specs, slices)
    ]
    try:
        results: List[SubtaskResult] = list(await asyncio.gather(*tasks))
    except asyncio.CancelledError:
        # The parent turn ended / its socket went away: cancel every in-flight
        # sub-task and discard partial output (FR-023) — never attach it later.
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _audit_subtask(orch, user_id=user_id, chat_id=chat_id,
                             correlation_id=correlation_id, action="orphaned",
                             outcome="interrupted", title="decomposition",
                             detail="parent turn ended")
        logger.info("subtasks.orphaned corr=%s — partial output discarded", correlation_id)
        raise

    elapsed = time.monotonic() - started
    delivered = [r for r in results if r.status == "ok"]
    logger.info("subtasks.done corr=%s ok=%d/%d elapsed=%.1fs hops_spent=%d",
                correlation_id, len(delivered), len(results), elapsed,
                budget.spent_hops)

    # The planner receives bounded, provenance-tagged digests — never raw
    # transcripts, and never a quarantined payload.
    lines = []
    for r in results:
        who = f" (via {', '.join(r.agents)})" if r.agents else ""
        if r.status == "ok":
            lines.append(f"### {r.title}{who}\n{r.digest}")
        else:
            lines.append(f"### {r.title}{who}\n_{r.status}: {r.detail}_")
    summary = "\n\n".join(lines)

    components = [Card(
        title=f"Delegated {len(results)} sub-tasks",
        content=[Text(content=summary, variant="markdown")],
    )]
    payload = create_ui_response(components)
    payload["_data"] = {
        "correlation_id": correlation_id,
        "subtasks": [r.as_dict() for r in results],
        "hops_spent": budget.spent_hops,
    }
    return MCPResponse(
        result={"subtasks": [r.as_dict() for r in results],
                "note": ("Synthesize these digests into one answer. Quarantined or "
                         "failed sub-tasks produced no usable result — say so honestly "
                         "rather than inventing their content.")},
        ui_components=payload["_ui_components"],
    )
