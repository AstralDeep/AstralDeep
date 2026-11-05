"""Builds the restore_checkpoint approval card's effect preview: it validates the server-side
checkpoint whitelist, asks the owner's Mac host for the live overwrite/delete counts, and
orchestrator.py attaches the folder, turn and counts to the review card that
remote_confirmation.py builds, the way write_review.py attaches the write_file diff.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from astralprims import Text

from orchestrator.computer_hosts import ComputerHostError

logger = logging.getLogger("RestoreReview")

DIFF_TIMEOUT_S = 6.0
UNAVAILABLE_TEXT = "The Mac did not report the affected file counts; it will still verify the checkpoint at restore."


def _checkpoint_id(args: Dict[str, Any]) -> Optional[str]:
    from orchestrator import computer_checkpoints

    value = args.get("checkpoint_id")
    return str(value).strip() if computer_checkpoints.valid_id(value) else None


async def _live_counts(orch, websocket, owner: str, chat: Optional[str], host, checkpoint_id: str) -> Optional[Dict[str, Any]]:
    from orchestrator.remote_confirmation import capture_computer_approval, computer_approval_current

    authority = capture_computer_approval(orch, websocket, owner, chat, {"computer": host.host_id})
    if authority is None:
        return None
    session = authority.session

    async def diff():
        async with session.lock:
            if not computer_approval_current(orch, websocket, authority):
                raise ComputerHostError("no_session", "the reviewed session changed before the diff")
            result = await orch.computer_hosts.request(authority.host, session.session_id, "diff_checkpoint",
                                                       {"checkpoint_id": checkpoint_id}, DIFF_TIMEOUT_S)
            session.verbs_run += 1
            orch.computer_sessions.touch(session)
            return result

    try:
        result = await asyncio.wait_for(diff(), DIFF_TIMEOUT_S + 1.0)
    except asyncio.TimeoutError:
        return None
    except ComputerHostError as exc:
        logger.info("restore review diff unavailable: %s", exc.code)
        return None
    except Exception:  # noqa: BLE001
        logger.warning("restore review diff failed", exc_info=True)
        return None
    if (not isinstance(result, dict) or type(result.get("would_overwrite")) is not int
            or type(result.get("would_delete")) is not int
            or not 0 <= result["would_overwrite"] <= 1_000_000 or not 0 <= result["would_delete"] <= 1_000_000):
        return None
    return {"would_overwrite": result["would_overwrite"],
            "would_delete": result["would_delete"]}


async def prepare_restore_review(orch, websocket, owner: str, chat: Optional[str],
                                 args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from orchestrator import computer_checkpoints
    from orchestrator.remote_confirmation import computer_card_host
    from orchestrator.hitl_confirmation import approved_call

    checkpoint_id = _checkpoint_id(args)
    if checkpoint_id is None or args.get("_remote_op_proposal_id") or approved_call(
            orch, owner, chat, "computer-use-1", "restore_checkpoint"):
        return None
    try:
        host = computer_card_host(orch, owner, chat, args)
        record = computer_checkpoints.restore_check(orch, owner, chat, host, checkpoint_id)
    except (ComputerHostError, computer_checkpoints.CheckpointRefusal):
        return None
    counts = await _live_counts(orch, websocket, owner, chat, host, checkpoint_id)
    return {"record": record, "counts": counts}


def _review_lines(record, counts: Optional[Dict[str, Any]]) -> List[str]:
    lines = [f"Folder: {record.root}", f"Turn: {record.turn_id or 'unknown'}"]
    if counts is None:
        lines.append(UNAVAILABLE_TEXT)
    else:
        lines.append(f"Would overwrite {counts['would_overwrite']} file(s) and delete "
                     f"{counts['would_delete']} file(s) created since the checkpoint.")
    return lines


async def attach_restore_review(orch, websocket, owner: str, chat: Optional[str], args: Dict[str, Any],
                                components: Any, *, snapshot: Optional[Dict[str, Any]] = None) -> Any:
    card = components[0] if isinstance(components, list) and len(components) == 1 else None
    if (not isinstance(card, dict) or card.get("type") != "card" or not isinstance(card.get("content"), list)
            or not any(isinstance(item, dict) and item.get("action") == "remote_op_decision"
                       for item in card["content"])):
        return components
    state = snapshot if snapshot is not None else await prepare_restore_review(orch, websocket, owner, chat, args)
    if state is None:
        return components
    component_id = f"{card['id']}_checkpoint" if isinstance(card.get("id"), str) else None
    parts = [Text(id=component_id, content="\n".join(_review_lines(state["record"], state.get("counts"))),
                  variant="caption").to_dict()]
    updated = {**card, "content": [*card["content"][:1], *parts, *card["content"][1:]]}
    if isinstance(card.get("display_digest"), str):
        from orchestrator import remote_confirmation

        digest = remote_confirmation.bind_display(orch, updated)
        proposal = next((item["payload"].get("proposal_id") for item in updated["content"]
                         if isinstance(item, dict) and isinstance(item.get("payload"), dict)), None)
        if proposal is not None and digest is not None:
            await remote_confirmation._audit_async(owner, "remote_op.review_updated",
                                                   "review card gained its checkpoint effect preview",
                                                   proposal_id=proposal, verb="restore_checkpoint",
                                                   outcome="in_progress", chat_id=chat, display_digest=digest)
    return [updated]
