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

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
)
from astralplane.contracts import IsolationLevel
from astralplane.repositories.workspaces import (
    CanvasComponentRecord,
    LayoutRecord,
    PublicationRebaseComponent,
    PublicationRebaseLayout,
    PublicationRecord,
)

# Ensure shared module is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from shared.feature_flags import flags
from shared.protocol import CANONICAL_TEXT_PART_VARIANTS
from orchestrator.conversation_publication import (
    canonical_components_sha256,
    canonical_layouts_sha256,
    current_conversation_publication,
    merge_conversation_publication,
)
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)
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


def _mutable_json_value(value: Any) -> Any:
    """Copy Plane's immutable JSON view into a transport-safe value."""
    if isinstance(value, Mapping):
        return {key: _mutable_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mutable_json_value(item) for item in value]
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
    if isinstance(value, (list, tuple)):
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
    if isinstance(value, (list, tuple)):
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


def _structured_parts(value: Any) -> list[dict[str, Any]]:
    """One canonical ``structured`` part, or an honest recovery if it is blank.

    ``_plain_text`` renders "" for an empty string and for a nested array that
    reduces to one, so a stored tool result carrying a blank element would
    otherwise commit a part whose rendition is invisible. Every Apple client
    guards ``continuityNonBlank(plain_text)`` — which TRIMS before testing
    emptiness — and decodes a snapshot all-or-nothing (``parts.count ==
    rawParts.count``, ``messages.count == transcript.count``), so ONE blank
    part discards the WHOLE ``conversation_snapshot`` and that chat's rail
    never hydrates. The blank ``text`` twin is already dropped by
    ``_rail_parts``; ``structured`` parts pass through it untouched, which is
    why this is the path that has to refuse.

    Degrading to the visible recovery pair mirrors how a ``None`` element is
    already handled: the turn says a saved response could not be displayed
    instead of silently committing something no client can render.
    """

    try:
        plain_text = _plain_text(value)
    except (TypeError, ValueError):
        return _recovery_parts()
    if not plain_text.strip():
        return _recovery_parts()
    return [
        {
            "type": "structured",
            "value": _strip_reserved_presentation(value),
            "plain_text": plain_text,
        }
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
            if isinstance(children, (list, tuple)):
                nested = [c for c in children if isinstance(c, Mapping) and "type" in c]
                if nested and not _is_rail_text_only(nested):
                    return False
    return True


# Text-only WRAPPER chrome whose nested words must survive the rail
# reduction. ``Orchestrator._chat_narrative`` persists a multi-paragraph
# answer as ``Card(title=…, content=[Text(answer)])`` — chat-rail narrative
# that never enters the workspace. Dropping the whole card (the pre-fix
# feature-063 rule) erased the assistant's answer from the committed
# conversation snapshot: the end-of-turn snapshot then REPLACED the live
# view answer-less on every client ("first message of a new chat gets no
# response"). Rich components inside a wrapper still drop to the canvas.
_RAIL_WRAPPER_TYPES = {"card", "container", "collapsible"}


def _rail_wrapper_is_anchored(component: Mapping[str, Any]) -> bool:
    """True when a wrapper carries an AUTHOR workspace identity.

    ``_component_identity`` preserves an author-supplied ``component_id``
    (e.g. the ``doc_…`` narrative doc card, which the workspace re-hydrates
    to the canvas beside its concise rail lead) and synthesizes a ``cc_…``
    fingerprint for identity-less components (the ``_chat_narrative`` card,
    which lives nowhere but the transcript). Lifting an anchored wrapper's
    words would duplicate its canvas rendition in the rail; dropping an
    unanchored one loses the words entirely.
    """
    identity = component.get("component_id")
    return isinstance(identity, str) and bool(identity) and not identity.startswith("cc_")


# Feature 066 T023 (CLOSED as a deliberate cross-client contract extension):
# a lifted caption's variant now rides the rail part as an optional bounded
# ``variant`` key — accepted by the server validator (shared/protocol.py
# CANONICAL_TEXT_PART_VARIANTS), the web/Windows/Android/Apple decoders, and
# documented in specs/060-…/contracts/conversation-continuity.md. Every other
# authoring variant still normalizes away, so the canonical boundary stays
# exact for non-caption text.
def _lifted_text_part(text: str, variant: Any) -> dict[str, Any]:
    """One canonical rail text part; caption weight survives the lift.

    The carry is gated by ``FF_RAIL_CAPTION_VARIANT`` (default OFF). This is
    the ONLY place a rail part gains the T023 ``variant`` key, and the gate
    exists because T023 shipped after apple-v1.2 / Android versionCode 4:
    those store builds compare the part's key set for EXACT equality, so a
    caption part makes them drop the whole ``conversation_snapshot`` and the
    rail silently stops committing. Emission is gated; ACCEPTANCE is not (see
    ``CANONICAL_TEXT_PART_VARIANTS``), so flipping the gate on later needs no
    client change. Pins: tests/test_rail_caption_emission_gate.py.

    The gate is deliberately GLOBAL, not per-target: while it is off a caption
    also flattens for web and Windows, which decode the shape correctly. There
    is no reliable client-version signal to scope it by (v1.2 and v1.3
    ``register_ui`` frames are byte-identical), and diverging per target would
    break snapshot parity. That cost is accepted — do not "fix" it by
    re-deriving weight downstream.

    The flag is read FIRST so the default path is one boolean and behaves
    exactly like pre-T023 code: ``variant`` arrives from stored agent output
    and an agent emitting plain dicts can make it unhashable (e.g. a list),
    which would raise TypeError from the membership test alone.
    """
    part: dict[str, Any] = {"type": "text", "text": text}
    if (
        flags.is_enabled("rail_caption_variant")
        and isinstance(variant, str)
        and variant in CANONICAL_TEXT_PART_VARIANTS
    ):
        part["variant"] = variant
    return part


def _wrapper_texts(component: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """Depth-first (text, variant) contents of one text-only wrapper."""
    texts: list[tuple[str, Any]] = []
    for key in ("content", "children"):
        children = component.get(key)
        if not isinstance(children, (list, tuple)):
            continue
        for child in children:
            if not isinstance(child, Mapping):
                continue
            child_type = str(child.get("type", "")).strip().lower()
            if child_type == "text":
                text = child.get("content")
                if not isinstance(text, str):
                    text = child.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append((text, child.get("variant")))
            elif child_type in _RAIL_WRAPPER_TYPES:
                texts.extend(_wrapper_texts(child))
    return texts


def _rail_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce a transcript message's parts to TEXT ONLY (feature 063).

    The chat rail is the conversation; the canvas is where UI lives. So a
    ``components`` part is reduced to the plain text of any top-level ``text``
    primitives it carries (lifted to ``text`` parts, so no assistant words are
    lost) and every other component — tables, lists, alerts, metrics — is
    dropped from the transcript: it is canvas state, not conversation. A message
    left with no parts is omitted (it was purely a rendered component).

    One refinement over the original feature-063 rule: a TEXT-ONLY wrapper
    (card/container/collapsible whose children are all words — the shape
    ``_chat_narrative`` persists for multi-paragraph answers) has its nested
    text lifted instead of being dropped, UNLESS it carries an author
    workspace identity (``doc_…`` narrative doc cards re-hydrate to the
    canvas; lifting them would duplicate the write-up beside its rail lead).
    Identity-less narrative chrome never enters the workspace, so dropping
    it lost the assistant's words entirely from the committed snapshot. A
    wrapper with any rich child still drops whole: it is a canvas component
    and the workspace re-hydrates it.

    This supersedes the feature-062 rule that kept text-like components in the
    rail (they still appeared as duplicate cards beside the canvas)."""
    kept: list[dict[str, Any]] = []
    for part in parts:
        if part.get("type") != "components":
            # Canonical-boundary normalization (066 R-9 rail fix): stored
            # narrative parts may carry authoring fields (``variant``,
            # ``content``) that the canonical transcript contract forbids —
            # text parts must be EXACTLY {"type","text"} plus the T023
            # bounded ``variant`` carve-out. A voice turn's rail delivery
            # rides canonical ``conversation_snapshot`` frames, so an
            # un-normalized part made every client reject the whole snapshot
            # (``invalid_snapshot``) and the rail silently never updated.
            if part.get("type") == "text":
                text = part.get("text")
                if not isinstance(text, str):
                    text = part.get("content")
                if isinstance(text, str) and text.strip():
                    kept.append(_lifted_text_part(text, part.get("variant")))
                continue
            kept.append(part)
            continue
        for comp in part.get("components", []):
            if not isinstance(comp, Mapping):
                continue
            comp_type = str(comp.get("type", "")).strip().lower()
            if comp_type == "text":
                text = comp.get("content")
                if not isinstance(text, str):
                    text = comp.get("text")
                if isinstance(text, str) and text.strip():
                    kept.append(_lifted_text_part(text, comp.get("variant")))
            elif (
                comp_type in _RAIL_WRAPPER_TYPES
                and not _rail_wrapper_is_anchored(comp)
                and _is_rail_text_only([comp])
            ):
                for text, variant in _wrapper_texts(comp):
                    kept.append(_lifted_text_part(text, variant))
            # anything else is a UI component — canvas only
    return kept


def _content_parts(
    stored: Any,
    *,
    already_decoded: bool = False,
) -> list[dict[str, Any]]:
    if already_decoded:
        if isinstance(stored, str):
            stripped = stored.strip()
            if not stripped:
                return _recovery_parts()
            if stripped.startswith(("{", "[")):
                try:
                    json.loads(
                        stored,
                        parse_constant=lambda constant: (_ for _ in ()).throw(
                            ValueError(f"non-finite JSON constant: {constant}")
                        ),
                    )
                except (json.JSONDecodeError, TypeError, ValueError):
                    # Plane preserves an invalid legacy TEXT value as a string.
                    # Retain the historic recovery posture for malformed values
                    # that claimed to be structured content.
                    return _recovery_parts()
        value = stored
    elif not isinstance(stored, str):
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
    if isinstance(value, (list, tuple)) and value and all(
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
    if isinstance(value, (list, tuple)) and any(
        isinstance(item, Mapping) and item.get("type") in allowed_types
        for item in value
    ):
        # A partially valid primitive group is not safe to reinterpret as
        # ordinary structured data: doing so would silently change its UI
        # semantics. Preserve a visible recovery value instead.
        return _recovery_parts()
    if isinstance(value, (list, tuple)):
        if not value:
            return [{"type": "structured", "value": [], "plain_text": "[]"}]
        parts: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, str) and item:
                parts.append({"type": "text", "text": item})
            elif item is None:
                parts.extend(_recovery_parts())
            else:
                parts.extend(_structured_parts(item))
        return parts or _recovery_parts()
    return _structured_parts(value)


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
    from webrender import render_component_fragment, render_one, render_workspace

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

    def augment_text(part: dict[str, Any]) -> None:
        """Feature 066: give an assistant rail text part its web rendition.

        The words-only rail (062) keeps only the raw markdown SOURCE of a
        text primitive, so the transcript rendered it inert and the user saw
        literal ``**asterisks**`` the moment a turn committed. The rendition
        rides the same transport-only ``_presentation`` envelope the
        components path uses and goes through the IDENTICAL escape-first
        pipeline (``render_text`` markdown branch -> ``block_md``), so the
        semantic value stays authoritative and nothing new is trusted.

        T023: a part carrying the bounded canonical ``variant`` renders at
        that weight (a lifted caption hydrates as a caption); everything
        else keeps the markdown default.
        """
        variant = part.get("variant")
        if variant not in CANONICAL_TEXT_PART_VARIANTS:
            variant = "markdown"
        rendered = render_one(
            {"type": "text", "variant": variant, "content": part["text"]}
        )
        if rendered:
            part["_presentation"] = {"target": "web", "html": rendered}

    transcript = candidate.get("transcript")
    if isinstance(transcript, list):
        for message in transcript:
            if not isinstance(message, dict):
                continue
            # Only the assistant's own prose is markdown; a user's typed
            # asterisks stay inert.
            is_assistant = message.get("role") == "assistant"
            for part in message.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "components":
                    part["components"] = augment(part.get("components") or [])
                elif is_assistant and part.get("type") == "text":
                    augment_text(part)
    canvas = candidate.get("canvas")
    if isinstance(canvas, dict):
        canvas["components"] = augment(canvas.get("components") or [])
    return candidate


class ConversationCommitRepository:
    """Deep publication policy over the application-scoped Plane runtime."""

    def __init__(
        self,
        database: Any = None,
        *,
        plane_runtime: Any = None,
        plane_repositories: Any = None,
        operation_coordinator: Any = None,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        if not callable(uuid_factory):
            raise TypeError("uuid_factory must be callable")
        runtime = plane_runtime or getattr(database, "plane_runtime", None)
        repositories = (
            plane_repositories
            or getattr(database, "plane_repositories", None)
            or (None if runtime is None else runtime.repositories)
        )
        if runtime is None or repositories is None:
            raise TypeError(
                "ConversationCommitRepository requires the application Plane runtime"
            )
        self.plane_runtime = runtime
        self.plane_repositories = repositories
        self._history = repositories.history
        self._workspaces = repositories.workspaces
        self._artifacts = repositories.artifacts
        self.operation_coordinator = operation_coordinator
        self.uuid_factory = uuid_factory

    @contextmanager
    def _transaction(self, operation_fence: Any = None) -> Iterator[Any]:
        if operation_fence is not None:
            if self.operation_coordinator is None:
                raise ValueError("operation coordinator is required for a fenced commit")
            with self.operation_coordinator.fenced_transaction(
                operation_fence
            ) as transaction:
                yield transaction
            return
        with self.plane_runtime.transaction() as transaction:
            yield transaction

    def _chat_for_update(
        self,
        transaction: Any,
        chat_id: str,
        owner_user_id: str,
    ) -> Mapping[str, Any]:
        record = self._history.conversations.get(
            transaction,
            owner_id=owner_user_id,
            conversation_id=chat_id,
            for_update=True,
        )
        if record is None:
            raise ConversationNotFound("conversation not found")
        return {
            "id": record.conversation_id,
            "user_id": record.owner_id,
            "title": record.title,
            "agent_id": record.agent_id,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "render_revision": record.render_revision,
            "conversation_commit_id": record.publication_id,
            "has_saved_components": record.has_saved_components,
            "snapshot_committed_at": record.snapshot_committed_at,
        }

    @staticmethod
    def _commit_record(row: Mapping[str, Any] | PublicationRecord) -> dict[str, Any]:
        if isinstance(row, PublicationRecord):
            return {
                "commit_id": row.publication_id,
                "chat_id": row.conversation_id,
                "owner_user_id": row.owner_id,
                "request_generation": row.request_generation,
                "base_render_revision": row.base_render_revision,
                "committed_render_revision": row.committed_render_revision,
                "state": row.state,
                "publication_role": row.publication_role,
                "parent_commit_id": row.parent_publication_id,
                "execution_base_render_revision": (
                    row.execution_base_render_revision
                ),
                "publication_rebase_count": row.publication_rebase_count,
                "committed_at": (
                    None
                    if row.committed_at is None
                    else _rfc3339(row.committed_at)
                ),
            }
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
            "publication_role": str(row.get("publication_role") or "atomic"),
            "parent_commit_id": (
                None
                if row.get("parent_commit_id") is None
                else str(row["parent_commit_id"])
            ),
            "execution_base_render_revision": (
                None
                if row.get("execution_base_render_revision") is None
                else int(row["execution_base_render_revision"])
            ),
            "publication_rebase_count": int(
                row.get("publication_rebase_count") or 0
            ),
            "committed_at": (
                None if row["committed_at"] is None else _rfc3339(row["committed_at"])
            ),
        }

    @staticmethod
    def _assert_operation_authority(
        operation: Any,
        *,
        owner_user_id: str,
        chat_id: str,
        request_generation: str,
        connection_generation: str,
        operation_owner: Any,
    ) -> None:
        expected_scope = getattr(operation_owner, "owner_scope", None)
        if operation.owner_scope != expected_scope:
            raise ConversationCommitConflict(
                "conversation operation owner scope changed"
            )
        if str(getattr(operation.owner_scope, "value", "")) != "user":
            raise ConversationCommitConflict(
                "voice publication requires user operation ownership"
            )
        if (
            operation.owner_user_id != owner_user_id
            or getattr(operation_owner, "owner_user_id", None) != owner_user_id
        ):
            raise ConversationCommitConflict("conversation operation owner changed")
        if operation.chat_id != chat_id:
            raise ConversationCommitConflict("conversation operation chat changed")
        if str(operation.request_generation or "") != request_generation:
            raise ConversationCommitConflict(
                "conversation operation request generation changed"
            )
        actual_connection = (
            None
            if operation.connection_generation is None
            else str(operation.connection_generation)
        )
        if actual_connection != connection_generation:
            raise ConversationCommitConflict(
                "conversation operation connection generation changed"
            )

    def _load_component_view(
        self,
        transaction: Any,
        *,
        chat_id: str,
        owner_user_id: str,
        commit_id: str | None,
        revision: int,
        require_state: str | None = None,
    ) -> list[dict[str, Any]]:
        if revision == 0:
            records = self._workspaces.canvas.list_current(
                transaction,
                owner_id=owner_user_id,
                conversation_id=chat_id,
            )
        else:
            if commit_id is None:
                raise ConversationSnapshotInvalid(
                    "conversation component anchor is unavailable"
                )
            records = self._workspaces.canvas.list_for_publication(
                transaction,
                owner_id=owner_user_id,
                conversation_id=chat_id,
                publication_id=commit_id,
                committed_render_revision=revision,
                require_state=require_state,
            )
        components: list[dict[str, Any]] = []
        for position, record in enumerate(records):
            raw = _strip_reserved_presentation(record.payload)
            if isinstance(raw, dict):
                raw["component_id"] = record.component_id
            components.append(_canonical_component(raw, position))
        return components

    def _load_layout_view(
        self,
        transaction: Any,
        *,
        chat_id: str,
        owner_user_id: str,
        commit_id: str | None,
        revision: int,
        require_state: str | None = None,
    ) -> list[dict[str, Any]]:
        if revision == 0:
            records = self._workspaces.layouts.list_current(
                transaction,
                owner_id=owner_user_id,
                conversation_id=chat_id,
            )
        else:
            if commit_id is None:
                raise ConversationSnapshotInvalid(
                    "conversation layout anchor is unavailable"
                )
            records = self._workspaces.layouts.list_for_publication(
                transaction,
                owner_id=owner_user_id,
                conversation_id=chat_id,
                publication_id=commit_id,
                committed_render_revision=revision,
                require_state=require_state,
            )
        layouts: list[dict[str, Any]] = []
        for record in records:
            tree = _strip_reserved_presentation(record.tree)
            if not isinstance(tree, list):
                raise ConversationSnapshotInvalid(
                    "saved canvas layout is malformed"
                )
            layouts.append(
                {
                    "layout_key": record.layout_key,
                    "position": record.position,
                    "layout": tree,
                }
            )
        return layouts

    def _insert_component_view(
        self,
        transaction: Any,
        *,
        chat_id: str,
        owner_user_id: str,
        commit_id: str,
        revision: int,
        components: Sequence[Mapping[str, Any]],
        current_ms: int,
    ) -> None:
        for position, component in enumerate(components):
            self._workspaces.canvas.create(
                transaction,
                CanvasComponentRecord(
                    row_id=str(self.uuid_factory()),
                    conversation_id=chat_id,
                    owner_id=owner_user_id,
                    component_id=str(component["component_id"]),
                    payload=copy.deepcopy(component),
                    component_type=str(component["type"]),
                    title=str(component.get("title") or component["type"])[:255],
                    position=position,
                    created_at=current_ms,
                    updated_at=current_ms,
                    publication_id=commit_id,
                    committed_render_revision=revision,
                ),
            )

    def _insert_layout_view(
        self,
        transaction: Any,
        *,
        chat_id: str,
        owner_user_id: str,
        commit_id: str,
        revision: int,
        layouts: Sequence[Mapping[str, Any]],
        current_ms: int,
    ) -> None:
        for layout in layouts:
            self._workspaces.layouts.create(
                transaction,
                LayoutRecord(
                    layout_id=0,
                    conversation_id=chat_id,
                    owner_id=owner_user_id,
                    layout_key=str(layout["layout_key"]),
                    position=int(layout["position"]),
                    tree=copy.deepcopy(layout["layout"]),
                    created_at=current_ms,
                    updated_at=current_ms,
                    publication_id=commit_id,
                    committed_render_revision=revision,
                ),
            )

    def accept_voice_turn(
        self,
        *,
        chat_id: str,
        owner_user_id: str,
        request_generation: Any,
        result_request_generation: Any,
        connection_generation: Any,
        user_content: Any,
        operation_fence: Any,
        operation_owner: Any,
        accept_turn: Callable[..., Any],
    ) -> dict[str, Any]:
        """Commit one user bubble and allocate its private result atomically.

        This is the short voice admission/snapshot critical section. The
        caller already owns a running no-queue execution fence. The callback
        joins this exact transaction to bind the content-free ``voice_turn`` row,
        so acknowledgement is impossible before both conversation commits and
        the voice correlation are durable.
        """

        chat_id = _uuid4_text(chat_id, "chat_id")
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        request_generation = _uuid4_text(
            request_generation, "request_generation"
        )
        result_request_generation = _uuid4_text(
            result_request_generation, "result_request_generation"
        )
        connection_generation = _uuid4_text(
            connection_generation, "connection_generation"
        )
        if result_request_generation == request_generation:
            raise ValueError("result request generation must be distinct")
        if operation_fence is None or operation_owner is None:
            raise ValueError("voice acceptance requires operation authority")
        if not callable(accept_turn):
            raise TypeError("accept_turn must be callable")
        message = self._validate_messages(
            [{"role": "user", "content": user_content}]
        )[0]
        acceptance_commit_id = _uuid4_text(
            self.uuid_factory(), "acceptance_commit_id"
        )
        result_commit_id = _uuid4_text(self.uuid_factory(), "result_commit_id")

        with self._transaction(operation_fence) as transaction:
            operation = self.operation_coordinator.assert_current_execution(
                operation_fence,
                transaction=transaction,
            )
            self._assert_operation_authority(
                operation,
                owner_user_id=owner_user_id,
                chat_id=chat_id,
                request_generation=request_generation,
                connection_generation=connection_generation,
                operation_owner=operation_owner,
            )
            chat = self._history.conversations.get(
                transaction,
                owner_id=owner_user_id,
                conversation_id=chat_id,
                for_update=True,
            )
            if chat is None:
                raise ConversationNotFound("conversation not found")
            existing = self._workspaces.publications.get_by_request(
                transaction,
                owner_id=owner_user_id,
                conversation_id=chat_id,
                request_generation=request_generation,
                for_update=True,
            )
            if existing is not None:
                if (
                    existing.owner_id != owner_user_id
                    or existing.publication_role != "user_acceptance"
                    or existing.state != "committed"
                    or str(existing.operation_id or "")
                    != str(operation_fence.operation_id)
                    or int(existing.operation_execution_generation or 0)
                    != int(operation_fence.execution_generation)
                ):
                    raise ConversationCommitConflict(
                        "voice acceptance idempotency identity changed"
                    )
                result = self._workspaces.publications.get_by_request(
                    transaction,
                    owner_id=owner_user_id,
                    conversation_id=chat_id,
                    request_generation=result_request_generation,
                    for_update=True,
                )
                if (
                    result is None
                    or result.publication_role != "assistant_result"
                    or result.parent_publication_id != existing.publication_id
                ):
                    raise ConversationCommitConflict(
                        "voice result idempotency identity changed"
                    )
                accepted_messages = self._history.messages.list_for_publication(
                    transaction,
                    owner_id=owner_user_id,
                    conversation_id=chat_id,
                    publication_id=existing.publication_id,
                    limit=2,
                )
                if (
                    len(accepted_messages) != 1
                    or accepted_messages[0].role != "user"
                    or accepted_messages[0].commit_position != 0
                ):
                    raise ConversationSnapshotInvalid(
                        "voice acceptance message is unavailable"
                    )
                return {
                    "acceptance": self._commit_record(existing),
                    "result": self._commit_record(result),
                    "message_id": accepted_messages[0].message_id,
                    "accepted_turn": None,
                    "components": self._load_component_view(
                        transaction,
                        chat_id=chat_id,
                        owner_user_id=owner_user_id,
                        commit_id=existing.publication_id,
                        revision=int(existing.committed_render_revision or 0),
                        require_state="committed",
                    ),
                    "layouts": self._load_layout_view(
                        transaction,
                        chat_id=chat_id,
                        owner_user_id=owner_user_id,
                        commit_id=existing.publication_id,
                        revision=int(existing.committed_render_revision or 0),
                        require_state="committed",
                    ),
                }

            base_revision = chat.render_revision
            base_commit_id = (
                None
                if base_revision == 0
                else chat.publication_id
            )
            if base_revision > 0 and not base_commit_id:
                raise ConversationSnapshotInvalid(
                    "current conversation commit anchor is unavailable"
                )
            components = self._load_component_view(
                transaction,
                chat_id=chat_id,
                owner_user_id=owner_user_id,
                commit_id=base_commit_id,
                revision=base_revision,
                require_state=(None if base_revision == 0 else "committed"),
            )
            layouts = self._load_layout_view(
                transaction,
                chat_id=chat_id,
                owner_user_id=owner_user_id,
                commit_id=base_commit_id,
                revision=base_revision,
                require_state=(None if base_revision == 0 else "committed"),
            )
            component_digest = canonical_components_sha256(components)
            layout_digest = canonical_layouts_sha256(layouts)
            next_revision = base_revision + 1
            operation_id = str(operation_fence.operation_id)
            operation_generation = int(operation_fence.execution_generation)
            current_time = datetime.now(UTC)
            current_ms = int(current_time.timestamp() * 1000)
            try:
                acceptance_stage = self._workspaces.publications.stage(
                    transaction,
                    publication_id=acceptance_commit_id,
                    owner_id=owner_user_id,
                    conversation_id=chat_id,
                    request_generation=request_generation,
                    operation_id=operation_id,
                    operation_execution_generation=operation_generation,
                    base_render_revision=base_revision,
                    started_at=current_time,
                    publication_role="user_acceptance",
                    execution_base_publication_id=base_commit_id,
                    execution_base_render_revision=base_revision,
                    execution_base_components_sha256=component_digest,
                    execution_base_layouts_sha256=layout_digest,
                )
                message_record = (
                    self._history.messages.append_next_to_staged_publication(
                        transaction,
                        owner_id=owner_user_id,
                        conversation_id=chat_id,
                        publication_id=acceptance_commit_id,
                        role=message["role"],
                        content=message["content"],
                    )
                )
            except RepositoryNotFoundError as exc:
                raise ConversationNotFound("conversation not found") from exc
            except RepositoryConflictError as exc:
                raise ConversationCommitConflict(
                    "voice acceptance publication lost its stage fence"
                ) from exc
            message_id = message_record.message_id
            self._insert_component_view(
                transaction,
                chat_id=chat_id,
                owner_user_id=owner_user_id,
                commit_id=acceptance_commit_id,
                revision=next_revision,
                components=components,
                current_ms=current_ms,
            )
            self._insert_layout_view(
                transaction,
                chat_id=chat_id,
                owner_user_id=owner_user_id,
                commit_id=acceptance_commit_id,
                revision=next_revision,
                layouts=layouts,
                current_ms=current_ms,
            )
            try:
                acceptance = self._workspaces.publications.commit_at_head(
                    transaction,
                    owner_id=owner_user_id,
                    conversation_id=chat_id,
                    publication_id=acceptance_stage.publication_id,
                    expected_staged_base_render_revision=base_revision,
                    expected_head_render_revision=base_revision,
                    expected_head_publication_id=base_commit_id,
                    committed_at=current_time,
                    updated_at=current_ms,
                )
                result = self._workspaces.publications.stage(
                    transaction,
                    publication_id=result_commit_id,
                    owner_id=owner_user_id,
                    conversation_id=chat_id,
                    request_generation=result_request_generation,
                    operation_id=operation_id,
                    operation_execution_generation=operation_generation,
                    base_render_revision=next_revision,
                    started_at=current_time,
                    publication_role="assistant_result",
                    parent_publication_id=acceptance_commit_id,
                    execution_base_publication_id=acceptance_commit_id,
                    execution_base_render_revision=next_revision,
                    execution_base_components_sha256=component_digest,
                    execution_base_layouts_sha256=layout_digest,
                )
            except RepositoryConflictError as exc:
                raise ConversationCommitConflict(
                    "voice acceptance publication lost its commit fence"
                ) from exc
            result_revision = next_revision + 1
            self._insert_component_view(
                transaction,
                chat_id=chat_id,
                owner_user_id=owner_user_id,
                commit_id=result_commit_id,
                revision=result_revision,
                components=components,
                current_ms=current_ms,
            )
            self._insert_layout_view(
                transaction,
                chat_id=chat_id,
                owner_user_id=owner_user_id,
                commit_id=result_commit_id,
                revision=result_revision,
                layouts=layouts,
                current_ms=current_ms,
            )
            accepted_turn = accept_turn(
                transaction=transaction,
                message_id=message_id,
                acceptance_commit_id=acceptance_commit_id,
                result_commit_id=result_commit_id,
            )
        return {
            "acceptance": self._commit_record(acceptance),
            "result": self._commit_record(result),
            "message_id": message_id,
            "accepted_turn": accepted_turn,
            "components": components,
            "layouts": layouts,
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
        with self._transaction(operation_fence) as transaction:
            chat = self._history.conversations.get(
                transaction,
                owner_id=owner_user_id,
                conversation_id=chat_id,
                for_update=True,
            )
            if chat is None:
                raise ConversationNotFound("conversation not found")
            if operation_fence is not None:
                operation = self.operation_coordinator.assert_current_execution(
                    operation_fence,
                    transaction=transaction,
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
            existing = self._workspaces.publications.get_by_request(
                transaction,
                owner_id=owner_user_id,
                conversation_id=chat_id,
                request_generation=request_generation,
                for_update=True,
            )
            if existing is not None:
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
                    existing.operation_id
                    != supplied_operation
                    or existing.operation_execution_generation
                    != supplied_generation
                ):
                    raise ConversationCommitConflict(
                        "conversation request generation changed operation fence"
                    )
                return self._commit_record(existing)
            try:
                staged = self._workspaces.publications.stage(
                    transaction,
                    publication_id=commit_id,
                    owner_id=owner_user_id,
                    conversation_id=chat_id,
                    request_generation=request_generation,
                    operation_id=operation_id,
                    operation_execution_generation=operation_generation,
                    base_render_revision=chat.render_revision,
                    started_at=datetime.now(UTC),
                )
            except RepositoryNotFoundError as exc:
                raise ConversationNotFound("conversation not found") from exc
            except RepositoryConflictError as exc:
                raise ConversationCommitConflict(
                    "conversation publication was not staged"
                ) from exc
        return self._commit_record(staged)

    @staticmethod
    def _assert_matching_operation_fence(
        staged: Mapping[str, Any] | PublicationRecord,
        operation_fence: Any,
    ) -> None:
        if isinstance(staged, PublicationRecord):
            staged_operation = staged.operation_id
            staged_generation = staged.operation_execution_generation
        else:
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

    def _staged_for_update(
        self,
        transaction: Any,
        commit_id: str,
        owner_user_id: str,
    ) -> PublicationRecord:
        staged = self._workspaces.publications.get_for_owner(
            transaction,
            owner_id=owner_user_id,
            publication_id=commit_id,
            for_update=True,
        )
        if staged is None:
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
        with self._transaction(operation_fence) as transaction:
            staged = self._staged_for_update(
                transaction, commit_id, owner_user_id
            )
            if staged.state != "staged":
                raise ConversationCommitConflict("conversation commit is terminal")
            self._assert_matching_operation_fence(staged, operation_fence)
            chat = self._history.conversations.get(
                transaction,
                owner_id=owner_user_id,
                conversation_id=staged.conversation_id,
                for_update=True,
            )
            if chat is None:
                raise ConversationNotFound("conversation not found")
            base_revision = staged.base_render_revision
            publication_role = staged.publication_role
            if (
                publication_role != "assistant_result"
                and chat.render_revision != base_revision
            ):
                raise ConversationCommitConflict("conversation base revision changed")
            next_revision = base_revision + 1
            prepared = self._workspaces.canvas.list_scoped(
                transaction,
                owner_id=owner_user_id,
                conversation_id=staged.conversation_id,
                publication_id=commit_id,
                committed_render_revision=next_revision,
                expected_base_render_revision=base_revision,
            )
            if prepared:
                raise ConversationCommitConflict(
                    "conversation canvas stage was already prepared"
                )
            if base_revision == 0:
                rows = self._workspaces.canvas.list_current(
                    transaction,
                    owner_id=owner_user_id,
                    conversation_id=staged.conversation_id,
                )
            else:
                source_publication_id = (
                    staged.execution_base_publication_id
                    if publication_role == "assistant_result"
                    else chat.publication_id
                )
                if source_publication_id is None:
                    raise ConversationSnapshotInvalid(
                        "conversation component anchor is unavailable"
                    )
                rows = self._workspaces.canvas.list_for_publication(
                    transaction,
                    owner_id=owner_user_id,
                    conversation_id=staged.conversation_id,
                    publication_id=source_publication_id,
                    committed_render_revision=base_revision,
                    require_state="committed",
                )
            for position, row in enumerate(rows):
                raw = _strip_reserved_presentation(row.payload)
                if isinstance(raw, dict):
                    raw["component_id"] = row.component_id
                component = _canonical_component(raw, position)
                self._workspaces.canvas.create(
                    transaction,
                    CanvasComponentRecord(
                        row_id=str(self.uuid_factory()),
                        conversation_id=staged.conversation_id,
                        owner_id=owner_user_id,
                        component_id=component["component_id"],
                        payload=component,
                        component_type=component["type"],
                        title=row.title,
                        position=position,
                        created_at=row.created_at,
                        updated_at=(
                            row.created_at
                            if row.updated_at is None
                            else row.updated_at
                        ),
                        publication_id=commit_id,
                        committed_render_revision=next_revision,
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
        with self._transaction(operation_fence) as transaction:
            staged = self._staged_for_update(
                transaction, commit_id, owner_user_id
            )
            if staged.state != "staged":
                raise ConversationCommitConflict("conversation commit is terminal")
            self._assert_matching_operation_fence(staged, operation_fence)
            message_timestamp = message["timestamp"]
            if not isinstance(message_timestamp, int) or message_timestamp < 0:
                message_timestamp = None
            try:
                message_record = (
                    self._history.messages.append_next_to_staged_publication(
                        transaction,
                        owner_id=owner_user_id,
                        conversation_id=staged.conversation_id,
                        publication_id=commit_id,
                        role=message["role"],
                        content=message["content"],
                        timestamp=message_timestamp,
                    )
                )
            except RepositoryNotFoundError as exc:
                raise ConversationNotFound("conversation not found") from exc
            except RepositoryConflictError as exc:
                raise ConversationCommitConflict(
                    "conversation message stage changed"
                ) from exc
            message_id = str(message_record.message_id)
            self._link_attachments(
                transaction,
                chat_id=staged.conversation_id,
                message_id=message_id,
                owner_user_id=owner_user_id,
                attachment_ids=message["attachments"],
                created_at_ms=message_record.timestamp,
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
        with self._transaction() as transaction:
            staged = self._staged_for_update(
                transaction, commit_id, owner_user_id
            )
            if staged.state == "committed":
                return self._commit_record(staged)
            if staged.state == "aborted":
                return self._commit_record(staged)
            try:
                aborted = self._workspaces.publications.abort(
                    transaction,
                    owner_id=owner_user_id,
                    conversation_id=staged.conversation_id,
                    publication_id=commit_id,
                    aborted_at=datetime.now(UTC),
                )
            except RepositoryConflictError as exc:
                raise ConversationCommitConflict(
                    "conversation abort lost its terminal CAS"
                ) from exc
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
        transaction: Any,
        *,
        chat_id: str,
        message_id: str,
        owner_user_id: str,
        attachment_ids: Sequence[Any],
        created_at_ms: int,
    ) -> None:
        for raw_attachment_id in attachment_ids:
            attachment_id = _uuid4_text(raw_attachment_id, "attachment_id")
            attachment = self._artifacts.attachments.get(
                transaction,
                owner_id=owner_user_id,
                attachment_id=attachment_id,
            )
            if attachment is None:
                raise ValueError("attachment is unavailable")
            self._artifacts.message_attachments.link(
                transaction,
                link_id=str(self.uuid_factory()),
                owner_id=owner_user_id,
                conversation_id=chat_id,
                message_id=message_id,
                attachment_id=attachment_id,
                created_at=created_at_ms,
            )

    def publish_voice_result(
        self,
        *,
        commit_id: Any,
        owner_user_id: str,
        canvas_components: Sequence[Mapping[str, Any]],
        canvas_layouts: Optional[Sequence[Mapping[str, Any]]] = None,
        operation_fence: Any,
    ) -> dict[str, Any]:
        """Three-way publish one private assistant result exactly once."""

        commit_id = _uuid4_text(commit_id, "commit_id")
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        if operation_fence is None:
            raise ValueError("voice result publication requires an execution fence")
        candidate_components = self._validate_canvas(canvas_components)
        candidate_layouts = self._validate_layouts(canvas_layouts)

        with self._transaction(operation_fence) as transaction:
            staged = self._staged_for_update(
                transaction, commit_id, owner_user_id
            )
            if staged.state == "committed":
                return self._commit_record(staged)
            if staged.state != "staged":
                raise ConversationCommitConflict("conversation commit is terminal")
            if staged.publication_role != "assistant_result":
                raise ConversationCommitConflict(
                    "voice result publication role changed"
                )
            self._assert_matching_operation_fence(staged, operation_fence)
            self.operation_coordinator.assert_current_execution(
                operation_fence,
                transaction=transaction,
            )
            chat = self._history.conversations.get(
                transaction,
                owner_id=owner_user_id,
                conversation_id=staged.conversation_id,
                for_update=True,
            )
            if chat is None:
                raise ConversationNotFound("conversation not found")

            execution_base_revision = staged.execution_base_render_revision or 0
            execution_base_commit_id = staged.execution_base_publication_id
            if execution_base_revision < 1 or execution_base_commit_id is None:
                raise ConversationSnapshotInvalid(
                    "voice result execution base is unavailable"
                )
            base_components = self._load_component_view(
                transaction,
                chat_id=staged.conversation_id,
                owner_user_id=owner_user_id,
                commit_id=execution_base_commit_id,
                revision=execution_base_revision,
                require_state="committed",
            )
            base_layouts = self._load_layout_view(
                transaction,
                chat_id=staged.conversation_id,
                owner_user_id=owner_user_id,
                commit_id=execution_base_commit_id,
                revision=execution_base_revision,
                require_state="committed",
            )
            if (
                canonical_components_sha256(base_components)
                != (staged.execution_base_components_sha256 or "")
                or canonical_layouts_sha256(base_layouts)
                != (staged.execution_base_layouts_sha256 or "")
            ):
                raise ConversationSnapshotInvalid(
                    "voice result execution base digest changed"
                )

            staged_components = self._load_component_view(
                transaction,
                chat_id=staged.conversation_id,
                owner_user_id=owner_user_id,
                commit_id=commit_id,
                revision=staged.base_render_revision + 1,
                require_state="staged",
            )
            if canonical_components_sha256(staged_components) != (
                canonical_components_sha256(candidate_components)
            ):
                raise ConversationSnapshotInvalid(
                    "voice result candidate component stage changed"
                )

            latest_revision = chat.render_revision
            latest_commit_id = (
                None if latest_revision == 0 else chat.publication_id
            )
            if latest_revision > 0 and latest_commit_id is None:
                raise ConversationSnapshotInvalid(
                    "latest conversation commit anchor is unavailable"
                )
            latest_components = self._load_component_view(
                transaction,
                chat_id=staged.conversation_id,
                owner_user_id=owner_user_id,
                commit_id=latest_commit_id,
                revision=latest_revision,
                require_state=(
                    None if latest_revision == 0 else "committed"
                ),
            )
            latest_layouts = self._load_layout_view(
                transaction,
                chat_id=staged.conversation_id,
                owner_user_id=owner_user_id,
                commit_id=latest_commit_id,
                revision=latest_revision,
                require_state=(
                    None if latest_revision == 0 else "committed"
                ),
            )
            merge = merge_conversation_publication(
                base_components=base_components,
                candidate_components=candidate_components,
                latest_components=latest_components,
                base_layouts=base_layouts,
                candidate_layouts=candidate_layouts,
                latest_layouts=latest_layouts,
            )
            rebased_components = tuple(
                PublicationRebaseComponent(
                    row_id=str(self.uuid_factory()),
                    component_id=component["component_id"],
                    payload=copy.deepcopy(component),
                    component_type=component["type"],
                    title=str(
                        component.get("title") or component["type"]
                    )[:255],
                    position=position,
                )
                for position, component in enumerate(merge.components)
            )
            rebased_layouts = tuple(
                PublicationRebaseLayout(
                    layout_key=layout["layout_key"],
                    position=layout["position"],
                    tree=copy.deepcopy(layout["layout"]),
                )
                for layout in merge.layouts
            )
            current_time = datetime.now(UTC)
            current_ms = int(current_time.timestamp() * 1000)
            try:
                self._workspaces.publications.rebase_assistant_stage(
                    transaction,
                    owner_id=owner_user_id,
                    conversation_id=staged.conversation_id,
                    publication_id=commit_id,
                    expected_staged_base_render_revision=(
                        staged.base_render_revision
                    ),
                    expected_head_render_revision=latest_revision,
                    expected_head_publication_id=latest_commit_id,
                    components=rebased_components,
                    layouts=rebased_layouts,
                    append_conflict_notice=merge.conflicted,
                )
                committed = self._workspaces.publications.commit_at_head(
                    transaction,
                    owner_id=owner_user_id,
                    conversation_id=staged.conversation_id,
                    publication_id=commit_id,
                    expected_staged_base_render_revision=(
                        staged.base_render_revision
                    ),
                    expected_head_render_revision=latest_revision,
                    expected_head_publication_id=latest_commit_id,
                    committed_at=current_time,
                    updated_at=current_ms,
                )
            except RepositoryNotFoundError as exc:
                raise ConversationNotFound("conversation not found") from exc
            except RepositoryConflictError as exc:
                raise ConversationCommitConflict(
                    "voice result revision CAS is stale"
                ) from exc

            from orchestrator.work_admission import OperationState

            self.operation_coordinator.terminalize(
                operation_fence,
                state=OperationState.COMPLETED,
                terminal_code=None,
                safe_summary="Conversation committed",
                retry_after_ms=None,
                transaction=transaction,
            )
        return self._commit_record(committed)
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

        with self._transaction(operation_fence) as transaction:
            staged = self._staged_for_update(
                transaction, commit_id, owner_user_id
            )
            if staged.state == "committed":
                return self._commit_record(staged)
            if staged.state != "staged":
                raise ConversationCommitConflict("conversation commit is terminal")
            self._assert_matching_operation_fence(staged, operation_fence)
            chat = self._history.conversations.get(
                transaction,
                owner_id=owner_user_id,
                conversation_id=staged.conversation_id,
                for_update=True,
            )
            if chat is None:
                raise ConversationNotFound("conversation not found")
            base_revision = staged.base_render_revision
            if chat.render_revision != base_revision:
                raise ConversationCommitConflict(
                    "conversation base revision changed"
                )
            next_revision = base_revision + 1
            current_time = datetime.now(UTC)
            current_ms = int(current_time.timestamp() * 1000)

            try:
                prepared = self._workspaces.publications.validate_stage(
                    transaction,
                    owner_id=owner_user_id,
                    conversation_id=staged.conversation_id,
                    publication_id=commit_id,
                )
                if validated_messages is None:
                    positions = [item.position for item in prepared.messages]
                    if positions != list(range(len(positions))) or any(
                        item.committed_render_revision != next_revision
                        for item in prepared.messages
                    ):
                        raise ConversationSnapshotInvalid(
                            "staged conversation messages are incomplete"
                        )
                else:
                    if prepared.messages:
                        raise ConversationCommitConflict(
                            "conversation messages were already prepared"
                        )
                    for message in validated_messages:
                        timestamp = message["timestamp"]
                        if not isinstance(timestamp, int) or timestamp < 0:
                            timestamp = None
                        record = (
                            self._history.messages.append_next_to_staged_publication(
                                transaction,
                                owner_id=owner_user_id,
                                conversation_id=staged.conversation_id,
                                publication_id=commit_id,
                                role=message["role"],
                                content=message["content"],
                                timestamp=timestamp,
                            )
                        )
                        self._link_attachments(
                            transaction,
                            chat_id=staged.conversation_id,
                            message_id=str(record.message_id),
                            owner_user_id=owner_user_id,
                            attachment_ids=message["attachments"],
                            created_at_ms=record.timestamp,
                        )
                if fault_hook is not None:
                    fault_hook("after_messages")

                prepared_canvas = self._workspaces.canvas.list_scoped(
                    transaction,
                    owner_id=owner_user_id,
                    conversation_id=staged.conversation_id,
                    publication_id=commit_id,
                    committed_render_revision=next_revision,
                    expected_base_render_revision=base_revision,
                )
                if prepared_canvas:
                    prepared_identities = [
                        record.component_id for record in prepared_canvas
                    ]
                    validated_identities = [
                        component["component_id"]
                        for component in validated_canvas
                    ]
                    if prepared_identities != validated_identities:
                        raise ConversationSnapshotInvalid(
                            "staged conversation canvas is incomplete"
                        )
                    for record, component in zip(
                        prepared_canvas, validated_canvas, strict=True
                    ):
                        self._workspaces.canvas.replace(
                            transaction,
                            owner_id=owner_user_id,
                            conversation_id=staged.conversation_id,
                            component_id=record.component_id,
                            payload=component,
                            component_type=component["type"],
                            title=str(
                                component.get("title") or component["type"]
                            )[:255],
                            expected_updated_at=record.updated_at,
                            updated_at=max(current_ms, record.updated_at + 1),
                            publication_id=commit_id,
                            committed_render_revision=next_revision,
                        )
                else:
                    self._insert_component_view(
                        transaction,
                        chat_id=staged.conversation_id,
                        owner_user_id=owner_user_id,
                        commit_id=commit_id,
                        revision=next_revision,
                        components=validated_canvas,
                        current_ms=current_ms,
                    )

                prepared_layouts = self._workspaces.layouts.list_scoped(
                    transaction,
                    owner_id=owner_user_id,
                    conversation_id=staged.conversation_id,
                    publication_id=commit_id,
                    committed_render_revision=next_revision,
                    expected_base_render_revision=base_revision,
                )
                for record in prepared_layouts:
                    removed = self._workspaces.layouts.remove(
                        transaction,
                        owner_id=owner_user_id,
                        conversation_id=staged.conversation_id,
                        layout_key=record.layout_key,
                        publication_id=commit_id,
                        committed_render_revision=next_revision,
                    )
                    if not removed:
                        raise ConversationCommitConflict(
                            "staged conversation layout changed"
                        )
                self._insert_layout_view(
                    transaction,
                    chat_id=staged.conversation_id,
                    owner_user_id=owner_user_id,
                    commit_id=commit_id,
                    revision=next_revision,
                    layouts=validated_layouts,
                    current_ms=current_ms,
                )
                if fault_hook is not None:
                    fault_hook("after_canvas")
                    fault_hook("before_publish")

                summary = self._workspaces.publications.validate_stage(
                    transaction,
                    owner_id=owner_user_id,
                    conversation_id=staged.conversation_id,
                    publication_id=commit_id,
                )
                message_positions = [item.position for item in summary.messages]
                component_identities = [
                    item.identity for item in summary.components
                ]
                layout_identities = [item.identity for item in summary.layouts]
                if (
                    message_positions != list(range(len(message_positions)))
                    or any(
                        item.committed_render_revision != next_revision
                        for item in (
                            *summary.messages,
                            *summary.components,
                            *summary.layouts,
                        )
                    )
                    or component_identities
                    != [
                        component["component_id"]
                        for component in validated_canvas
                    ]
                    or layout_identities
                    != [layout["layout_key"] for layout in validated_layouts]
                ):
                    raise ConversationSnapshotInvalid(
                        "staged conversation publication is incomplete"
                    )
                committed = self._workspaces.publications.commit_at_head(
                    transaction,
                    owner_id=owner_user_id,
                    conversation_id=staged.conversation_id,
                    publication_id=commit_id,
                    expected_staged_base_render_revision=base_revision,
                    expected_head_render_revision=base_revision,
                    expected_head_publication_id=chat.publication_id,
                    committed_at=current_time,
                    updated_at=current_ms,
                )
            except RepositoryNotFoundError as exc:
                raise ConversationNotFound("conversation not found") from exc
            except RepositoryConflictError as exc:
                raise ConversationCommitConflict(
                    "conversation publication lost its CAS"
                ) from exc

            if operation_fence is not None:
                from orchestrator.work_admission import OperationState

                self.operation_coordinator.terminalize(
                    operation_fence,
                    state=OperationState.COMPLETED,
                    terminal_code=None,
                    safe_summary="Conversation committed",
                    retry_after_ms=None,
                    transaction=transaction,
                )
        return self._commit_record(committed)
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
        request_generation = _uuid4_text(
            request_generation, "request_generation"
        )
        if snapshot_purpose not in {"hydration", "commit"}:
            raise ValueError("snapshot_purpose is invalid")
        return self._build_snapshot_typed(
            chat_id=chat_id,
            owner_user_id=owner_user_id,
            connection_generation=connection_generation,
            request_generation=request_generation,
            snapshot_purpose=snapshot_purpose,
        )
    def _build_snapshot_typed(
        self,
        *,
        chat_id: str,
        owner_user_id: str,
        connection_generation: str,
        request_generation: str,
        snapshot_purpose: str,
    ) -> dict[str, Any]:
        """Build one repeatable-read snapshot exclusively through Plane."""

        with self.plane_runtime.transaction(
            isolation=IsolationLevel.REPEATABLE_READ
        ) as transaction:
            chat = self._history.conversations.get(
                transaction,
                owner_id=owner_user_id,
                conversation_id=chat_id,
            )
            if chat is None:
                raise ConversationNotFound("conversation not found")
            render_revision = chat.render_revision
            committed_at: Any = chat.updated_at
            current_publication = None
            if render_revision > 0:
                if chat.publication_id is None:
                    raise ConversationSnapshotInvalid(
                        "current conversation commit anchor is unavailable"
                    )
                current_publication = self._workspaces.publications.get(
                    transaction,
                    owner_id=owner_user_id,
                    conversation_id=chat_id,
                    publication_id=chat.publication_id,
                )
                if not (
                    current_publication is not None
                    and current_publication.state == "committed"
                    and current_publication.committed_render_revision
                    == render_revision
                    and current_publication.committed_at is not None
                ):
                    raise ConversationSnapshotInvalid(
                        "current conversation commit anchor is inconsistent"
                    )
                committed_at = current_publication.committed_at
            elif chat.snapshot_committed_at is not None:
                raise ConversationSnapshotInvalid(
                    "revision-zero conversation has committed snapshot metadata"
                )
            if snapshot_purpose == "commit":
                if current_publication is None:
                    raise ConversationSnapshotInvalid(
                        "commit snapshot has no committed revision"
                    )
                if current_publication.request_generation != request_generation:
                    raise ConversationSnapshotInvalid(
                        "commit snapshot request generation does not match its commit"
                    )

            message_records = self._history.messages.list_visible(
                transaction,
                owner_id=owner_user_id,
                conversation_id=chat_id,
                through_render_revision=render_revision,
                limit=1000,
            )
            if len(message_records) >= 1000:
                raise ConversationSnapshotInvalid(
                    "conversation transcript exceeds the supported bound"
                )
            transcript = []
            for message in message_records:
                # Plane's typed repository has already decoded the legacy TEXT
                # representation.  Parsing semantic strings again would turn
                # newly persisted prose such as ``"[]"`` or ``"null"`` into a
                # different JSON type at the orchestration boundary.
                parts = _rail_parts(
                    _content_parts(message.content, already_decoded=True)
                )
                if not parts:
                    continue
                transcript.append(
                    {
                        "message_id": str(message.message_id),
                        "role": message.role,
                        "created_at": _rfc3339(message.timestamp),
                        "parts": parts,
                        "attachments": self._typed_attachments(
                            transaction,
                            message_id=message.message_id,
                            owner_user_id=owner_user_id,
                        ),
                    }
                )

            component_records = self._workspaces.canvas.list_current(
                transaction,
                owner_id=owner_user_id,
                conversation_id=chat_id,
            )
            components = []
            for position, record in enumerate(component_records):
                raw = _strip_reserved_presentation(record.payload)
                if isinstance(raw, dict):
                    raw["component_id"] = record.component_id
                components.append(_canonical_component(raw, position))
            layout_records = self._workspaces.layouts.list_current(
                transaction,
                owner_id=owner_user_id,
                conversation_id=chat_id,
            )
            if layout_records:
                from orchestrator import ui_designer
                from orchestrator.workspace import iter_layout_refs

                layouts = []
                for record in layout_records:
                    tree = _strip_reserved_presentation(record.tree)
                    if not isinstance(tree, list):
                        raise ConversationSnapshotInvalid(
                            "saved canvas layout is malformed"
                        )
                    layouts.append(
                        {
                            "layout_key": record.layout_key,
                            "position": record.position,
                            "layout": tree,
                        }
                    )
                by_id = {
                    component["component_id"]: component
                    for component in components
                }
                claimed = {
                    component_id
                    for layout in layouts
                    for component_id in iter_layout_refs(layout["layout"])
                }
                stream = [
                    (record.position, 0, [components[position]])
                    for position, record in enumerate(component_records)
                    if components[position]["component_id"] not in claimed
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
            return {
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

    def _typed_attachments(
        self,
        transaction: Any,
        *,
        message_id: int,
        owner_user_id: str,
    ) -> list[dict[str, str]]:
        links = self._artifacts.message_attachments.list_for_message(
            transaction,
            owner_id=owner_user_id,
            message_id=message_id,
        )
        output = []
        for link in links:
            attachment = self._artifacts.attachments.get(
                transaction,
                owner_id=owner_user_id,
                attachment_id=link.attachment_id,
            )
            if attachment is None:
                continue
            output.append(
                {
                    "attachment_id": attachment.attachment_id,
                    "filename": attachment.filename,
                    "category": attachment.category,
                }
            )
        return output

    def committed_assistant_content(
        self,
        *,
        commit_id: Any,
        owner_user_id: str,
    ) -> Any | None:
        """Return the last assistant payload from one exact committed result."""

        commit_id = _uuid4_text(commit_id, "commit_id")
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        with self._transaction() as transaction:
            publication = self._workspaces.publications.get_for_owner(
                transaction,
                owner_id=owner_user_id,
                publication_id=commit_id,
            )
            if (
                publication is None
                or publication.state != "committed"
                or publication.publication_role != "assistant_result"
            ):
                return None
            record = (
                self._workspaces.publications.get_latest_committed_assistant_content(
                    transaction,
                    owner_id=owner_user_id,
                    publication_id=commit_id,
                )
            )
        return (
            None
            if record is None
            else _strip_reserved_presentation(record.content)
        )

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
        if not isinstance(item, Mapping):
            continue
        text = item.get("content") if item.get("type") == "text" else None
        if not isinstance(text, str) or not text.strip():
            text = item.get("title")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return " ".join(" ".join(parts).split())


class HistoryManager:
    def __init__(
        self,
        data_dir: str = "data",
        *,
        plane_runtime=None,
        plane_repositories=None,
    ):
        self.data_dir = data_dir
        legacy_json = os.path.join(data_dir, "chats.json")
        if os.path.isfile(legacy_json):
            raise RuntimeError(
                "legacy chats.json was detected; AstralDeep will not import or "
                "rename it during startup. Preserve the file and use a reviewed "
                "AstralPlane import/recovery procedure."
            )
        self.plane_runtime = plane_runtime
        self.plane_repositories = plane_repositories
        if plane_runtime is None:
            raise ValueError("HistoryManager requires the application Plane runtime")
        history_repository, runtime = repository_from(
            "history",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=None,
        )
        self._history = PlaneRepositoryContext(
            repository=history_repository,
            plane_runtime=runtime,
        )
        workspaces_repository, _ = repository_from(
            "workspaces",
            plane_runtime=runtime,
            repositories=plane_repositories,
            legacy_database=None,
        )
        self._workspaces = PlaneRepositoryContext(
            repository=workspaces_repository,
            plane_runtime=runtime,
        )
        artifacts_repository, _ = repository_from(
            "artifacts",
            plane_runtime=runtime,
            repositories=plane_repositories,
            legacy_database=None,
        )
        self._artifacts = PlaneRepositoryContext(
            repository=artifacts_repository,
            plane_runtime=runtime,
        )
        repository, runtime = repository_from(
            "conversation_files",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=None,
        )
        self._conversation_files = PlaneRepositoryContext(
            repository=repository,
            plane_runtime=runtime,
        )
        steps_repository, _ = repository_from(
            "chat_steps",
            plane_runtime=runtime,
            repositories=plane_repositories,
            legacy_database=None,
        )
        self._chat_steps = PlaneRepositoryContext(
            repository=steps_repository,
            plane_runtime=runtime,
        )

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
        self._history.call(
            self._history.repository.conversations.create,
            conversation_id=chat_id,
            owner_id=user_id,
            title="New Chat",
            agent_id=agent_id,
            created_at=timestamp,
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
        with self._history.transaction() as transaction:
            conversations = self._history.repository.conversations
            messages = self._history.repository.messages
            chat = conversations.get(
                transaction,
                owner_id=user_id,
                conversation_id=chat_id,
                for_update=True,
            )
            if chat is None:
                logger.warning(
                    "Attempted to add message to non-existent chat %s", chat_id
                )
                return
            if chat.render_revision > 0:
                raise RuntimeError(
                    "revisioned conversation messages require a publication stage"
                )
            title = chat.title
            if role == "user" and messages.latest_visible_id(
                transaction,
                owner_id=user_id,
                conversation_id=chat_id,
            ) is None:
                display_content = str(content)
                title = (
                    display_content[:30] + "..."
                    if len(display_content) > 30
                    else display_content
                )
            messages.append(
                transaction,
                owner_id=user_id,
                conversation_id=chat_id,
                role=role,
                content=content,
                timestamp=timestamp,
            )
            conversations.rename(
                transaction,
                owner_id=user_id,
                conversation_id=chat_id,
                title=title,
                expected_updated_at=chat.updated_at,
                updated_at=max(timestamp, chat.updated_at + 1),
            )

    def get_latest_message_id(self, chat_id: str, user_id: str = 'legacy'):
        """Return the integer id of the most recent message in a chat.

        Added for feature 014 — chat-step recorder needs the
        ``messages.id`` of the user message that initiated a turn so step
        rows can FK back to it (see chat_steps.turn_message_id). Returns
        ``None`` if the chat has no messages.
        """
        return self._history.call(
            self._history.repository.messages.latest_visible_id,
            owner_id=user_id,
            conversation_id=chat_id,
        )

    def get_chat_agent(
        self,
        chat_id: str,
        user_id: str = "legacy",
    ) -> Optional[str]:
        """Return one owner's bound agent without loading conversation content."""

        conversation = self._history.call(
            self._history.repository.conversations.get,
            owner_id=user_id,
            conversation_id=chat_id,
        )
        return None if conversation is None else conversation.agent_id

    def get_conversation_record(
        self,
        chat_id: str,
        user_id: str = "legacy",
    ):
        """Return one owner-scoped detached Plane conversation record.

        Callers that only need authority or revision metadata must not load the
        conversation's bounded message inventory through :meth:`get_chat`.
        """

        return self._history.call(
            self._history.repository.conversations.get,
            owner_id=user_id,
            conversation_id=chat_id,
        )

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
        with self._history.transaction() as transaction:
            conversations = self._history.repository.conversations
            chat = conversations.get(
                transaction,
                owner_id=user_id,
                conversation_id=chat_id,
                for_update=True,
            )
            if chat is None:
                return
            conversations.rename(
                transaction,
                owner_id=user_id,
                conversation_id=chat_id,
                title=title,
                expected_updated_at=chat.updated_at,
                updated_at=max(timestamp, chat.updated_at + 1),
            )

    def get_chat(self, chat_id: str, user_id: str = 'legacy') -> Optional[Dict]:
        """Get full details of a specific chat."""
        stage = current_scheduled_history_stage()
        publication_stage = current_conversation_publication()
        chat_record = self._history.call(
            self._history.repository.conversations.get,
            owner_id=user_id,
            conversation_id=chat_id,
        )
        if chat_record is None:
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
            chat_id_value = chat_id
            title_value = "New Chat"
            agent_id_value = stage.agent_id
            created_at_value = first_timestamp
            updated_at_value = first_timestamp
        else:
            chat_id_value = chat_record.conversation_id
            title_value = chat_record.title
            agent_id_value = chat_record.agent_id
            created_at_value = chat_record.created_at
            updated_at_value = chat_record.updated_at

        publication_revision = None
        if (
            publication_stage is not None
            and publication_stage.matches(self, chat_id, user_id)
        ):
            publication_revision = int(
                publication_stage.execution_base_render_revision
            )
        message_records = self._history.call(
            self._history.repository.messages.list_visible,
            owner_id=user_id,
            conversation_id=chat_id,
            through_render_revision=publication_revision,
            limit=1000,
        )
        if len(message_records) >= 1000:
            raise ConversationSnapshotInvalid(
                "conversation message inventory exceeds the supported bound"
            )
        messages = []
        for message in message_records:
            messages.append({
                "id": message.message_id,
                "role": message.role,
                "content": _mutable_json_value(message.content),
                "timestamp": message.timestamp,
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
            "id": chat_id_value,
            "title": (
                stage.requested_title
                if stage is not None
                and stage.matches(self, chat_id, user_id)
                and stage.requested_title is not None
                else title_value
            ),
            "agent_id": agent_id_value,
            "created_at": created_at_value,
            "updated_at": updated_at_value,
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
        rows = self._history.call(
            self._history.repository.conversations.list_recent_nonempty,
            owner_id=user_id,
            limit=limit,
        )

        results = []
        for row in rows:
            content = row.latest_message_content
            preview = ""
            if content is not None:
                content_obj = content
                if isinstance(content_obj, str):
                    preview = content_obj
                elif isinstance(content_obj, (list, tuple)):
                    preview = _component_preview_text(content_obj)
                elif isinstance(content_obj, Mapping):
                    preview = _component_preview_text([content_obj])
                else:
                    preview = str(content_obj)
                # 066: a preview is an excerpt of PROSE — strip the markdown
                # so the list never shows literal "**asterisks**". The single
                # choke point for every consumer (web surface, history_list
                # frame, REST /api/chats, voice extraction).
                from webrender.sanitize import plain_md

                preview = plain_md(preview)
                if len(preview) > PREVIEW_MAX_CHARS:
                    preview = preview[:PREVIEW_MAX_CHARS] + "..."

            results.append({
                "id": row.conversation_id,
                "title": row.title,
                "agent_id": row.agent_id,
                "updated_at": row.updated_at,
                "preview": preview,
                "has_saved_components": row.has_saved_components,
            })

        return results
    
    def delete_chat(self, chat_id: str, user_id: str = 'legacy'):
        """Fence voice state and delete one owner chat atomically."""

        from orchestrator.voice_sessions import VoiceSessionRepository

        mutation = VoiceSessionRepository(
            plane_runtime=self.plane_runtime,
            plane_repositories=self.plane_repositories,
        ).mark_chat_unavailable(
            user_id=user_id,
            chat_id=chat_id,
            reason="deleted",
            delete_chat=True,
            now=datetime.now(UTC),
        )
        # component_version deliberately has no chats FK. Sweep it through
        # Plane's owner-scoped typed repository after the voice/chat mutation.
        self._artifacts.call(
            self._artifacts.repository.versions.delete_for_conversation,
            owner_id=user_id,
            conversation_id=chat_id,
        )
        return mutation

    def mark_chat_authorization_unavailable(
        self,
        chat_id: str,
        user_id: str = "legacy",
    ):
        """Fence voice publication/speech before an external access revocation.

        The caller remains responsible for the authorization-store mutation;
        this hook deliberately leaves normal chat content in place so both
        changes can be coordinated by the owning access-control workflow.
        """

        from orchestrator.voice_sessions import VoiceSessionRepository

        return VoiceSessionRepository(
            plane_runtime=self.plane_runtime,
            plane_repositories=self.plane_repositories,
        ).mark_chat_unavailable(
            user_id=user_id,
            chat_id=chat_id,
            reason="access_revoked",
            delete_chat=False,
            now=datetime.now(UTC),
        )

    # =========================================================================
    # Saved UI Components Methods
    # =========================================================================
    
    def save_component(self, chat_id: str, component_data: any, component_type: str, title: str = None, user_id: str = 'legacy') -> str:
        """Save one revision-zero component through the typed Plane canvas."""

        row_id = str(uuid.uuid4())
        payload = copy.deepcopy(component_data)
        semantic_id = (
            str(payload.get("component_id"))
            if isinstance(payload, Mapping) and payload.get("component_id")
            else row_id
        )
        created_at = int(time.time() * 1000)
        if not title:
            title = component_type.replace("_", " ").title()
        with self._workspaces.transaction() as transaction:
            conversation = self._history.repository.conversations.get(
                transaction,
                owner_id=user_id,
                conversation_id=chat_id,
                for_update=True,
            )
            if conversation is None:
                raise ConversationNotFound("conversation not found")
            if conversation.render_revision > 0:
                raise RuntimeError(
                    "revisioned workspace writes require a publication stage"
                )
            current = self._workspaces.repository.canvas.list_current(
                transaction,
                owner_id=user_id,
                conversation_id=chat_id,
            )
            position = 1 + max(
                (record.position or 0 for record in current), default=0
            )
            self._workspaces.repository.canvas.create(
                transaction,
                CanvasComponentRecord(
                    row_id=row_id,
                    conversation_id=chat_id,
                    owner_id=user_id,
                    component_id=semantic_id,
                    payload=payload,
                    component_type=component_type,
                    title=title,
                    position=position,
                    created_at=created_at,
                    updated_at=created_at,
                ),
            )
            self._workspaces.repository.canvas.sync_legacy_presence(
                transaction,
                owner_id=user_id,
                conversation_id=chat_id,
            )
        return row_id
    
    def get_saved_components(self, chat_id: str = None, user_id: str = 'legacy') -> List[Dict]:
        """Get saved components, optionally filtered by chat_id."""
        records = self._workspaces.call(
            self._workspaces.repository.canvas.list_current_for_owner,
            owner_id=user_id,
            conversation_id=chat_id,
            limit=1000,
        )
        if len(records) >= 1000:
            raise ConversationSnapshotInvalid(
                "saved component inventory exceeds the supported bound"
            )
        return [self._saved_component_dict(record) for record in records]
    
    def delete_component(self, component_id: str, user_id: str = 'legacy') -> bool:
        """Delete one current revision-zero component and its artifact history."""

        with self._workspaces.transaction() as transaction:
            visible = self._workspaces.repository.canvas.get_current_by_row_id(
                transaction,
                owner_id=user_id,
                row_id=component_id,
            )
            if visible is None:
                return False
            conversation = self._history.repository.conversations.get(
                transaction,
                owner_id=user_id,
                conversation_id=visible.conversation_id,
                for_update=True,
            )
            if conversation is None:
                return False
            if conversation.render_revision > 0:
                raise RuntimeError(
                    "revisioned workspace writes require a publication stage"
                )
            removed = self._workspaces.repository.canvas.remove_current_by_row_id(
                transaction,
                owner_id=user_id,
                row_id=component_id,
            )
            if removed is None:
                return False
            self._artifacts.repository.versions.delete_for_component(
                transaction,
                owner_id=user_id,
                conversation_id=removed.conversation_id,
                component_id=removed.component_id,
            )
            self._workspaces.repository.canvas.sync_legacy_presence(
                transaction,
                owner_id=user_id,
                conversation_id=removed.conversation_id,
            )
        return True
    
    def get_component_by_id(self, component_id: str, user_id: str = 'legacy') -> Optional[Dict]:
        """Get a single saved component by ID."""
        record = self._workspaces.call(
            self._workspaces.repository.canvas.get_current_by_row_id,
            owner_id=user_id,
            row_id=component_id,
        )
        return None if record is None else self._saved_component_dict(record)

    def replace_components(self, old_ids: list, new_components: list, chat_id: str, user_id: str = 'legacy') -> list:
        """Atomically replace revision-zero rows through Plane typed records."""

        if len(set(old_ids)) != len(old_ids):
            raise ValueError("old component ids must be unique")
        with self._workspaces.transaction() as transaction:
            conversation = self._history.repository.conversations.get(
                transaction,
                owner_id=user_id,
                conversation_id=chat_id,
                for_update=True,
            )
            if conversation is None:
                raise ConversationNotFound("conversation not found")
            if conversation.render_revision > 0:
                raise RuntimeError(
                    "revisioned workspace writes require a publication stage"
                )
            old_records = []
            for old_id in old_ids:
                record = self._workspaces.repository.canvas.get_current_by_row_id(
                    transaction,
                    owner_id=user_id,
                    row_id=old_id,
                    for_update=True,
                )
                if record is not None and record.conversation_id != chat_id:
                    raise ConversationNotFound("component conversation changed")
                if record is not None:
                    old_records.append(record)
            for record in old_records:
                removed = self._workspaces.repository.canvas.remove_current_by_row_id(
                    transaction,
                    owner_id=user_id,
                    row_id=record.row_id,
                )
                if removed is None:
                    raise ConversationCommitConflict(
                        "component replacement lost its row fence"
                    )
                self._artifacts.repository.versions.delete_for_component(
                    transaction,
                    owner_id=user_id,
                    conversation_id=chat_id,
                    component_id=removed.component_id,
                )

            current = self._workspaces.repository.canvas.list_current(
                transaction,
                owner_id=user_id,
                conversation_id=chat_id,
            )
            next_position = 1 + max(
                (record.position or 0 for record in current), default=0
            )
            created = []
            observed_at = int(time.time() * 1000)
            for offset, comp in enumerate(new_components):
                if not isinstance(comp, Mapping):
                    raise ValueError("new component must be an object")
                row_id = str(uuid.uuid4())
                payload = copy.deepcopy(comp.get("component_data", {}))
                semantic_id = (
                    str(payload.get("component_id"))
                    if isinstance(payload, Mapping) and payload.get("component_id")
                    else row_id
                )
                component_type = str(comp.get("component_type", "combined"))
                component_title = str(comp.get("title", "Combined Component"))
                created_at = observed_at + offset
                record = self._workspaces.repository.canvas.create(
                    transaction,
                    CanvasComponentRecord(
                        row_id=row_id,
                        conversation_id=chat_id,
                        owner_id=user_id,
                        component_id=semantic_id,
                        payload=payload,
                        component_type=component_type,
                        title=component_title,
                        position=next_position + offset,
                        created_at=created_at,
                        updated_at=created_at,
                    ),
                )
                created.append(self._saved_component_dict(record))
            self._workspaces.repository.canvas.sync_legacy_presence(
                transaction,
                owner_id=user_id,
                conversation_id=chat_id,
            )
        return created

    def chat_has_saved_components(self, chat_id: str, user_id: str = 'legacy') -> bool:
        """Check if a chat has saved components."""
        conversation = self._history.call(
            self._history.repository.conversations.get,
            owner_id=user_id,
            conversation_id=chat_id,
        )
        return bool(
            conversation is not None and conversation.has_saved_components
        )

    @staticmethod
    def _saved_component_dict(record: CanvasComponentRecord) -> Dict[str, Any]:
        return {
            "id": record.row_id,
            "chat_id": record.conversation_id,
            "component_data": _strip_reserved_presentation(record.payload),
            "component_type": record.component_type,
            "title": record.title,
            "created_at": record.created_at,
        }

    def add_file_mapping(self, chat_id: str, original_name: str, backend_path: str, user_id: str = 'legacy'):
        """Register a mapping between an original filename and its backend UUID path."""
        import time
        timestamp = int(time.time() * 1000)
        self._conversation_files.call(
            self._conversation_files.repository.add_mapping,
            owner_id=user_id,
            conversation_id=chat_id,
            original_name=original_name,
            backend_path=backend_path,
            uploaded_at=timestamp,
        )

    def get_file_mappings(self, chat_id: str, user_id: str = 'legacy') -> List[Dict]:
        """Retrieve all file mappings for a chat."""
        records = self._conversation_files.call(
            self._conversation_files.repository.list_mappings,
            owner_id=user_id,
            conversation_id=chat_id,
            limit=1000,
        )
        return [
            {
                "original_name": record.original_name,
                "backend_path": record.backend_path,
            }
            for record in records
        ]

    def list_chat_steps(self, chat_id: str, user_id: str = "legacy"):
        """Return one owner's detached durable progress trail from Plane."""

        return self._chat_steps.call(
            self._chat_steps.repository.list_steps,
            owner_id=user_id,
            conversation_id=chat_id,
            limit=1000,
        )
