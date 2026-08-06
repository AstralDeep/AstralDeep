"""Agentic agent/tool creation from chat.

Implements the orchestrator meta-tools (``create_capability``,
``extend_agent``). When the chat LLM determines no offered tool can serve the
user's request, it calls a meta-tool; the handler auto-creates a draft through
the existing lifecycle, self-tests it, and returns an in-chat card with
approve / refine / discard decisions. Nothing reaches the live fleet without
explicit user approval; live-agent revisions re-pass the security gate before
a backed-up, rollback-safe swap.

Audit: one correlation_id per capability gap — the draft id (a uuid4)
— pairing ``lifecycle.gap_detected`` with the terminal lifecycle events
(event_class ``agent_lifecycle``).
"""
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Any, Dict, List, Optional

from astralprims import Alert, Button, Card, Text
from orchestrator.bounded_work import run_generation
from orchestrator.code_security import blocks_execution
from shared.feature_flags import flags
from shared.protocol import MCPResponse

logger = logging.getLogger("Orchestrator.AgenticCreation")

META_AGENT_ID = "__orchestrator__"

SELF_TEST_TIMEOUT_S = 120          # bound per attempt
SELF_TEST_MAX_AUTO_REFINES = 1     # bound on auto-refine retries

SYSTEM_PROMPT_ADDENDUM = """
CAPABILITY GAPS (create_capability / extend_agent):
- If NO available tool can serve the user's request, call `create_capability` to build a new
  agent for it (the system generates, security-checks, and self-tests a draft; the user approves
  before anything goes live). Restate the user's request verbatim in `user_request`.
- This INCLUDES requests for a persistent tool the user wants to UPDATE or maintain over time
  (e.g. "build me a budget tracker I can update each month") — a one-off static dashboard or
  sample-data mockup does NOT serve such a request; call `create_capability` instead.
- To ADD a tool to an agent the user already owns, call `extend_agent` instead.
- Do NOT call these when a suitable tool exists but is disabled or permission-restricted (you
  will see a "restricted" tool error if you try it) — in that case tell the user to enable it
  under Settings → Agents & permissions.
- Call `create_capability` at most once per distinct missing capability; if a draft already
  exists for it the system will point at the existing draft.
"""


