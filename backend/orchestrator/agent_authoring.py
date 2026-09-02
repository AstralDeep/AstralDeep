"""BYO agent authoring orchestration (feature 058, T009 + T016 + T017 + T019).

Two entry points onto the SAME create→analyze→generate→deliver spine:

* :func:`author_and_deliver` — the minimal one-shot path (T009).
* the **5-phase guided flow** (T017): ``specify → clarify → plan → tasks →
  analyze → generate``. Each phase artifact is ASSISTANT-DRAFTED (the user's own
  configured LLM, via ``orch._call_llm_json``) and HUMAN-EDITABLE; advancing is
  always an explicit act. The chrome surface
  (:mod:`orchestrator.projection_surfaces.authoring`) is the only UI over it.

The load-bearing property is STRUCTURAL: the 057 Analyze gate runs BEFORE
``generate_code`` so a constitution-violating draft produces NO code and never
goes live (FR-003/SC-004). Generation is unreachable from a pre-Analyze state —
:func:`generation_gate` refuses on the SERVER (phase, stored pass, constitution
version, and a fingerprint of the exact artifacts Analyze saw), so a hand-forged
``chrome_author_generate`` on a half-finished session is refused, not merely
un-clickable. On a passing Analyze the draft is generated (static code gates run
inside the lifecycle), the ``user_agent`` row is marked ``validated``, and the
bundle is DELIVERED to the owner's desktop host — never Popen'd on the
orchestrator (SC-002). The host runs it and dials back in over the tunnel
(register → go_live).

**Storage (T017)**: no new tables and no new columns — the 057 migration already
added exactly the authoring state this needs (``phase``, ``clarify_answers``,
``plan_json``, ``analyze_result``, ``constitution_version`` on ``draft_agents``).
The Tasks artifact rides ``plan_json["tasks"]``: it is part of the same authoring
plan blob, nothing else reads it, and ``agent_analyze.check`` only ever looks at
``plan["tools_used"]`` / ``plan["tool_scopes"]`` — so an extra key is inert. That
keeps ``SCHEMA_REVISION`` untouched.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid

from shared.feature_flags import flags

logger = logging.getLogger("Orchestrator.AgentAuthoring")


def byo_enabled() -> bool:
    """FF_BYO_AGENTS. Every authoring entry point re-checks this and fails closed
    — the delivery/tunnel/lifecycle seams underneath are reachable ONLY through
    an authoring entry point, so this is the gate that keeps the feature inert
    (FR-009: flag-off behavior is byte-identical to today)."""
    return flags.is_enabled("byo_agents")


def _generation_claim_id(
    *,
    owner_user_id: str,
    draft_uuid: str,
    source_state_revision: int,
    target_agent_id: str,
) -> str:
    """Derive one replay-stable UUID4 for an exact draft generation source."""

    if type(source_state_revision) is not int or source_state_revision <= 0:
        raise ValueError("source_state_revision must be positive")
    payload = json.dumps(
        {
            "domain": "astraldeep.generated-agent.claim/v1",
            "draft_uuid": str(uuid.UUID(str(draft_uuid))),
            "owner_user_id": owner_user_id,
            "source_state_revision": source_state_revision,
            "target_agent_id": target_agent_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return str(uuid.UUID(bytes=hashlib.sha256(payload).digest()[:16], version=4))


#: The bundle the desktop host expects (contracts/host-bundle.md §2).
BUNDLE_FILENAMES = ("agent_main.py", "mcp_tools.py", "manifest.json")

#: The 5 authoring phases + the terminal generate step. Order IS the state
#: machine: a session only ever moves one step right, and only on an explicit
#: user action that clears that phase's gate.
PHASES: Tuple[str, ...] = ("specify", "clarify", "plan", "tasks", "analyze", "generate")

PHASE_LABELS = {
    "specify": "Specify",
    "clarify": "Clarify",
    "plan": "Plan",
    "tasks": "Tasks",
    "analyze": "Analyze",
    "generate": "Generate",
}

#: Phases whose artifact the user edits + submits to advance (no gate of their
#: own beyond "the artifact must be non-empty"). Clarify/Analyze/Generate have
#: dedicated gated handlers.
_PLAIN_PHASES = ("specify", "plan", "tasks")

#: What an unanswered clarification looks like. The assistant is told to mark
#: anything it could not decide with this token; a blank answer counts too.
NEEDS_CLARIFICATION = "NEEDS CLARIFICATION"

_DEFAULT_SCOPE = "tools:read"

_DRAFT_MUTABLE_FIELDS = frozenset(
    {
        "agent_name",
        "description",
        "clarify_answers",
        "plan_json",
        "analyze_result",
        "constitution_version",
        "phase",
        "tools_spec",
        "origin",
        "revises_agent_id",
        "status",
    }
)


@dataclass(frozen=True)
class DraftCASResult:
    """Result of one owner-scoped draft compare-and-set transition."""

    outcome: str
    current_revision: int
    draft: Dict[str, Any]
    refresh_action: str = "refresh"

    @property
    def applied(self) -> bool:
        """Whether this request or its exact replay already applied."""

        return self.outcome in {"applied", "replayed"}


def _current_execution_fence():
    """Return the admitted connection operation fence when one is active."""

    try:
        from orchestrator.orchestrator import _CONNECTION_OPERATION_CONTEXT

        context = _CONNECTION_OPERATION_CONTEXT.get() or {}
        return context.get("execution_fence")
    except Exception:
        return None


def cas_draft_update(
    db,
    *,
    draft_id: str,
    owner_user_id: str,
    expected_revision: int,
    updates: Dict[str, Any],
    transition_kind: str,
    transition_id: Optional[str] = None,
    operation_fence=None,
) -> DraftCASResult:
    """Apply one owner-scoped draft mutation only at ``expected_revision``.

    When invoked from an admitted UI operation, the transition identity and
    exact operation execution generation are recorded in ``draft_transition``.
    Direct/internal callers still receive strict revision CAS semantics, but do
    not fabricate an operation fence merely to populate the audit table.
    """

    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected_revision must be non-negative")
    if not isinstance(updates, dict) or not updates:
        raise ValueError("updates must be a non-empty mapping")
    unknown = set(updates) - _DRAFT_MUTABLE_FIELDS
    if unknown:
        raise ValueError(f"unsupported draft fields: {sorted(unknown)}")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", transition_kind or ""):
        raise ValueError("transition_kind must be bounded snake_case")
    parsed_transition = None
    if transition_id is not None:
        try:
            parsed_transition = str(uuid.UUID(str(transition_id)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("transition_id must be a UUID") from exc
    operation_fence = operation_fence or _current_execution_fence()
    compare_and_set = getattr(db, "compare_and_set_with_transition", None)
    if not callable(compare_and_set):
        raise TypeError("draft store must expose compare_and_set_with_transition")
    outcome, current_revision, draft = compare_and_set(
        draft_id=draft_id,
        owner_user_id=owner_user_id,
        expected_revision=expected_revision,
        updates=updates,
        transition_kind=transition_kind,
        transition_id=parsed_transition,
        operation_fence=operation_fence,
    )
    return DraftCASResult(outcome, current_revision, draft)


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Session access (owner-scoped)
# ---------------------------------------------------------------------------

def _draft_store(orch):
    """Resolve the one application-scoped Plane draft persistence boundary."""

    lifecycle = getattr(orch, "lifecycle_manager", None)
    store = vars(lifecycle).get("draft_store") if hasattr(lifecycle, "__dict__") else None
    if store is None:
        store = vars(orch).get("draft_store") if hasattr(orch, "__dict__") else None
    if store is None:
        raise RuntimeError("Plane draft persistence is unavailable")
    return store

def get_session(orch, user_id: str, draft_id: str) -> Optional[Dict[str, Any]]:
    """The authoring session ``draft_id`` IF it belongs to ``user_id`` and is a
    BYO session. Cross-user reads return None (FR-016 owner isolation) — a
    non-owner cannot even see that the session exists."""
    from orchestrator.agent_lifecycle import BYO_ORIGIN
    if not user_id or not draft_id:
        return None
    row = _draft_store(orch).get_owned_draft_agent(user_id, str(draft_id))
    if not row or row.get("origin") != BYO_ORIGIN:
        return None
    return dict(row)


def list_sessions(orch, user_id: str) -> List[Dict[str, Any]]:
    """The user's in-progress BYO authoring sessions (most recent first)."""
    from orchestrator.agent_lifecycle import BYO_ORIGIN
    rows = _draft_store(orch).list_byo_sessions(
        user_id,
        origin=BYO_ORIGIN,
        limit=25,
    )
    return [dict(r) for r in rows]


