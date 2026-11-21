"""Reads retained assignments and applies explicit owner controls through AssignmentService."""

from __future__ import annotations

import re
import uuid

from shared.protocol import MCPResponse

from .models import AssignmentError, ControlRequest
from .service import public_record

META_AGENT_ID = "__persistent_assignments__"
_COMMANDS = ("list", "status", "pause", "stop", "revoke")
_RETIRED_COMMANDS = ("create", "revise", "resume", "run-now")
SYSTEM_PROMPT_ADDENDUM = (
    "Use ongoing_agent to list retained assignments, check their status, pause or stop them, "
    "or revoke their authorization. Specify the exact assignment ID or unique name."
)


def meta_tool_definitions():
    return [{"type": "function", "function": {
        "name": "ongoing_agent", "description": SYSTEM_PROMPT_ADDENDUM,
        "parameters": {"type": "object", "additionalProperties": False,
            "properties": {
                "command": {"type": "string", "enum": list(_COMMANDS)},
                "assignment": {"type": "string", "description": "Exact ID or unique name"},
            }, "required": ["command"]},
    }}]


async def handle_meta_tool(orch, tool_name, args, *, user_id, chat_id=None, websocket=None):
    service = getattr(orch, "persistent_assignments", None)
    claims = getattr(orch, "ui_sessions", {}).get(websocket, {})
    try:
        if service is None:
            raise AssignmentError("assignment_runtime_unavailable", 503)
        service._owner(user_id, claims)
        if getattr(websocket, "task", None) is not None:
            raise AssignmentError("assignment_human_required", 403)
        if tool_name != "ongoing_agent" or not isinstance(args, dict):
            raise AssignmentError("assignment_request_invalid", 422)
        command = args.get("command")
        if command in _RETIRED_COMMANDS:
            raise AssignmentError("turn_engine_retired", 410)
        if command not in _COMMANDS or set(args) - {"command", "assignment"}:
            raise AssignmentError("assignment_request_invalid", 422)
        records = await service.list(user_id, claims, limit=100)
        if command == "list":
            return MCPResponse(result={"assignments": [public_record(record) for record in records]})
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
        original = orch._current_request_text(chat_id)
        if not isinstance(original, str):
            raise AssignmentError("assignment_explicit_chat_command_required", 403)
        owner_command = re.fullmatch(
            r"(?:please\s+)?(pause|stop|revoke)\s+(?:ongoing agent\s+)?(.+?)\s*[.!]?",
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
        return MCPResponse(error={"code": exc.code, "message": "The ongoing agent request was refused.",
                                  "retryable": False})
