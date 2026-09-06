"""Owner chat controls; models may propose authority but cannot consent."""

from __future__ import annotations

import re
import uuid

from astralprims import Button, Card, Text
from shared.protocol import MCPResponse

from .models import AssignmentError, ControlRequest
from .service import public_record

META_AGENT_ID = "__persistent_assignments__"
SYSTEM_PROMPT_ADDENDUM = (
    "ONGOING AGENTS: Use ongoing_agent for work that retains instructions and progress "
    "between events. Creation and revision open an owner consent form. Status, pause, "
    "resume, stop and revoke are available through this tool. Use an exact assignment "
    "ID or unique name; never claim an assignment was created before owner consent."
)


def meta_tool_definitions():
    return [{"type": "function", "function": {
        "name": "ongoing_agent", "description": SYSTEM_PROMPT_ADDENDUM,
        "parameters": {"type": "object", "additionalProperties": False,
            "properties": {
                "command": {"type": "string", "enum": ["list", "status", "create", "revise",
                    "pause", "resume", "stop", "revoke", "run-now"]},
                "assignment": {"type": "string", "description": "Exact ID or unique name"},
                "name": {"type": "string"}, "instructions": {"type": "string"},
                "source_url": {"type": "string"},
            }, "required": ["command"]},
    }}]


async def handle_meta_tool(orch, tool_name, args, *, user_id, chat_id=None, websocket=None):
    service = getattr(orch, "persistent_assignments", None)
    claims = getattr(orch, "ui_sessions", {}).get(websocket, {})
    try:
        if service is None:
            raise AssignmentError("assignment_runtime_unavailable", 503)
        # This is an owner-control surface. External observations, machine turns
        # and delegated agents cannot invoke it even if a model names the tool.
        service._owner(user_id, claims)
        if getattr(websocket, "task", None) is not None:
            raise AssignmentError("assignment_human_required", 403)
        if (tool_name != "ongoing_agent" or not isinstance(args, dict)
                or set(args) - {"command", "assignment", "name", "instructions", "source_url"}):
            raise AssignmentError("assignment_request_invalid", 422)
        command = args.get("command")
        if (not isinstance(command, str)
                or command not in {"list", "status", "create", "revise", "pause", "resume", "stop", "revoke", "run-now"}):
            raise AssignmentError("assignment_request_invalid", 422)
        records = await service.list(user_id, claims, limit=100)
        if command == "list":
            return MCPResponse(result={"assignments": [public_record(record) for record in records]})
        selected = None
        if command != "create":
            needle = args.get("assignment")
            if not isinstance(needle, str) or not 1 <= len(needle) <= 120:
                raise AssignmentError("assignment_request_invalid", 422)
            matches = [record for record in records if needle in {record.assignment_id, record.definition.name}]
            if len(matches) != 1:
                return MCPResponse(result={"selection_required": True, "assignments": [
                    {"assignment_id": record.assignment_id, "name": record.definition.name}
                    for record in records]})
            selected = matches[0]
        if command == "status":
            return MCPResponse(result={"assignment": public_record(selected),
                "activity": [
                    {"title": item.title, "summary": item.summary, "sequence": item.sequence}
                    for item in await service.activity(user_id, claims, selected.assignment_id, limit=20)]})
        if command in {"create", "revise"}:
            draft = {}
            for key, maximum in (("name", 120), ("instructions", 4096), ("source_url", 2048)):
                value = args.get(key)
                if value is not None:
                    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
                        raise AssignmentError("assignment_request_invalid", 422)
                    draft[key] = value
            params = {"tab": "schedule", "assignment_mode": command,
                      "assignment_draft": draft, "conversation_id": chat_id}
            if selected is not None:
                params["assignment_id"] = selected.assignment_id
            card = Card(title="Review ongoing agent", content=[
                Text(content="Review instructions, source, tool access and finite limits on your phone or computer. "
                     "Only your explicit consent activates this assignment."),
                Button(label="Review ongoing agent", action="chrome_open",
                       payload={"surface": "personalization", "params": params}),
            ]).to_dict()
            return MCPResponse(result={"review_required": True}, ui_components=[card])
        # A model-selected tool cannot turn observed page/email text into an
        # owner command. Require the authenticated turn's exact control intent.
        original = orch._current_request_text(chat_id)
        if not isinstance(original, str):
            raise AssignmentError("assignment_explicit_chat_command_required", 403)
        owner_command = re.fullmatch(
            r"(?:please\s+)?(pause|resume|stop|revoke|run-now)\s+(?:ongoing agent\s+)?(.+?)\s*[.!]?",
            original.strip(), re.IGNORECASE,
        )
        if (owner_command is None or owner_command[1].lower() != command
                or owner_command[2] not in {selected.assignment_id, selected.definition.name}):
            raise AssignmentError("assignment_explicit_chat_command_required", 403)
        result = await service.control(user_id, claims, selected.assignment_id, command,
            ControlRequest(submission_id=str(uuid.uuid4()),
                           expected_instruction_revision=selected.instruction_revision,
                           expected_control_epoch=selected.control_epoch))
        return MCPResponse(result={"assignment": public_record(getattr(result, "assignment", result))})
    except AssignmentError as exc:
        return MCPResponse(error={"code": exc.code, "message": "The ongoing agent request needs review.",
                                  "retryable": False})