def phase_of(row: Dict[str, Any]) -> str:
    """The session's current phase; anything unrecognized reads as ``specify``
    (fail-closed: an unknown phase is never treated as generate-ready)."""
    phase = (row or {}).get("phase")
    return phase if phase in PHASES else "specify"


# ---------------------------------------------------------------------------
# Artifacts (persisted on the 057 draft_agents columns)
# ---------------------------------------------------------------------------

def _loads(raw, default):
    if not raw:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _declaration_sequence(value: object, field: str) -> list[Any]:
    """Reject string/mapping coercion at the external authoring boundary."""

    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{field} must be a list or tuple")
    return list(value)


def clarify_items(row: Dict[str, Any]) -> List[Dict[str, str]]:
    """The clarification Q&A list: ``[{"question", "answer"}]``."""
    items = _loads(row.get("clarify_answers"), None)
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, dict) and it.get("question"):
            out.append({"question": str(it["question"]),
                        "answer": str(it.get("answer") or "")})
    return out


def plan_artifact(row: Dict[str, Any]) -> Dict[str, Any]:
    """The Plan artifact (also the home of the Tasks artifact — see module doc)."""
    plan = _loads(row.get("plan_json"), None)
    return plan if isinstance(plan, dict) else {}


def analyze_record(row: Dict[str, Any]) -> Dict[str, Any]:
    rec = _loads(row.get("analyze_result"), None)
    return rec if isinstance(rec, dict) else {}


def unresolved_clarifications(row: Dict[str, Any]) -> List[str]:
    """The questions still without a usable answer. The Clarify gate refuses to
    advance while this is non-empty (T019)."""
    out = []
    for it in clarify_items(row):
        answer = (it.get("answer") or "").strip()
        if not answer or NEEDS_CLARIFICATION.lower() in answer.lower():
            out.append(it["question"])
    return out


def parse_tool_lines(text: str) -> List[Dict[str, str]]:
    """Parse the Plan phase's human-editable tool table.

    One tool per line, ``name | scope | what it does``. Pipes (not JSON) because
    the artifact must be hand-editable in a textarea on every client — and the
    tool→scope mapping has to survive that edit, since Analyze's B/C checks are
    decided from it.
    """
    tools: List[Dict[str, str]] = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-*").strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        name = re.sub(r"[^a-zA-Z0-9_]+", "_", parts[0]).strip("_")
        if not name:
            continue
        scope = parts[1] if len(parts) > 1 and parts[1] else _DEFAULT_SCOPE
        desc = parts[2] if len(parts) > 2 else ""
        tools.append({"name": name, "scope": scope, "description": desc})
    return tools


def format_tool_lines(tools: List[Dict[str, Any]]) -> str:
    return "\n".join(
        f"{t.get('name', '')} | {t.get('scope') or _DEFAULT_SCOPE} | {t.get('description', '')}"
        for t in (tools or []) if t.get("name"))


def build_plan(tools_text: str, scopes_text: str, egress_text: str,
               notes: str = "", tasks: Optional[List[str]] = None) -> Dict[str, Any]:
    """Assemble the Plan artifact from the surface's edited fields.

    ``declared_scopes`` is what the USER asked for (not what the tools imply), so
    an over-request is caught by Analyze's least-privilege check rather than
    silently normalized away."""
    tools = parse_tool_lines(tools_text)
    scopes = [s.strip() for s in re.split(r"[,\n]", scopes_text or "") if s.strip()]
    if not scopes:
        scopes = sorted({t["scope"] for t in tools}) or [_DEFAULT_SCOPE]
    egress = [e.strip() for e in re.split(r"[,\n]", egress_text or "") if e.strip()]
    return {
        "tools": tools,
        "tools_used": [t["name"] for t in tools],
        "tool_scopes": {t["name"]: t["scope"] for t in tools},
        "declared_scopes": scopes,
        "declared_egress": egress or None,
        "notes": (notes or "").strip(),
        "tasks": list(tasks or []),
    }


def session_spec(row: Dict[str, Any]) -> Dict[str, Any]:
    """The drafted spec :func:`agent_analyze.check` consumes, assembled from the
    session's artifacts. This is the ONE place the spec is built, so the Analyze
    gate and the generation gate can never disagree about what was checked."""
    plan = plan_artifact(row)
    user_id = row.get("user_id") or ""
    return {
        "display_name": row.get("agent_name") or "",
        "description": row.get("description") or "",
        "agent_id": session_agent_id(row),
        "owner_user_id": user_id,
        "declared_tools": list(plan.get("tools_used") or []),
        "declared_scopes": list(plan.get("declared_scopes") or []),
        "declared_egress": plan.get("declared_egress"),
        "plan": plan,
        "clarify_answers": [i["answer"] for i in clarify_items(row)],
    }


def session_agent_id(row: Dict[str, Any]) -> str:
    """The identity this session will register as. A REVISION keeps the revised
    agent's id (so the host replaces the same agent); a new 060 session uses its
    immutable UUID target, independent of display and storage names."""
    agent_id = row.get("revises_agent_id") or row.get("target_agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise ValueError("draft target_agent_id is required")
    return agent_id


def spec_fingerprint(spec: Dict[str, Any]) -> str:
    """Stable digest of the exact artifacts Analyze evaluated. Stamped into the
    stored analyze result; the generation gate recomputes it, so ANY edit after a
    pass invalidates the pass and forces a re-Analyze (T019)."""
    canonical = json.dumps(spec, sort_keys=True, default=str)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Assistant drafting (the user's own configured LLM — never the system one)
# ---------------------------------------------------------------------------

_DRAFT_SYSTEM = (
    "You help a person design a small personal agent that will run on THEIR OWN "
    "computer. It may only touch its owner's data, must declare every tool it "
    "uses, must ask for the fewest permissions that work, and can never be "
    "shared, published, or transferred to anyone else. Answer with JSON only."
)

_SPECIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "specification": {"type": "string"},
    },
    "required": ["specification"],
    "additionalProperties": False,
}

_CLARIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["questions"],
    "additionalProperties": False,
}

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "tools": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "scope": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name", "scope", "description"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["tools", "notes"],
    "additionalProperties": False,
}

_TASKS_SCHEMA = {
    "type": "object",
    "properties": {"tasks": {"type": "array", "items": {"type": "string"}}},
    "required": ["tasks"],
    "additionalProperties": False,
}

#: Bounds on assistant-drafted artifacts (Constitution G — bounded work).
_MAX_QUESTIONS = 5
_MAX_TOOLS = 6
_MAX_TASKS = 8


async def _draft_json(orch, websocket, messages, schema) -> Optional[Dict[str, Any]]:
    """One structured-output call on the CALLER's LLM configuration.

    ``_call_llm_json`` resolves credentials from the caller's socket (feature
    054), so drafting always runs on the user's own provider. Fail-open: any
    failure returns None and the phase keeps whatever artifact it had — a
    missing/erroring LLM must never block a human from writing the artifact
    themselves."""
    try:
        return await orch._call_llm_json(
            websocket, messages, schema=schema, schema_name="authoring",
            feature="byo_authoring")
    except Exception:
        logger.exception("byo authoring: assistant draft failed (continuing without it)")
        return None


