import copy
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence
import logging
import sys

# Ensure shared module is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from shared.database import Database
from orchestrator.scheduled_publication import current_scheduled_history_stage

logger = logging.getLogger('HistoryManager')

# Maximum length of a chat-list preview snippet before truncation (030).
PREVIEW_MAX_CHARS = 140


class ConversationCommitConflict(RuntimeError):
    """A staged logical turn no longer owns its declared base revision."""


class ConversationNotFound(LookupError):
    """Non-disclosing owner-scoped chat lookup failure."""


class ConversationSnapshotInvalid(RuntimeError):
    """A complete canonical snapshot could not be constructed safely."""


def _uuid4_text(value: Any, field_name: str) -> str:
    try:
        parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a UUID4") from exc
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise ValueError(f"{field_name} must be a UUID4")
    return str(parsed)


def _required_text(value: Any, field_name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{field_name} must be non-empty bounded text")
    return value


def _rfc3339(value: Any) -> str:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
    else:
        raise ConversationSnapshotInvalid("snapshot timestamp is unavailable")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    # Second precision: the native continuity validators parse RFC 3339 with
    # a plain ISO8601DateFormatter, which rejects fractional seconds — a
    # microsecond-bearing timestamp makes every Apple client silently drop
    # the committed conversation snapshot.
    return (
        moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _contains_reserved_presentation(value: Any) -> bool:
    if isinstance(value, Mapping):
        return "_presentation" in value or any(
            _contains_reserved_presentation(item) for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_reserved_presentation(item) for item in value)
    return False


def _strip_reserved_presentation(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_reserved_presentation(item)
            for key, item in value.items()
            if key != "_presentation"
        }
    if isinstance(value, list):
        return [_strip_reserved_presentation(item) for item in value]
    return copy.deepcopy(value)


def _plain_text(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return json.dumps(value, allow_nan=False)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if not value:
            return "[]"
        return ", ".join(_plain_text(item) for item in value)
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        return "; ".join(
            f"{key}: {_plain_text(value[key])}" for key in sorted(value)
        )
    return "Saved value"


def _recovery_part() -> dict[str, str]:
    return {
        "type": "recovery",
        "code": "saved_content_unrenderable",
        "message": "A saved response could not be displayed.",
    }


def _recovery_parts() -> list[dict[str, Any]]:
    """Return a visible recovery value plus a deterministic diagnostic."""

    diagnostic = {"code": "saved_content_unrenderable"}
    return [
        _recovery_part(),
        {
            "type": "structured",
            "value": diagnostic,
            "plain_text": _plain_text(diagnostic),
        },
    ]


def _allowed_component_types() -> set[str]:
    from webrender.renderer import allowed_primitive_types

    return set(allowed_primitive_types())


def _component_identity(component: Mapping[str, Any], position: int) -> str:
    existing = component.get("component_id")
    if isinstance(existing, str) and existing and len(existing) <= 512:
        return existing
    semantic = json.dumps(
        _strip_reserved_presentation(component),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return "cc_" + hashlib.sha256(f"{position}:{semantic}".encode()).hexdigest()[:24]


def _canonical_component(component: Any, position: int) -> dict[str, Any]:
    if not isinstance(component, Mapping):
        raise ConversationSnapshotInvalid("component is not an object")
    clean = _strip_reserved_presentation(component)
    component_type = clean.get("type")
    if component_type not in _allowed_component_types():
        raise ConversationSnapshotInvalid("component type is not renderable")
    if component_type == "text":
        content = clean.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ConversationSnapshotInvalid("text component content is empty")
    clean["component_id"] = _component_identity(clean, position)
    try:
        json.dumps(clean, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ConversationSnapshotInvalid("component is not canonical JSON") from exc
    return clean


# Feature 045's chat-rail rule, applied to the 060 snapshot transcript: the
# rail is WORDS ONLY. Rich components (tables/charts/metrics/heroes/…) live on
# the canvas and re-hydrate from the workspace — a transcript message surfaces
# only its text-like primitives. Mirrors Orchestrator._TEXT_ONLY_TYPES /
# _is_text_only_components (web load_chat's `_transcript_html` filter), which
# cannot be imported here without a cycle.
_RAIL_TEXT_ONLY_TYPES = {
    "text", "card", "container", "collapsible", "divider", "list", "alert"
}


def _is_rail_text_only(components: list[Any]) -> bool:
    for comp in components:
        if not isinstance(comp, Mapping):
            continue
        if str(comp.get("type", "")).strip().lower() not in _RAIL_TEXT_ONLY_TYPES:
            return False
        for key in ("children", "content"):
            children = comp.get(key, [])
            if isinstance(children, list):
                nested = [c for c in children if isinstance(c, Mapping) and "type" in c]
                if nested and not _is_rail_text_only(nested):
                    return False
    return True


def _rail_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce a transcript message's parts to TEXT ONLY (feature 063).

    The chat rail is the conversation; the canvas is where UI lives. So a
    ``components`` part is reduced to the plain text of any top-level ``text``
    primitives it carries (lifted to ``text`` parts, so no assistant words are
    lost) and every other component — cards, tables, lists, alerts, metrics — is
    dropped from the transcript: it is canvas state, not conversation. A message
    left with no parts is omitted (it was purely a rendered component).

    This supersedes the feature-062 rule that kept text-like components in the
    rail (they still appeared as duplicate cards beside the canvas)."""
    kept: list[dict[str, Any]] = []
    for part in parts:
        if part.get("type") != "components":
            kept.append(part)
            continue
        for comp in part.get("components", []):
            if not isinstance(comp, Mapping):
                continue
            if str(comp.get("type", "")).strip().lower() != "text":
                continue  # a UI component — canvas only
            text = comp.get("content")
            if not isinstance(text, str):
                text = comp.get("text")
            if isinstance(text, str) and text.strip():
                kept.append({"type": "text", "text": text})
    return kept


def _content_parts(stored: Any) -> list[dict[str, Any]]:
    if not isinstance(stored, str):
        value = stored
    else:
        stripped = stored.strip()
        if not stripped:
            return _recovery_parts()
        try:
            value = json.loads(
                stored,
                parse_constant=lambda constant: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON constant: {constant}")
                ),
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            if stripped.startswith(("{", "[")):
                return _recovery_parts()
            return [{"type": "text", "text": stored}]

    if value is None:
        return _recovery_parts()
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value else _recovery_parts()
    allowed_types = _allowed_component_types()
    if isinstance(value, Mapping) and value.get("type") in allowed_types:
        try:
            component = _canonical_component(value, 0)
        except ConversationSnapshotInvalid:
            return _recovery_parts()
        return [{"type": "components", "components": [component]}]
    if isinstance(value, list) and value and all(
        isinstance(item, Mapping) and item.get("type") in allowed_types
        for item in value
    ):
        try:
            components = [
                _canonical_component(item, position)
                for position, item in enumerate(value)
            ]
        except ConversationSnapshotInvalid:
            return _recovery_parts()
        return [{"type": "components", "components": components}]
    if isinstance(value, list) and any(
        isinstance(item, Mapping) and item.get("type") in allowed_types
        for item in value
    ):
        # A partially valid primitive group is not safe to reinterpret as
        # ordinary structured data: doing so would silently change its UI
        # semantics. Preserve a visible recovery value instead.
        return _recovery_parts()
    if isinstance(value, list):
        if not value:
            return [{"type": "structured", "value": [], "plain_text": "[]"}]
        parts: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, str) and item:
                parts.append({"type": "text", "text": item})
            elif item is None:
                parts.extend(_recovery_parts())
            else:
                parts.append(
                    {
                        "type": "structured",
                        "value": _strip_reserved_presentation(item),
                        "plain_text": _plain_text(item),
                    }
                )
        return parts or _recovery_parts()
    return [
        {
            "type": "structured",
            "value": _strip_reserved_presentation(value),
            "plain_text": _plain_text(value),
        }
    ]


def augment_conversation_snapshot_for_target(
    snapshot: Mapping[str, Any], profile: Any, *, target: str
) -> dict[str, Any]:
    """Return a transport copy with presentation added only for web sockets.

    Reserved presentation is removed first even when handed an already
    augmented value. It therefore cannot become semantic/durable authority.
    """

    candidate = _strip_reserved_presentation(snapshot)
    if target != "web":
        return candidate
    from webrender import render_component_fragment, render_workspace

    workspace_html = render_workspace([], profile)
    workspace = {
        "export": 'data-astral-export="1"' in workspace_html,
        "share": 'data-astral-share="1"' in workspace_html,
    }

    def augment(components: Sequence[Any]) -> list[dict[str, Any]]:
        output = []
        for position, raw in enumerate(components):
            component = _canonical_component(raw, position)
            component["_presentation"] = {
                "target": "web",
                "html": render_component_fragment(component, profile),
                "workspace": dict(workspace),
            }
            output.append(component)
        return output

    transcript = candidate.get("transcript")
    if isinstance(transcript, list):
        for message in transcript:
            if not isinstance(message, dict):
                continue
            for part in message.get("parts") or []:
                if isinstance(part, dict) and part.get("type") == "components":
                    part["components"] = augment(part.get("components") or [])
    canvas = candidate.get("canvas")
    if isinstance(canvas, dict):
        canvas["components"] = augment(canvas.get("components") or [])
    return candidate


class ConversationCommitRepository:
    """PostgreSQL authority for one complete logical conversation revision."""

    def __init__(
        self,
        database: Any,
        *,
        operation_coordinator: Any = None,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        if database is None or not callable(getattr(database, "_get_connection", None)):
            raise TypeError("database must provide _get_connection()")
        if not callable(uuid_factory):
            raise TypeError("uuid_factory must be callable")
        self.database = database
        self.operation_coordinator = operation_coordinator
        self.uuid_factory = uuid_factory

    @contextmanager
    def _transaction(self, operation_fence: Any = None) -> Iterator[Any]:
        if operation_fence is not None:
            if self.operation_coordinator is None:
                raise ValueError("operation coordinator is required for a fenced commit")
            with self.operation_coordinator.fenced_transaction(operation_fence) as cursor:
                if not callable(getattr(cursor, "execute", None)):
                    raise TypeError("conversation commits require a PostgreSQL transaction")
                yield cursor
            return
        connection = self.database._get_connection()
        cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            try:
                cursor.close()
            finally:
                connection.close()

    @staticmethod
    def _chat_for_update(cursor: Any, chat_id: str, owner_user_id: str) -> Mapping[str, Any]:
        cursor.execute(
            "SELECT * FROM chats WHERE id = %s AND user_id = %s FOR UPDATE",
            (chat_id, owner_user_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise ConversationNotFound("conversation not found")
        return row

    @staticmethod
    def _commit_record(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "commit_id": str(row["commit_id"]),
            "chat_id": str(row["chat_id"]),
            "owner_user_id": str(row["owner_user_id"]),
            "request_generation": str(row["request_generation"]),
            "base_render_revision": int(row["base_render_revision"]),
            "committed_render_revision": (
                None
                if row["committed_render_revision"] is None
                else int(row["committed_render_revision"])
            ),
            "state": str(row["state"]),
            "committed_at": (
                None if row["committed_at"] is None else _rfc3339(row["committed_at"])
            ),
        }

    def stage_commit(
        self,
        *,
        chat_id: str,
        owner_user_id: str,
        request_generation: Any,
        operation_fence: Any = None,
        operation_owner: Any = None,
        connection_generation: Any = None,
    ) -> dict[str, Any]:
        chat_id = _uuid4_text(chat_id, "chat_id")
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        request_generation = _uuid4_text(request_generation, "request_generation")
        connection_generation = (
            None
            if connection_generation is None
            else _uuid4_text(connection_generation, "connection_generation")
        )
        commit_id = _uuid4_text(self.uuid_factory(), "commit_id")
        operation_id = None
        operation_generation = None
        if operation_fence is not None:
            if operation_owner is None:
                raise ValueError("operation_owner is required for a fenced commit")
            operation_id = str(operation_fence.operation_id)
            operation_generation = int(operation_fence.execution_generation)
        elif operation_owner is not None or connection_generation is not None:
            raise ValueError(
                "operation owner/generation require an execution fence"
            )
        with self._transaction(operation_fence) as cursor:
            chat = self._chat_for_update(cursor, chat_id, owner_user_id)
            if operation_fence is not None:
                operation = self.operation_coordinator.assert_current_execution(
                    operation_fence,
                    transaction=cursor,
                )
                expected_scope = getattr(operation_owner, "owner_scope", None)
                if operation.owner_scope != expected_scope:
                    raise ConversationCommitConflict(
                        "conversation operation owner scope changed"
                    )
                if str(getattr(operation.owner_scope, "value", "")) in {
                    "user",
                    "schedule",
                }:
                    if (
                        operation.owner_user_id != owner_user_id
                        or getattr(operation_owner, "owner_user_id", None)
                        != owner_user_id
                    ):
                        raise ConversationCommitConflict(
                            "conversation operation owner changed"
                        )
                elif (
                    operation.connection_scope_id
                    != getattr(operation_owner, "connection_scope_id", None)
                ):
                    raise ConversationCommitConflict(
                        "conversation operation connection owner changed"
                    )
                if operation.chat_id != chat_id:
                    raise ConversationCommitConflict(
                        "conversation operation chat changed"
                    )
                if str(operation.request_generation or "") != request_generation:
                    raise ConversationCommitConflict(
                        "conversation operation request generation changed"
                    )
                if (
                    None
                    if operation.connection_generation is None
                    else str(operation.connection_generation)
                ) != connection_generation:
                    raise ConversationCommitConflict(
                        "conversation operation connection generation changed"
                    )
            cursor.execute(
                "SELECT * FROM conversation_commit "
                "WHERE chat_id = %s AND request_generation = %s",
                (chat_id, request_generation),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if str(existing["owner_user_id"]) != owner_user_id:
                    raise ConversationNotFound("conversation not found")
                existing_operation = existing["operation_id"]
                existing_generation = existing["operation_execution_generation"]
                supplied_operation = (
                    None
                    if operation_fence is None
                    else str(operation_fence.operation_id)
                )
                supplied_generation = (
                    None
                    if operation_fence is None
                    else int(operation_fence.execution_generation)
                )
                if (
                    (None if existing_operation is None else str(existing_operation))
                    != supplied_operation
                    or (
                        None
                        if existing_generation is None
                        else int(existing_generation)
                    )
                    != supplied_generation
                ):
                    raise ConversationCommitConflict(
                        "conversation request generation changed operation fence"
                    )
                return self._commit_record(existing)
            cursor.execute(
                """
                INSERT INTO conversation_commit (
                    commit_id, chat_id, owner_user_id, request_generation,
                    operation_id, operation_execution_generation,
                    base_render_revision, state
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'staged')
                RETURNING *
                """,
                (
                    commit_id,
                    chat_id,
                    owner_user_id,
                    request_generation,
                    operation_id,
                    operation_generation,
                    int(chat.get("render_revision") or 0),
                ),
            )
            staged = cursor.fetchone()
        return self._commit_record(staged)

    @staticmethod
    def _assert_matching_operation_fence(
        staged: Mapping[str, Any], operation_fence: Any
    ) -> None:
        staged_operation = staged["operation_id"]
        staged_generation = staged["operation_execution_generation"]
        supplied_operation = (
            None if operation_fence is None else str(operation_fence.operation_id)
        )
        supplied_generation = (
            None
            if operation_fence is None
            else int(operation_fence.execution_generation)
        )
        if (
            (None if staged_operation is None else str(staged_operation))
            != supplied_operation
            or (None if staged_generation is None else int(staged_generation))
            != supplied_generation
        ):
            raise ConversationCommitConflict("conversation operation fence changed")

    @staticmethod
    def _staged_for_update(
        cursor: Any, commit_id: str, owner_user_id: str
    ) -> Mapping[str, Any]:
        cursor.execute(
            "SELECT * FROM conversation_commit WHERE commit_id = %s FOR UPDATE",
            (commit_id,),
        )
        staged = cursor.fetchone()
        if staged is None or str(staged["owner_user_id"]) != owner_user_id:
            raise ConversationNotFound("conversation not found")
        return staged

    def prepare_canvas_stage(
        self,
        *,
        commit_id: Any,
        owner_user_id: str,
        operation_fence: Any = None,
    ) -> int:
        """Copy the complete authoritative canvas into an invisible stage."""

        commit_id = _uuid4_text(commit_id, "commit_id")
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        with self._transaction(operation_fence) as cursor:
            staged = self._staged_for_update(cursor, commit_id, owner_user_id)
            if staged["state"] != "staged":
                raise ConversationCommitConflict("conversation commit is terminal")
            self._assert_matching_operation_fence(staged, operation_fence)
            chat = self._chat_for_update(
                cursor, str(staged["chat_id"]), owner_user_id
            )
            base_revision = int(staged["base_render_revision"])
            if int(chat.get("render_revision") or 0) != base_revision:
                raise ConversationCommitConflict("conversation base revision changed")
            cursor.execute(
                "SELECT COUNT(*) AS count FROM saved_components "
                "WHERE conversation_commit_id = %s",
                (commit_id,),
            )
            if int(cursor.fetchone()["count"]):
                raise ConversationCommitConflict(
                    "conversation canvas stage was already prepared"
                )
            cursor.execute(
                """
                SELECT component.*
                FROM saved_components AS component
                LEFT JOIN conversation_commit AS source_commit
                  ON source_commit.commit_id = component.conversation_commit_id
                 AND source_commit.chat_id = component.chat_id
                 AND source_commit.owner_user_id = component.user_id
                WHERE component.chat_id = %s AND component.user_id = %s
                  AND (
                    (component.conversation_commit_id IS NULL AND %s = 0)
                    OR (source_commit.state = 'committed'
                        AND source_commit.committed_render_revision = %s
                        AND component.committed_render_revision = %s)
                  )
                ORDER BY COALESCE(component.position, 2147483647),
                         component.created_at, component.id
                """,
                (
                    staged["chat_id"],
                    owner_user_id,
                    base_revision,
                    base_revision,
                    base_revision,
                ),
            )
            rows = list(cursor.fetchall())
            next_revision = base_revision + 1
            for position, row in enumerate(rows):
                try:
                    raw = json.loads(row["component_data"])
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ConversationSnapshotInvalid(
                        "saved canvas component is malformed"
                    ) from exc
                if isinstance(raw, dict) and row.get("component_id"):
                    raw["component_id"] = str(row["component_id"])
                component = _canonical_component(raw, position)
                cursor.execute(
                    """
                    INSERT INTO saved_components (
                        id, chat_id, user_id, component_data, component_type,
                        title, created_at, component_id, position, updated_at,
                        conversation_commit_id, committed_render_revision
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(self.uuid_factory()),
                        staged["chat_id"],
                        owner_user_id,
                        json.dumps(
                            component,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ),
                        component["type"],
                        str(row.get("title") or component["type"])[:255],
                        row["created_at"],
                        component["component_id"],
                        position,
                        row.get("updated_at") or row["created_at"],
                        commit_id,
                        next_revision,
                    ),
                )
        return len(rows)

    def append_staged_message(
        self,
        *,
        commit_id: Any,
        owner_user_id: str,
        role: str,
        content: Any,
        attachments: Optional[Sequence[Any]] = None,
        timestamp: Optional[int] = None,
        operation_fence: Any = None,
    ) -> str:
        """Append one invisible ordered message under the staged commit."""

        commit_id = _uuid4_text(commit_id, "commit_id")
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        message = self._validate_messages(
            [
                {
                    "role": role,
                    "content": content,
                    "attachments": list(attachments or ()),
                    "timestamp": timestamp,
                }
            ]
        )[0]
        with self._transaction(operation_fence) as cursor:
            staged = self._staged_for_update(cursor, commit_id, owner_user_id)
            if staged["state"] != "staged":
                raise ConversationCommitConflict("conversation commit is terminal")
            self._assert_matching_operation_fence(staged, operation_fence)
            chat = self._chat_for_update(
                cursor, str(staged["chat_id"]), owner_user_id
            )
            base_revision = int(staged["base_render_revision"])
            if int(chat.get("render_revision") or 0) != base_revision:
                raise ConversationCommitConflict("conversation base revision changed")
            cursor.execute(
                "SELECT COALESCE(MAX(commit_position), -1) + 1 AS position "
                "FROM messages WHERE conversation_commit_id = %s",
                (commit_id,),
            )
            position = int(cursor.fetchone()["position"])
            cursor.execute("SELECT clock_timestamp() AS current_time")
            current_time = cursor.fetchone()["current_time"]
            current_ms = int(current_time.timestamp() * 1000)
            message_timestamp = message["timestamp"]
            if not isinstance(message_timestamp, int) or message_timestamp < 0:
                message_timestamp = current_ms + position
            cursor.execute(
                """
                INSERT INTO messages (
                    chat_id, user_id, role, content, timestamp,
                    conversation_commit_id, commit_position,
                    committed_render_revision
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    staged["chat_id"],
                    owner_user_id,
                    message["role"],
                    json.dumps(
                        message["content"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    message_timestamp,
                    commit_id,
                    position,
                    base_revision + 1,
                ),
            )
            message_id = str(cursor.fetchone()["id"])
            self._link_attachments(
                cursor,
                chat_id=str(staged["chat_id"]),
                message_id=message_id,
                owner_user_id=owner_user_id,
                attachment_ids=message["attachments"],
                created_at_ms=current_ms,
            )
        return message_id

    def abort_commit(
        self,
        *,
        commit_id: Any,
        owner_user_id: str,
    ) -> dict[str, Any]:
        """Discard only invisible staged rows; a committed winner is immutable."""

        commit_id = _uuid4_text(commit_id, "commit_id")
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        with self._transaction() as cursor:
            staged = self._staged_for_update(cursor, commit_id, owner_user_id)
            if staged["state"] == "committed":
                return self._commit_record(staged)
            if staged["state"] == "aborted":
                return self._commit_record(staged)
            cursor.execute(
                "DELETE FROM saved_components WHERE conversation_commit_id = %s",
                (commit_id,),
            )
            cursor.execute(
                "DELETE FROM messages WHERE conversation_commit_id = %s",
                (commit_id,),
            )
            cursor.execute(
                """
                UPDATE conversation_commit
                SET state = 'aborted', aborted_at = clock_timestamp()
                WHERE commit_id = %s AND state = 'staged'
                RETURNING *
                """,
                (commit_id,),
            )
            aborted = cursor.fetchone()
            if aborted is None:
                raise ConversationCommitConflict(
                    "conversation abort lost its terminal CAS"
                )
        return self._commit_record(aborted)

    @staticmethod
    def _validate_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            raise ValueError("messages must be an ordered array")
        validated = []
        for message in messages:
            if not isinstance(message, Mapping):
                raise ValueError("message must be an object")
            role = message.get("role")
            if role not in {"user", "assistant", "system", "tool"}:
                raise ValueError("message role is invalid")
            if message.get("content") is None:
                raise ValueError("message content is required")
            if _contains_reserved_presentation(message["content"]):
                raise ValueError("_presentation is server-owned transport metadata")
            try:
                json.dumps(message["content"], allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("message content must be canonical JSON") from exc
            attachments = message.get("attachments") or []
            if not isinstance(attachments, list):
                raise ValueError("message attachments must be an array")
            validated.append(
                {
                    "role": role,
                    "content": copy.deepcopy(message["content"]),
                    "timestamp": message.get("timestamp"),
                    "attachments": list(attachments),
                }
            )
        return validated

    @staticmethod
    def _validate_canvas(components: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(components, Sequence) or isinstance(components, (str, bytes)):
            raise ValueError("canvas components must be an ordered array")
        if _contains_reserved_presentation(components):
            raise ValueError("_presentation is server-owned transport metadata")
        output = [
            _canonical_component(component, position)
            for position, component in enumerate(components)
        ]
        identities = [component["component_id"] for component in output]
        if len(identities) != len(set(identities)):
            raise ValueError("canvas component identities must be unique")
        return output

    @staticmethod
    def _validate_layouts(
        layouts: Optional[Sequence[Mapping[str, Any]]],
    ) -> list[dict[str, Any]]:
        if layouts is None:
            return []
        if not isinstance(layouts, Sequence) or isinstance(layouts, (str, bytes)):
            raise ValueError("canvas layouts must be an ordered array")
        output = []
        seen_keys = set()
        for raw in layouts:
            if not isinstance(raw, Mapping):
                raise ValueError("canvas layout must be an object")
            layout_key = raw.get("layout_key")
            position = raw.get("position")
            layout = raw.get("layout")
            if (
                not isinstance(layout_key, str)
                or not layout_key
                or len(layout_key) > 512
                or layout_key in seen_keys
            ):
                raise ValueError("canvas layout identity is invalid")
            if (
                isinstance(position, bool)
                or not isinstance(position, int)
                or position < 0
            ):
                raise ValueError("canvas layout position is invalid")
            if not isinstance(layout, list) or _contains_reserved_presentation(layout):
                raise ValueError("canvas layout tree is invalid")
            try:
                json.dumps(layout, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("canvas layout tree is not canonical JSON") from exc
            seen_keys.add(layout_key)
            output.append(
                {
                    "layout_key": layout_key,
                    "position": position,
                    "layout": copy.deepcopy(layout),
                }
            )
        return output

    def _link_attachments(
        self,
        cursor: Any,
        *,
        chat_id: str,
        message_id: str,
        owner_user_id: str,
        attachment_ids: Sequence[Any],
        created_at_ms: int,
    ) -> None:
        for raw_attachment_id in attachment_ids:
            attachment_id = _uuid4_text(raw_attachment_id, "attachment_id")
            cursor.execute(
                "SELECT attachment_id FROM user_attachments "
                "WHERE attachment_id = %s AND user_id = %s "
                "AND deleted_at IS NULL",
                (attachment_id, owner_user_id),
            )
            if cursor.fetchone() is None:
                raise ValueError("attachment is unavailable")
            cursor.execute(
                """
                INSERT INTO message_attachment (
                    id, chat_id, message_id, attachment_id,
                    user_id, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    str(self.uuid_factory()),
                    chat_id,
                    message_id,
                    attachment_id,
                    owner_user_id,
                    created_at_ms,
                ),
            )

    def publish_commit(
        self,
        *,
        commit_id: Any,
        owner_user_id: str,
        messages: Optional[Sequence[Mapping[str, Any]]],
        canvas_components: Sequence[Mapping[str, Any]],
        canvas_layouts: Optional[Sequence[Mapping[str, Any]]] = None,
        operation_fence: Any = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        commit_id = _uuid4_text(commit_id, "commit_id")
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        validated_messages = (
            None if messages is None else self._validate_messages(messages)
        )
        validated_canvas = self._validate_canvas(canvas_components)
        validated_layouts = self._validate_layouts(canvas_layouts)

        with self._transaction(operation_fence) as cursor:
            staged = self._staged_for_update(cursor, commit_id, owner_user_id)
            if staged["state"] == "committed":
                return self._commit_record(staged)
            if staged["state"] != "staged":
                raise ConversationCommitConflict("conversation commit is terminal")
            self._assert_matching_operation_fence(staged, operation_fence)
            chat = self._chat_for_update(
                cursor, str(staged["chat_id"]), owner_user_id
            )
            base_revision = int(staged["base_render_revision"])
            if int(chat.get("render_revision") or 0) != base_revision:
                raise ConversationCommitConflict("conversation base revision changed")
            next_revision = base_revision + 1
            cursor.execute("SELECT clock_timestamp() AS current_time")
            current_time = cursor.fetchone()["current_time"]
            current_ms = int(current_time.timestamp() * 1000)

            cursor.execute(
                "SELECT commit_position, committed_render_revision "
                "FROM messages WHERE conversation_commit_id = %s "
                "ORDER BY commit_position",
                (commit_id,),
            )
            prepared_messages = list(cursor.fetchall())
            if validated_messages is None:
                positions = [int(row["commit_position"]) for row in prepared_messages]
                if positions != list(range(len(positions))) or any(
                    int(row["committed_render_revision"]) != next_revision
                    for row in prepared_messages
                ):
                    raise ConversationSnapshotInvalid(
                        "staged conversation messages are incomplete"
                    )
                messages_to_insert: Sequence[Mapping[str, Any]] = ()
            else:
                if prepared_messages:
                    raise ConversationCommitConflict(
                        "conversation messages were already prepared"
                    )
                messages_to_insert = validated_messages

            for position, message in enumerate(messages_to_insert):
                content = json.dumps(
                    message["content"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                timestamp = message["timestamp"]
                if not isinstance(timestamp, int) or timestamp < 0:
                    timestamp = current_ms + position
                cursor.execute(
                    """
                    INSERT INTO messages (
                        chat_id, user_id, role, content, timestamp,
                        conversation_commit_id, commit_position,
                        committed_render_revision
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        staged["chat_id"],
                        owner_user_id,
                        message["role"],
                        content,
                        timestamp,
                        commit_id,
                        position,
                        next_revision,
                    ),
                )
                message_id = str(cursor.fetchone()["id"])
                self._link_attachments(
                    cursor,
                    chat_id=str(staged["chat_id"]),
                    message_id=message_id,
                    owner_user_id=owner_user_id,
                    attachment_ids=message["attachments"],
                    created_at_ms=current_ms,
                )
            if fault_hook is not None:
                fault_hook("after_messages")

            cursor.execute(
                "SELECT id, component_id FROM saved_components "
                "WHERE chat_id = %s AND user_id = %s "
                "AND conversation_commit_id = %s "
                "AND committed_render_revision = %s "
                "ORDER BY position, created_at, id",
                (
                    staged["chat_id"],
                    owner_user_id,
                    commit_id,
                    next_revision,
                ),
            )
            prepared_canvas = list(cursor.fetchall())
            if prepared_canvas:
                prepared_identities = [
                    str(row["component_id"]) for row in prepared_canvas
                ]
                validated_identities = [
                    component["component_id"] for component in validated_canvas
                ]
                if prepared_identities != validated_identities:
                    raise ConversationSnapshotInvalid(
                        "staged conversation canvas is incomplete"
                    )
                cursor.execute(
                    "DELETE FROM saved_components "
                    "WHERE chat_id = %s AND user_id = %s "
                    "AND (conversation_commit_id IS NULL "
                    "OR conversation_commit_id <> %s)",
                    (staged["chat_id"], owner_user_id, commit_id),
                )
            else:
                cursor.execute(
                    "DELETE FROM saved_components "
                    "WHERE chat_id = %s AND user_id = %s",
                    (staged["chat_id"], owner_user_id),
                )
            cursor.execute(
                "DELETE FROM workspace_layout WHERE chat_id = %s AND user_id = %s",
                (staged["chat_id"], owner_user_id),
            )
            for position, component in enumerate(validated_canvas):
                component_json = json.dumps(
                    component,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                if prepared_canvas:
                    cursor.execute(
                        "UPDATE saved_components SET component_data = %s, "
                        "component_type = %s, title = %s, position = %s, "
                        "updated_at = %s WHERE id = %s AND chat_id = %s "
                        "AND user_id = %s AND conversation_commit_id = %s "
                        "AND committed_render_revision = %s",
                        (
                            component_json,
                            component["type"],
                            str(component.get("title") or component["type"])[:255],
                            position,
                            current_ms,
                            prepared_canvas[position]["id"],
                            staged["chat_id"],
                            owner_user_id,
                            commit_id,
                            next_revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ConversationSnapshotInvalid(
                            "staged conversation canvas update was lost"
                        )
                else:
                    cursor.execute(
                        """
                        INSERT INTO saved_components (
                            id, chat_id, user_id, component_data, component_type,
                            title, created_at, component_id, position, updated_at,
                            conversation_commit_id, committed_render_revision
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            str(self.uuid_factory()),
                            staged["chat_id"],
                            owner_user_id,
                            component_json,
                            component["type"],
                            str(component.get("title") or component["type"])[:255],
                            current_ms,
                            component["component_id"],
                            position,
                            current_ms,
                            commit_id,
                            next_revision,
                        ),
                    )
            for layout in validated_layouts:
                cursor.execute(
                    """
                    INSERT INTO workspace_layout (
                        chat_id, user_id, layout_key, position, layout,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        staged["chat_id"],
                        owner_user_id,
                        layout["layout_key"],
                        layout["position"],
                        json.dumps(
                            layout["layout"],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ),
                        current_ms,
                        current_ms,
                    ),
                )
            if fault_hook is not None:
                fault_hook("after_canvas")
                fault_hook("before_publish")

            cursor.execute(
                """
                UPDATE conversation_commit
                SET state = 'committed', committed_render_revision = %s,
                    committed_at = %s
                WHERE commit_id = %s AND state = 'staged'
                  AND base_render_revision = %s
                RETURNING *
                """,
                (next_revision, current_time, commit_id, base_revision),
            )
            committed = cursor.fetchone()
            if committed is None:
                raise ConversationCommitConflict("conversation publication lost its CAS")
            cursor.execute(
                """
                UPDATE chats
                SET render_revision = %s, snapshot_committed_at = %s,
                    conversation_commit_id = %s,
                    has_saved_components = %s, updated_at = %s
                WHERE id = %s AND user_id = %s AND render_revision = %s
                """,
                (
                    next_revision,
                    current_time,
                    commit_id,
                    bool(validated_canvas),
                    current_ms,
                    staged["chat_id"],
                    owner_user_id,
                    base_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationCommitConflict("conversation revision CAS is stale")
            if operation_fence is not None:
                from orchestrator.work_admission import OperationState

                self.operation_coordinator.terminalize(
                    operation_fence,
                    state=OperationState.COMPLETED,
                    terminal_code=None,
                    safe_summary="Conversation committed",
                    retry_after_ms=None,
                    transaction=cursor,
                )
        return self._commit_record(committed)

    @staticmethod
    def _attachments(cursor: Any, message_id: Any, owner_user_id: str) -> list[dict[str, str]]:
        cursor.execute(
            """
            SELECT attachment.attachment_id, attachment.filename,
                   attachment.category
            FROM message_attachment AS link
            JOIN user_attachments AS attachment
              ON attachment.attachment_id = link.attachment_id
             AND attachment.user_id = link.user_id
             AND attachment.deleted_at IS NULL
            WHERE link.message_id = %s AND link.user_id = %s
            ORDER BY link.created_at, link.id
            """,
            (str(message_id), owner_user_id),
        )
        return [
            {
                "attachment_id": str(row["attachment_id"]),
                "filename": str(row["filename"]),
                "category": str(row["category"]),
            }
            for row in cursor.fetchall()
        ]

    def build_snapshot(
        self,
        *,
        chat_id: str,
        owner_user_id: str,
        connection_generation: Any,
        request_generation: Any,
        snapshot_purpose: str,
    ) -> dict[str, Any]:
        chat_id = _uuid4_text(chat_id, "chat_id")
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        connection_generation = _uuid4_text(
            connection_generation, "connection_generation"
        )
        request_generation = _uuid4_text(request_generation, "request_generation")
        if snapshot_purpose not in {"hydration", "commit"}:
            raise ValueError("snapshot_purpose is invalid")

        connection = self.database._get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            cursor.execute(
                "SELECT * FROM chats WHERE id = %s AND user_id = %s",
                (chat_id, owner_user_id),
            )
            chat = cursor.fetchone()
            if chat is None:
                raise ConversationNotFound("conversation not found")
            render_revision = int(chat.get("render_revision") or 0)
            committed_at = chat.get("updated_at")
            current_commit = None
            if render_revision > 0:
                commit_id = chat.get("conversation_commit_id")
                if commit_id is None:
                    raise ConversationSnapshotInvalid(
                        "current conversation commit anchor is unavailable"
                    )
                cursor.execute(
                    """
                    SELECT committed_at, request_generation
                    FROM conversation_commit
                    WHERE commit_id = %s AND chat_id = %s AND owner_user_id = %s
                      AND state = 'committed' AND committed_render_revision = %s
                    """,
                    (commit_id, chat_id, owner_user_id, render_revision),
                )
                current_commit = cursor.fetchone()
                if current_commit is None or current_commit["committed_at"] is None:
                    raise ConversationSnapshotInvalid(
                        "current conversation commit anchor is inconsistent"
                    )
                committed_at = current_commit["committed_at"]
            elif chat.get("snapshot_committed_at") is not None:
                raise ConversationSnapshotInvalid(
                    "revision-zero conversation has committed snapshot metadata"
                )
            if snapshot_purpose == "commit":
                if current_commit is None:
                    raise ConversationSnapshotInvalid(
                        "commit snapshot has no committed revision"
                    )
                if str(current_commit["request_generation"]) != request_generation:
                    raise ConversationSnapshotInvalid(
                        "commit snapshot request generation does not match its commit"
                    )
            cursor.execute(
                """
                SELECT message.*
                FROM messages AS message
                LEFT JOIN conversation_commit AS commit
                  ON commit.commit_id = message.conversation_commit_id
                 AND commit.chat_id = message.chat_id
                 AND commit.owner_user_id = message.user_id
                WHERE message.chat_id = %s AND message.user_id = %s
                  AND (
                    message.conversation_commit_id IS NULL
                    OR (commit.state = 'committed'
                        AND commit.committed_render_revision =
                            message.committed_render_revision
                        AND message.committed_render_revision <= %s)
                  )
                ORDER BY COALESCE(message.committed_render_revision, 0),
                         CASE WHEN message.conversation_commit_id IS NULL
                              THEN message.timestamp ELSE message.commit_position END,
                         message.id
                """,
                (chat_id, owner_user_id, render_revision),
            )
            transcript = []
            fallback_time = chat.get("snapshot_committed_at") or chat.get("updated_at")
            for row in cursor.fetchall():
                created = row.get("timestamp")
                if created is None:
                    created = fallback_time
                parts = _rail_parts(_content_parts(row["content"]))
                if not parts:
                    # Pure canvas content (per-round tool components) — the
                    # workspace re-hydrates it; the rail shows no bubble.
                    continue
                transcript.append(
                    {
                        "message_id": str(row["id"]),
                        "role": str(row["role"]),
                        "created_at": _rfc3339(created),
                        "parts": parts,
                        "attachments": self._attachments(
                            cursor, row["id"], owner_user_id
                        ),
                    }
                )
            cursor.execute(
                """
                SELECT component.*
                FROM saved_components AS component
                LEFT JOIN conversation_commit AS commit
                  ON commit.commit_id = component.conversation_commit_id
                 AND commit.chat_id = component.chat_id
                 AND commit.owner_user_id = component.user_id
                WHERE component.chat_id = %s AND component.user_id = %s
                  AND (
                    (component.conversation_commit_id IS NULL AND %s = 0)
                    OR (commit.state = 'committed'
                        AND commit.committed_render_revision =
                            component.committed_render_revision
                        AND component.committed_render_revision = %s)
                  )
                ORDER BY COALESCE(component.position, 2147483647),
                         component.created_at, component.id
                """,
                (chat_id, owner_user_id, render_revision, render_revision),
            )
            component_rows = list(cursor.fetchall())
            components = []
            for position, row in enumerate(component_rows):
                try:
                    raw = json.loads(row["component_data"])
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ConversationSnapshotInvalid(
                        "saved canvas component is malformed"
                    ) from exc
                if isinstance(raw, dict) and row.get("component_id"):
                    raw["component_id"] = str(row["component_id"])
                components.append(_canonical_component(raw, position))
            cursor.execute(
                """
                SELECT layout_key, position, layout
                FROM workspace_layout
                WHERE chat_id = %s AND user_id = %s
                ORDER BY position, id
                """,
                (chat_id, owner_user_id),
            )
            layout_rows = list(cursor.fetchall())
            if layout_rows:
                from orchestrator import ui_designer
                from orchestrator.workspace import iter_layout_refs

                layouts = []
                for row in layout_rows:
                    try:
                        tree = json.loads(row["layout"])
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise ConversationSnapshotInvalid(
                            "saved canvas layout is malformed"
                        ) from exc
                    if not isinstance(tree, list):
                        raise ConversationSnapshotInvalid(
                            "saved canvas layout is malformed"
                        )
                    layouts.append(
                        {
                            "layout_key": str(row["layout_key"]),
                            "position": int(row["position"]),
                            "layout": tree,
                        }
                    )
                by_id = {
                    component["component_id"]: component for component in components
                }
                claimed = set()
                for layout in layouts:
                    claimed.update(iter_layout_refs(layout["layout"]))
                stream = [
                    (
                        int(component_rows[position].get("position") or 0),
                        0,
                        [component],
                    )
                    for position, component in enumerate(components)
                    if component["component_id"] not in claimed
                ]
                try:
                    stream.extend(
                        (
                            layout["position"],
                            1,
                            ui_designer.materialize(layout["layout"], by_id),
                        )
                        for layout in layouts
                    )
                    materialized = [
                        component
                        for _position, _kind, payload in sorted(
                            stream, key=lambda entry: (entry[0], entry[1])
                        )
                        for component in payload
                    ]
                    components = [
                        _canonical_component(component, position)
                        for position, component in enumerate(materialized)
                    ]
                except Exception as exc:
                    raise ConversationSnapshotInvalid(
                        "saved canvas layout cannot be materialized"
                    ) from exc
            snapshot = {
                "type": "conversation_snapshot",
                "schema_version": 1,
                "snapshot_id": _uuid4_text(self.uuid_factory(), "snapshot_id"),
                "chat_id": chat_id,
                "connection_generation": connection_generation,
                "request_generation": request_generation,
                "snapshot_purpose": snapshot_purpose,
                "render_revision": render_revision,
                "committed_at": _rfc3339(committed_at),
                "transcript": transcript,
                "canvas": {"target": "canvas", "components": components},
            }
            connection.commit()
            return snapshot
        except BaseException:
            connection.rollback()
            raise
        finally:
            try:
                cursor.close()
            finally:
                connection.close()


def _component_preview_text(components) -> str:
    """Flatten a component-list message into human-readable preview text.

    Feature 030 bug fix: assistant messages are stored as JSON lists of UI
    component dicts, and the history list previously previewed them with
    ``str(...)`` — leaking Python repr like ``[{'type': 'text', ...}]``.
    Walks the components in order, preferring the ``content`` of
    ``type == "text"`` components, falling back to a component's ``title``,
    and skipping anything without human text (charts, raw data payloads).
    Returns the joined pieces with whitespace collapsed; truncation is the
    caller's responsibility.
    """
    parts = []
    for item in components:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        text = item.get("content") if item.get("type") == "text" else None
        if not isinstance(text, str) or not text.strip():
            text = item.get("title")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return " ".join(" ".join(parts).split())


class HistoryManager:
    def __init__(self, data_dir: str = "data", database_url: str = None):
        self.data_dir = data_dir
        self.json_file = os.path.join(data_dir, "chats.json")
        self.db = Database(database_url)
        self._ensure_data_dir()
        self._migrate_from_json()

    def _ensure_data_dir(self):
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)

    def _migrate_from_json(self):
        """Migrate existing JSON history to the database."""
        if not os.path.exists(self.json_file):
            return

        # Check if DB is empty
        try:
            row = self.db.fetch_one("SELECT COUNT(*) as count FROM chats")
            if row['count'] > 0:
                # DB already has data, assume migration done or not needed
                return
        except Exception as e:
            logger.error(f"Error checking DB state: {e}")
            return

        logger.info("Migrating JSON history to database...")
        try:
            with open(self.json_file, 'r') as f:
                chats = json.load(f)
            
            for chat_id, chat_data in chats.items():
                self.db.execute(
                    "INSERT INTO chats (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (chat_id, 'legacy', chat_data.get('title'), chat_data.get('created_at'), chat_data.get('updated_at'))
                )
                
                for msg in chat_data.get('messages', []):
                    # Serialize content if it's not a string
                    content = msg.get('content')
                    if not isinstance(content, str):
                        content = json.dumps(content)
                        
                    self.db.execute(
                        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
                        (chat_id, 'legacy', msg.get('role'), content, msg.get('timestamp'))
                    )
            
            # Rename JSON file to backup to prevent re-migration
            os.rename(self.json_file, self.json_file + ".bak")
            logger.info("Migration complete. JSON file renamed to .bak")
            
        except Exception as e:
            logger.error(f"Migration failed: {e}")

    def create_chat(
        self,
        chat_id: Optional[str] = None,
        user_id: str = 'legacy',
        agent_id: Optional[str] = None,
    ) -> str:
        """Create a new chat session.

        Feature 013: ``agent_id`` binds the new chat to a specific agent so
        the UI can render the active-agent indicator (FR-006) and detect
        unavailability (FR-009). Pass None for unbound chats (legacy
        behaviour); a NULL ``agent_id`` is later interpreted by the
        frontend as "Unknown agent — pick one".
        """
        if not chat_id:
            chat_id = str(uuid.uuid4())
        stage = current_scheduled_history_stage()
        if stage is not None:
            stage._assert_write_target(self, chat_id, user_id)
            if agent_id is not None and stage.agent_id != agent_id:
                raise ValueError("scheduled staged chat agent identity changed")
            return chat_id
        timestamp = int(time.time() * 1000)
        self.db.execute(
            "INSERT INTO chats (id, user_id, title, agent_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, user_id, "New Chat", agent_id, timestamp, timestamp),
        )
        return chat_id

    def add_message(self, chat_id: str, role: str, content: any, user_id: str = 'legacy'):
        """Add a message to a chat session."""
        stage = current_scheduled_history_stage()
        if stage is not None:
            stage.add_message(
                self,
                chat_id=chat_id,
                user_id=user_id,
                role=role,
                content=content,
            )
            return
        timestamp = int(time.time() * 1000)
        
        # Serialize content if needed
        content_str = content
        if not isinstance(content, str):
            content_str = json.dumps(content)

        # Check if chat exists (scoped by user)
        chat = self.db.fetch_one(
            "SELECT id, render_revision FROM chats WHERE id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        if not chat:
            logger.warning(f"Attempted to add message to non-existent chat {chat_id}")
            return
        if int(chat.get("render_revision") or 0) > 0:
            raise RuntimeError(
                "revisioned conversation messages require a publication stage"
            )

        # Auto-update title logic
        if role == "user":
            # Check message count
            count_row = self.db.fetch_one("SELECT COUNT(*) as count FROM messages WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            if count_row['count'] == 0:
                # First message, update title
                display_content = str(content)
                title = display_content[:30] + "..." if len(display_content) > 30 else display_content
                self.db.execute("UPDATE chats SET title = ? WHERE id = ? AND user_id = ?", (title, chat_id, user_id))

        self.db.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (chat_id, user_id, role, content_str, timestamp)
        )
        
        # Update chat timestamp
        self.db.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (timestamp, chat_id))

    def get_latest_message_id(self, chat_id: str, user_id: str = 'legacy'):
        """Return the integer id of the most recent message in a chat.

        Added for feature 014 — chat-step recorder needs the
        ``messages.id`` of the user message that initiated a turn so step
        rows can FK back to it (see chat_steps.turn_message_id). Returns
        ``None`` if the chat has no messages.
        """
        row = self.db.fetch_one(
            "SELECT message.id FROM messages AS message "
            "LEFT JOIN conversation_commit AS publication "
            "ON publication.commit_id = message.conversation_commit_id "
            "AND publication.chat_id = message.chat_id "
            "AND publication.owner_user_id = message.user_id "
            "WHERE message.chat_id = ? AND message.user_id = ? AND ("
            "message.conversation_commit_id IS NULL OR ("
            "publication.state = 'committed' AND "
            "publication.committed_render_revision = "
            "message.committed_render_revision)) "
            "ORDER BY message.id DESC LIMIT 1",
            (chat_id, user_id),
        )
        return row["id"] if row else None

    def update_chat_title(self, chat_id: str, title: str, user_id: str = 'legacy'):
        """Update the title of a specific chat."""
        stage = current_scheduled_history_stage()
        if stage is not None:
            stage.update_title(
                self,
                chat_id=chat_id,
                user_id=user_id,
                title=title,
            )
            return
        timestamp = int(time.time() * 1000)
        self.db.execute("UPDATE chats SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?", (title, timestamp, chat_id, user_id))

    def get_chat(self, chat_id: str, user_id: str = 'legacy') -> Optional[Dict]:
        """Get full details of a specific chat."""
        stage = current_scheduled_history_stage()
        chat_row = self.db.fetch_one("SELECT * FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id))
        if not chat_row:
            if (
                stage is None
                or not stage.matches(self, chat_id, user_id)
                or not stage.create_chat_if_missing
            ):
                return None
            first_timestamp = (
                stage.messages[0].timestamp_ms
                if stage.messages
                else int(time.time() * 1000)
            )
            chat_row = {
                "id": chat_id,
                "title": "New Chat",
                "agent_id": stage.agent_id,
                "created_at": first_timestamp,
                "updated_at": first_timestamp,
            }

        messages_rows = self.db.fetch_all(
            "SELECT message.* FROM messages AS message "
            "LEFT JOIN conversation_commit AS publication "
            "ON publication.commit_id = message.conversation_commit_id "
            "AND publication.chat_id = message.chat_id "
            "AND publication.owner_user_id = message.user_id "
            "WHERE message.chat_id = ? AND message.user_id = ? AND ("
            "message.conversation_commit_id IS NULL OR ("
            "publication.state = 'committed' AND "
            "publication.committed_render_revision = "
            "message.committed_render_revision)) "
            "ORDER BY message.timestamp ASC, message.id ASC",
            (chat_id, user_id),
        )
        messages = []
        for row in messages_rows:
            content = row['content']
            # Try to deserialize JSON content
            try:
                content = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                pass # Keep as string
            
            messages.append({
                "id": row['id'],
                "role": row['role'],
                "content": content,
                "timestamp": row['timestamp']
            })

        if stage is not None and stage.matches(self, chat_id, user_id):
            for offset, message in enumerate(stage.messages, start=1):
                try:
                    content = json.loads(message.content)
                except (json.JSONDecodeError, TypeError):
                    content = message.content
                messages.append(
                    {
                        "id": None,
                        "role": message.role,
                        "content": content,
                        "timestamp": message.timestamp_ms,
                        "staged_position": offset,
                    }
                )

        return {
            "id": chat_row['id'],
            "title": (
                stage.requested_title
                if stage is not None
                and stage.matches(self, chat_id, user_id)
                and stage.requested_title is not None
                else chat_row['title']
            ),
            "agent_id": chat_row.get("agent_id"),
            "created_at": chat_row['created_at'],
            "updated_at": chat_row['updated_at'],
            "messages": messages
        }

    def get_recent_chats(self, limit: int = 20, user_id: str = 'legacy') -> List[Dict]:
        """Get list of recent chats (metadata only).

        Excludes draft-test chats and zero-message chats (feature 030):
        eagerly created "New Chat" husks stay out of the listing until
        their first message lands, at which point the chat appears
        automatically — chat creation itself is unchanged. Previews are
        human text: component-list message content is flattened via
        _component_preview_text() instead of leaking its Python repr,
        and every preview is truncated to PREVIEW_MAX_CHARS.

        Single round trip (feature 052): the last-message preview comes
        from a correlated subquery and the saved-components flag from the
        chats row itself, replacing the previous 1 + 2N per-chat lookups.
        """
        rows = self.db.fetch_all(
            "SELECT id, title, agent_id, updated_at, has_saved_components, "
            "(SELECT m.content FROM messages m WHERE m.chat_id = chats.id "
            "AND m.user_id = chats.user_id AND (m.conversation_commit_id IS NULL "
            "OR EXISTS (SELECT 1 FROM conversation_commit publication "
            "WHERE publication.commit_id = m.conversation_commit_id "
            "AND publication.chat_id = m.chat_id "
            "AND publication.owner_user_id = m.user_id "
            "AND publication.state = 'committed' "
            "AND publication.committed_render_revision = "
            "m.committed_render_revision)) "
            "ORDER BY m.timestamp DESC, m.id DESC LIMIT 1) AS last_content "
            "FROM chats WHERE user_id = ? AND id NOT LIKE 'draft-test-%' "
            "AND EXISTS (SELECT 1 FROM messages m2 WHERE m2.chat_id = chats.id "
            "AND m2.user_id = chats.user_id AND (m2.conversation_commit_id IS NULL "
            "OR EXISTS (SELECT 1 FROM conversation_commit publication2 "
            "WHERE publication2.commit_id = m2.conversation_commit_id "
            "AND publication2.chat_id = m2.chat_id "
            "AND publication2.owner_user_id = m2.user_id "
            "AND publication2.state = 'committed' "
            "AND publication2.committed_render_revision = "
            "m2.committed_render_revision))) "
            "ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        )

        results = []
        for row in rows:
            content = row.get("last_content")
            preview = ""
            if content is not None:
                try:
                    content_obj = json.loads(content)
                except Exception:
                    content_obj = content
                if isinstance(content_obj, str):
                    preview = content_obj
                elif isinstance(content_obj, list):
                    preview = _component_preview_text(content_obj)
                elif isinstance(content_obj, dict):
                    preview = _component_preview_text([content_obj])
                else:
                    preview = str(content_obj)
                if len(preview) > PREVIEW_MAX_CHARS:
                    preview = preview[:PREVIEW_MAX_CHARS] + "..."

            results.append({
                "id": row['id'],
                "title": row['title'],
                "agent_id": row.get("agent_id"),
                "updated_at": row['updated_at'],
                "preview": preview,
                "has_saved_components": bool(row.get("has_saved_components"))
            })

        return results
    
    def delete_chat(self, chat_id: str, user_id: str = 'legacy'):
        """Delete a chat and its messages."""
        self.db.execute("DELETE FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id))
        # Feature 055 (US4): component_version has no chats FK (unlike
        # saved_components), so the chat's refine history is swept manually
        # once the chat row is gone. Chat row first — a failed sweep only
        # orphans version rows; the reverse order could lose history for a
        # still-live chat.
        try:
            from orchestrator import artifact_versions
            artifact_versions.delete_for_chat(self.db, chat_id, user_id)
        except Exception:
            logger.debug("version-history cascade failed on delete_chat", exc_info=True)

    # =========================================================================
    # Saved UI Components Methods
    # =========================================================================
    
    def save_component(self, chat_id: str, component_data: any, component_type: str, title: str = None, user_id: str = 'legacy') -> str:
        """Save a UI component to the database."""
        import json
        import uuid
        import time
        
        chat = self.db.fetch_one(
            "SELECT render_revision FROM chats WHERE id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        if chat is None:
            raise ConversationNotFound("conversation not found")
        if int(chat.get("render_revision") or 0) > 0:
            raise RuntimeError(
                "revisioned workspace writes require a publication stage"
            )
        component_id = str(uuid.uuid4())
        created_at = int(time.time() * 1000)
        
        # Serialize component data
        component_json = json.dumps(component_data)
        
        # Use component type as title if not provided
        if not title:
            title = component_type.replace('_', ' ').title()
        
        self.db.execute(
            """INSERT INTO saved_components
               (id, chat_id, user_id, component_data, component_type, title, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (component_id, chat_id, user_id, component_json, component_type, title, created_at)
        )
        
        # Update chat flag
        self.db.execute(
            "UPDATE chats SET has_saved_components = TRUE WHERE id = ? AND user_id = ?",
            (chat_id, user_id)
        )
        
        return component_id
    
    def get_saved_components(self, chat_id: str = None, user_id: str = 'legacy') -> List[Dict]:
        """Get saved components, optionally filtered by chat_id."""
        import json
        
        if chat_id:
            rows = self.db.fetch_all(
                "SELECT component.* FROM saved_components AS component "
                "JOIN chats AS chat ON chat.id = component.chat_id "
                "AND chat.user_id = component.user_id "
                "WHERE component.chat_id = ? AND component.user_id = ? AND ("
                "(chat.render_revision = 0 "
                "AND component.conversation_commit_id IS NULL "
                "AND component.committed_render_revision IS NULL) OR "
                "(chat.render_revision > 0 "
                "AND component.conversation_commit_id = chat.conversation_commit_id "
                "AND component.committed_render_revision = chat.render_revision)) "
                "ORDER BY component.created_at DESC, component.id DESC",
                (chat_id, user_id)
            )
        else:
            rows = self.db.fetch_all(
                "SELECT component.* FROM saved_components AS component "
                "JOIN chats AS chat ON chat.id = component.chat_id "
                "AND chat.user_id = component.user_id "
                "WHERE component.user_id = ? AND ("
                "(chat.render_revision = 0 "
                "AND component.conversation_commit_id IS NULL "
                "AND component.committed_render_revision IS NULL) OR "
                "(chat.render_revision > 0 "
                "AND component.conversation_commit_id = chat.conversation_commit_id "
                "AND component.committed_render_revision = chat.render_revision)) "
                "ORDER BY component.created_at DESC, component.id DESC",
                (user_id,)
            )
        
        components = []
        for row in rows:
            try:
                component_data = json.loads(row['component_data'])
            except (json.JSONDecodeError, TypeError):
                component_data = row['component_data']
            
            components.append({
                "id": row['id'],
                "chat_id": row['chat_id'],
                "component_data": component_data,
                "component_type": row['component_type'],
                "title": row['title'],
                "created_at": row['created_at']
            })
        
        return components
    
    def delete_component(self, component_id: str, user_id: str = 'legacy') -> bool:
        """Delete a saved component."""
        # Get chat_id (and the workspace identity, feature 055) before deleting
        row = self.db.fetch_one(
            "SELECT component.chat_id, component.component_data, "
            "chat.render_revision FROM saved_components AS component "
            "JOIN chats AS chat ON chat.id = component.chat_id "
            "AND chat.user_id = component.user_id "
            "WHERE component.id = ? AND component.user_id = ? AND ("
            "(chat.render_revision = 0 "
            "AND component.conversation_commit_id IS NULL "
            "AND component.committed_render_revision IS NULL) OR "
            "(chat.render_revision > 0 "
            "AND component.conversation_commit_id = chat.conversation_commit_id "
            "AND component.committed_render_revision = chat.render_revision))",
            (component_id, user_id)
        )

        if not row:
            return False

        if int(row.get("render_revision") or 0) > 0:
            raise RuntimeError(
                "revisioned workspace writes require a publication stage"
            )

        chat_id = row['chat_id']

        # Delete the component
        self.db.execute(
            "DELETE FROM saved_components WHERE id = ? AND user_id = ?",
            (component_id, user_id)
        )

        # Feature 055 (US4): sweep the component's refine history. Versions
        # are keyed by the workspace component_id carried inside the stored
        # dict, not by this row's uuid.
        try:
            data = row.get('component_data')
            if isinstance(data, str):
                data = json.loads(data)
            ws_component_id = data.get('component_id') if isinstance(data, dict) else None
            if ws_component_id:
                from orchestrator import artifact_versions
                artifact_versions.delete_for_component(self.db, chat_id, user_id, ws_component_id)
        except Exception:
            logger.debug("version-history cascade failed on delete_component", exc_info=True)

        # Check if chat still has components
        count_row = self.db.fetch_one(
            "SELECT COUNT(*) as count FROM saved_components WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        )
        
        if count_row['count'] == 0:
            # Update chat flag
            self.db.execute(
                "UPDATE chats SET has_saved_components = FALSE WHERE id = ? AND user_id = ?",
                (chat_id, user_id)
            )
        
        return True
    
    def get_component_by_id(self, component_id: str, user_id: str = 'legacy') -> Optional[Dict]:
        """Get a single saved component by ID."""
        row = self.db.fetch_one(
            "SELECT component.* FROM saved_components AS component "
            "JOIN chats AS chat ON chat.id = component.chat_id "
            "AND chat.user_id = component.user_id "
            "WHERE component.id = ? AND component.user_id = ? AND ("
            "(chat.render_revision = 0 "
            "AND component.conversation_commit_id IS NULL "
            "AND component.committed_render_revision IS NULL) OR "
            "(chat.render_revision > 0 "
            "AND component.conversation_commit_id = chat.conversation_commit_id "
            "AND component.committed_render_revision = chat.render_revision))",
            (component_id, user_id)
        )
        if not row:
            return None
        
        try:
            component_data = json.loads(row['component_data'])
        except (json.JSONDecodeError, TypeError):
            component_data = row['component_data']
        
        return {
            "id": row['id'],
            "chat_id": row['chat_id'],
            "component_data": component_data,
            "component_type": row['component_type'],
            "title": row['title'],
            "created_at": row['created_at']
        }

    def replace_components(self, old_ids: list, new_components: list, chat_id: str, user_id: str = 'legacy') -> list:
        """Atomically delete old components and insert new ones. Returns list of new component dicts."""
        chat = self.db.fetch_one(
            "SELECT render_revision FROM chats WHERE id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        if chat is None:
            raise ConversationNotFound("conversation not found")
        if int(chat.get("render_revision") or 0) > 0:
            raise RuntimeError(
                "revisioned workspace writes require a publication stage"
            )
        # Delete old components
        for old_id in old_ids:
            self.db.execute(
                "DELETE FROM saved_components WHERE id = ? AND user_id = ?",
                (old_id, user_id)
            )
        
        # Insert new components
        created = []
        for comp in new_components:
            component_id = str(uuid.uuid4())
            created_at = int(time.time() * 1000)
            component_json = json.dumps(comp.get("component_data", {}))
            component_type = comp.get("component_type", "combined")
            title = comp.get("title", "Combined Component")
            
            self.db.execute(
                """INSERT INTO saved_components
                   (id, chat_id, user_id, component_data, component_type, title, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (component_id, chat_id, user_id, component_json, component_type, title, created_at)
            )
            
            created.append({
                "id": component_id,
                "chat_id": chat_id,
                "component_data": comp.get("component_data", {}),
                "component_type": component_type,
                "title": title,
                "created_at": created_at
            })
        
        # Check if chat still has components
        count_row = self.db.fetch_one(
            "SELECT COUNT(*) as count FROM saved_components WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        )
        has_components = count_row and count_row['count'] > 0
        self.db.execute(
            "UPDATE chats SET has_saved_components = ? WHERE id = ? AND user_id = ?",
            (bool(has_components), chat_id, user_id)
        )
        
        return created

    def chat_has_saved_components(self, chat_id: str, user_id: str = 'legacy') -> bool:
        """Check if a chat has saved components."""
        row = self.db.fetch_one(
            "SELECT has_saved_components FROM chats WHERE id = ? AND user_id = ?",
            (chat_id, user_id)
        )
        
        if not row:
            return False
        
        return bool(row['has_saved_components'])

    def add_file_mapping(self, chat_id: str, original_name: str, backend_path: str, user_id: str = 'legacy'):
        """Register a mapping between an original filename and its backend UUID path."""
        import time
        timestamp = int(time.time() * 1000)
        self.db.execute(
            "INSERT INTO chat_files (chat_id, user_id, original_name, backend_path, uploaded_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, user_id, original_name, backend_path, timestamp)
        )

    def get_file_mappings(self, chat_id: str, user_id: str = 'legacy') -> List[Dict]:
        """Retrieve all file mappings for a chat."""
        rows = self.db.fetch_all(
            "SELECT original_name, backend_path FROM chat_files WHERE chat_id = ? AND user_id = ? ORDER BY uploaded_at ASC",
            (chat_id, user_id)
        )
        return [{"original_name": r["original_name"], "backend_path": r["backend_path"]} for r in rows]