def meta_tool_definitions() -> List[Dict[str, Any]]:
    """OpenAI-style tool definitions for the orchestrator meta-tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": "create_capability",
                "description": (
                    "Create a new agent with the tools needed to serve the user's request "
                    "when NO available tool can — including requests for a persistent tool "
                    "the user wants to update/maintain over time, which a static dashboard "
                    "cannot serve. A draft is generated, security-checked and "
                    "self-tested; the user approves before it goes live. Do NOT use this for "
                    "capabilities that exist but are disabled/unauthorized."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_name": {"type": "string", "description": "Short human name for the new agent"},
                        "description": {"type": "string", "description": "What the agent does, in plain language (at least 10 characters)"},
                        "tools_spec": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["name", "description"],
                            },
                            "description": "1-4 tools the agent needs",
                        },
                        "user_request": {"type": "string", "description": "The user's request, verbatim — used to self-test the new capability"},
                    },
                    "required": ["agent_name", "description", "tools_spec", "user_request"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "extend_agent",
                "description": (
                    "Add or change a tool on a live agent the user OWNS. Prepares a draft "
                    "revision; nothing changes on the live agent until the user approves and "
                    "security checks re-pass."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string", "description": "Live agent id to extend (must be owned by the user)"},
                        "instruction": {"type": "string", "description": "What to add or change, in plain language"},
                        "user_request": {"type": "string", "description": "The user's request, verbatim"},
                    },
                    "required": ["agent_id", "instruction"],
                },
            },
        },
    ]


def should_inject(draft_agent_id: Optional[str]) -> bool:
    """Meta-tools are offered on normal chat turns only.

    Excluded: draft-test sessions (the draft's own tools are under test) and
    turns where the feature flag is off. Text-only turns are excluded at the
    call site.
    """
    return flags.is_enabled("agentic_creation") and not draft_agent_id


def gap_fingerprint(agent_name: str, tools_spec: Optional[List[Dict]] = None,
                    extra: str = "") -> str:
    """Stable fingerprint of a requested capability (dedup key)."""
    names = sorted((t.get("name") or "").strip().lower() for t in (tools_spec or []))
    basis = "|".join([(agent_name or "").strip().lower(), *names, (extra or "").strip().lower()])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------

async def _audit(user_id: str, action_type: str, description: str,
                 correlation_id: str, outcome: str = "success",
                 chat_id: Optional[str] = None, agent_id: Optional[str] = None,
                 inputs_meta: Optional[Dict] = None) -> None:
    """Record an ``agent_lifecycle`` audit event (best-effort, never raises)."""
    try:
        from datetime import datetime, timezone

        from audit.recorder import get_recorder
        from audit.schemas import AuditEventCreate
        rec = get_recorder()
        if rec is None:
            return
        await rec.record(AuditEventCreate(
            actor_user_id=user_id or "unknown",
            auth_principal=user_id or "unknown",
            agent_id=agent_id,
            event_class="agent_lifecycle",
            action_type=action_type,
            description=description[:1024],
            conversation_id=chat_id,
            correlation_id=correlation_id,
            outcome=outcome,
            inputs_meta=inputs_meta or {},
            started_at=datetime.now(timezone.utc),
        ))
    except Exception:
        logger.debug("agentic: audit record failed (%s)", action_type, exc_info=True)


# ---------------------------------------------------------------------------
# Evolutionary archive (C-N4) — surrogate cheap-reject + archive on success
# ---------------------------------------------------------------------------

def _read_draft_code(orch, draft: Dict[str, Any]) -> str:
    """Best-effort read of a draft's generated ``mcp_tools.py`` (empty on miss)."""
    try:
        agents_dir = orch.lifecycle_manager._agents_dir
        path = os.path.join(agents_dir, draft["agent_slug"], "mcp_tools.py")
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        logger.debug("archive: could not read draft code for %s", draft.get("id"),
                     exc_info=True)
        return ""


#: Surrogate score at/above which a fresh draft is "high-confidence" enough to
#: skip the costly behavioural self-test (the static security/spec gate at
#: approval still runs). Tunable via ``DRAFT_ARCHIVE_SKIP_SCORE``.
_SKIP_SELF_TEST_SCORE = float(os.getenv("DRAFT_ARCHIVE_SKIP_SCORE", "0.85"))


def _maybe_skip_self_test(orch, draft: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """C-N4 surrogate predictor: when the archive feature is ON, use the cheap
    static rubric to skip the expensive behavioural self-test.

    Two skip paths, both flag-gated:

    * **High-confidence skip** — ``surrogate_score`` ≥ :data:`_SKIP_SELF_TEST_SCORE`:
      the draft looks well-formed, so trade the redundant behavioural run for
      the predictor and synthesise a *passing* verdict (the security/spec gate
      at approval still runs).
    * **Cheap-reject skip** — :func:`draft_archive.should_skip_self_test`
      (surrogate below the reject floor): the draft is predicted to fail, so
      don't pay for a self-test at all; synthesise a *failing* verdict that
      routes into the normal auto-refine loop.

    Returns a self-test-shaped dict to USE (a skip happened), or ``None`` to
    fall through to the real :func:`_self_test_draft`. Fail-open: OFF / any
    error / a mid-range score returns ``None`` so the real self-test runs
    exactly as before.
    """
    try:
        from orchestrator import draft_archive
        if not draft_archive.archive_enabled():
            return None
        code = _read_draft_code(orch, draft)
        if not code:
            return None
        score = draft_archive.surrogate_score(code)
        if score >= _SKIP_SELF_TEST_SCORE:
            logger.info(
                "archive: skipping self-test for high-confidence draft %s "
                "(surrogate=%.2f ≥ %.2f)", draft.get("id"), score,
                _SKIP_SELF_TEST_SCORE)
            return {
                "status": "passed",
                "summary": (f"Self-test skipped — surrogate predictor score "
                            f"{score:.2f} ≥ {_SKIP_SELF_TEST_SCORE:.2f} "
                            f"(evolutionary archive)."),
                "tools_called": [], "errors": [], "evidence": "",
                "surrogate_score": round(score, 4),
                "self_test_skipped": True,
                "tested_at": int(time.time() * 1000),
            }
        if draft_archive.should_skip_self_test(code):
            logger.info(
                "archive: cheap-rejecting draft %s before self-test "
                "(surrogate=%.2f predicted failure)", draft.get("id"), score)
            return {
                "status": "failed",
                "summary": (f"Self-test skipped — surrogate predictor score "
                            f"{score:.2f} predicts failure (evolutionary archive); "
                            f"refine the tools."),
                "tools_called": [], "errors": ["surrogate predicted failure"],
                "evidence": "", "surrogate_score": round(score, 4),
                "self_test_skipped": True,
                "tested_at": int(time.time() * 1000),
            }
    except Exception:  # pragma: no cover — surrogate is best-effort
        logger.debug("archive: surrogate pre-check failed", exc_info=True)
    return None


def _archive_on_success(orch, draft: Dict[str, Any], self_test: Dict[str, Any]) -> None:
    """C-N4: archive a passing draft's code as a future exemplar. No-op unless
    the archive flag is on and the self-test passed. Never raises."""
    try:
        from orchestrator import draft_archive
        if not draft_archive.archive_enabled():
            return
        if (self_test or {}).get("status") != "passed":
            return
        code = _read_draft_code(orch, draft)
        if not code:
            return
        # Prefer the surrogate score the skip-path already computed; otherwise
        # treat a real passing self-test as a strong (1.0) exemplar.
        score = self_test.get("surrogate_score")
        if not isinstance(score, (int, float)) or score <= 0:
            score = 1.0
        draft_archive.record_archived_draft(
            draft_archive.draft_fingerprint(draft),
            code,
            float(score),
            owner_user_id=str(draft.get("user_id") or ""),
            draft_uuid=str(draft.get("draft_uuid") or draft.get("id") or ""),
            source_state_revision=int(draft.get("state_revision") or 0),
        )
    except Exception:  # pragma: no cover — archiving is best-effort
        logger.debug("archive: record-on-success failed", exc_info=True)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _summarize_outputs(outputs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Distill a VirtualWebSocket capture into a self-test verdict."""
    tools_called: List[str] = []
    error_messages: List[str] = []
    component_count = 0
    text_preview = ""
    for frame in outputs:
        ftype = frame.get("type")
        if ftype == "chat_step":
            step = frame.get("step") or {}
            if step.get("kind") == "tool_call" and step.get("name"):
                if step["name"] not in tools_called:
                    tools_called.append(step["name"])
        elif ftype in ("ui_render", "ui_update"):
            for comp in frame.get("components") or []:
                if not isinstance(comp, dict):
                    continue
                # Fallback tool attribution: tool-produced components carry
                # _source_tool tags even when chat_step frames are absent.
                src_tool = comp.get("_source_tool")
                if src_tool and src_tool not in tools_called:
                    tools_called.append(src_tool)
                if comp.get("type") == "alert" and comp.get("variant") == "error":
                    error_messages.append(str(comp.get("message", ""))[:200])
                else:
                    component_count += 1
                    if not text_preview and comp.get("type") == "card":
                        for child in comp.get("content") or []:
                            if isinstance(child, dict) and child.get("type") == "text":
                                text_preview = str(child.get("content", ""))[:280]
                                break
    passed = component_count > 0 and not error_messages
    summary = (
        f"{len(tools_called)} tool(s) exercised, {component_count} component(s) produced"
        + (f"; errors: {error_messages[0]}" if error_messages else "")
    )
    return {
        "status": "passed" if passed else "failed",
        "summary": summary,
        "tools_called": tools_called,
        "errors": error_messages[:3],
        "evidence": text_preview,
        "tested_at": int(time.time() * 1000),
    }


async def _self_test_draft(orch, draft: Dict[str, Any], user_request: str,
                           user_id: str, attachments=None) -> Dict[str, Any]:
    """Run the user's originating request as a draft-test chat turn.

    Executes on a ``VirtualWebSocket`` (audit-attributable, no real socket)
    in an isolated chat so the user's conversation is not polluted. Bounded
    by ``SELF_TEST_TIMEOUT_S``.

    ``attachments`` lets an auto-created *parser* draft self-test against the
    exact uploaded file that triggered its creation — the structured
    attachment block is injected so the draft's ``parse_<ext>`` tool runs on
    the real file.
    """
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket

    test_chat_id = await run_generation(
        orch.history.create_chat, user_id=user_id
    )
    task = BackgroundTask(task_id=f"selftest-{draft['id'][:8]}", chat_id=test_chat_id,
                          user_id=user_id)
    vws = VirtualWebSocket(task)
    # 056 US2 (FR-012): a self-test is a machine turn — derive its authority at
    # the SAME shared seam scheduled runs and parser replays use, so the draft's
    # tools dispatch delegated under the owner's standing consent in production
    # instead of being refused fail-closed. An AuthoritySkip is not fatal: the
    # self-test still runs (unchanged dev behavior) and production refuses its
    # real-agent dispatches exactly as it does today.
    from orchestrator.chain_authority import AuthoritySkip
    authority = await orch.derive_machine_authority(
        user_id=user_id, agent_id=None, turn_class="draft_self_test")
    if not isinstance(authority, AuthoritySkip):
        orch._bind_machine_turn(vws, authority)
    try:
        await asyncio.wait_for(
            orch.handle_chat_message(
                vws, user_request, test_chat_id,
                user_id=user_id, draft_agent_id=draft["id"],
                attachments=attachments,
            ),
            timeout=SELF_TEST_TIMEOUT_S,
        )
        return _summarize_outputs(task.outputs)
    except asyncio.TimeoutError:
        return {"status": "timeout", "summary": f"Self-test exceeded {SELF_TEST_TIMEOUT_S}s",
                "tools_called": [], "errors": ["timeout"], "evidence": "",
                "tested_at": int(time.time() * 1000)}
    except Exception as exc:
        logger.exception("agentic: self-test crashed for draft %s", draft["id"])
        return {"status": "failed", "summary": f"Self-test error: {exc}",
                "tools_called": [], "errors": [str(exc)[:200]], "evidence": "",
                "tested_at": int(time.time() * 1000)}
    finally:
        orch._unbind_machine_turn(vws)


def _redteam_allowed_scopes(draft: Dict[str, Any]) -> List[str]:
    """The scopes a fresh draft may use without it counting as escalation: the
    draft's declared scopes if present, else the read-class default."""
    sc = draft.get("scopes") or draft.get("declared_scopes")
    if isinstance(sc, (list, tuple)) and sc:
        return [str(s).lower() for s in sc]
    return ["tools:read", "tools:files", "tools:search"]


async def _run_redteam_gate(orch, draft: Dict[str, Any], user_id: str):
    """Drive the draft through the seeded adversarial scenarios and return a
    RedTeamVerdict. Returns None on a harness/infrastructure error so the
    caller proceeds to the standard gate (fail-open on errors; the verdict
    itself fails CLOSED on a real violation)."""
    try:
        from orchestrator import redteam
        results: List[Dict[str, Any]] = []
        for sc in redteam.scenarios():
            try:
                results.append(await _self_test_draft(orch, draft, sc["prompt"], user_id))
            except Exception:
                logger.exception("redteam: scenario %s errored", sc.get("id"))
        agent_id = f"{draft['agent_slug'].replace('_', '-')}-1"
        tool_scope_map: Dict[str, str] = {}
        try:
            tool_scope_map = await run_generation(
                orch.tool_permissions.get_tool_scope_map, agent_id
            )
        except Exception:
            logger.debug("redteam: no tool scope map for %s", agent_id, exc_info=True)
        phi_check = None
        try:
            from personalization.phi_gate import get_phi_gate
            phi_check = get_phi_gate().contains_phi
        except Exception:
            logger.debug("redteam: PHI gate unavailable", exc_info=True)
        return redteam.verdict(results, allowed_scopes=_redteam_allowed_scopes(draft),
                               tool_scope_map=tool_scope_map, phi_check=phi_check)
    except Exception:
        logger.exception("redteam: gate failed — proceeding to standard approval")
        return None


# ---------------------------------------------------------------------------
# In-chat cards
# ---------------------------------------------------------------------------

def _decision_buttons(draft_id: str, revision: bool = False) -> List[Dict[str, Any]]:
    approve_action = "revision_apply" if revision else "draft_approve"
    discard_action = "revision_discard" if revision else "draft_discard"
    approve_label = "Apply to live agent" if revision else "Approve"
    return [
        Button(label=approve_label, action=approve_action, payload={"draft_id": draft_id}).to_dict(),
        Button(label="Refine", action="draft_refine", payload={"draft_id": draft_id}).to_dict(),
        Button(label="Discard", action=discard_action, payload={"draft_id": draft_id}).to_dict(),
    ]


def creation_card(draft: Dict[str, Any], self_test: Dict[str, Any],
                  revision: bool = False, note: str = "") -> Dict[str, Any]:
    """The approve/refine/discard card presented in chat."""
    status = self_test.get("status", "unknown")
    verdict = {"passed": "✓ Self-test passed", "failed": "✗ Self-test failed",
               "timeout": "✗ Self-test timed out"}.get(status, "Self-test pending")
    lines = [
        Text(content=draft.get("description", ""), variant="caption").to_dict(),
        Text(content=f"**{verdict}** — {self_test.get('summary', '')}", variant="markdown").to_dict(),
    ]
    if self_test.get("evidence"):
        lines.append(Text(content=f"Preview: {self_test['evidence']}", variant="caption").to_dict())
    if note:
        lines.append(Text(content=note, variant="caption").to_dict())
    what = "Draft revision" if revision else "Draft agent"
    return Card(
        # Stable author identity: every state of this draft's card carries the
        # same id, so decision outcomes REPLACE the actionable card on the
        # canvas instead of leaving stale Approve/Refine/Discard buttons
        # clickable after a decision was already made.
        id=f"draft-card-{draft['id']}",
        title=f"{what}: {draft.get('agent_name', 'unnamed')}",
        content=lines + _decision_buttons(draft["id"], revision=revision),
    ).to_dict()


def _error_card(message: str) -> Dict[str, Any]:
    return Alert(message=message, variant="error").to_dict()


# ---------------------------------------------------------------------------
# Meta-tool dispatch
# ---------------------------------------------------------------------------

async def handle_meta_tool(orch, tool_name: str, args: Dict[str, Any], *,
                           user_id: str, chat_id: Optional[str],
                           websocket=None) -> MCPResponse:
    """Entry point for ``__orchestrator__`` pseudo-agent tool calls."""
    try:
        if tool_name == "create_capability":
            return await _create_capability(orch, args, user_id=user_id,
                                            chat_id=chat_id, websocket=websocket)
        if tool_name == "extend_agent":
            return await _extend_agent(orch, args, user_id=user_id,
                                       chat_id=chat_id, websocket=websocket)
        return MCPResponse(error={"message": f"Unknown meta-tool: {tool_name}", "retryable": False})
    except Exception as exc:
        logger.exception("agentic: meta-tool %s failed", tool_name)
        card = _error_card(
            "Creating the capability failed unexpectedly. You can retry, rephrase the "
            "request, or create the agent manually under Settings → Agents & permissions."
        )
        return MCPResponse(
            result={"status": "error", "detail": str(exc)[:300]},
            ui_components=[card],
        )


async def _create_capability(orch, args: Dict[str, Any], *, user_id: str,
                             chat_id: Optional[str], websocket=None) -> MCPResponse:
    agent_name = (args.get("agent_name") or "").strip()
    description = (args.get("description") or "").strip()
    tools_spec = args.get("tools_spec") or []
    user_request = (args.get("user_request") or description).strip()
    if not agent_name or len(description) < 10 or not tools_spec:
        return MCPResponse(error={
            "message": "create_capability needs agent_name, a description (≥10 chars) and tools_spec",
            "retryable": False})
    tools_spec = tools_spec[:4]

    fingerprint = gap_fingerprint(agent_name, tools_spec)
    existing = await run_generation(
        orch.history.db.find_gap_draft,
        user_id,
        chat_id or "",
        fingerprint,
    )
    if existing:
        # Route repeat requests to the staged draft, never duplicate.
        self_test = json.loads(existing.get("self_test") or "{}")
        card = creation_card(existing, self_test,
                             note="This capability is already staged — decide on the existing draft.")
        return MCPResponse(
            result={"status": "duplicate", "draft_id": existing["id"],
                    "draft_status": existing.get("status")},
            ui_components=[card],
        )

    lifecycle = orch.lifecycle_manager
    draft = await lifecycle.create_draft(
        user_id=user_id, agent_name=agent_name, description=description,
        tools_spec=[{"name": t.get("name", ""), "description": t.get("description", "")}
                    for t in tools_spec],
    )
    draft_id = draft["id"]
    await run_generation(
        orch.history.db.update_draft_agent,
        draft_id,
        origin="auto_chat",
        source_chat_id=chat_id or "",
        gap_fingerprint=fingerprint,
    )
    await _audit(user_id, "lifecycle.gap_detected",
                 f"Capability gap: {agent_name} — auto-creating draft",
                 correlation_id=draft_id, outcome="in_progress", chat_id=chat_id,
                 inputs_meta={"gap_fingerprint": fingerprint, "draft_id": draft_id})

    # Generate + start + self-test (≤1 auto-refine on failure).
    draft = await lifecycle.generate_code(draft_id, websocket=websocket)
    if draft.get("status") in ("error", "rejected"):
        await _audit(user_id, "lifecycle.auto_created", "Generation failed",
                     correlation_id=draft_id, outcome="failure", chat_id=chat_id,
                     agent_id=None, inputs_meta={"draft_id": draft_id})
        card = creation_card(draft, {"status": "failed",
                                     "summary": draft.get("error_message") or "generation failed"},
                             note="Generation failed — Refine to retry with guidance, or Discard.")
        return MCPResponse(result={"status": "generation_failed", "draft_id": draft_id},
                           ui_components=[card])

    draft = await lifecycle.start_draft_agent(draft_id, websocket=websocket)
    # C-N4 surrogate cheap-skip: a high-confidence draft skips the costly
    # behavioural self-test (flag-gated; falls through to the real run otherwise).
    self_test = await run_generation(_maybe_skip_self_test, orch, draft) \
        or await _self_test_draft(orch, draft, user_request, user_id)

    refines = 0
    while self_test["status"] != "passed" and refines < SELF_TEST_MAX_AUTO_REFINES:
        refines += 1
        failure = "; ".join(self_test.get("errors") or [self_test.get("summary", "failed")])
        logger.info("agentic: self-test failed for %s — auto-refine %d (%s)",
                    draft_id, refines, failure[:120])
        draft = await lifecycle.refine_agent(
            draft_id, f"The self-test failed: {failure}. Fix the tools so this request "
                      f"succeeds: {user_request}", websocket=websocket)
        if draft.get("status") == "error":
            break
        draft = await lifecycle.start_draft_agent(draft_id, websocket=websocket)
        # The surrogate re-scores the (now refined) code; a high-confidence
        # refinement skips the costly run, a still-weak one is cheap-rejected.
        self_test = await run_generation(_maybe_skip_self_test, orch, draft) \
            or await _self_test_draft(orch, draft, user_request, user_id)

    self_test["auto_refines"] = refines
    await run_generation(
        orch.history.db.update_draft_agent,
        draft_id,
        self_test=json.dumps(self_test),
    )
    # C-N4: a passing draft becomes a future codegen exemplar (flag-gated).
    stored_draft = await run_generation(
        orch.history.db.get_draft_agent, draft_id
    )
    await run_generation(
        _archive_on_success, orch, stored_draft or draft, self_test
    )
    await _audit(user_id, "lifecycle.auto_created",
                 f"Auto-created draft '{agent_name}' ({draft_id})",
                 correlation_id=draft_id, chat_id=chat_id,
                 inputs_meta={"draft_id": draft_id, "gap_fingerprint": fingerprint})
    await _audit(user_id, "lifecycle.self_test",
                 f"Self-test {self_test['status']}: {self_test['summary']}",
                 correlation_id=draft_id,
                 outcome="success" if self_test["status"] == "passed" else "failure",
                 chat_id=chat_id, inputs_meta={"draft_id": draft_id})

    card = creation_card(
        await run_generation(orch.history.db.get_draft_agent, draft_id) or draft,
        self_test,
    )
    return MCPResponse(
        result={"status": "created", "draft_id": draft_id,
                "self_test": self_test["status"],
                "next": "user must approve, refine, or discard via the buttons"},
        ui_components=[card],
    )


# ---------------------------------------------------------------------------
# Live-agent revision (extend_agent → staged draft → gated swap)
# ---------------------------------------------------------------------------

def _live_agent_dir_and_draft(orch, agent_id: str):
    """Resolve a live, lifecycle-managed agent's draft row + directory."""
    lifecycle = orch.lifecycle_manager
    row = lifecycle._get_draft_by_agent_id(agent_id)
    if not row or row.get("status") != "live":
        return None, None
    agent_dir = os.path.join(lifecycle._agents_dir, row["agent_slug"])
    return row, agent_dir


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as stream:
        return stream.read()


def _stage_revision_code(lifecycle, rev, live_row, rev_dir, new_code):
    """Compile, write, and gate one revision in the generation executor.

    Returns ``(report, validation)`` where ``validation`` is ``None`` when the
    security gate refused the code before the validator (which EXECUTES the
    tools) could run — the H4 pre-execution contract.
    """

    # Gate BEFORE writing (H4): flagged code must never touch the agents tree
    # that discovery/start scans. The generate_code path asserts exactly this
    # (its test checks the tools file does not exist on a block); the revision
    # path must hold the same contract rather than writing first and gating
    # after.
    compile(new_code, "mcp_tools.py", "exec")
    report = lifecycle.security.analyze(
        new_code, filename=f"{rev['agent_slug']}/mcp_tools.py"
    )
    if blocks_execution(report):
        return report, None
    os.makedirs(rev_dir, exist_ok=True)
    with open(os.path.join(rev_dir, "mcp_tools.py"), "w", encoding="utf-8") as stream:
        stream.write(new_code)
    # Validate the STAGED bytes — the revision slug's own directory. This
    # used to point at the live agent's slug, so the validator imported the
    # unmodified live file and never inspected the staged code at all.
    validation = lifecycle.validator.validate(
        new_code, rev["agent_slug"], lifecycle._agents_dir
    )
    return report, validation


def _gate_revision_code(lifecycle, rev, live_row, new_code):
    report = lifecycle.security.analyze(
        new_code, filename=f"{live_row['agent_slug']}/mcp_tools.py"
    )
    if blocks_execution(report):
        return report, None
    validation = lifecycle.validator.validate(
        new_code, rev["agent_slug"], lifecycle._agents_dir
    )
    return report, validation


async def _extend_agent(orch, args: Dict[str, Any], *, user_id: str,
                        chat_id: Optional[str], websocket=None) -> MCPResponse:
    agent_id = (args.get("agent_id") or "").strip()
    instruction = (args.get("instruction") or "").strip()
    if not agent_id or not instruction:
        return MCPResponse(error={"message": "extend_agent needs agent_id and instruction",
                                  "retryable": False})

    # Ownership gate: only the owner may stage a revision.
    db = orch.history.db
    ownership = await run_generation(db.get_agent_ownership, agent_id)
    user = await run_generation(db.get_user, user_id) or {}
    owner_email = user.get("email", user_id)
    if not ownership or ownership.get("owner_email") not in (owner_email, user_id):
        return MCPResponse(
            result={"status": "not_owned", "agent_id": agent_id},
            ui_components=[_error_card(
                f"You don't own '{agent_id}', so it can't be extended. You can create a "
                f"new agent with this capability instead.")],
        )

    live_row, live_dir = await run_generation(
        _live_agent_dir_and_draft, orch, agent_id
    )
    if live_row is None or not await run_generation(os.path.isdir, live_dir):
        return MCPResponse(
            result={"status": "not_revisable", "agent_id": agent_id},
            ui_components=[_error_card(
                f"'{agent_id}' is not a lifecycle-managed agent, so it can't be revised "
                f"in place. Ask me to create a new agent with this capability instead.")],
        )

    fingerprint = gap_fingerprint(agent_id, extra=instruction)
    existing = await run_generation(
        db.find_gap_draft, user_id, chat_id or "", fingerprint
    )
    if existing:
        self_test = json.loads(existing.get("self_test") or "{}")
        card = creation_card(existing, self_test, revision=True,
                             note="This revision is already staged — decide on it below.")
        return MCPResponse(result={"status": "duplicate", "draft_id": existing["id"]},
                           ui_components=[card])

    lifecycle = orch.lifecycle_manager
    rev = await lifecycle.create_draft(
        user_id=user_id,
        agent_name=f"{live_row['agent_name']} (revision)",
        description=f"Revision of {agent_id}: {instruction}",
    )
    rev_id = rev["id"]
    await run_generation(
        db.update_draft_agent,
        rev_id,
        origin="revision",
        source_chat_id=chat_id or "",
        gap_fingerprint=fingerprint,
        revises_agent_id=agent_id,
    )
    await _audit(user_id, "lifecycle.gap_detected",
                 f"Revision requested for {agent_id}: {instruction[:120]}",
                 correlation_id=rev_id, outcome="in_progress", chat_id=chat_id,
                 agent_id=agent_id, inputs_meta={"draft_id": rev_id, "revises_agent_id": agent_id})

    # Stage: refine a copy of the live agent's tools file via the generator,
    # then gate-check the staged code with the validator harness (its sample
    # executions are the revision's self-test — no clone process needed).
    rev_dir = os.path.join(lifecycle._agents_dir, rev["agent_slug"])
    try:
        live_tools = os.path.join(live_dir, "mcp_tools.py")
        current_code = await run_generation(_read_text_file, live_tools)
        new_code = await lifecycle.generator.refine_tools_file(
            current_code=current_code, user_message=instruction,
            agent_name=live_row["agent_name"], description=live_row["description"])
        report, validation = await run_generation(
            _stage_revision_code,
            lifecycle,
            rev,
            live_row,
            rev_dir,
            new_code,
        )
        sec_blocker = getattr(report, "max_severity", None)
        sec_name = getattr(sec_blocker, "name", str(sec_blocker or "")).upper()
        # validation is None when the security gate refused the staged code
        # before the validator (which executes it) could run.
        passed = validation is not None and validation.passed
        validator_summary = (
            f"validator: {validation.tools_passed}/{validation.tools_tested} tools passed"
            if validation is not None
            else "validator: skipped (security gate refused execution)"
        )
        self_test = {
            "status": "passed" if passed else "failed",
            "summary": (f"{validator_summary}; "
                        f"security max severity: {sec_name or 'NONE'}"),
            "tools_called": [], "errors": [] if passed else ["gate checks failed"],
            "evidence": "", "tested_at": int(time.time() * 1000),
        }
        await run_generation(
            db.update_draft_agent,
            rev_id,
            status="generated",
            self_test=json.dumps(self_test),
            security_report=json.dumps(report.to_dict()),
            validation_report=json.dumps(
                validation.to_dict() if validation is not None else {"passed": False}
            ),
        )
    except Exception as exc:
        logger.exception("agentic: revision staging failed for %s", agent_id)
        await run_generation(
            db.update_draft_agent,
            rev_id,
            status="error",
            error_message=str(exc)[:500],
        )
        await _audit(user_id, "lifecycle.auto_created", f"Revision staging failed: {exc}",
                     correlation_id=rev_id, outcome="failure", chat_id=chat_id, agent_id=agent_id)
        return MCPResponse(result={"status": "staging_failed", "draft_id": rev_id},
                           ui_components=[_error_card(
                               "Staging the revision failed. Refine with more detail or discard it.")])

    await _audit(user_id, "lifecycle.auto_created",
                 f"Staged revision {rev_id} for {agent_id}",
                 correlation_id=rev_id, chat_id=chat_id, agent_id=agent_id)
    await _audit(user_id, "lifecycle.self_test",
                 f"Revision gate-check {self_test['status']}: {self_test['summary']}",
                 correlation_id=rev_id,
                 outcome="success" if self_test["status"] == "passed" else "failure",
                 chat_id=chat_id, agent_id=agent_id)

    card = creation_card(
        await run_generation(db.get_draft_agent, rev_id),
        self_test,
        revision=True,
    )
    return MCPResponse(
        result={"status": "revision_staged", "draft_id": rev_id,
                "self_test": self_test["status"],
                "next": "user must apply, refine, or discard via the buttons"},
        ui_components=[card],
    )


async def apply_revision(orch, rev: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """Gate + swap a staged revision into its live agent.

    The live agent's code changes only inside this function, and every
    failure path restores the backup before restart — a failed gate or
    restart never leaves the live agent modified.
    Returns {applied: bool, detail: str}.
    """
    lifecycle = orch.lifecycle_manager
    db = orch.history.db
    agent_id = rev.get("revises_agent_id") or ""
    live_row, live_dir = await run_generation(
        _live_agent_dir_and_draft, orch, agent_id
    )
    if live_row is None:
        return {"applied": False, "detail": f"Live agent {agent_id} not found"}

    rev_dir = os.path.join(lifecycle._agents_dir, rev["agent_slug"])
    staged = os.path.join(rev_dir, "mcp_tools.py")
    if not await run_generation(os.path.exists, staged):
        return {"applied": False, "detail": "Staged revision file missing"}
    new_code = await run_generation(_read_text_file, staged)

    # Re-run the full gate on the staged code at apply time (it may be stale).
    # A HIGH/CRITICAL security report returns validation=None — the staged
    # code was refused before the validator could execute it (H4).
    report, validation = await run_generation(
        _gate_revision_code, lifecycle, rev, live_row, new_code
    )
    sec_name = getattr(getattr(report, "max_severity", None), "name", "").upper()
    if validation is None or not validation.passed:
        validator_note = (
            f"{validation.tools_passed}/{validation.tools_tested}"
            if validation is not None
            else "skipped (security gate refused execution)"
        )
        await run_generation(
            db.update_draft_agent,
            rev["id"],
            status="rejected",
            error_message=(
                f"Gate failed: security={sec_name or 'NONE'}, validator "
                f"{validator_note}"
            ),
        )
        await _audit(user_id, "lifecycle.rejected",
                     f"Revision {rev['id']} failed the gate — live agent unchanged",
                     correlation_id=rev["id"], outcome="failure", agent_id=agent_id)
        return {"applied": False,
                "detail": "Security/validation gate failed — the live agent is unchanged. "
                          "The revision stays editable (Refine) or can be discarded."}

    live_tools = os.path.join(live_dir, "mcp_tools.py")
    backup = live_tools + ".bak027"
    # Snapshot scopes so the restart doesn't widen them (start_draft_agent
    # re-enables all scopes for testing; live agents must keep theirs).
    scopes_snapshot = {}
    try:
        scopes_snapshot = dict(
            await run_generation(
                orch.tool_permissions.get_agent_scopes, user_id, agent_id
            ) or {}
        )
    except Exception:
        logger.debug("agentic: scope snapshot failed", exc_info=True)

    try:
        await lifecycle.stop_draft_agent(live_row["id"])
        await run_generation(shutil.copy2, live_tools, backup)
        await run_generation(Path(live_tools).write_text, new_code, encoding="utf-8")
        await lifecycle.start_draft_agent(live_row["id"], align_scopes=False)
        await run_generation(db.update_draft_agent, live_row["id"], status="live")
    except Exception as exc:
        logger.exception("agentic: revision swap failed for %s — rolling back", agent_id)
        try:
            if await run_generation(os.path.exists, backup):
                await run_generation(shutil.copy2, backup, live_tools)
            await lifecycle.start_draft_agent(live_row["id"], align_scopes=False)
            await run_generation(db.update_draft_agent, live_row["id"], status="live")
        except Exception:
            logger.exception("agentic: rollback restart failed for %s", agent_id)
        await run_generation(
            db.update_draft_agent,
            rev["id"],
            status="rejected",
            error_message=str(exc)[:500],
        )
        await _audit(user_id, "lifecycle.revision_rolled_back",
                     f"Revision {rev['id']} swap failed; backup restored",
                     correlation_id=rev["id"], outcome="failure", agent_id=agent_id)
        return {"applied": False, "detail": "Swap failed — the previous version was restored."}
    finally:
        try:
            if scopes_snapshot:
                await run_generation(
                    orch.tool_permissions.set_agent_scopes,
                    user_id,
                    agent_id,
                    scopes_snapshot,
                )
        except Exception:
            logger.debug("agentic: scope restore failed", exc_info=True)
        try:
            if await run_generation(os.path.exists, backup):
                await run_generation(os.remove, backup)
        except OSError:
            pass

    # Success: clean up the staged clone + row.
    try:
        await lifecycle.delete_draft(rev["id"])
    except Exception:
        logger.warning("agentic: revision cleanup failed for %s", rev["id"], exc_info=True)
    # Feature 040 (US2): a revision reintroduces (possibly un-reviewed) code, so
    # reset any owner-safe marker on the live agent — re-approval is required.
    try:
        from orchestrator import agent_trust
        await agent_trust.reset_on_revision(db, agent_id, actor_user=user_id)
    except Exception:
        logger.debug("agentic: safe-marker reset failed for %s", agent_id, exc_info=True)
    await _audit(user_id, "lifecycle.revision_applied",
                 f"Revision applied to {agent_id}",
                 correlation_id=rev["id"], agent_id=agent_id)
    return {"applied": True, "detail": f"Revision applied — {agent_id} restarted with the new tools."}


# ---------------------------------------------------------------------------
# Decision handlers (chat cards + drafts surface; registered via chrome_events)
# ---------------------------------------------------------------------------

async def _owned_draft(
    orch, user_id: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    draft_id = str(payload.get("draft_id") or "")
    draft = (
        await run_generation(orch.history.db.get_draft_agent, draft_id)
        if draft_id else None
    )
    if not draft or draft.get("user_id") != user_id:
        return None
    return draft


async def _decidable_draft(
    orch, user_id: str, roles, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Draft the caller may refine/discard: the owner, OR an admin acting on an
    auto-created attachment parser (origin ``auto_attachment``)."""
    draft_id = str(payload.get("draft_id") or "")
    draft = (
        await run_generation(orch.history.db.get_draft_agent, draft_id)
        if draft_id else None
    )
    if not draft:
        return None
    if draft.get("user_id") == user_id:
        return draft
    if draft.get("origin") == "auto_attachment" and "admin" in (roles or []):
        return draft
    return None


async def _send_chat_card(orch, websocket, component: Dict[str, Any]):
    await orch.send_ui_render(websocket, [component], target="chat")


async def _replace_card_state(orch, websocket, user_id: str, draft_id: str,
                              card: Dict[str, Any]) -> None:
    """Swap the canvas decision card for its post-decision state.

    The creation card persists in the chat's workspace under the stable
    author id ``draft-card-<draft_id>``; upserting a card with the same id
    morphs it in place on every socket, so a decided draft can no longer be
    re-actioned from stale buttons. Best-effort: a missing active chat
    (e.g. decision made from the Drafts surface) is fine — the chat bubble
    already communicated the outcome.
    """
    try:
        chat_id = orch._ws_active_chat.get(id(websocket)) if websocket is not None else None
        if not chat_id:
            return
        card = dict(card)
        card["id"] = f"draft-card-{draft_id}"
        await orch._send_or_replace_components(websocket, [card], chat_id, user_id=user_id)
    except Exception:
        logger.debug("decision card replacement failed (non-fatal)", exc_info=True)


def _terminal_card(draft_id: str, title: str, message: str) -> Dict[str, Any]:
    """Button-less end-state card that replaces the decision card."""
    return Card(id=f"draft-card-{draft_id}", title=title, content=[
        Text(content=message, variant="caption").to_dict(),
    ]).to_dict()


async def _promote_parser_global(orch, draft, agent_id, *, approved_by):
    """Promote an approved attachment parser to a global, public capability and
    mark the registry live so every user's future uploads of that type resolve
    to ``covered``. Best-effort; never raises.
    """
    try:
        from orchestrator import attachment_autoparse
        from orchestrator.attachments.parser_repo import AttachmentParserRepository

        parser_repo = AttachmentParserRepository(orch.history.db)
        row = await run_generation(parser_repo.get_by_draft, draft["id"]) or {}
        gap = row.get("gap_fingerprint")
        extension = row.get("extension")
        requested_by = row.get("requested_by")
        tool_name = attachment_autoparse._tool_name_for(extension)

        # Make the agent public (global), then mark the registry live.
        try:
            await run_generation(
                orch.history.db.set_agent_visibility, agent_id, True
            )
        except Exception:
            logger.debug("autoparse: set_agent_visibility failed", exc_info=True)
        if gap:
            await run_generation(
                parser_repo.mark_live,
                gap,
                live_agent_id=agent_id,
                tool_name=tool_name,
                approved_by=approved_by,
            )

        # Enable the (read-only) scopes the parser needs for the originating
        # user so it's usable immediately; other users pick it up via the
        # public-catalog consent path (feature 030).
        try:
            scopes = await run_generation(
                orch.tool_permissions.scopes_required_by_tools, agent_id
            ) or []
            grant = {s: True for s in scopes if s != "tools:write"}
            if grant and requested_by:
                await run_generation(
                    orch.tool_permissions.set_agent_scopes,
                    requested_by,
                    agent_id,
                    grant,
                )
        except Exception:
            logger.debug("autoparse: scope grant failed", exc_info=True)

        # The parser is live and the uploader is scoped, so re-run their
        # original request and deliver the parsed result into the original
        # chat. If the original turn can't be recovered/replayed, fall back to
        # the notify ("ask again to read your file").
        if requested_by:
            replayed = await attachment_autoparse.auto_continue_after_go_live(
                orch,
                requested_by=requested_by,
                source_chat_id=row.get("source_chat_id") or draft.get("source_chat_id"),
                source_attachment_id=(row.get("source_attachment_id")
                                      or draft.get("source_attachment_id")),
                extension=extension,
                category=row.get("category"),
            )
            if replayed:
                await attachment_autoparse._notify_user(
                    orch, requested_by,
                    f"The .{extension} reader is live — I re-read your file; "
                    "see the new reply in your chat.")
            else:
                await attachment_autoparse._notify_user(
                    orch, requested_by,
                    f"The .{extension} reader is live — ask again to read your file.")
    except Exception:
        logger.exception("autoparse: global promotion failed for draft %s", draft.get("id"))


async def _h_draft_approve(orch, websocket, user_id, roles, payload):
    """Approve a draft: existing security gate → live.

    Auto-created attachment-parser drafts (origin ``auto_attachment``) require
    the **admin** role to approve and are promoted **globally** (public,
    available to all users), not into the approver's private fleet. Non-admins
    are refused and audited.
    """
    draft_id = str(payload.get("draft_id") or "")
    raw_draft = (
        await run_generation(orch.history.db.get_draft_agent, draft_id)
        if draft_id else None
    )
    is_autoparse = bool(raw_draft and raw_draft.get("origin") == "auto_attachment")

    if is_autoparse:
        # Admin-only approval; the uploader (owner) cannot self-approve.
        if "admin" not in (roles or []):
            await _audit(user_id, "lifecycle.rejected",
                         f"Non-admin approval attempt on parser draft {draft_id}",
                         correlation_id=draft_id, outcome="failure")
            await _send_chat_card(orch, websocket, _error_card(
                "Approving an auto-created file parser requires the admin role."))
            return None
        draft = raw_draft
    else:
        draft = await _owned_draft(orch, user_id, payload)
        if draft is None:
            await _send_chat_card(orch, websocket, _error_card("Draft not found (it may have been discarded)."))
            return None

    # Adversarial red-team gate before promotion. Drive the draft through the
    # seeded adversarial scenarios; if it makes an out-of-scope tool call,
    # attempts egress, or emits PHI on any of them, BLOCK promotion
    # (fail-closed on a real violation). Flag-gated (default OFF); a harness
    # error returns None and falls through to the standard security gate.
    from orchestrator import redteam
    if redteam.redteam_enabled():
        rt = await _run_redteam_gate(orch, draft, user_id)
        if rt is not None and not rt.passed:
            reasons = "; ".join(f"{v.kind}: {v.detail}" for v in rt.violations[:5])
            await _audit(user_id, "lifecycle.rejected",
                         f"Draft {draft['id']} blocked by red-team gate ({reasons})",
                         correlation_id=draft["id"], outcome="failure")
            try:
                await run_generation(
                    orch.history.db.update_draft_agent,
                    draft["id"],
                    status="rejected",
                    error_message=f"Red-team gate: {reasons}"[:500],
                )
            except Exception:
                logger.debug("redteam: status update failed", exc_info=True)
            blocked = Card(title=f"{draft['agent_name']}: blocked by safety test", content=[
                Text(content=(f"The agent failed the adversarial safety test — {reasons}. "
                              f"The draft stays editable: Refine it or Discard it."),
                     variant="default").to_dict(),
            ] + _decision_buttons(draft["id"])).to_dict()
            await _send_chat_card(orch, websocket, blocked)
            await _replace_card_state(orch, websocket, user_id, draft["id"], blocked)
            return None

    result = await orch.lifecycle_manager.approve_agent(draft["id"], websocket=websocket)
    status = (result or {}).get("status")
    corr = draft["id"]
    if status == "live":
        agent_id = f"{draft['agent_slug'].replace('_', '-')}-1"
        if is_autoparse:
            await _promote_parser_global(orch, draft, agent_id, approved_by=user_id)
        # C-N4: an approved (now-live) draft is a strong exemplar for future
        # codegen of similar capability gaps. Flag-gated + best-effort.
        try:
            approved_draft = await run_generation(
                orch.history.db.get_draft_agent, draft["id"]
            ) or draft
            approved_self_test = json.loads(approved_draft.get("self_test") or "{}")
            if approved_self_test.get("status") != "passed":
                # Approval is itself a success signal even if the self-test
                # verdict isn't recorded as "passed" (e.g. revision gate-check).
                approved_self_test = {"status": "passed"}
            await run_generation(
                _archive_on_success, orch, approved_draft, approved_self_test
            )
        except Exception:
            logger.debug("archive: approval-time record failed", exc_info=True)
        await _audit(user_id, "lifecycle.approved", f"Draft {draft['id']} approved → live",
                     correlation_id=corr, agent_id=agent_id)
        live_msg = ("Security checks passed. The parser is live and available to everyone — "
                    "re-upload or ask again to read that file type."
                    if is_autoparse else
                    "Security checks passed. The agent joined your fleet and is usable "
                    "right now — just ask again.")
        await _send_chat_card(orch, websocket, Card(title=f"{draft['agent_name']} is live", content=[
            Text(content=live_msg, variant="default").to_dict(),
        ]).to_dict())
        await _replace_card_state(orch, websocket, user_id, draft["id"], _terminal_card(
            draft["id"], f"✓ Approved: {draft['agent_name']}",
            "Approved and live — ask again to use it."))
    else:
        detail = (result or {}).get("error_message") or f"status: {status}"
        await _audit(user_id, "lifecycle.rejected", f"Draft {draft['id']} not promoted ({status})",
                     correlation_id=corr, outcome="failure")
        not_promoted = Card(title=f"{draft['agent_name']}: not promoted", content=[
            Text(content=f"The approval gate did not pass — {detail}. The draft stays "
                         f"editable: Refine it or Discard it.", variant="default").to_dict(),
        ] + _decision_buttons(draft["id"])).to_dict()
        await _send_chat_card(orch, websocket, not_promoted)
        await _replace_card_state(orch, websocket, user_id, draft["id"], not_promoted)
    return None


async def _h_draft_refine(orch, websocket, user_id, roles, payload):
    """Refine a draft conversationally."""
    draft = await _decidable_draft(orch, user_id, roles, payload)
    if draft is None:
        await _send_chat_card(orch, websocket, _error_card("Draft not found (it may have been discarded)."))
        return None
    message = str(payload.get("message") or (payload.get("fields") or {}).get("message") or "").strip()
    if not message:
        # Render an inline refine-input card.
        await _send_chat_card(orch, websocket, Card(title=f"Refine {draft['agent_name']}", content=[
            {"type": "param_picker", "title": "What should change?",
             "fields": [{"name": "message", "kind": "text", "label": "Describe the fix/change"}],
             "submit_label": "Refine",
             "submit_message_template": f"Refine draft {draft['id']}: {{message}}"},
        ]).to_dict())
        return None
    result = await orch.lifecycle_manager.refine_agent(draft["id"], message, websocket=websocket)
    await _audit(user_id, "lifecycle.refined", f"Draft {draft['id']} refined",
                 correlation_id=draft["id"])
    note = ("Refined. Test it again in chat, then Approve / Discard."
            if result.get("status") != "error"
            else f"Refine failed: {result.get('error_message', 'unknown error')}")
    latest = await run_generation(
        orch.history.db.get_draft_agent, draft["id"]
    ) or {}
    self_test = json.loads(latest.get("self_test") or "{}")
    refreshed = creation_card(
        latest or draft, self_test,
        revision=bool(draft.get("revises_agent_id")), note=note)
    await _send_chat_card(orch, websocket, refreshed)
    # Same stable id → the canvas card morphs to the refreshed state.
    await _replace_card_state(orch, websocket, user_id, draft["id"], refreshed)
    return None


async def _h_draft_discard(orch, websocket, user_id, roles, payload):
    """Decline/discard a draft (declined drafts are removed)."""
    draft = await _decidable_draft(orch, user_id, roles, payload)
    if draft is None:
        await _send_chat_card(orch, websocket, _error_card("Draft not found (already discarded?)."))
        return None
    # If this is an auto-created parser, mark its registry row discarded so the
    # format can be re-attempted by a later upload.
    if draft.get("origin") == "auto_attachment":
        try:
            from orchestrator.attachments.parser_repo import (
                AttachmentParserRepository, STATUS_DISCARDED,
            )
            _pr = AttachmentParserRepository(orch.history.db)
            _row = await run_generation(_pr.get_by_draft, draft["id"])
            if _row:
                await run_generation(
                    _pr.mark_status,
                    _row["gap_fingerprint"],
                    STATUS_DISCARDED,
                )
        except Exception:
            logger.debug("autoparse: discard registry update failed", exc_info=True)
    await orch.lifecycle_manager.delete_draft(draft["id"])
    await _audit(user_id, "lifecycle.discarded", f"Draft {draft['id']} discarded",
                 correlation_id=draft["id"])
    await _send_chat_card(orch, websocket, Card(title="Draft discarded", content=[
        Text(content=f"'{draft['agent_name']}' was removed. I'll answer with existing "
                     f"capabilities where I can.", variant="default").to_dict(),
    ]).to_dict())
    await _replace_card_state(orch, websocket, user_id, draft["id"], _terminal_card(
        draft["id"], f"Discarded: {draft['agent_name']}",
        "This draft was removed — nothing went live."))
    return None


async def _h_revision_apply(orch, websocket, user_id, roles, payload):
    """Apply a staged revision to its live agent (gate → swap → rollback-safe)."""
    rev = await _owned_draft(orch, user_id, payload)
    if rev is None or not rev.get("revises_agent_id"):
        await _send_chat_card(orch, websocket, _error_card("Revision not found."))
        return None
    outcome = await apply_revision(orch, rev, user_id)
    if outcome["applied"]:
        await _send_chat_card(orch, websocket, Card(title="Revision applied", content=[
            Text(content=outcome["detail"], variant="default").to_dict()]).to_dict())
        await _replace_card_state(orch, websocket, user_id, rev["id"], _terminal_card(
            rev["id"], "✓ Revision applied", outcome["detail"]))
    else:
        not_applied = Card(title="Revision not applied", content=[
            Text(content=outcome["detail"], variant="default").to_dict(),
        ] + _decision_buttons(rev["id"], revision=True)).to_dict()
        await _send_chat_card(orch, websocket, not_applied)
        await _replace_card_state(orch, websocket, user_id, rev["id"], not_applied)
    return None


async def _h_revision_discard(orch, websocket, user_id, roles, payload):
    return await _h_draft_discard(orch, websocket, user_id, roles, payload)


HANDLERS = {
    "draft_approve": _h_draft_approve,
    "draft_refine": _h_draft_refine,
    "draft_discard": _h_draft_discard,
    "revision_apply": _h_revision_apply,
    "revision_discard": _h_revision_discard,
}
