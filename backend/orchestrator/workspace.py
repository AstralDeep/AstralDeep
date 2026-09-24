"""Owns the per-chat canvas: component identity resolution, ordered upserts, layout
arrangements, and the snapshot timeline, atomic via conversation_publication.py. Used
by orchestrator.py, stream_manager.py, and ui_designer.py.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from astralplane.repositories.workspaces import (
    CanvasComponentRecord,
    LayoutRecord,
)

from orchestrator.conversation_publication import (
    ConversationPublicationStage,
    current_conversation_publication,
)

logger = logging.getLogger("orchestrator.workspace")

_ORDINAL_SUFFIX_RE = re.compile(r"~\d+$")

_PRIVATE_PARAM_PREFIX = "_"

_ACTIVE_WORKSPACE_TRANSACTION: ContextVar[tuple[object, object] | None] = ContextVar(
    "astraldeep_workspace_transaction",
    default=None,
)


def _plane_atomic(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        active = _ACTIVE_WORKSPACE_TRANSACTION.get()
        if active is not None and active[0] is self:
            return method(self, *args, **kwargs)
        runtime = self._require_plane_runtime()
        stage = current_conversation_publication()
        staged_state = None
        if stage is not None:
            staged_state = (
                copy.deepcopy(stage.layouts),
                stage.dirty,
                stage.snapshot_cause,
            )
        try:
            with runtime.transaction() as transaction:
                token = _ACTIVE_WORKSPACE_TRANSACTION.set((self, transaction))
                try:
                    return method(self, *args, **kwargs)
                finally:
                    _ACTIVE_WORKSPACE_TRANSACTION.reset(token)
        except BaseException:
            # Restore staged state on rollback, or a later publish leaks it
            if stage is not None and staged_state is not None and not stage.sealed:
                stage.layouts = staged_state[0]
                stage.dirty = staged_state[1]
                stage.snapshot_cause = staged_state[2]
            raise

    return wrapped


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_copy(item) for item in value]
    return copy.deepcopy(value)


def canonical_params(params: Optional[Dict[str, Any]]) -> str:
    if not isinstance(params, dict):
        return "{}"
    clean = {k: v for k, v in sorted(params.items()) if not str(k).startswith(_PRIVATE_PARAM_PREFIX)}
    try:
        return json.dumps(clean, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return "{}"


def fingerprint(agent_id: str, tool_name: str, params: Optional[Dict[str, Any]]) -> str:
    basis = f"{agent_id or ''}|{tool_name or ''}|{canonical_params(params)}"
    return "wc_" + hashlib.sha1(basis.encode()).hexdigest()[:16]


def ordinal_identity(base_cid: str, ordinal: int) -> str:
    if ordinal <= 0:
        return base_cid
    return f"{base_cid}~{ordinal}"


def family_base_identity(component_id: str) -> str:
    return _ORDINAL_SUFFIX_RE.sub("", component_id or "")


def _ordinal_of(component_id: str) -> int:
    m = _ORDINAL_SUFFIX_RE.search(component_id or "")
    return int(m.group(0)[1:]) if m else 0


def layout_key_for(chat_id: str, turn_marker: str) -> str:
    basis = f"{chat_id or ''}|{turn_marker or ''}"
    return "ly_" + hashlib.sha1(basis.encode()).hexdigest()[:16]


def iter_layout_refs(node: Any):
    if isinstance(node, list):
        for item in node:
            yield from iter_layout_refs(item)
        return
    if not isinstance(node, dict):
        return
    if node.get("type") == "ref":
        cid = node.get("component_id")
        if cid:
            yield str(cid)
        return
    for key in ("children", "content"):
        nested = node.get(key)
        if isinstance(nested, list):
            yield from iter_layout_refs(nested)
    tabs = node.get("tabs")
    if isinstance(tabs, list):
        for tab in tabs:
            if isinstance(tab, dict):
                yield from iter_layout_refs(tab.get("content"))


def prune_layout_refs(node: Any, drop: set) -> Any:
    if isinstance(node, list):
        out = []
        for item in node:
            pruned = prune_layout_refs(item, drop)
            if pruned is not None:
                out.append(pruned)
        return out
    if not isinstance(node, dict):
        return node
    if node.get("type") == "ref":
        return None if str(node.get("component_id")) in drop else node
    result = dict(node)
    for key in ("children", "content"):
        nested = node.get(key)
        if isinstance(nested, list):
            result[key] = prune_layout_refs(nested, drop)
    tabs = node.get("tabs")
    if isinstance(tabs, list):
        new_tabs = []
        for tab in tabs:
            if isinstance(tab, dict):
                tab = dict(tab)
                tab["content"] = prune_layout_refs(tab.get("content") or [], drop)
            new_tabs.append(tab)
        result["tabs"] = new_tabs
    return result


class WorkspaceManager:
    def __init__(
        self,
        history,
        *,
        plane_runtime=None,
        plane_repositories=None,
    ):
        self.history = history
        legacy_database = getattr(history, "db", None)
        self.plane_runtime = (
            plane_runtime
            or getattr(history, "plane_runtime", None)
            or getattr(legacy_database, "plane_runtime", None)
        )
        self.plane_repositories = (
            plane_repositories
            or getattr(history, "plane_repositories", None)
            or getattr(legacy_database, "plane_repositories", None)
            or (
                None
                if self.plane_runtime is None
                else self.plane_runtime.repositories
            )
        )

    def _require_plane_runtime(self):
        if self.plane_runtime is None or self.plane_repositories is None:
            raise RuntimeError(
                "WorkspaceManager requires the initialized application AstralPlane runtime"
            )
        return self.plane_runtime

    def _call(self, operation, /, **kwargs):
        active = _ACTIVE_WORKSPACE_TRANSACTION.get()
        if active is not None and active[0] is self:
            return operation(active[1], **kwargs)
        runtime = self._require_plane_runtime()
        with runtime.transaction() as transaction:
            return operation(transaction, **kwargs)

    @property
    def _workspace_repository(self):
        self._require_plane_runtime()
        return self.plane_repositories.workspaces

    @property
    def _history_repository(self):
        self._require_plane_runtime()
        return self.plane_repositories.history

    @staticmethod
    def _row(record: CanvasComponentRecord) -> Dict[str, Any]:
        return {
            "id": record.row_id,
            "chat_id": record.conversation_id,
            "component_id": record.component_id,
            "component_data": _json_copy(record.payload),
            "component_type": record.component_type,
            "title": record.title,
            "position": record.position,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    @staticmethod
    def _layout(record: LayoutRecord) -> Dict[str, Any]:
        return {
            "layout_key": record.layout_key,
            "position": record.position,
            "layout": _json_copy(record.tree),
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    @staticmethod
    def _advanced_time(previous: int) -> int:
        return max(_now_ms(), int(previous) + 1)

    def _publication_stage(
        self,
        chat_id: str,
        user_id: str,
        *,
        mutable: bool = False,
    ) -> Optional[ConversationPublicationStage]:
        stage = current_conversation_publication()
        if stage is None or not stage.matches(self.history, chat_id, user_id):
            return None
        if stage.sealed:
            if mutable:
                stage.ensure_mutable()
            return None
        if mutable:
            stage.ensure_mutable()
        return stage

    def _assert_legacy_write_allowed(self, chat_id: str, user_id: str) -> None:
        conversation = self._call(
            self._history_repository.conversations.get,
            owner_id=user_id,
            conversation_id=chat_id,
        )
        if conversation is None:
            raise RuntimeError("workspace conversation does not exist")
        if conversation.render_revision > 0:
            raise RuntimeError(
                "revisioned workspace writes require a publication stage"
            )

    def resolve_identity(self, comp: Dict[str, Any]) -> str:
        existing = comp.get("component_id")
        if existing and str(existing).startswith("wel_"):
            logger.warning("workspace: refused ephemeral wel_ identity %r", existing)
            comp.pop("component_id", None)
            existing = None
        if existing:
            return existing
        author_id = comp.get("id")
        if author_id and str(author_id).startswith("wel_"):
            logger.warning("workspace: refused ephemeral wel_ author id %r", author_id)
            author_id = None
        if author_id:
            author_id = str(author_id)
            cid = author_id if author_id.startswith(("wc_", "au_")) else f"au_{author_id}"
        else:
            cid = fingerprint(
                comp.get("_source_agent", ""),
                comp.get("_source_tool", ""),
                comp.get("_source_params"),
            )
        comp["component_id"] = cid
        return cid

    def live_rows(self, chat_id: str, user_id: str) -> List[Dict[str, Any]]:
        stage = self._publication_stage(chat_id, user_id)
        if stage is not None:
            records = self._call(
                self._workspace_repository.canvas.list_scoped,
                owner_id=user_id,
                conversation_id=chat_id,
                publication_id=stage.commit_id,
                committed_render_revision=stage.next_render_revision,
                expected_base_render_revision=stage.base_render_revision,
            )
        else:
            records = self._call(
                self._workspace_repository.canvas.list_current,
                owner_id=user_id,
                conversation_id=chat_id,
            )
        return [self._row(record) for record in records]

    def live_components(self, chat_id: str, user_id: str) -> List[Dict[str, Any]]:
        comps = []
        for row in self.live_rows(chat_id, user_id):
            data = row["component_data"]
            if isinstance(data, dict):
                if row.get("component_id") and not data.get("component_id"):
                    data["component_id"] = row["component_id"]
                comps.append(data)
        return comps

    def get_by_component_id(self, chat_id: str, user_id: str, component_id: str) -> Optional[Dict[str, Any]]:
        for row in self.live_rows(chat_id, user_id):
            if row.get("component_id") == component_id:
                return row
        return None

    @_plane_atomic
    def upsert(self, chat_id: str, user_id: str, components: List[Dict[str, Any]],
               *, force_component_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if not chat_id or not components:
            return []
        stage = self._publication_stage(chat_id, user_id, mutable=True)
        if stage is None:
            self._assert_legacy_write_allowed(chat_id, user_id)
        live = self.live_rows(chat_id, user_id)
        by_cid = {r["component_id"]: r for r in live if r.get("component_id")}
        by_source: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for r in live:
            data = r["component_data"]
            if isinstance(data, dict):
                key = (data.get("_source_agent", ""), data.get("_source_tool", ""))
                if key != ("", ""):
                    by_source.setdefault(key, []).append(r)

        batch_by_source: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for comp in components:
            if isinstance(comp, dict):
                key = (comp.get("_source_agent", ""), comp.get("_source_tool", ""))
                batch_by_source.setdefault(key, []).append(comp)

        family_remap: Dict[Tuple[str, str], List[str]] = {}
        family_slot: Dict[Tuple[str, str], int] = {}
        if force_component_id is None:
            for key, batch_comps in batch_by_source.items():
                if key == ("", "") or len(batch_comps) < 2:
                    continue
                if any(c.get("component_id") or c.get("id") for c in batch_comps):
                    continue
                live_family = by_source.get(key, [])
                if len(live_family) != len(batch_comps):
                    continue
                live_ids = [r.get("component_id") or "" for r in live_family]
                if not all(i.startswith("wc_") for i in live_ids):
                    continue
                live_bases = {family_base_identity(i) for i in live_ids}
                if len(live_bases) != 1:
                    continue
                new_bases = {fingerprint(key[0], key[1], c.get("_source_params"))
                             for c in batch_comps}
                if len(new_bases) != 1 or live_bases & new_bases:
                    continue
                ordered_rows = sorted(
                    live_family, key=lambda r: _ordinal_of(r.get("component_id") or ""))
                if ([str(r.get("component_type")) for r in ordered_rows]
                        != [str(c.get("type", "unknown")) for c in batch_comps]):
                    continue
                family_remap[key] = [r["component_id"] for r in ordered_rows]
                family_slot[key] = 0
                logger.info(
                    "workspace.family_supersede: chat_id=%s family=%s agent=%s "
                    "tool=%s size=%d — fingerprint-new re-run updates in place",
                    chat_id, next(iter(live_bases)), key[0], key[1], len(batch_comps),
                )

        ops: List[Dict[str, Any]] = []
        next_pos = 1 + max([r.get("position") or 0 for r in live], default=0)
        batch_fp_seen: Dict[str, int] = {}
        force_family_base: Optional[str] = None
        if force_component_id and sum(1 for c in components if isinstance(c, dict)) > 1:
            force_family_base = family_base_identity(force_component_id)
        batch_targets: set = set()
        slot = -1
        for i, comp in enumerate(components):
            if not isinstance(comp, dict):
                continue
            slot += 1
            if force_family_base is not None:
                cid = ordinal_identity(force_family_base, slot)
                comp["component_id"] = cid
            elif force_component_id and i == 0:
                cid = force_component_id
                comp["component_id"] = cid
            else:
                src_key = (comp.get("_source_agent", ""), comp.get("_source_tool", ""))
                if src_key in family_remap:
                    cid = family_remap[src_key][family_slot[src_key]]
                    family_slot[src_key] += 1
                    comp["component_id"] = cid
                else:
                    explicit_identity = bool(comp.get("component_id") or comp.get("id"))
                    cid = self.resolve_identity(comp)
                    seen = batch_fp_seen.get(cid, 0)
                    batch_fp_seen[cid] = seen + 1
                    if seen:
                        cid = ordinal_identity(cid, seen)
                        comp["component_id"] = cid
                    if cid not in by_cid and not explicit_identity:
                        candidates = by_source.get(src_key, [])
                        if (src_key != ("", "") and len(candidates) == 1
                                and len(batch_by_source.get(src_key, ())) == 1):
                            cid = candidates[0]["component_id"] or cid
                            comp["component_id"] = cid
            # One batch must never target the same id twice, or it overwrites
            if cid in batch_targets:
                base = family_base_identity(cid)
                n = 1
                while True:
                    candidate = ordinal_identity(base, n)
                    if candidate not in batch_targets and candidate not in by_cid:
                        break
                    n += 1
                logger.warning(
                    "workspace.upsert duplicate target: chat_id=%s component_id=%s "
                    "already written in this batch; appending as %s instead of overwriting",
                    chat_id, cid, candidate,
                )
                cid = candidate
                comp["component_id"] = cid
            batch_targets.add(cid)
            existing = by_cid.get(cid)
            created = existing is None
            if existing:
                self._call(
                    self._workspace_repository.canvas.replace,
                    owner_id=user_id,
                    conversation_id=chat_id,
                    component_id=cid,
                    payload=comp,
                    component_type=comp.get("type", existing["component_type"]),
                    title=comp.get("title", existing["title"]),
                    expected_updated_at=int(existing["updated_at"]),
                    updated_at=self._advanced_time(existing["updated_at"]),
                    publication_id=None if stage is None else stage.commit_id,
                    committed_render_revision=(
                        None if stage is None else stage.next_render_revision
                    ),
                )
            else:
                row_id = str(uuid.uuid4())
                title = comp.get("title") or str(comp.get("type", "Component")).replace("_", " ").title()
                observed_at = _now_ms()
                self._call(
                    self._workspace_repository.canvas.create,
                    record=CanvasComponentRecord(
                        row_id=row_id,
                        conversation_id=chat_id,
                        owner_id=user_id,
                        component_id=cid,
                        payload=copy.deepcopy(comp),
                        component_type=comp.get("type", "unknown"),
                        title=title,
                        position=next_pos,
                        created_at=observed_at,
                        updated_at=observed_at,
                        publication_id=None if stage is None else stage.commit_id,
                        committed_render_revision=(
                            None if stage is None else stage.next_render_revision
                        ),
                    ),
                )
                by_cid[cid] = {"id": row_id, "component_id": cid,
                               "component_data": comp,
                               "component_type": comp.get("type", "unknown"),
                               "title": title, "position": next_pos,
                               "created_at": observed_at,
                               "updated_at": observed_at}
                key = (comp.get("_source_agent", ""), comp.get("_source_tool", ""))
                if key != ("", ""):
                    by_source.setdefault(key, []).append(by_cid[cid])
                next_pos += 1
            ops.append({"op": "upsert", "component_id": cid, "component": comp, "created": created})
        if ops:
            if stage is not None:
                stage.mark_dirty()
            else:
                self._call(
                    self._workspace_repository.canvas.sync_legacy_presence,
                    owner_id=user_id,
                    conversation_id=chat_id,
                )
        return ops

    def live_layouts(self, chat_id: str, user_id: str) -> List[Dict[str, Any]]:
        stage = self._publication_stage(chat_id, user_id)
        if stage is not None:
            return copy.deepcopy(
                sorted(
                    stage.layouts,
                    key=lambda item: (
                        int(item.get("position") or 0),
                        str(item.get("layout_key") or ""),
                    ),
                )
            )
        records = self._call(
            self._workspace_repository.layouts.list_current,
            owner_id=user_id,
            conversation_id=chat_id,
        )
        return [self._layout(record) for record in records]

    def next_canvas_position(self, chat_id: str, user_id: str) -> int:
        component_positions = [
            int(row.get("position") or 0) for row in self.live_rows(chat_id, user_id)
        ]
        layout_positions = [
            int(row.get("position") or 0) for row in self.live_layouts(chat_id, user_id)
        ]
        return 1 + max(component_positions + layout_positions, default=0)

    @_plane_atomic
    def upsert_layout(self, chat_id: str, user_id: str, layout_key: str,
                      layout: List[Dict[str, Any]]) -> bool:
        if not chat_id or not layout_key or not isinstance(layout, list):
            return False
        stage = self._publication_stage(chat_id, user_id, mutable=True)
        if stage is None:
            self._assert_legacy_write_allowed(chat_id, user_id)
        claimed = set(iter_layout_refs(layout))
        if stage is not None:
            if claimed:
                for other in stage.layouts:
                    if other.get("layout_key") == layout_key:
                        continue
                    overlap = set(iter_layout_refs(other.get("layout"))) & claimed
                    if overlap:
                        other["layout"] = prune_layout_refs(
                            other.get("layout"), overlap
                        )
            for existing in stage.layouts:
                if existing.get("layout_key") == layout_key:
                    existing["layout"] = copy.deepcopy(layout)
                    stage.mark_dirty()
                    return True
            stage.layouts.append(
                {
                    "layout_key": layout_key,
                    "position": self.next_canvas_position(chat_id, user_id),
                    "layout": copy.deepcopy(layout),
                }
            )
            stage.mark_dirty()
            return True
        if claimed:
            for other in self.live_layouts(chat_id, user_id):
                if other["layout_key"] == layout_key:
                    continue
                other_refs = set(iter_layout_refs(other["layout"]))
                overlap = other_refs & claimed
                if overlap:
                    pruned = prune_layout_refs(other["layout"], overlap)
                    self._call(
                        self._workspace_repository.layouts.replace,
                        owner_id=user_id,
                        conversation_id=chat_id,
                        layout_key=other["layout_key"],
                        tree=pruned,
                        expected_updated_at=int(other["updated_at"]),
                        updated_at=self._advanced_time(other["updated_at"]),
                    )
        existing = self._call(
            self._workspace_repository.layouts.get_scoped,
            owner_id=user_id,
            conversation_id=chat_id,
            layout_key=layout_key,
        )
        if existing:
            self._call(
                self._workspace_repository.layouts.replace,
                owner_id=user_id,
                conversation_id=chat_id,
                layout_key=layout_key,
                tree=layout,
                expected_updated_at=existing.updated_at,
                updated_at=self._advanced_time(existing.updated_at),
            )
        else:
            observed_at = _now_ms()
            self._call(
                self._workspace_repository.layouts.create,
                record=LayoutRecord(
                    layout_id=0,
                    conversation_id=chat_id,
                    owner_id=user_id,
                    layout_key=layout_key,
                    position=self.next_canvas_position(chat_id, user_id),
                    tree=copy.deepcopy(layout),
                    created_at=observed_at,
                    updated_at=observed_at,
                ),
            )
        return True

    @_plane_atomic
    def remove(self, chat_id: str, user_id: str, component_id: str) -> bool:
        stage = self._publication_stage(chat_id, user_id, mutable=True)
        if stage is None:
            self._assert_legacy_write_allowed(chat_id, user_id)
        if stage is not None:
            removed = self._call(
                self._workspace_repository.canvas.remove,
                owner_id=user_id,
                conversation_id=chat_id,
                component_id=component_id,
                publication_id=stage.commit_id,
                committed_render_revision=stage.next_render_revision,
            )
            if removed:
                stage.mark_dirty()
                for layout in stage.layouts:
                    if component_id in set(iter_layout_refs(layout.get("layout"))):
                        layout["layout"] = prune_layout_refs(
                            layout.get("layout"), {component_id}
                        )
            return removed

        authoritative = self.get_by_component_id(chat_id, user_id, component_id)
        if authoritative is None:
            return False
        removed = self._call(
            self._workspace_repository.canvas.remove,
            owner_id=user_id,
            conversation_id=chat_id,
            component_id=component_id,
        )
        if removed:
            for layout in self.live_layouts(chat_id, user_id):
                if component_id in set(iter_layout_refs(layout["layout"])):
                    pruned = prune_layout_refs(layout["layout"], {component_id})
                    self._call(
                        self._workspace_repository.layouts.replace,
                        owner_id=user_id,
                        conversation_id=chat_id,
                        layout_key=layout["layout_key"],
                        tree=pruned,
                        expected_updated_at=int(layout["updated_at"]),
                        updated_at=self._advanced_time(layout["updated_at"]),
                    )
            self._call(
                self.plane_repositories.artifacts.versions.delete_for_component,
                owner_id=user_id,
                conversation_id=chat_id,
                component_id=component_id,
            )
            self._call(
                self._workspace_repository.canvas.sync_legacy_presence,
                owner_id=user_id,
                conversation_id=chat_id,
            )
        return removed

    @_plane_atomic
    def snapshot(self, chat_id: str, user_id: str, cause: str,
                 turn_message_id: Optional[int] = None) -> Optional[int]:
        if not chat_id:
            return None
        stage = self._publication_stage(chat_id, user_id)
        if stage is not None:
            stage.snapshot_cause = str(cause or "conversation_commit")[:128]
            return None
        components = self.live_components(chat_id, user_id)
        layouts = self.live_layouts(chat_id, user_id)
        record = self._call(
            self._workspace_repository.snapshots.capture,
            owner_id=user_id,
            conversation_id=chat_id,
            cause=cause,
            components=components,
            layouts=layouts,
            created_at=_now_ms(),
            turn_message_id=turn_message_id,
        )
        return record.snapshot_id

    def list_snapshots(self, chat_id: str, user_id: str, limit: int = 50,
                       offset: int = 0) -> List[Dict[str, Any]]:
        records = self._call(
            self._workspace_repository.snapshots.list_for_conversation,
            owner_id=user_id,
            conversation_id=chat_id,
            limit=limit,
            offset=offset,
        )
        return [
            {
                "id": record.snapshot_id,
                "chat_id": record.conversation_id,
                "turn_message_id": record.turn_message_id,
                "cause": record.cause,
                "created_at": record.created_at,
            }
            for record in records
        ]

    def count_snapshots(self, chat_id: str, user_id: str) -> int:
        return self._call(
            self._workspace_repository.snapshots.count_for_conversation,
            owner_id=user_id,
            conversation_id=chat_id,
        )

    def get_snapshot(self, snapshot_id: int, user_id: str) -> Optional[Dict[str, Any]]:
        record = self._call(
            self._workspace_repository.snapshots.get,
            owner_id=user_id,
            snapshot_id=snapshot_id,
        )
        if record is None:
            return None
        return {
            "id": record.snapshot_id,
            "chat_id": record.conversation_id,
            "turn_message_id": record.turn_message_id,
            "cause": record.cause,
            "components": _json_copy(record.components),
            "layouts": _json_copy(record.layouts),
            "created_at": record.created_at,
        }

    async def alive_rows(self, chat_id: str, user_id: str) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.live_rows, chat_id, user_id)

    async def alive_components(self, chat_id: str, user_id: str) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.live_components, chat_id, user_id)

    async def aget_by_component_id(self, chat_id: str, user_id: str,
                                   component_id: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self.get_by_component_id, chat_id, user_id, component_id)

    async def aupsert(self, chat_id: str, user_id: str, components: List[Dict[str, Any]],
                      *, force_component_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(
            self.upsert, chat_id, user_id, components,
            force_component_id=force_component_id,
        )

    async def alive_layouts(self, chat_id: str, user_id: str) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.live_layouts, chat_id, user_id)

    async def aupsert_layout(self, chat_id: str, user_id: str, layout_key: str,
                             layout: List[Dict[str, Any]]) -> bool:
        return await asyncio.to_thread(self.upsert_layout, chat_id, user_id, layout_key, layout)

    async def aremove(self, chat_id: str, user_id: str, component_id: str) -> bool:
        return await asyncio.to_thread(self.remove, chat_id, user_id, component_id)

    async def asnapshot(self, chat_id: str, user_id: str, cause: str,
                        turn_message_id: Optional[int] = None) -> Optional[int]:
        return await asyncio.to_thread(self.snapshot, chat_id, user_id, cause, turn_message_id)

    async def alist_snapshots(self, chat_id: str, user_id: str, limit: int = 50,
                              offset: int = 0) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_snapshots, chat_id, user_id, limit, offset)

    async def acount_snapshots(self, chat_id: str, user_id: str) -> int:
        return await asyncio.to_thread(self.count_snapshots, chat_id, user_id)

    async def aget_snapshot(self, snapshot_id: int, user_id: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self.get_snapshot, snapshot_id, user_id)
