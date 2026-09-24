"""Exposes cross-session memory recall and writes as LLM tool calls under a pseudo agent
id outside normal per-agent permission gates, mirroring scheduling_chat.py. Memory
writes are PHI-gated but execute immediately, without a consent card.
"""

import logging
from typing import Any, Dict, List, Optional

from astralprims import Alert, Text
from shared.feature_flags import flags

logger = logging.getLogger("Orchestrator.MemoryChat")

META_AGENT_ID = "__memory__"

SYSTEM_PROMPT_ADDENDUM = """
CROSS-SESSION MEMORY (remember / memory_search / memory_get):
- This system DOES support durable, cross-session memory of NON-PHI personalization
  facts. When the user asks you to remember a preference/goal/workflow ("remember I
  prefer concise answers", "note that I work on NSF grants"), call `remember`.
- To answer "what do you know about me / my preferences", call `memory_search` (with a
  query) or `memory_get` (everything). Prefer recalling stored facts over guessing.
- Never store protected health information (PHI) — the system refuses PHI writes
  automatically; do not work around it.
"""

_CATEGORIES = ("profession", "goal", "preference", "workflow_tag", "context")


def meta_tool_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "remember",
                "description": (
                    "Durably remember a NON-PHI personalization fact about the user "
                    "(a preference, goal, profession, or working-context note) so it is "
                    "recalled in future sessions. PHI is refused automatically."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string", "description": "The fact to remember, phrased succinctly"},
                        "category": {"type": "string", "enum": list(_CATEGORIES),
                                     "description": "Kind of fact (defaults to 'context')"},
                    },
                    "required": ["value"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_search",
                "description": "Search the user's durable memory for facts matching a query.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to look for"},
                        "limit": {"type": "integer", "description": "Max results (default 10)"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_get",
                "description": "Return everything currently remembered about the user.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


def should_inject(draft_agent_id: Optional[str]) -> bool:
    return flags.is_enabled("memory_chat") and not draft_agent_id


def _memory_tools(orch):
    cached = getattr(orch, "_memory_tools", None)
    if cached is not None:
        return cached
    from personalization.memory_tools import MemoryTools
    repo = orch.personalization_service.repo
    tools = MemoryTools(repo)
    orch._memory_tools = tools
    return tools


async def _audit(user_id: str, action_type: str, description: str,
                 outcome: str = "success", chat_id: Optional[str] = None,
                 inputs_meta: Optional[Dict] = None) -> None:
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
            event_class="personalization",
            action_type=action_type,
            description=description[:1024],
            conversation_id=chat_id,
            outcome=outcome,
            inputs_meta=inputs_meta or {},
            started_at=datetime.now(timezone.utc),
        ))
    except Exception:
        logger.debug("memory_chat: audit record failed (%s)", action_type, exc_info=True)


async def handle_meta_tool(orch, tool_name: str, args: Dict[str, Any], *,
                           user_id: str, chat_id: Optional[str], websocket):
    from shared.protocol import MCPResponse

    args = args or {}
    tools = _memory_tools(orch)

    if tool_name == "remember":
        value = str(args.get("value") or "").strip()
        category = str(args.get("category") or "context").strip()

        async def _reconcile_llm(messages):
            msg, _ = await orch._call_llm(websocket, messages, feature="memory_reconcile")
            return getattr(msg, "content", None) if msg else None

        res = await tools.remember_reconciled(user_id, category, value, llm_call=_reconcile_llm)
        action = res.get("action", "add")
        if res.get("stored"):
            verb = "Updated" if action == "update" else "Remembered"
            await _audit(user_id, "memory.remember",
                         f"{verb} a {res.get('category')} fact", chat_id=chat_id,
                         inputs_meta={"category": res.get("category"), "memory_id": res.get("id"),
                                      "action": action, "superseded": res.get("superseded")})
            msg_text = ("Got it — I've updated what I remember."
                        if action == "update" else "Got it — I'll remember that.")
            comp = Alert(message=msg_text, variant="success").to_dict()
            return MCPResponse(result={"status": "stored", **res}, ui_components=[comp])
        if action in ("noop", "delete"):
            await _audit(user_id, f"memory.reconcile_{action}",
                         f"Memory {action} via reconciliation", chat_id=chat_id,
                         inputs_meta={"superseded": res.get("superseded")})
            msg_text = ("I already had that noted." if action == "noop"
                        else "Got it — I've removed that from memory.")
            comp = Alert(message=msg_text, variant="info").to_dict()
            return MCPResponse(result={"status": action, **res}, ui_components=[comp])
        await _audit(user_id, "memory.remember_refused", "Memory write refused",
                     outcome="denied", chat_id=chat_id)
        comp = Alert(message=res.get("reason", "I could not save that."),
                     variant="warning").to_dict()
        return MCPResponse(result={"status": "refused", **res}, ui_components=[comp])

    if tool_name == "memory_search":
        query = str(args.get("query") or "").strip()
        try:
            limit = int(args.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        items = tools.memory_search(user_id, query, limit=max(1, min(limit, 50)))
        return MCPResponse(result={"status": "ok", "count": len(items), "items": items},
                           ui_components=[_recall_component(items)])

    if tool_name == "memory_get":
        items = tools.memory_get(user_id)
        return MCPResponse(result={"status": "ok", "count": len(items), "items": items},
                           ui_components=[_recall_component(items)])

    return MCPResponse(error={"message": f"Unknown memory tool '{tool_name}'",
                              "retryable": False})


def _recall_component(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return Text(content="I don't have anything remembered about you yet.",
                    variant="caption").to_dict()
    lines = "\n".join(f"- ({it.get('category', 'context')}) {it.get('value', '')}" for it in items)
    return Text(content=f"Here's what I remember:\n{lines}").to_dict()
