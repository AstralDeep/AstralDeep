"""Task-local contextvar selecting the staged, not-yet-committed canvas rows for the
current conversation commit, read by workspace.py and history.py; does not itself own
the database transaction or publish.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


_SUMMARY_SOURCE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_SUMMARY_TEXT_CHARS = 8_000


def _uuid4_text(value: Any, field_name: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID4") from exc
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise ValueError(f"{field_name} must be a UUID4")
    return str(parsed)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("conversation publication value is not canonical JSON") from exc


def _collection_digest(values: Sequence[Mapping[str, Any]]) -> str:
    payload = [_canonical_json(value) for value in values]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def canonical_components_sha256(
    components: Sequence[Mapping[str, Any]],
) -> str:
    return _collection_digest(components)


def canonical_layouts_sha256(layouts: Sequence[Mapping[str, Any]]) -> str:
    return _collection_digest(layouts)


@dataclass(frozen=True, slots=True)
class ConversationCompletionSummary:
    summary_text: str
    summary_source: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.summary_text, str)
            or not self.summary_text.strip()
            or len(self.summary_text) > _MAX_SUMMARY_TEXT_CHARS
            or any(
                ord(character) < 32 and character not in "\n\r\t"
                for character in self.summary_text
            )
        ):
            raise ValueError("summary_text is invalid")
        if (
            not isinstance(self.summary_source, str)
            or _SUMMARY_SOURCE.fullmatch(self.summary_source) is None
        ):
            raise ValueError("summary_source is invalid")
        object.__setattr__(self, "summary_text", self.summary_text.strip())


# Ignore a notification's summary field — not authoritative
def completion_summary_from_content(
    content: Any,
) -> ConversationCompletionSummary | None:
    values = content if isinstance(content, list) else [content]
    for value in values:
        if not isinstance(value, Mapping):
            continue
        if "summary_text" not in value and "summary_source" not in value:
            continue
        try:
            return ConversationCompletionSummary(
                summary_text=value.get("summary_text"),
                summary_source=value.get("summary_source"),
            )
        except (TypeError, ValueError):
            return None
    return None


def _identity_map(
    values: Sequence[Mapping[str, Any]],
    *,
    key: str,
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            raise ValueError("conversation publication value must be an object")
        identity = value.get(key)
        if not isinstance(identity, str) or not identity or len(identity) > 512:
            raise ValueError(f"conversation publication {key} is invalid")
        if identity in output:
            raise ValueError(f"conversation publication {key} is duplicated")
        output[identity] = copy.deepcopy(dict(value))
    return output


def _ordered_identities(
    *views: Sequence[Mapping[str, Any]],
    key: str,
) -> Iterable[str]:
    seen: set[str] = set()
    for view in views:
        for value in view:
            identity = value.get(key)
            if isinstance(identity, str) and identity not in seen:
                seen.add(identity)
                yield identity


def _iter_layout_refs(value: Any) -> Iterable[str]:
    if isinstance(value, list):
        for item in value:
            yield from _iter_layout_refs(item)
        return
    if not isinstance(value, Mapping):
        return
    if value.get("type") == "ref":
        component_id = value.get("component_id")
        if isinstance(component_id, str) and component_id:
            yield component_id
        return
    for key in ("children", "content"):
        nested = value.get(key)
        if isinstance(nested, list):
            yield from _iter_layout_refs(nested)
    tabs = value.get("tabs")
    if isinstance(tabs, list):
        for tab in tabs:
            if isinstance(tab, Mapping):
                yield from _iter_layout_refs(tab.get("content"))


@dataclass(frozen=True, slots=True)
class ConversationPublicationMerge:
    components: tuple[dict[str, Any], ...]
    layouts: tuple[dict[str, Any], ...]
    component_conflicts: tuple[str, ...]
    layout_conflicts: tuple[str, ...]

    @property
    def conflicted(self) -> bool:
        return bool(self.component_conflicts or self.layout_conflicts)


def _merge_keyed_view(
    base: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
    latest: Sequence[Mapping[str, Any]],
    *,
    key: str,
) -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
    base_by_id = _identity_map(base, key=key)
    candidate_by_id = _identity_map(candidate, key=key)
    latest_by_id = _identity_map(latest, key=key)
    merged: list[dict[str, Any]] = []
    conflicts: list[str] = []
    sources: dict[str, str] = {}
    for identity in _ordered_identities(latest, candidate, base, key=key):
        base_value = base_by_id.get(identity)
        candidate_value = candidate_by_id.get(identity)
        latest_value = latest_by_id.get(identity)
        base_digest = None if base_value is None else _canonical_json(base_value)
        candidate_digest = (
            None if candidate_value is None else _canonical_json(candidate_value)
        )
        latest_digest = None if latest_value is None else _canonical_json(latest_value)
        candidate_changed = candidate_digest != base_digest
        latest_changed = latest_digest != base_digest
        if (
            candidate_changed
            and latest_changed
            and candidate_digest != latest_digest
        ):
            selected = latest_value
            source = "latest"
            conflicts.append(identity)
        elif candidate_changed:
            selected = candidate_value
            source = "candidate"
        else:
            selected = latest_value
            source = "latest"
        if selected is not None:
            merged.append(copy.deepcopy(selected))
            sources[identity] = source
    return merged, conflicts, sources


def merge_conversation_publication(
    *,
    base_components: Sequence[Mapping[str, Any]],
    candidate_components: Sequence[Mapping[str, Any]],
    latest_components: Sequence[Mapping[str, Any]],
    base_layouts: Sequence[Mapping[str, Any]],
    candidate_layouts: Sequence[Mapping[str, Any]],
    latest_layouts: Sequence[Mapping[str, Any]],
) -> ConversationPublicationMerge:
    components, component_conflicts, _component_sources = _merge_keyed_view(
        base_components,
        candidate_components,
        latest_components,
        key="component_id",
    )
    layouts, layout_conflicts, layout_sources = _merge_keyed_view(
        base_layouts,
        candidate_layouts,
        latest_layouts,
        key="layout_key",
    )
    component_ids = {
        str(component["component_id"]) for component in components
    }
    latest_layout_by_id = _identity_map(latest_layouts, key="layout_key")
    validated_layouts: list[dict[str, Any]] = []
    for layout in layouts:
        layout_key = str(layout["layout_key"])
        tree = layout.get("layout")
        valid = isinstance(tree, list) and set(_iter_layout_refs(tree)).issubset(
            component_ids
        )
        if valid:
            validated_layouts.append(layout)
            continue
        fallback = latest_layout_by_id.get(layout_key)
        fallback_tree = None if fallback is None else fallback.get("layout")
        fallback_valid = isinstance(fallback_tree, list) and set(
            _iter_layout_refs(fallback_tree)
        ).issubset(component_ids)
        if layout_sources.get(layout_key) == "candidate" and fallback_valid:
            validated_layouts.append(copy.deepcopy(fallback))
        if layout_key not in layout_conflicts:
            layout_conflicts.append(layout_key)

    return ConversationPublicationMerge(
        components=tuple(components),
        layouts=tuple(validated_layouts),
        component_conflicts=tuple(sorted(component_conflicts)),
        layout_conflicts=tuple(sorted(layout_conflicts)),
    )


@dataclass(slots=True)
class ConversationPublicationStage:
    history: Any
    commit_id: str
    chat_id: str
    user_id: str
    base_render_revision: int
    next_render_revision: int
    operation_fence: Any = None
    publication_role: str = "atomic"
    execution_base_render_revision: int | None = None
    summary_text: str | None = None
    summary_source: str | None = None
    layouts: list[dict[str, Any]] = field(default_factory=list)
    dirty: bool = False
    snapshot_cause: str | None = None
    sealed: bool = False
    committed: bool = False

    def __post_init__(self) -> None:
        if self.history is None:
            raise ValueError("history is required")
        self.commit_id = _uuid4_text(self.commit_id, "commit_id")
        self.chat_id = _uuid4_text(self.chat_id, "chat_id")
        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise ValueError("user_id is required")
        self.user_id = self.user_id.strip()
        if (
            isinstance(self.base_render_revision, bool)
            or not isinstance(self.base_render_revision, int)
            or self.base_render_revision < 0
        ):
            raise ValueError("base_render_revision must be a non-negative integer")
        if (
            isinstance(self.next_render_revision, bool)
            or not isinstance(self.next_render_revision, int)
            or self.next_render_revision != self.base_render_revision + 1
        ):
            raise ValueError("next_render_revision must equal base_render_revision + 1")
        if self.publication_role not in {
            "atomic",
            "user_acceptance",
            "assistant_result",
        }:
            raise ValueError("publication_role is invalid")
        if self.execution_base_render_revision is None:
            self.execution_base_render_revision = self.base_render_revision
        if (
            isinstance(self.execution_base_render_revision, bool)
            or not isinstance(self.execution_base_render_revision, int)
            or self.execution_base_render_revision < 0
        ):
            raise ValueError(
                "execution_base_render_revision must be a non-negative integer"
            )
        if self.summary_text is None and self.summary_source is None:
            pass
        elif self.summary_text is None or self.summary_source is None:
            raise ValueError(
                "summary_text and summary_source must be supplied together"
            )
        else:
            summary = ConversationCompletionSummary(
                summary_text=self.summary_text,
                summary_source=self.summary_source,
            )
            self.summary_text = summary.summary_text
            self.summary_source = summary.summary_source
        if not isinstance(self.layouts, list) or any(
            not isinstance(layout, dict) for layout in self.layouts
        ):
            raise ValueError("layouts must be an array of layout objects")
        self.layouts = copy.deepcopy(self.layouts)
        if self.committed and not self.sealed:
            raise ValueError("a committed stage must be sealed")

    def matches(self, history: Any, chat_id: str, user_id: str) -> bool:
        return (
            self.history is history
            and str(chat_id) == self.chat_id
            and str(user_id) == self.user_id
        )

    def ensure_mutable(self) -> None:
        if self.sealed:
            raise RuntimeError("conversation publication stage is sealed")

    def mark_dirty(self) -> None:
        self.ensure_mutable()
        self.dirty = True

    def set_completion_summary(self, *, text: str, source: str) -> None:
        self.ensure_mutable()
        summary = ConversationCompletionSummary(text, source)
        self.summary_text = summary.summary_text
        self.summary_source = summary.summary_source

    def seal(self, *, committed: bool) -> None:
        committed = bool(committed)
        if self.sealed:
            if self.committed != committed:
                raise ValueError("sealed stage committed outcome cannot change")
            return
        self.committed = committed
        self.sealed = True


_CURRENT_CONVERSATION_PUBLICATION: ContextVar[
    ConversationPublicationStage | None
] = ContextVar("astraldeep_conversation_publication", default=None)


def current_conversation_publication() -> ConversationPublicationStage | None:
    return _CURRENT_CONVERSATION_PUBLICATION.get()


def activate_conversation_publication(
    stage: ConversationPublicationStage,
) -> Token[ConversationPublicationStage | None]:
    if not isinstance(stage, ConversationPublicationStage):
        raise TypeError("stage must be a ConversationPublicationStage")
    stage.ensure_mutable()
    return _CURRENT_CONVERSATION_PUBLICATION.set(stage)


def reset_conversation_publication(
    token: Token[ConversationPublicationStage | None],
) -> None:
    _CURRENT_CONVERSATION_PUBLICATION.reset(token)