async def draft_phase(orch, websocket, user_id: str, draft_id: str) -> Tuple[bool, str]:
    """Assistant-draft the CURRENT phase's artifact for ``draft_id`` and persist
    it. Returns ``(drafted, message)``; the user edits whatever lands (nothing
    here is binding — every artifact is rewritable before it is submitted)."""
    # Feature 052: the DB accessors here are synchronous, so every one of them
    # rides asyncio.to_thread on this async path (the loop-blocking detector is
    # CI-enforced with an empty allowlist).
    row = await asyncio.to_thread(get_session, orch, user_id, draft_id)
    if row is None:
        return False, "That authoring session is not available."
    phase = phase_of(row)
    expected_revision = int(row.get("state_revision") or 0)

    async def persist(updates: Dict[str, Any]) -> bool:
        transition = await asyncio.to_thread(
            cas_draft_update,
            _draft_store(orch),
            draft_id=draft_id,
            owner_user_id=user_id,
            expected_revision=expected_revision,
            updates=updates,
            transition_kind="save",
        )
        return transition.applied
    name = row.get("agent_name") or ""
    description = row.get("description") or ""
    ctx = f"Agent name: {name}\nWhat the owner asked for:\n{description}"
    answered = clarify_items(row)
    if answered:
        ctx += "\n\nClarifications:\n" + "\n".join(
            f"- {i['question']} → {i['answer'] or '(unanswered)'}" for i in answered)

    if phase == "specify":
        out = await _draft_json(orch, websocket, [
            {"role": "system", "content": _DRAFT_SYSTEM},
            {"role": "user", "content":
                f"{ctx}\n\nWrite a short specification (markdown, <200 words) with: what the "
                f"agent does, what it needs as input, what it produces, and how the owner will "
                f"know it worked. Key: specification."},
        ], _SPECIFY_SCHEMA)
        text = str((out or {}).get("specification") or "").strip()
        if not text:
            return False, "Couldn't draft a specification — write one yourself and continue."
        if not await persist({"description": text[:4000]}):
            return False, "This draft changed elsewhere — refresh before drafting again."
        return True, "Drafted a specification — edit it however you like."

    if phase == "clarify":
        out = await _draft_json(orch, websocket, [
            {"role": "system", "content": _DRAFT_SYSTEM},
            {"role": "user", "content":
                f"{ctx}\n\nList up to {_MAX_QUESTIONS} questions you must have answered before "
                f"this agent can be designed safely (ambiguities, missing inputs, unclear "
                f"limits). If nothing is ambiguous, return an empty list. Key: questions."},
        ], _CLARIFY_SCHEMA)
        if out is None:
            # FAIL CLOSED: writing an empty question list would ASSERT "nothing is
            # ambiguous" on the strength of an LLM that never answered — and that
            # assertion is exactly what the Clarify gate lets a session past. The
            # artifact stays absent, so the gate keeps holding.
            return False, "Couldn't check for open questions right now — try again."
        questions = [str(q).strip() for q in (out.get("questions") or []) if str(q).strip()]
        existing = {i["question"]: i["answer"] for i in clarify_items(row)}
        items = [{"question": q, "answer": existing.get(q, "")}
                 for q in questions[:_MAX_QUESTIONS]]
        if not await persist({"clarify_answers": json.dumps(items)}):
            return False, "This draft changed elsewhere — refresh before drafting again."
        if not items:
            return True, "No open questions — answer nothing and continue."
        return True, f"{len(items)} question(s) to answer before this can move on."

    if phase == "plan":
        plan = plan_artifact(row)
        out = await _draft_json(orch, websocket, [
            {"role": "system", "content": _DRAFT_SYSTEM},
            {"role": "user", "content":
                f"{ctx}\n\nPropose up to {_MAX_TOOLS} tools this agent needs. For each: a "
                f"snake_case name, the single least-privileged scope it needs (one of "
                f"tools:read, tools:write, tools:search, tools:system, tools:files, "
                f"tools:execute), and one line saying what it does. Keys: tools, notes."},
        ], _PLAN_SCHEMA)
        tools = [t for t in ((out or {}).get("tools") or []) if isinstance(t, dict)][:_MAX_TOOLS]
        if not tools:
            return False, "Couldn't draft a plan — list the tools yourself and continue."
        drafted = build_plan(format_tool_lines(tools), "", "",
                             notes=str((out or {}).get("notes") or ""),
                             tasks=list(plan.get("tasks") or []))
        if not await persist({"plan_json": json.dumps(drafted)}):
            return False, "This draft changed elsewhere — refresh before drafting again."
        return True, f"Drafted {len(tools)} tool(s) — adjust the names, scopes, and egress."

    if phase == "tasks":
        plan = plan_artifact(row)
        out = await _draft_json(orch, websocket, [
            {"role": "system", "content": _DRAFT_SYSTEM},
            {"role": "user", "content":
                f"{ctx}\n\nPlan: {json.dumps(plan.get('tools') or [])}\n\nBreak the build into "
                f"up to {_MAX_TASKS} short, concrete tasks. Key: tasks."},
        ], _TASKS_SCHEMA)
        tasks = [str(t).strip() for t in ((out or {}).get("tasks") or []) if str(t).strip()]
        if not tasks:
            return False, "Couldn't draft tasks — write them yourself and continue."
        plan["tasks"] = tasks[:_MAX_TASKS]
        if not await persist({"plan_json": json.dumps(plan)}):
            return False, "This draft changed elsewhere — refresh before drafting again."
        return True, f"Drafted {len(plan['tasks'])} task(s) — edit them however you like."

    return False, "Nothing to draft at this step."


# ---------------------------------------------------------------------------
# The phase machine
# ---------------------------------------------------------------------------

async def start_session(orch, *, user_id: str, agent_name: str, description: str,
                        revises_agent_id: Optional[str] = None) -> Dict[str, Any]:
    """Open a new authoring session at ``specify``.

    ``origin='byo_client'`` is stamped IMMEDIATELY: it is what keeps this draft
    off every server-side execution path (boot relaunch, ``start_draft_agent``,
    ``approve_agent``), so it must be true of the row from the moment the row can
    be picked up (SC-002)."""
    from orchestrator.agent_lifecycle import BYO_ORIGIN
    draft = await orch.lifecycle_manager.create_draft(
        user_id=user_id, agent_name=agent_name, description=description,
        tools_spec=None,
        revises_agent_id=revises_agent_id,
        origin=BYO_ORIGIN,
    )
    draft_id = draft["id"]
    initialized = await asyncio.to_thread(
        cas_draft_update,
        _draft_store(orch),
        draft_id=draft_id,
        owner_user_id=user_id,
        expected_revision=int(draft.get("state_revision") or 0),
        updates={
            "phase": "specify",
        },
        transition_kind="save",
    )
    if not initialized.applied:  # pragma: no cover - newly created row is private
        raise RuntimeError("new authoring session lost initialization CAS")
    logger.info("byo authoring: opened session %s for %s (revises=%s)",
                draft_id, user_id, revises_agent_id)
    return await asyncio.to_thread(get_session, orch, user_id, draft_id) or dict(draft)


