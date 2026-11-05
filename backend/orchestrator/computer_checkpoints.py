"""Server-side identity registry for Mac file checkpoints: mutating host verbs report
checkpoint ids, hashes and counts, and restore_checkpoint may only dispatch an id this
server saw created for the same owner, host and still-granted root. Snapshot bytes never
leave the Mac host store; this registry holds only ids, digests and counts.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

MAX_RECORDS = 512
MAX_TURNS = 256
_CHECKPOINT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REASONS = frozenset({"over_cap", "root_unavailable"})
_REGISTRY_ATTR = "_computer_checkpoints"
_TURN_ATTR = "_chat_turn_message_ids"


class CheckpointRefusal(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CheckpointRecord:
    checkpoint_id: str
    owner: str
    chat: Optional[str]
    host_id: str
    verb: str
    root: str
    root_digest: str
    tree_hash: str
    file_count: int
    turn_id: Optional[str]
    created_at: float


@dataclass
class TurnCheckpoints:
    turn_id: Optional[str]
    checkpoint_ids: list


def root_digest(path: Any) -> Optional[str]:
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and _CHECKPOINT_ID.fullmatch(value) is not None


def valid_digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def parse_report(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict) or not valid_id(value.get("id")):
        return None
    root, digest, tree = value.get("root"), value.get("root_digest"), value.get("tree_hash")
    count = value.get("file_count")
    if (not isinstance(root, str) or not root.strip() or len(root) > 128
            or not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None
            or not isinstance(tree, str) or _DIGEST.fullmatch(tree) is None
            or isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1_000_000):
        return None
    return {"id": value["id"], "root": root, "root_digest": digest,
            "tree_hash": tree, "file_count": count}


def parse_reason(value: Any) -> Optional[str]:
    return value if value in _REASONS else None


class CheckpointRegistry:
    def __init__(self) -> None:
        self._records: Dict[str, CheckpointRecord] = {}
        self._turns: Dict[Tuple[str, Optional[str]], TurnCheckpoints] = {}
        self._lock = threading.Lock()

    def note_created(self, record: CheckpointRecord) -> None:
        with self._lock:
            self._records[record.checkpoint_id] = record
            while len(self._records) > MAX_RECORDS:
                self._records.pop(next(iter(self._records)))
            key = (record.owner, record.chat)
            turn = self._turns.get(key)
            if turn is None or turn.turn_id != record.turn_id:
                turn = TurnCheckpoints(record.turn_id, [])
                self._turns[key] = turn
                while len(self._turns) > MAX_TURNS:
                    self._turns.pop(next(iter(self._turns)))
            turn.checkpoint_ids.append(record.checkpoint_id)

    def lookup(self, checkpoint_id: str) -> Optional[CheckpointRecord]:
        with self._lock:
            return self._records.get(checkpoint_id)

    def turn_checkpoint_ids(self, owner: str, chat: Optional[str]) -> Tuple[str, list]:
        with self._lock:
            turn = self._turns.get((owner, chat))
            if turn is None:
                return None, []
            return turn.turn_id, list(turn.checkpoint_ids)


def registry_for(orch: Any) -> CheckpointRegistry:
    registry = getattr(orch, _REGISTRY_ATTR, None)
    if not isinstance(registry, CheckpointRegistry):
        registry = CheckpointRegistry()
        setattr(orch, _REGISTRY_ATTR, registry)
    return registry


def _review_ids() -> Tuple[Optional[str], Optional[str]]:
    from orchestrator.hitl_confirmation import _APPROVAL

    approval = _APPROVAL.get()
    call = getattr(approval, "call", None)
    if call is None:
        return None, None
    proposal = getattr(call, "remote_proposal", None)
    return (str(proposal) if proposal else None, str(getattr(call, "request_id", "") or "") or None)


def _record_event(orch: Any, owner: str, action_type: str, description: str, chat: Optional[str],
                  correlation: str, inputs_meta: Dict[str, Any], outcome: str) -> None:
    from audit.recorder import get_recorder, now_utc
    from audit.schemas import AuditEventCreate

    recorder = getattr(orch, "audit_recorder", None) or get_recorder()
    if recorder is None:
        return
    try:
        event = AuditEventCreate(
            actor_user_id=owner, auth_principal=owner, agent_id="computer-use-1",
            event_class="agent_tool_call", action_type=action_type[:128], description=description[:1024],
            conversation_id=chat, correlation_id=correlation, outcome=outcome,
            inputs_meta=inputs_meta, started_at=now_utc(), completed_at=now_utc())
        recorder.record_blocking(event)
    except Exception:
        pass


def note_host_checkpoint(orch: Any, owner: str, chat: Optional[str], host: Any, verb: str,
                         result: Any, turn_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(result, dict):
        return None
    proposal_id, request_id = _review_ids()
    report = parse_report(result.get("checkpoint"))
    if report is not None:
        record = CheckpointRecord(
            checkpoint_id=report["id"], owner=owner, chat=chat, host_id=host.host_id, verb=verb,
            root=report["root"], root_digest=report["root_digest"], tree_hash=report["tree_hash"],
            file_count=report["file_count"], turn_id=turn_id, created_at=time.time())
        registry_for(orch).note_created(record)
        _record_event(orch, owner, "checkpoint.created",
                      f"checkpoint {record.checkpoint_id[:8]} noted before {verb} on {host.name}",
                      chat, proposal_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"computer-checkpoint:{record.checkpoint_id}")),
                      {"checkpoint_id": record.checkpoint_id, "verb": verb, "host_id": host.host_id,
                       "root": record.root, "root_digest": record.root_digest, "tree_hash": record.tree_hash,
                       "file_count": record.file_count, "turn_id": turn_id,
                       "remote_op_id": proposal_id, "tool_request_id": request_id},
                      "success")
        return {"id": record.checkpoint_id, "root": record.root,
                "tree_hash": record.tree_hash, "file_count": record.file_count}
    reason = parse_reason(result.get("checkpoint_reason"))
    if reason is None:
        return None
    return {"reason": reason}


def _granted_root_digests(orch: Any, owner: str, chat: Optional[str], host: Any) -> Dict[str, str]:
    from orchestrator.computer_hosts import ComputerHostError
    from orchestrator.project_authority import project_roots

    try:
        roots = project_roots(orch, owner, chat, host, required=False)
    except ComputerHostError as exc:
        raise CheckpointRefusal(exc.code, exc.message) from None
    except Exception:
        raise CheckpointRefusal("checkpoint_root_unknown", "The granted folders are unavailable.") from None
    digests: Dict[str, str] = {}
    for root in roots or ():
        if isinstance(root, dict):
            digest = root_digest(root.get("path"))
            if digest is not None:
                digests[digest] = str(root.get("name") or root.get("path") or "")[:128]
    return digests


def restore_check(orch: Any, owner: str, chat: Optional[str], host: Any,
                  checkpoint_id: Any) -> CheckpointRecord:
    if not valid_id(checkpoint_id):
        raise CheckpointRefusal("out_of_range", "checkpoint_id must be the id a checkpoint tool reported")
    record = registry_for(orch).lookup(str(checkpoint_id))
    if record is None or record.owner != owner:
        raise CheckpointRefusal(
            "unknown_checkpoint",
            "That checkpoint id is not known to this server for your account, so nothing was restored.")
    if record.host_id != host.host_id:
        raise CheckpointRefusal(
            "unknown_checkpoint",
            "That checkpoint belongs to a different computer, so nothing was restored.")
    digests = _granted_root_digests(orch, owner, chat, host)
    if record.root_digest not in digests:
        raise CheckpointRefusal(
            "checkpoint_root_not_granted",
            "The folder of that checkpoint is no longer shared on this computer, so nothing was restored.")
    return record


def note_restored(orch: Any, owner: str, chat: Optional[str], host: Any, record: CheckpointRecord,
                  result: Any) -> None:
    if not isinstance(result, dict):
        return
    proposal_id, request_id = _review_ids()

    def _count(key: str) -> int:
        value = result.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000 else 0

    restored, skipped = _count("restored"), _count("skipped")
    _record_event(orch, owner, "checkpoint.restored",
                  f"restored {restored} file(s) from checkpoint {record.checkpoint_id[:8]} on {host.name}",
                  chat, proposal_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"computer-checkpoint:{record.checkpoint_id}")),
                  {"checkpoint_id": record.checkpoint_id, "host_id": host.host_id, "root": record.root,
                   "root_digest": record.root_digest, "tree_hash": record.tree_hash,
                   "restored": restored, "skipped": skipped,
                   "turn_id": record.turn_id, "remote_op_id": proposal_id, "tool_request_id": request_id},
                  "success" if result.get("complete") is not False else "interrupted")


def turn_checkpoint_ids(orch: Any, owner: str, chat: Optional[str]) -> Tuple[Optional[str], list]:
    return registry_for(orch).turn_checkpoint_ids(owner, chat)


def note_turn(orch: Any, chat: Optional[str], turn_id: Any) -> None:
    turns = getattr(orch, _TURN_ATTR, None)
    if not isinstance(turns, dict):
        turns = {}
        setattr(orch, _TURN_ATTR, turns)
    if chat is None:
        return
    if type(turn_id) is int:
        turns[chat] = turn_id
    else:
        turns.pop(chat, None)
    while len(turns) > MAX_TURNS:
        turns.pop(next(iter(turns)))


def current_turn_id(orch: Any, chat: Optional[str]) -> Optional[str]:
    turns = getattr(orch, _TURN_ATTR, None)
    value = turns.get(chat) if isinstance(turns, dict) else None
    return str(value) if type(value) is int else None
