"""Builds the bounded, redacted unified diff that write_file approval cards carry, reading the
current file through the owner's computer host only while the reviewed session authority is
current; orchestrator.py attaches it to the approval card that remote_confirmation.py builds,
and hitl_confirmation.py places it beside the exact-arguments preview on its review card.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from astralprims import CodeBlock, Text

from orchestrator.computer_hosts import ComputerHostError
from orchestrator.computer_use_policy import MAX_READ_BYTES

logger = logging.getLogger("WriteReview")

DIFF_LIMIT_BYTES = 64 * 1024
READ_TIMEOUT_S = 5.0
TRUNCATED_MARKER = "… diff truncated at 64 KiB"
UNAVAILABLE_TEXT = "diff_unavailable: the current file could not be read."
UNCHANGED_TEXT = "No changes to the current file."
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _masked(text: str) -> str:
    from shared.phi_redactor import PHI_VALUE_PATTERNS, SECRET_VALUE_PATTERNS

    for pattern, replacement in SECRET_VALUE_PATTERNS + PHI_VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _shown(line: str) -> str:
    from orchestrator.hitl_confirmation import _SECRET_MARKERS

    line = _CONTROL.sub("", line)
    if not line.startswith(("--- ", "+++ ", "@@ ")) and _SECRET_MARKERS.search(line):
        return line[:1] + "[REDACTED:secret]"
    return line


def unified_diff(path: str, current: Optional[str], proposed: str) -> Tuple[str, bool]:
    budget = DIFF_LIMIT_BYTES - len(TRUNCATED_MARKER.encode("utf-8"))
    lines: List[str] = []
    used = 0
    for line in difflib.unified_diff(_masked(current or "").splitlines(), _masked(proposed).splitlines(),
                                     fromfile="/dev/null" if current is None else path, tofile=path,
                                     lineterm=""):
        shown = _shown(line)
        cost = len(shown.encode("utf-8")) + 1
        if used + cost > budget:
            return "\n".join([*lines, TRUNCATED_MARKER]), True
        lines.append(shown)
        used += cost
    return "\n".join(lines), False


async def _current_text(orch, websocket, owner: str, chat: Optional[str], args: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    from orchestrator.remote_confirmation import capture_computer_approval, computer_approval_current

    from orchestrator.hitl_confirmation import consent_decision
    authority = capture_computer_approval(orch, websocket, owner, chat, args,
        standing=consent_decision(orch, websocket, owner, chat, "computer-use-1", "write_file", args, ())[0] == "standing")
    if authority is None:
        return "unavailable", None
    session = authority.session

    async def read():
        async with session.lock:
            if not computer_approval_current(orch, websocket, authority):
                raise ComputerHostError("no_session", "the reviewed session changed before the read")
            result = await orch.computer_hosts.request(authority.host, session.session_id, "read_file",
                                                       {"path": args["path"], "max_bytes": MAX_READ_BYTES},
                                                       READ_TIMEOUT_S)
            session.verbs_run += 1
            orch.computer_sessions.touch(session)
            return result

    try:
        result = await asyncio.wait_for(read(), READ_TIMEOUT_S)
    except ComputerHostError as exc:
        if exc.code == "not_found":
            return "missing", None
        logger.info("write review read unavailable: %s", getattr(exc, "code", type(exc).__name__))
        return "unavailable", None
    except asyncio.TimeoutError:
        return "unavailable", None
    except Exception:  # noqa: BLE001
        logger.warning("write review read failed", exc_info=True)
        return "unavailable", None
    if not isinstance(result, dict) or result.get("truncated") or not isinstance(result.get("text"), str):
        return "unavailable", None
    return "present", result["text"]


async def prepare_write_review(orch, websocket, owner: str, chat: Optional[str], args: Dict[str, Any]):
    from orchestrator.computer_use_policy import CwdRefusal, resolve_workspace_path
    from orchestrator.hitl_confirmation import approved_call, consent_decision
    from orchestrator.remote_confirmation import _MARKER, computer_card_host

    if args.get(_MARKER) or approved_call(orch, owner, chat, "computer-use-1", "write_file"):
        return None
    if consent_decision(orch, websocket, owner, chat, "computer-use-1", "write_file", args, ())[0] == "refuse":
        return None
    try:
        host = computer_card_host(orch, owner, chat, args)
        if host.platform != "macos":
            return None
        from orchestrator.project_authority import project_roots
        roots = project_roots(orch, owner, chat, host, required=True)
        primary = roots[0]["path"] if roots and getattr(host, "project_mode", False) else None
        args["path"] = resolve_workspace_path(args.get("path"), roots, host.name, primary=primary)
    except (ComputerHostError, CwdRefusal):
        return None
    snapshot = await _current_text(orch, websocket, owner, chat, args)
    if snapshot[0] == "missing":
        args["if_exists"] = "refuse"
    return snapshot


async def diff_components(orch, websocket, owner: str, chat: Optional[str], args: Dict[str, Any],
                          component_id: Optional[str], *, snapshot=None) -> List[Dict[str, Any]]:
    path, proposed = args.get("path"), args.get("content")
    if not isinstance(path, str) or not isinstance(proposed, str):
        return [Text(id=component_id, content=UNAVAILABLE_TEXT, variant="caption").to_dict()]
    state, current = snapshot if snapshot is not None else await _current_text(orch, websocket, owner, chat, args)
    if state == "unavailable" or state == "missing" and args.get("if_exists") == "overwrite":
        return [Text(id=component_id, content=UNAVAILABLE_TEXT, variant="caption").to_dict()]
    diff, _truncated = await asyncio.to_thread(unified_diff, path, current, proposed)
    if not diff:
        return [Text(id=component_id, content="Create an empty file." if state == "missing" else UNCHANGED_TEXT,
                     variant="caption").to_dict()]
    return [CodeBlock(id=component_id, code=diff, language="diff").to_dict()]


async def review_components(orch, websocket, owner: str, chat: Optional[str], args: Dict[str, Any],
                            component_id: Optional[str], *, snapshot=None) -> List[Dict[str, Any]]:
    try:
        return await diff_components(orch, websocket, owner, chat, args, component_id, snapshot=snapshot)
    except Exception:  # noqa: BLE001
        logger.warning("write review diff could not be built", exc_info=True)
        return [Text(id=component_id, content=UNAVAILABLE_TEXT, variant="caption").to_dict()]


async def attach_write_diff(orch, websocket, owner: str, chat: Optional[str], args: Dict[str, Any],
                            components: Any, *, snapshot=None) -> Any:
    card = components[0] if isinstance(components, list) and len(components) == 1 else None
    if (not isinstance(card, dict) or card.get("type") != "card" or not isinstance(card.get("content"), list)
            or not any(isinstance(item, dict) and item.get("action") == "remote_op_decision"
                       for item in card["content"])):
        return components
    component_id = f"{card['id']}_diff" if isinstance(card.get("id"), str) else None
    parts = await review_components(orch, websocket, owner, chat, args, component_id, snapshot=snapshot)
    summary = card["content"][:1]
    if summary and any(part.get("code", "").startswith("--- /dev/null\n")
                       or part.get("content") == "Create an empty file." for part in parts):
        summary = [{**summary[0], "content": summary[0].get("content", "").replace("(overwrite)", "(create)", 1)}]
    return [{**card, "content": [*summary, *parts, *card["content"][1:]]}]