def _mutation_identity(
    fields: Dict[str, Any], row: Dict[str, Any]
) -> tuple[int, Optional[str]]:
    """Parse a client CAS identity, falling back only for legacy callers."""

    raw_revision = fields.get("expected_revision")
    if raw_revision in (None, ""):
        expected_revision = int(row.get("state_revision") or 0)
    else:
        if isinstance(raw_revision, bool):
            raise ValueError("expected revision is invalid")
        try:
            expected_revision = int(str(raw_revision))
        except (TypeError, ValueError) as exc:
            raise ValueError("expected revision is invalid") from exc
        if expected_revision < 0 or str(expected_revision) != str(raw_revision).strip():
            raise ValueError("expected revision is invalid")
    transition_id = fields.get("transition_id")
    if transition_id in (None, ""):
        return expected_revision, None
    try:
        parsed = uuid.UUID(str(transition_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("transition identity is invalid") from exc
    if parsed.version != 4:
        raise ValueError("transition identity is invalid")
    return expected_revision, str(parsed)


def _cas_message(result: DraftCASResult, success: str) -> Tuple[bool, str]:
    if result.applied:
        return True, success
    return (
        False,
        f"This draft changed elsewhere (revision {result.current_revision}). "
        "Refresh before saving again.",
    )


def save_artifact(orch, user_id: str, draft_id: str, fields: Dict[str, str]) -> Tuple[bool, str]:
    """Persist the human's edit of the CURRENT phase artifact without advancing.

    Editing is always allowed; advancing never happens implicitly (T017: "the
    user can rewrite the artifact before advancing")."""
    row = get_session(orch, user_id, draft_id)
    if row is None:
        return False, "That authoring session is not available."
    phase = phase_of(row)
    db = _draft_store(orch)
    try:
        expected_revision, transition_id = _mutation_identity(fields, row)
    except ValueError as exc:
        return False, str(exc)

    if phase == "specify":
        name = (fields.get("agent_name") or "").strip() or row.get("agent_name")
        text = (fields.get("specification") or "").strip()
        if len(text) < 10:
            return False, "The specification needs at least 10 characters."
        result = cas_draft_update(
            db,
            draft_id=draft_id,
            owner_user_id=user_id,
            expected_revision=expected_revision,
            updates={"agent_name": name[:100], "description": text[:4000]},
            transition_kind="save",
            transition_id=transition_id,
        )
        return _cas_message(result, "Specification saved.")

    if phase == "clarify":
        # Only ANSWERS are client-supplied; the questions are server-owned. A
        # session that never ran Clarify has no question list, and submitting an
        # empty answer set must not conjure one — that would let a client walk
        # past the mandatory gate by simply not asking for it.
        if row.get("clarify_answers") is None:
            return False, ("Run Clarify first — the assistant has to look for open questions "
                           "before they can be answered.")
        items = _answers_from_fields(row, fields)
        result = cas_draft_update(
            db,
            draft_id=draft_id,
            owner_user_id=user_id,
            expected_revision=expected_revision,
            updates={"clarify_answers": json.dumps(items)},
            transition_kind="save",
            transition_id=transition_id,
        )
        return _cas_message(result, "Answers saved.")

    if phase == "plan":
        plan = build_plan(
            fields.get("tools", ""), fields.get("scopes", ""), fields.get("egress", ""),
            notes=fields.get("notes", ""), tasks=list(plan_artifact(row).get("tasks") or []))
        if not plan["tools"]:
            return False, "List at least one tool (one per line: name | scope | what it does)."
        result = cas_draft_update(
            db,
            draft_id=draft_id,
            owner_user_id=user_id,
            expected_revision=expected_revision,
            updates={"plan_json": json.dumps(plan)},
            transition_kind="save",
            transition_id=transition_id,
        )
        return _cas_message(result, "Plan saved.")

    if phase == "tasks":
        plan = plan_artifact(row)
        plan["tasks"] = [t.strip() for t in (fields.get("tasks") or "").splitlines() if t.strip()]
        result = cas_draft_update(
            db,
            draft_id=draft_id,
            owner_user_id=user_id,
            expected_revision=expected_revision,
            updates={"plan_json": json.dumps(plan)},
            transition_kind="save",
            transition_id=transition_id,
        )
        return _cas_message(result, "Tasks saved.")

    return False, "There is nothing to edit at this step."


def _answers_from_fields(row: Dict[str, Any], fields: Dict[str, str]) -> List[Dict[str, str]]:
    """Merge submitted ``q<i>`` answers back onto the stored question list (the
    questions themselves are server-owned; a client can only answer them)."""
    items = clarify_items(row)
    for idx, item in enumerate(items):
        submitted = fields.get(f"q{idx}")
        if submitted is not None:
            item["answer"] = str(submitted).strip()
    return items


def advance(orch, user_id: str, draft_id: str,
            fields: Optional[Dict[str, str]] = None) -> Tuple[bool, str, str]:
    """Save the current phase artifact and move ONE phase right.

    Returns ``(advanced, phase_now, message)``. Refuses at every gate:

    * ``specify``/``plan``/``tasks`` — the artifact must be present/valid.
    * ``clarify`` — HARD GATE (T019): refuses while any question is unanswered.
    * ``analyze`` — not advanced here; :func:`run_analyze` is the only way past.
    * ``generate`` — terminal; :func:`generate_from_session` re-checks the gate.
    """
    row = get_session(orch, user_id, draft_id)
    if row is None:
        return False, "", "That authoring session is not available."
    phase = phase_of(row)

    if phase == "analyze":
        return False, phase, ("Run Analyze to continue — the agent constitution is "
                              "checked before any code is written.")
    if phase == "generate":
        return False, phase, "This session already passed Analyze — generate the agent."

    transition_id = None
    if fields:
        save_fields = dict(fields)
        transition_id = save_fields.pop("transition_id", None)
        ok, message = save_artifact(orch, user_id, draft_id, save_fields)
        if not ok:
            return False, phase, message
        row = get_session(orch, user_id, draft_id) or row
    else:
        try:
            _expected, transition_id = _mutation_identity({}, row)
        except ValueError:
            transition_id = None

    if phase == "clarify":
        # HARD GATE — an unanswered question is an unresolved ambiguity, and a
        # spec with unresolved ambiguity must never reach Plan (let alone code).
        open_qs = unresolved_clarifications(row)
        if open_qs:
            first = open_qs[0]
            return False, phase, (
                f"{len(open_qs)} question(s) still need an answer before this can move on — "
                f"starting with: “{first}”")
        if row.get("clarify_answers") is None:
            return False, phase, ("Run Clarify first — the assistant has to look for open "
                                  "questions before the design can move on.")

    if phase in _PLAIN_PHASES and phase != "specify":
        plan = plan_artifact(row)
        if phase == "plan" and not plan.get("tools_used"):
            return False, phase, "List at least one tool before moving on."
        if phase == "tasks" and not plan.get("tasks"):
            return False, phase, "Write at least one task before moving on."
    if phase == "specify" and len((row.get("description") or "").strip()) < 10:
        return False, phase, "Write the specification before moving on."

    nxt = PHASES[PHASES.index(phase) + 1]
    result = cas_draft_update(
        _draft_store(orch),
        draft_id=draft_id,
        owner_user_id=user_id,
        expected_revision=int(row.get("state_revision") or 0),
        updates={"phase": nxt},
        transition_kind="advance",
        transition_id=transition_id,
    )
    if not result.applied:
        return (
            False,
            phase,
            f"This draft changed elsewhere (revision {result.current_revision}). "
            "Refresh before continuing.",
        )
    return True, nxt, f"{PHASE_LABELS[phase]} complete — now {PHASE_LABELS[nxt]}."


def run_analyze(
    orch,
    user_id: str,
    draft_id: str,
    *,
    expected_revision: Optional[int] = None,
    transition_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The Analyze HARD GATE (T019). Runs the DETERMINISTIC 057 checker over the
    session's artifacts.

    On fail: the result is persisted, the session STAYS at ``analyze``, and the
    violations (each naming its offending field in plain language) come back —
    no code is generated and nothing advances. On pass: the constitution version
    is stamped and the session advances to ``generate``, which is the ONLY way
    that phase is ever reached."""
    from orchestrator import agent_analyze
    from orchestrator.agent_constitution import AGENT_CONSTITUTION_VERSION

    row = get_session(orch, user_id, draft_id)
    if row is None:
        return {"status": "unavailable"}
    if phase_of(row) not in ("analyze", "generate"):
        return {"status": "too_early", "phase": phase_of(row)}
    current_revision = int(row.get("state_revision") or 0)
    if expected_revision is None:
        expected_revision = current_revision
    if type(expected_revision) is not int or expected_revision < 0:
        return {"status": "invalid_revision", "refresh": "refresh"}
    if expected_revision != current_revision and transition_id is None:
        return {
            "status": "conflict",
            "current_revision": current_revision,
            "refresh": "refresh",
        }

    spec = session_spec(row)
    result = agent_analyze.check(
        spec,
        constitution_version=AGENT_CONSTITUTION_VERSION,
        db=orch.user_agent_registry,
    )
    record = result.as_dict()
    record["at"] = _now_ms()
    record["spec_fingerprint"] = spec_fingerprint(spec)
    transition = cas_draft_update(
        _draft_store(orch),
        draft_id=draft_id,
        owner_user_id=user_id,
        expected_revision=expected_revision,
        updates={
            "analyze_result": json.dumps(record),
            "constitution_version": (
                result.constitution_version if result.passed else None
            ),
            "phase": "generate" if result.passed else "analyze",
        },
        transition_kind="analyze",
        transition_id=transition_id,
    )
    if not transition.applied:
        return {
            "status": "conflict",
            "current_revision": transition.current_revision,
            "refresh": transition.refresh_action,
        }
    if not result.passed:
        logger.info("byo authoring: Analyze blocked session %s (%d violations)",
                    draft_id, len(result.violations))
        return {"status": "analyze_failed", "violations": record["violations"],
                "constitution_version": record["constitution_version"]}
    return {"status": "passed", "constitution_version": record["constitution_version"],
            "phase": "generate"}


def generation_gate(orch, row: Dict[str, Any]) -> Tuple[bool, str]:
    """May this session generate code? The STRUCTURAL half of T019.

    Four independent conditions, all fail-closed. Any one of them missing means a
    ``chrome_author_generate`` — hand-forged or not — is refused on the SERVER:

    1. the session is at ``generate`` (only :func:`run_analyze` puts it there);
    2. a stored Analyze result exists and PASSED;
    3. it ran against the CURRENT agent constitution (a bumped constitution
       forces a re-Analyze — FR-028/Constitution L);
    4. the artifacts have not changed since that pass (fingerprint match) — so an
       edit after a pass cannot ride a stale approval into codegen.
    """
    from orchestrator.agent_constitution import (
        AGENT_CONSTITUTION_VERSION,
        USER_AGENT_POLICY_REVISION,
    )
    if phase_of(row) != "generate":
        return False, ("This agent has not passed Analyze yet — the constitution check runs "
                       "before any code is written.")
    record = analyze_record(row)
    if not record or not record.get("passed"):
        return False, "Analyze has not passed for this agent yet."
    if record.get("constitution_version") != AGENT_CONSTITUTION_VERSION:
        return False, ("The agent rules changed since this was checked — run Analyze again "
                       "before generating.")
    if record.get("policy_revision") != USER_AGENT_POLICY_REVISION:
        return False, ("The Analyze policy changed since this was checked — run Analyze again "
                       "before generating.")
    if record.get("spec_fingerprint") != spec_fingerprint(session_spec(row)):
        return False, ("The design changed after it was checked — run Analyze again before "
                       "generating.")
    return True, ""


def _bundle_files(gen: Dict[str, Any]) -> Dict[str, str]:
    """The generated bundle out of a completed ``generate_code`` result.

    ``generate_code`` returns the draft row (a ``draft_agents`` record) with the
    final file contents attached under ``files`` — the source itself lives only on
    disk otherwise, which the host cannot reach. An absent/empty ``files`` is a
    generator contract breach, not something to paper over with a fallback: the
    caller refuses to deliver rather than shipping an empty bundle."""
    files = gen.get("files")
    if not isinstance(files, dict) or not files:
        return {}
    return {str(k): str(v) for k, v in files.items()}


def _spec_conformance_errors(code: str, tool_names: List[str],
                             tool_scopes: Dict[str, str]) -> List[str]:
    """How the GENERATED tool registry departs from what Analyze APPROVED.

    The gate approved a named tool set with a named scope each; if the generator
    invents a tool (or widens a scope), the agent the owner runs is not the agent
    that was checked. Read by AST — the code is never executed (058 G1).
    """
    from orchestrator.agent_validator import registry_from_source

    declared = {str(t) for t in (tool_names or []) if t}
    if not declared:
        return []          # nothing was approved by name — nothing to enforce
    registry = registry_from_source(code)
    errors: List[str] = []
    extra = sorted(set(registry) - declared)
    if extra:
        errors.append(
            "the generated agent defines tool(s) that were never approved: "
            + ", ".join(extra))
    for name, entry in sorted(registry.items()):
        approved = (tool_scopes or {}).get(name)
        got = entry.get("scope") or _DEFAULT_SCOPE
        if approved and got != approved:
            errors.append(
                f"tool '{name}' asks for scope '{got}' but '{approved}' was approved")
    return errors


async def _generate_and_deliver(orch, *, user_id: str, agent_id: str, draft_id: str,
                                tool_names: List[str], declared_scopes: List[str],
                                declared_egress: Optional[List[str]] = None,
                                tool_scopes: Optional[Dict[str, str]] = None,
                                websocket=None,
                                expected_revision: Optional[int] = None,
                                generation_claim_id: Optional[str] = None) -> Dict[str, Any]:
    """Generate the bundle for an ALREADY-ANALYZED draft and push it to the host.

    Shared spine of the one-shot path and the guided flow. Never Popens the agent
    (SC-002): a validated bundle is DELIVERED, and the owner's desktop host runs
    it and dials back in.

    Fail-CLOSED on every disagreement between what Analyze approved and what the
    generator produced: a failed spec validation, an invented tool, or a widened
    scope all stop the bundle here — ``validated`` must keep meaning "Analyze
    passed AND this is the thing Analyze passed"."""
    from orchestrator import user_agents as ua
    from orchestrator.agent_constitution import (
        AGENT_CONSTITUTION_VERSION,
        USER_AGENT_POLICY_REVISION,
    )
    from orchestrator.agent_lifecycle import (
        BYO_TARGET,
        RevisionActivationError,
        RevisionActivationRecoveryPendingError,
    )

    durable_draft = await asyncio.to_thread(
        _draft_store(orch).get_draft_agent,
        draft_id,
    )
    if not (
        isinstance(durable_draft, dict)
        and durable_draft.get("user_id") == user_id
        and durable_draft.get("target_agent_id") == agent_id
    ):
        raise RuntimeError("owner-scoped draft target is unavailable")
    await asyncio.to_thread(
        ua.admit_authoring_target,
        orch.user_agent_registry,
        agent_id=agent_id,
        owner_user_id=user_id,
        display_name=str(durable_draft.get("agent_name") or ""),
        draft_id=draft_id,
        expected_constitution_version=AGENT_CONSTITUTION_VERSION,
        revises_agent_id=durable_draft.get("revises_agent_id"),
        declared_tools=tool_names,
        declared_scopes=declared_scopes,
        declared_egress=declared_egress,
    )

    # target='byo' + the OWNER-NAMESPACED agent_id: the generated card must
    # present the id the registry knows, or registration is refused fail-closed
    # (user_agents.authorize_registration) and the refusal is silent on the wire.
    gen = await orch.lifecycle_manager.generate_code(
        draft_id,
        websocket=websocket,
        target=BYO_TARGET,
        agent_id=agent_id,
        expected_state_revision=expected_revision,
        generation_claim_id=generation_claim_id,
    )
    if (gen or {}).get("generation_outcome") == "conflict" or (
        gen or {}
    ).get("status") == "conflict":
        return {
            "status": "conflict",
            "agent_id": agent_id,
            "draft_id": draft_id,
            "current_revision": int((gen or {}).get("current_revision") or 0),
            "refresh": "refresh",
        }
    if (gen or {}).get("status") in ("error", "rejected"):
        return {"status": "generation_failed", "agent_id": agent_id,
                "draft_id": draft_id, "error": (gen or {}).get("error_message")}

    files = _bundle_files(gen or {})
    if not files:
        logger.error("byo authoring: generator returned no files for %s — not delivering",
                     agent_id)
        return {"status": "generation_failed", "agent_id": agent_id,
                "draft_id": draft_id, "error": "generated bundle was empty"}

    # A bundle whose spec validation FAILED is not a validated bundle. Delivering
    # it (and marking the row 'validated') would ship code the gates rejected.
    validation = _loads((gen or {}).get("validation_report"), None)
    if isinstance(validation, dict) and not validation.get("passed", False):
        problems = "; ".join(
            f.get("message", "") for f in (validation.get("findings") or [])
            if f.get("severity") == "error")[:400]
        logger.warning("byo authoring: %s failed spec validation — not delivering (%s)",
                       agent_id, problems)
        return {"status": "generation_failed", "agent_id": agent_id, "draft_id": draft_id,
                "error": "the generated agent failed validation: " + (problems or "unknown")}

    mismatches = _spec_conformance_errors(
        files.get("mcp_tools.py", ""), tool_names, tool_scopes or {})
    if mismatches:
        logger.warning("byo authoring: %s does not match the approved plan — not "
                       "delivering (%s)", agent_id, mismatches)
        return {"status": "generation_failed", "agent_id": agent_id, "draft_id": draft_id,
                "error": ("the generated agent does not match the plan that was "
                          "approved: " + "; ".join(mismatches))}

    # ── Validated → deliver to the host (NEVER Popen on the orchestrator). ─────
    # The incumbent user_agent row is deliberately untouched here. Candidate
    # metadata becomes authoritative only in the same transaction that promotes
    # the revision/runtime pointers.
    runtime_manifest = (gen or {}).get("runtime_manifest")
    if isinstance(runtime_manifest, dict):
        from orchestrator.agent_generator import BYO_BUNDLE_FILENAMES

        delivery_files = {
            name: files[name]
            for name in BYO_BUNDLE_FILENAMES
            if name in files
        }
        if set(delivery_files) != set(BYO_BUNDLE_FILENAMES):
            return {
                "status": "generation_failed",
                "agent_id": agent_id,
                "draft_id": draft_id,
                "error": "the finalized runtime bundle is incomplete",
            }
        try:
            delivered = await orch.deliver_agent_bundle(
                user_id,
                agent_id,
                delivery_files,
                AGENT_CONSTITUTION_VERSION,
                runtime_manifest=runtime_manifest,
                bundle_sha256=(gen or {}).get("bundle_sha256"),
                revision_id=(gen or {}).get("revision_id"),
                runtime_contract_version=(gen or {}).get(
                    "runtime_contract_version"
                ),
                required_runtime_lock_sha256=(gen or {}).get(
                    "required_runtime_lock_sha256"
                ),
                artifact_relative_path=(gen or {}).get(
                    "artifact_relative_path"
                ),
                draft_id=draft_id,
                draft_state_revision=int((gen or {}).get("state_revision") or 0),
                display_name=str((gen or {}).get("agent_name") or ""),
                declared_tools=tool_names,
                declared_scopes=declared_scopes,
                declared_egress=declared_egress,
                validated_policy_revision=USER_AGENT_POLICY_REVISION,
            )
        except RevisionActivationRecoveryPendingError as exc:
            return {
                "status": "delivery_pending",
                "agent_id": agent_id,
                "draft_id": draft_id,
                "error": "desktop activation is awaiting durable recovery",
                "failure_code": str(exc),
            }
        except RevisionActivationError as exc:
            return {
                "status": "delivery_failed",
                "agent_id": agent_id,
                "draft_id": draft_id,
                "error": "the desktop host could not activate this revision",
                "failure_code": str(exc),
            }
    else:
        # Compatibility for injected feature-058 test generators. Production
        # v3 generation always returns the finalized metadata above.
        if durable_draft.get("revises_agent_id") is not None:
            return {
                "status": "generation_failed",
                "agent_id": agent_id,
                "draft_id": draft_id,
                "error": "legacy generation cannot safely revise a live agent",
            }
        await asyncio.to_thread(
            ua.mark_validated,
            orch.user_agent_registry,
            agent_id,
            AGENT_CONSTITUTION_VERSION,
            declared_tools=tool_names,
            declared_scopes=declared_scopes,
        )
        delivered = await orch.deliver_agent_bundle(
            user_id, agent_id, files, AGENT_CONSTITUTION_VERSION
        )
    logger.info("byo authoring: delivered %s to %d desktop host socket(s)",
                agent_id, delivered)
    return {"status": "delivered" if delivered else "no_host",
            "agent_id": agent_id, "draft_id": draft_id, "delivered_to": delivered}


async def generate_from_session(
    orch,
    user_id: str,
    draft_id: str,
    websocket=None,
    *,
    expected_revision: Optional[int] = None,
    transition_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate + deliver the agent for a guided session.

    Generation is reachable ONLY post-Analyze-pass: :func:`generation_gate` is
    re-evaluated here on the server, and the deterministic checker is then re-run
    over the artifacts one last time before ``generate_code`` is called. A
    violation at that point pushes the session BACK to ``analyze`` — there is no
    path from a failing spec to generated code (FR-003/SC-004)."""
    from orchestrator import agent_analyze
    from orchestrator.agent_constitution import AGENT_CONSTITUTION_VERSION

    if not byo_enabled():
        return {"status": "disabled"}
    row = await asyncio.to_thread(get_session, orch, user_id, draft_id)
    if row is None:
        return {"status": "unavailable"}
    current_revision = int(row.get("state_revision") or 0)
    if expected_revision is None:
        expected_revision = current_revision
    if type(expected_revision) is not int or expected_revision < 0:
        return {"status": "invalid_revision", "refresh": "refresh"}
    # A request bearing a transition id may be the exact replay of a response
    # that was lost after the transition committed. The exact prior transition
    # read (or the durable publication lookup below) authenticates that replay;
    # an unkeyed stale request still fails before any analysis or mutation.
    if expected_revision != current_revision and transition_id is None:
        return {
            "status": "conflict",
            "current_revision": current_revision,
            "refresh": "refresh",
        }
    if transition_id is not None:
        try:
            parsed_transition = uuid.UUID(str(transition_id))
        except (TypeError, ValueError, AttributeError):
            return {"status": "invalid_transition", "refresh": "refresh"}
        if parsed_transition.version != 4:
            return {"status": "invalid_transition", "refresh": "refresh"}
        transition_id = str(parsed_transition)
    ok, reason = generation_gate(orch, row)
    if not ok:
        logger.info("byo authoring: generate refused for session %s — %s", draft_id, reason)
        return {"status": "gate_blocked", "reason": reason, "phase": phase_of(row)}

    spec = session_spec(row)
    agent_id = spec["agent_id"]
    result = await asyncio.to_thread(
        agent_analyze.check, spec,
        constitution_version=AGENT_CONSTITUTION_VERSION,
        db=orch.user_agent_registry,
    )
    if not result.passed:
        # Belt and braces: the gate said this spec passed, so a violation here
        # means the world moved under us (constitution reload, an id taken by
        # another user). Fail closed and send the session back to Analyze.
        record = result.as_dict()
        record["at"] = _now_ms()
        failed_transition = await asyncio.to_thread(
            cas_draft_update,
            _draft_store(orch),
            draft_id=draft_id,
            owner_user_id=user_id,
            expected_revision=expected_revision,
            updates={
                "analyze_result": json.dumps(record),
                "phase": "analyze",
            },
            transition_kind="analyze",
            transition_id=transition_id,
        )
        if not failed_transition.applied:
            return {
                "status": "conflict",
                "current_revision": failed_transition.current_revision,
                "refresh": failed_transition.refresh_action,
            }
        return {"status": "analyze_failed", "agent_id": agent_id,
                "violations": record["violations"]}

    tool_names = list(spec["declared_tools"])
    declared_scopes = list(spec["declared_scopes"])
    plan = spec.get("plan") or {}
    tool_scopes = dict(plan.get("tool_scopes") or {})

    # A delivered publication is keyed by the claimed draft revision, not by a
    # transient UI request. Re-open that exact journal result before mutating
    # the draft again so a no-host retry and a lost HTTP/WebSocket response do
    # not invoke the model or stage a second bundle. An in-flight exact claim is
    # likewise retried without advancing the revision underneath its worker.
    generation_expected_revision: int | None = None
    generation_claim_id: str | None = None
    generation_updates = {"tools_spec": json.dumps([
        {"name": t.get("name"), "description": t.get("description", ""),
         "scope": t.get("scope") or _DEFAULT_SCOPE}
        for t in (plan.get("tools") or []) if t.get("name")
    ] or [{"name": n, "description": "", "scope": tool_scopes.get(n, _DEFAULT_SCOPE)}
          for n in tool_names])}
    if expected_revision != current_revision:
        # A stale caller may proceed only when Plane recognizes the exact prior
        # claim_generation transition (draft/owner/kind/expected revision).
        # The original operation fence remains immutable evidence on that row,
        # but a later observer operation must not impersonate it merely to read
        # the result of a lost response. A fresh UUID therefore cannot borrow
        # authority to reopen an in-flight or terminal publication.
        read_transition = getattr(
            _draft_store(orch),
            "get_exact_draft_transition",
            None,
        )
        if not callable(read_transition):
            raise RuntimeError("draft store exact-transition lookup is unavailable")
        replay_transition = await asyncio.to_thread(
            read_transition,
            draft_id=draft_id,
            owner_user_id=user_id,
            transition_id=transition_id,
            transition_kind="claim_generation",
            expected_revision=expected_revision,
        )
        if (
            replay_transition is None
            or replay_transition[1] not in {"applied", "replayed"}
        ):
            return {
                "status": "conflict",
                "current_revision": current_revision,
                "refresh": "refresh",
            }
        generation_expected_revision = replay_transition[0]
        generation_claim_id = _generation_claim_id(
            owner_user_id=user_id,
            draft_uuid=str(row.get("draft_uuid") or draft_id),
            source_state_revision=generation_expected_revision + 1,
            target_agent_id=agent_id,
        )
    publication_service = getattr(
        orch.lifecycle_manager,
        "generated_agent_publication_service",
        None,
    )
    if publication_service is not None and current_revision > 0:
        # Acquiring the generation claim advances the draft to the journal's
        # source revision.  The atomic terminal commit advances it once more,
        # so a generated row's durable source is exactly ``current - 1``.
        # A still-generating row remains at the source revision itself.
        publication_source_revision = current_revision
        if (
            row.get("status") == "generated"
            and row.get("published_revision_id")
            and current_revision > 1
        ):
            publication_source_revision -= 1
        published = await publication_service.load_published(
            owner_id=user_id,
            draft_uuid=str(row.get("draft_uuid") or draft_id),
            source_state_revision=publication_source_revision,
        )
        if published is not None:
            if str(published.revision.agent_id) != agent_id:
                return {
                    "status": "conflict",
                    "current_revision": current_revision,
                    "refresh": "refresh",
                }
            published_claim_id = str(
                published.publication.generation_claim_id
            )
            if (
                generation_claim_id is not None
                and generation_claim_id != published_claim_id
            ):
                return {
                    "status": "conflict",
                    "current_revision": current_revision,
                    "refresh": "refresh",
                }
            if published.revision.state == "failed":
                return {
                    "status": "delivery_failed",
                    "agent_id": agent_id,
                    "draft_id": draft_id,
                    "error": "the desktop host could not activate this revision",
                    "failure_code": published.revision.failure_code,
                }
            if published.revision.state == "retired":
                return {
                    "status": "delivery_failed",
                    "agent_id": agent_id,
                    "draft_id": draft_id,
                    "error": "this generated revision has been superseded",
                    "failure_code": "revision_superseded",
                }
            generation_expected_revision = publication_source_revision - 1
            generation_claim_id = published_claim_id
    if (
        generation_claim_id is None
        and row.get("status") == "generating"
        and row.get("generation_claim_id")
        and current_revision > 0
    ):
        generation_expected_revision = current_revision - 1
        generation_claim_id = str(row["generation_claim_id"])

    # PERSIST the approved tool set onto the draft. Codegen reads
    # ``draft_agents.tools_spec``; without this it saw only the free-text
    # description and invented its own tools — so the agent the owner ran was not
    # the agent Analyze approved and the registry recorded.
    if generation_claim_id is None:
        generation_transition = await asyncio.to_thread(
            cas_draft_update,
            _draft_store(orch),
            draft_id=draft_id,
            owner_user_id=user_id,
            expected_revision=expected_revision,
            updates=generation_updates,
            transition_kind="claim_generation",
            transition_id=transition_id,
        )
        if not generation_transition.applied:
            return {
                "status": "conflict",
                "current_revision": generation_transition.current_revision,
                "refresh": generation_transition.refresh_action,
            }
        generation_expected_revision = generation_transition.current_revision
        generation_claim_id = _generation_claim_id(
            owner_user_id=user_id,
            draft_uuid=str(row.get("draft_uuid") or draft_id),
            source_state_revision=generation_transition.current_revision + 1,
            target_agent_id=agent_id,
        )

    if generation_expected_revision is None:  # pragma: no cover - invariant.
        raise RuntimeError("generation retry identity is incomplete")

    return await _generate_and_deliver(
        orch, user_id=user_id, agent_id=agent_id, draft_id=draft_id,
        tool_names=tool_names, declared_scopes=declared_scopes,
        declared_egress=list(spec.get("declared_egress") or []),
        tool_scopes=tool_scopes, websocket=websocket,
        expected_revision=generation_expected_revision,
        generation_claim_id=generation_claim_id)


async def revise(orch, user_id: str, agent_id: str) -> Dict[str, Any]:
    """Re-enter authoring for an existing agent (T027 authoring half).

    Opens a NEW session bound to the same agent id (``revises_agent_id``), seeded
    from the live agent's declared surface. The revision must walk the whole flow
    again — including a fresh Analyze pass — before it can generate: the new
    session starts at ``specify``, so :func:`generation_gate` refuses it until
    Analyze passes anew.

    The live row is deliberately NOT flipped to ``revalidation_required`` here:
    the prior version keeps running until the revision registers (FR-026), and
    flipping it would take a healthy agent offline on its next reconnect for no
    security gain — the revision cannot ship without its own Analyze pass anyway.
    """
    from orchestrator import user_agents as ua
    if not byo_enabled():
        return {"status": "disabled"}
    row = await asyncio.to_thread(
        ua.get_user_agent,
        orch.user_agent_registry,
        agent_id,
    )
    if row is None or row.get("owner_user_id") != user_id or row.get("deleted_at"):
        return {"status": "unavailable"}
    tools = _loads(row.get("declared_tools"), []) or []
    session = await start_session(
        orch, user_id=user_id, agent_name=row.get("display_name") or agent_id,
        description=(row.get("display_name") or agent_id) + " — revision. Describe what should "
                    "change.", revises_agent_id=agent_id)
    plan = build_plan("\n".join(f"{t} | {_DEFAULT_SCOPE} | " for t in tools), "", "")
    await asyncio.to_thread(
        _draft_store(orch).update_draft_agent,
        session["id"],
        plan_json=json.dumps(plan),
    )
    return {"status": "revising", "draft_id": session["id"], "agent_id": agent_id}


# ---------------------------------------------------------------------------
# Derived runtime state (never persisted — data-model: liveness is socket
# presence, and persisting it would drift on crash)
# ---------------------------------------------------------------------------

def agent_status(orch, owner_sub: str, agent_id: str) -> str:
    """``running`` iff the owner has a LIVE tunnel registration for this agent —
    i.e. the desktop host's child process is connected inward AND the
    orchestrator routes to it. Anything else is honestly ``offline``."""
    sock = (getattr(orch, "_tunnel_sockets", None) or {}).get((owner_sub, agent_id))
    if sock is None:
        return "offline"
    if (getattr(orch, "agents", None) or {}).get(agent_id) is not sock:
        return "offline"
    return "running"


def host_presence(orch, owner_sub: str) -> Dict[str, Any]:
    """The owner's desktop-host presence as the surface should state it.

    ``online`` is true when the owner has a signed-in desktop client that
    declared itself an agent host (``owner_host_sockets``) OR any of the
    owner's agents has a live tunnel. Feature 077: the tunnel-only answer told
    every first-time user — whose healthy client had simply not hosted an
    agent yet — that no host was online. ``label`` names the machine when the
    same owner announced a 076 computer host, else the platform + version the
    host registered with, else "your desktop client".
    """
    tunnels = any(k[0] == owner_sub
                  for k in (getattr(orch, "_tunnel_sockets", None) or {}))
    sockets = []
    try:
        sockets = list(orch.owner_host_sockets(owner_sub))
    except Exception:  # noqa: BLE001 — a test double without the registry
        sockets = []
    label = "your desktop client"
    try:
        hosts = getattr(orch, "computer_hosts", None)
        if hosts is not None:
            named = [h for h in hosts.online_for_owner(owner_sub) if getattr(h, "name", "")]
            if named:
                label = named[0].name
    except Exception:  # noqa: BLE001
        pass
    if label == "your desktop client" and sockets:
        sessions = getattr(orch, "_personal_agent_host_sessions", None) or {}
        for sock in sockets:
            record = sessions.get(id(sock))
            if record is not None:
                platform = str(getattr(record, "platform", "") or "").strip()
                version = str(getattr(record, "client_version", "") or "").strip()
                if platform:
                    label = f"{platform} desktop client" + (f" v{version}" if version else "")
                    break
    return {"online": bool(sockets) or tunnels, "label": label,
            "hosts": len(sockets), "tunnels": tunnels}


def host_online(orch, owner_sub: str) -> bool:
    """Whether the owner has a desktop host online (see :func:`host_presence`)."""
    return bool(host_presence(orch, owner_sub)["online"])


# ---------------------------------------------------------------------------
# One-shot path (T009)
# ---------------------------------------------------------------------------

async def author_and_deliver(
    orch, *, user_id: str, agent_name: str, description: str,
    declared_tools: Optional[List[Any]] = None,
    declared_scopes: Optional[List[str]] = None,
    declared_egress: Optional[List[str]] = None,
    plan: Optional[Dict[str, Any]] = None,
    agent_id: Optional[str] = None, chat_id: Optional[str] = None,
    websocket=None,
) -> Dict[str, Any]:
    """Run the BYO create→analyze→generate→deliver flow. Returns a status dict:

    - ``analyze_failed`` (+ ``violations``): the drafted spec violates the agent
      constitution; **no code was generated** (FR-003).
    - ``generation_failed`` (+ ``error``): Analyze passed but code generation or
      the static code gates failed.
    - ``delivered`` / ``no_host``: the validated bundle was pushed to the owner's
      desktop host (or no host was online to receive it).
    - ``delivery_failed``: immutable generation succeeded, but the selected host
      could not activate the now-terminal revision; retry requires a new revision.
    - ``delivery_pending``: promotion authority is temporarily ambiguous and the
      durable recovery path, rather than a new generation, owns convergence.
    """
    from orchestrator import agent_analyze
    from orchestrator.agent_constitution import AGENT_CONSTITUTION_VERSION
    from orchestrator.agent_lifecycle import BYO_ORIGIN

    declared_tools = _declaration_sequence(declared_tools, "declared_tools")
    declared_scopes = _declaration_sequence(declared_scopes, "declared_scopes")
    declared_egress = _declaration_sequence(declared_egress, "declared_egress")
    tool_names = [t.get("name") if isinstance(t, dict) else t
                  for t in declared_tools]
    tool_names = [str(t) for t in tool_names if t]
    lifecycle = orch.lifecycle_manager
    tool_scopes = dict((plan or {}).get("tool_scopes") or {})
    for tool in declared_tools:
        if isinstance(tool, dict) and tool.get("name") and tool.get("scope"):
            tool_scopes.setdefault(str(tool["name"]), str(tool["scope"]))
    requested_agent_id = agent_id
    durable_plan = dict(plan or {})
    durable_tools = [
        {
            "name": name,
            "description": next(
                (
                    str(tool.get("description") or "")
                        for tool in declared_tools
                    if isinstance(tool, dict) and str(tool.get("name") or "") == name
                ),
                "",
            ),
            "scope": tool_scopes.get(name, _DEFAULT_SCOPE),
        }
        for name in tool_names
    ]
    durable_plan.update(
        {
            "tools": durable_tools,
            "tools_used": tool_names,
            "tool_scopes": {
                name: tool_scopes.get(name, _DEFAULT_SCOPE)
                for name in tool_names
            },
            "declared_scopes": list(declared_scopes),
            "declared_egress": list(declared_egress) or None,
        }
    )

    # The server allocates the immutable target identity in the draft creation
    # transaction. A display-name collision or later rename can never recompute
    # it, and a revision keeps the exact existing target.
    draft = await lifecycle.create_draft(
        user_id=user_id,
        agent_name=agent_name,
        description=description,
        tools_spec=durable_tools,
        origin=BYO_ORIGIN,
        revises_agent_id=requested_agent_id,
        plan_json=json.dumps(
            durable_plan,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        constitution_version=AGENT_CONSTITUTION_VERSION,
    )
    draft_id = str(draft["id"])
    agent_id = str(draft.get("target_agent_id") or "")
    if not agent_id:
        raise RuntimeError("draft target_agent_id is unavailable")
    if requested_agent_id is not None and agent_id != requested_agent_id:
        raise RuntimeError("revision draft target_agent_id changed during persistence")

    # ── Analyze gate (T016) — BEFORE any generation. ──────────────────────────
    spec = {
        "display_name": agent_name, "description": description,
        "agent_id": agent_id, "owner_user_id": user_id,
        "declared_tools": tool_names, "declared_scopes": declared_scopes,
        "declared_egress": declared_egress, "plan": durable_plan,
    }
    result = await asyncio.to_thread(
        agent_analyze.check, spec,
        constitution_version=AGENT_CONSTITUTION_VERSION,
        db=orch.user_agent_registry,
    )
    analyze_record = result.as_dict()
    analyze_record["at"] = _now_ms()
    analyze_record["spec_fingerprint"] = spec_fingerprint(spec)
    analyze_transition = await asyncio.to_thread(
        cas_draft_update,
        _draft_store(orch),
        draft_id=draft_id,
        owner_user_id=user_id,
        expected_revision=int(draft.get("state_revision") or 0),
        updates={
            "analyze_result": json.dumps(analyze_record),
            "constitution_version": (
                result.constitution_version if result.passed else None
            ),
        },
        transition_kind="analyze",
    )
    if not analyze_transition.applied:
        return {
            "status": "conflict",
            "agent_id": agent_id,
            "draft_id": draft_id,
            "current_revision": analyze_transition.current_revision,
            "refresh": analyze_transition.refresh_action,
        }
    if not result.passed:
        logger.info("byo authoring: Analyze blocked %s (%d violations) — no code generated",
                    agent_id, len(result.violations))
        return {"status": "analyze_failed", "agent_id": agent_id,
                "draft_id": draft_id,
                "constitution_version": result.constitution_version,
                "violations": [
                    {"principle": v.principle, "title": v.title,
                     "plain_language": v.plain_language, "offending_field": v.offending_field}
                    for v in result.violations]}

    # ── Generate (static code gates run inside the lifecycle). ────────────────
    # ``origin`` is set BEFORE generation: it is what keeps this draft off every
    # server-side execution path (boot relaunch, start_draft_agent), so it must
    # be true of the row from the moment the row can be picked up (SC-002).
    return await _generate_and_deliver(
        orch, user_id=user_id, agent_id=agent_id, draft_id=draft_id,
        tool_names=tool_names, declared_scopes=declared_scopes,
        declared_egress=list(declared_egress),
        tool_scopes=tool_scopes, websocket=websocket,
        expected_revision=analyze_transition.current_revision)
