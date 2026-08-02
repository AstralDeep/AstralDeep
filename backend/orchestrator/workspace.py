"""Feature 028 — per-chat persistent workspace (research D11/D13/D14).

The workspace is the durable, ordered set of rich components a user sees on
the canvas for one chat. ``saved_components`` is its store (rows gain a
stable ``component_id``, ``position`` and ``updated_at`` — see data-model.md);
``workspace_snapshot`` records the full workspace state at every turn
boundary and component-action mutation for the read-only timeline.

Component identity (FR-019, research D11)
-----------------------------------------
Resolution order for a top-level component entering the workspace:

1. **Author identity** — an explicit astralprims ``id`` on the primitive wins
   (namespaced ``au_<id>``), letting agents/LLM target a component across
   parameter changes.
2. **Fingerprint** — ``wc_<sha1(agent|tool|canonical-params)[:16]>``. Two
   outputs of the same tool with different parameters get different
   fingerprints and coexist (fixing the pre-028 ``(tool, agent)`` clobber).
3. **Single-source supersede** — when a fingerprint is new but the workspace
   holds exactly ONE live component from the same (agent, tool) and this
   batch carries exactly one component for that pair, the new content
   *updates that component in place* (keeping its identity). This is the
   existing system-prompt contract ("re-call the SAME tool with corrected
   parameters — do NOT create duplicates") made real. With zero or multiple
   candidates the component appends as new (ambiguity ⇒ safest behavior).
   Applies ONLY to fingerprint-derived identities: a component carrying an
   explicit author/echoed id never supersedes a different identity — a new
   explicit id appends (FR-019 "a new identity MUST append").
4. **Slot-matched family supersede** (030, S7 regression) — rule 3 scaled to
   multi-component results. A re-invocation with corrected parameters mints
   a brand-new fingerprint family, which used to stack a duplicate dashboard
   above the stale one. When an id-less batch of ≥2 components from one
   (agent, tool) dispatch is fingerprint-new and the live workspace holds
   exactly ONE fingerprint family from that same (agent, tool) with an
   identical component count and identical ordered component types, the
   batch re-assigns that family's identities slot-for-slot — the re-run
   updates in place. Reused ids mean any ``workspace_layout`` arrangement
   referencing the family keeps resolving (now to fresh data). Zero or
   multiple prior families, count/type divergence, or ANY explicit id (in
   the batch or the live family) ⇒ append exactly as before (no guessing).

Deterministic component actions bypass all of this: they target an explicit
``component_id`` and the result inherits it (contracts/component-action.md).
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
from typing import Any, Dict, List, Optional, Tuple

from orchestrator.conversation_publication import (
    ConversationPublicationStage,
    current_conversation_publication,
)

logger = logging.getLogger("orchestrator.workspace")

# Ordinal family suffix produced by ordinal_identity() — ``~<N>`` at the end.
_ORDINAL_SUFFIX_RE = re.compile(r"~\d+$")

# Private/system keys excluded from identity fingerprints.
_PRIVATE_PARAM_PREFIX = "_"


def _now_ms() -> int:
    return int(time.time() * 1000)


def canonical_params(params: Optional[Dict[str, Any]]) -> str:
    """Stable JSON form of tool params for fingerprinting (private keys dropped)."""
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
    """Identity for the Nth same-identity component within one upsert batch.

    One round may carry MANY components that resolve to a single identity —
    a multi-component tool result (shared source fingerprint) or parallel
    calls to a tool that hardcodes an author id (the general agent's
    ``chart-card``). Without disambiguation each would supersede the previous
    down to a single surviving row. Ordinal 0 keeps the plain identity (full
    backward compatibility for the common one-per-batch case); later
    occurrences get a deterministic ``~N`` suffix — prefix-preserving (an
    echoed ``wc_…~1``/``au_…~1`` still resolves verbatim) and stable, so
    re-running the same round supersedes slot-for-slot.
    """
    if ordinal <= 0:
        return base_cid
    return f"{base_cid}~{ordinal}"


def family_base_identity(component_id: str) -> str:
    """Base identity of an ordinal family member (``wc_abc~3`` → ``wc_abc``).

    An identity without an ``~N`` suffix is its own base. Inverse of
    :func:`ordinal_identity` for the suffixed members.
    """
    return _ORDINAL_SUFFIX_RE.sub("", component_id or "")


def _ordinal_of(component_id: str) -> int:
    """Slot ordinal of a family member (``wc_abc~3`` → 3; the base → 0)."""
    m = _ORDINAL_SUFFIX_RE.search(component_id or "")
    return int(m.group(0)[1:]) if m else 0


def layout_key_for(chat_id: str, turn_marker: str) -> str:
    """Deterministic per-round layout key (feature 029).

    Re-designing the same round (same chat + turn marker) upserts the same
    row, so garnish updates in place instead of duplicating (FR-019).
    """
    basis = f"{chat_id or ''}|{turn_marker or ''}"
    return "ly_" + hashlib.sha1(basis.encode()).hexdigest()[:16]


def iter_layout_refs(node: Any):
    """Yield every ``ref`` node's component_id in a layout tree (depth-first).

    Layout trees nest through the same keys the component validator walks:
    ``children``, ``content``, and ``tabs[*].content``.
    """
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
    """Return a copy of a layout tree with ``ref`` nodes in ``drop`` removed.

    Containers that end up empty are kept (they may carry garnish text);
    materialization simply renders them without the pruned leaves.
    """
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
    """Owns workspace identity, upserts, ordering, snapshots and timeline reads."""

    def __init__(self, history):
        self.history = history
        self.db = history.db

    def _publication_stage(
        self,
        chat_id: str,
        user_id: str,
        *,
        mutable: bool = False,
    ) -> Optional[ConversationPublicationStage]:
        """Return the exact task-local stage matching this workspace access."""
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
        """Fail closed once a chat is governed by revisioned publication."""

        row = self.db.fetch_one(
            "SELECT render_revision FROM chats WHERE id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        if row is None:
            raise RuntimeError("workspace conversation does not exist")
        if int(row.get("render_revision") or 0) > 0:
            raise RuntimeError(
                "revisioned workspace writes require a publication stage"
            )

    # ── identity ─────────────────────────────────────────────────────────
    def resolve_identity(self, comp: Dict[str, Any]) -> str:
        """Compute (and stamp) the stable component_id for one component."""
        existing = comp.get("component_id")
        if existing and str(existing).startswith("wel_"):
            # 055 US1: wel_ is the EPHEMERAL welcome-canvas namespace — such
            # components must never persist. If one somehow reaches the
            # workspace (an agent echoing a welcome id), the identity is
            # discarded and the component resolves as if unidentified.
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
            # An author echoing back a workspace identity (the system prompt
            # instructs the LLM to do exactly this for in-place updates) is
            # honored verbatim; anything else gets the au_ namespace.
            cid = author_id if author_id.startswith(("wc_", "au_")) else f"au_{author_id}"
        else:
            cid = fingerprint(
                comp.get("_source_agent", ""),
                comp.get("_source_tool", ""),
                comp.get("_source_params"),
            )
        comp["component_id"] = cid
        return cid

    # ── live workspace reads ─────────────────────────────────────────────
    def live_rows(self, chat_id: str, user_id: str) -> List[Dict[str, Any]]:
        """Return the one complete canvas selected by publication context.

        A matching active stage reads only its commit-versioned complete copy.
        Every other task sees either the legacy revision-zero rows or exactly
        the commit/revision currently named by the chat authority row.
        """
        stage = self._publication_stage(chat_id, user_id)
        if stage is not None:
            rows = self.db.fetch_all(
                "SELECT component.* FROM saved_components AS component "
                "JOIN conversation_commit AS publication "
                "ON publication.commit_id = component.conversation_commit_id "
                "WHERE component.chat_id = ? AND component.user_id = ? "
                "AND component.conversation_commit_id = ? "
                "AND component.committed_render_revision = ? "
                "AND publication.chat_id = ? AND publication.owner_user_id = ? "
                "AND publication.base_render_revision = ? "
                "AND publication.state = 'staged' "
                "ORDER BY COALESCE(component.position, 2147483647) ASC, "
                "component.created_at ASC",
                (
                    chat_id,
                    user_id,
                    stage.commit_id,
                    stage.next_render_revision,
                    chat_id,
                    user_id,
                    stage.base_render_revision,
                ),
            )
        else:
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
                "ORDER BY COALESCE(component.position, 2147483647) ASC, "
                "component.created_at ASC",
                (chat_id, user_id),
            )
        out = []
        for row in rows:
            try:
                data = json.loads(row["component_data"])
            except (json.JSONDecodeError, TypeError):
                data = row["component_data"]
            out.append({
                "id": row["id"],
                "chat_id": row["chat_id"],
                "component_id": row.get("component_id"),
                "component_data": data,
                "component_type": row["component_type"],
                "title": row["title"],
                "position": row.get("position"),
                "created_at": row["created_at"],
                "updated_at": row.get("updated_at"),
            })
        return out

    def live_components(self, chat_id: str, user_id: str) -> List[Dict[str, Any]]:
        """Ordered structured component dicts (each carrying component_id)."""
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

    # ── upsert / remove ──────────────────────────────────────────────────
    def upsert(self, chat_id: str, user_id: str, components: List[Dict[str, Any]],
               *, force_component_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Persist a batch of components into the workspace.

        Returns the ordered op list for a ``ui_upsert`` message:
        ``[{op:'upsert', component_id, component}]``. ``force_component_id``
        (deterministic component actions) pins the result onto an existing
        identity regardless of its own fingerprint: a single-component result
        pins the FIRST component of the batch onto that exact identity; a
        MULTI-component result re-assigns the whole batch slot-for-slot onto
        the ordinal-identity family of the pinned id's base (``wc_<fp>``,
        ``wc_<fp>~1``, …), so refreshing ANY member of a multi-component
        family morphs every member in place instead of shifting the family
        one slot and colliding on the clicked id.
        """
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

        # Group same-(agent,tool) components within THIS batch (in batch
        # order) — parallel same-tool calls in one turn must coexist, never
        # supersede, and the family-supersede plan below needs slot order.
        batch_by_source: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for comp in components:
            if isinstance(comp, dict):
                key = (comp.get("_source_agent", ""), comp.get("_source_tool", ""))
                batch_by_source.setdefault(key, []).append(comp)

        # Slot-matched family supersede (docstring rule 4, 030/S7): a multi-
        # component re-run with CHANGED params mints a brand-new fingerprint
        # family, which would append a duplicate dashboard above the stale
        # one. When the live workspace holds exactly ONE fingerprint family
        # from that (agent, tool) and the id-less batch matches it
        # slot-for-slot (same count, same ordered types), reuse the existing
        # identities so the re-run updates in place. Anything ambiguous —
        # zero/many live families, count or type divergence, any explicit
        # id on either side — appends exactly as before.
        family_remap: Dict[Tuple[str, str], List[str]] = {}
        family_slot: Dict[Tuple[str, str], int] = {}
        if force_component_id is None:
            for key, batch_comps in batch_by_source.items():
                if key == ("", "") or len(batch_comps) < 2:
                    continue  # single-component case stays with rule 3
                if any(c.get("component_id") or c.get("id") for c in batch_comps):
                    continue  # explicit/echoed ids are authoritative (FR-019)
                live_family = by_source.get(key, [])
                if len(live_family) != len(batch_comps):
                    continue  # divergent shape — no guessing, append
                live_ids = [r.get("component_id") or "" for r in live_family]
                if not all(i.startswith("wc_") for i in live_ids):
                    continue  # explicit-id rows are never superseded
                live_bases = {family_base_identity(i) for i in live_ids}
                if len(live_bases) != 1:
                    continue  # more than one prior family ⇒ ambiguous
                new_bases = {fingerprint(key[0], key[1], c.get("_source_params"))
                             for c in batch_comps}
                if len(new_bases) != 1 or live_bases & new_bases:
                    # Parallel distinct calls aren't one re-run; an identical
                    # fingerprint already supersedes via the ordinal path.
                    continue
                ordered_rows = sorted(
                    live_family, key=lambda r: _ordinal_of(r.get("component_id") or ""))
                if ([str(r.get("component_type")) for r in ordered_rows]
                        != [str(c.get("type", "unknown")) for c in batch_comps]):
                    continue  # divergent ordered types — append
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
        # Family-aware pinning: when a component action's re-executed tool
        # returns MULTIPLE components, pinning only batch index 0 while the
        # siblings run through the zero-based ordinal enumeration below would
        # shift every output one identity slot AND collide on the clicked id
        # whenever the clicked member was not the family base (data
        # corruption). Instead, re-assign the whole batch slot-for-slot onto
        # the ordinal identities of the clicked member's family base.
        # Single-component results keep the exact-id pin (FR-037 contract).
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
                    # Slot-matched family supersede (docstring rule 4): take
                    # the prior family's identity for this slot so the re-run
                    # morphs the existing components instead of appending.
                    cid = family_remap[src_key][family_slot[src_key]]
                    family_slot[src_key] += 1
                    comp["component_id"] = cid
                else:
                    explicit_identity = bool(comp.get("component_id") or comp.get("id"))
                    cid = self.resolve_identity(comp)
                    # Same resolved identity twice in ONE batch (multi-component
                    # tool result, or parallel calls of a tool with a hardcoded
                    # author id): the 2nd+ occurrence gets a deterministic ordinal
                    # identity instead of superseding its batch siblings.
                    seen = batch_fp_seen.get(cid, 0)
                    batch_fp_seen[cid] = seen + 1
                    if seen:
                        cid = ordinal_identity(cid, seen)
                        comp["component_id"] = cid
                    if cid not in by_cid and not explicit_identity:
                        # Single-source supersede (docstring rule 3) — only for
                        # fingerprint-derived identities. An author-declared id is
                        # authoritative (FR-019): a NEW explicit identity appends,
                        # never steals an existing component's place.
                        candidates = by_source.get(src_key, [])
                        if (src_key != ("", "") and len(candidates) == 1
                                and len(batch_by_source.get(src_key, ())) == 1):
                            cid = candidates[0]["component_id"] or cid
                            comp["component_id"] = cid
            # Duplicate-target guard: one batch must never write the same
            # resolved identity twice — the second write would silently
            # overwrite the first (the pre-fix corruption signature). Fall
            # back to APPENDING under the first free ordinal identity of the
            # colliding id's family and leave a structured trace.
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
                if stage is not None:
                    cursor = self.db.execute(
                        "UPDATE saved_components SET component_data = ?, component_type = ?, "
                        "title = ?, updated_at = ? WHERE id = ? AND chat_id = ? "
                        "AND component_id = ? AND user_id = ? "
                        "AND conversation_commit_id = ? "
                        "AND committed_render_revision = ?",
                        (
                            json.dumps(comp),
                            comp.get("type", existing["component_type"]),
                            comp.get("title", existing["title"]),
                            _now_ms(),
                            existing["id"],
                            chat_id,
                            cid,
                            user_id,
                            stage.commit_id,
                            stage.next_render_revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("staged workspace update lost publication scope")
                else:
                    cursor = self.db.execute(
                        "UPDATE saved_components SET component_data = ?, component_type = ?, "
                        "title = ?, updated_at = ? WHERE id = ? AND chat_id = ? "
                        "AND component_id = ? AND user_id = ?",
                        (
                            json.dumps(comp),
                            comp.get("type", existing["component_type"]),
                            comp.get("title", existing["title"]),
                            _now_ms(),
                            existing["id"],
                            chat_id,
                            cid,
                            user_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("workspace update lost authoritative row")
            else:
                row_id = str(uuid.uuid4())
                title = comp.get("title") or str(comp.get("type", "Component")).replace("_", " ").title()
                if stage is not None:
                    self.db.execute(
                        "INSERT INTO saved_components (id, chat_id, user_id, component_data, "
                        "component_type, title, created_at, component_id, position, updated_at, "
                        "conversation_commit_id, committed_render_revision) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            row_id,
                            chat_id,
                            user_id,
                            json.dumps(comp),
                            comp.get("type", "unknown"),
                            title,
                            _now_ms(),
                            cid,
                            next_pos,
                            _now_ms(),
                            stage.commit_id,
                            stage.next_render_revision,
                        ),
                    )
                else:
                    self.db.execute(
                        "INSERT INTO saved_components (id, chat_id, user_id, component_data, "
                        "component_type, title, created_at, component_id, position, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (row_id, chat_id, user_id, json.dumps(comp), comp.get("type", "unknown"),
                         title, _now_ms(), cid, next_pos, _now_ms()),
                    )
                by_cid[cid] = {"id": row_id, "component_id": cid,
                               "component_data": comp,
                               "component_type": comp.get("type", "unknown"),
                               "title": title, "position": next_pos}
                key = (comp.get("_source_agent", ""), comp.get("_source_tool", ""))
                if key != ("", ""):
                    by_source.setdefault(key, []).append(by_cid[cid])
                next_pos += 1
            ops.append({"op": "upsert", "component_id": cid, "component": comp, "created": created})
        if ops:
            if stage is not None:
                stage.mark_dirty()
            else:
                self.db.execute(
                    "UPDATE chats SET has_saved_components = TRUE WHERE id = ? AND user_id = ?",
                    (chat_id, user_id),
                )
        return ops

    # ── canvas arrangements (feature 029, adaptive UI designer) ──────────
    def live_layouts(self, chat_id: str, user_id: str) -> List[Dict[str, Any]]:
        """Ordered designed arrangements for a chat (overlay over components)."""
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
        chat = self.db.fetch_one(
            "SELECT render_revision, conversation_commit_id FROM chats "
            "WHERE id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        render_revision = int((chat or {}).get("render_revision") or 0)
        if render_revision == 0:
            rows = self.db.fetch_all(
                "SELECT layout_key, position, layout FROM workspace_layout "
                "WHERE chat_id = ? AND user_id = ? "
                "AND conversation_commit_id IS NULL "
                "AND committed_render_revision IS NULL "
                "ORDER BY position ASC, id ASC",
                (chat_id, user_id),
            )
        else:
            commit_id = (chat or {}).get("conversation_commit_id")
            if commit_id is None:
                return []
            rows = self.db.fetch_all(
                "SELECT layout_key, position, layout FROM workspace_layout "
                "WHERE chat_id = ? AND user_id = ? "
                "AND conversation_commit_id = ? "
                "AND committed_render_revision = ? "
                "ORDER BY position ASC, id ASC",
                (chat_id, user_id, str(commit_id), render_revision),
            )
        out = []
        for row in rows:
            try:
                tree = json.loads(row["layout"])
            except (json.JSONDecodeError, TypeError):
                continue
            out.append({"layout_key": row["layout_key"], "position": row["position"],
                        "layout": tree})
        return out

    def next_canvas_position(self, chat_id: str, user_id: str) -> int:
        """Next position in the SHARED ordering space of components + layouts."""
        component_positions = [
            int(row.get("position") or 0) for row in self.live_rows(chat_id, user_id)
        ]
        layout_positions = [
            int(row.get("position") or 0) for row in self.live_layouts(chat_id, user_id)
        ]
        return 1 + max(component_positions + layout_positions, default=0)

    def upsert_layout(self, chat_id: str, user_id: str, layout_key: str,
                      layout: List[Dict[str, Any]]) -> bool:
        """Persist one designed arrangement; later layouts steal claimed refs.

        A component_id may be claimed by at most one live arrangement: refs
        the new layout claims are pruned from earlier layouts (later wins —
        re-designing a round that re-uses an old component moves it).
        Existing (chat, layout_key) rows update in place keeping position.
        """
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
                    self.db.execute(
                        "UPDATE workspace_layout SET layout = ?, updated_at = ? "
                        "WHERE chat_id = ? AND user_id = ? AND layout_key = ?",
                        (json.dumps(pruned), _now_ms(), chat_id, user_id, other["layout_key"]),
                    )
        existing = self.db.fetch_one(
            "SELECT id FROM workspace_layout WHERE chat_id = ? AND user_id = ? AND layout_key = ?",
            (chat_id, user_id, layout_key),
        )
        if existing:
            self.db.execute(
                "UPDATE workspace_layout SET layout = ?, updated_at = ? "
                "WHERE chat_id = ? AND user_id = ? AND layout_key = ?",
                (json.dumps(layout), _now_ms(), chat_id, user_id, layout_key),
            )
        else:
            self.db.execute(
                "INSERT INTO workspace_layout (chat_id, user_id, layout_key, position, "
                "layout, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, user_id, layout_key, self.next_canvas_position(chat_id, user_id),
                 json.dumps(layout), _now_ms(), _now_ms()),
            )
        return True

    def remove(self, chat_id: str, user_id: str, component_id: str) -> bool:
        stage = self._publication_stage(chat_id, user_id, mutable=True)
        if stage is None:
            self._assert_legacy_write_allowed(chat_id, user_id)
        if stage is not None:
            cur = self.db.execute(
                "DELETE FROM saved_components WHERE chat_id = ? "
                "AND component_id = ? AND user_id = ? "
                "AND conversation_commit_id = ? "
                "AND committed_render_revision = ?",
                (
                    chat_id,
                    component_id,
                    user_id,
                    stage.commit_id,
                    stage.next_render_revision,
                ),
            )
            removed = bool(getattr(cur, "rowcount", 0))
            if removed:
                stage.mark_dirty()
                for layout in stage.layouts:
                    if component_id in set(iter_layout_refs(layout.get("layout"))):
                        layout["layout"] = prune_layout_refs(
                            layout.get("layout"), {component_id}
                        )
            # ``has_saved_components`` and component-version cleanup describe
            # authoritative state and therefore wait for atomic publication.
            return removed

        authoritative = self.get_by_component_id(chat_id, user_id, component_id)
        if authoritative is None:
            return False
        cur = self.db.execute(
            "DELETE FROM saved_components WHERE id = ? AND chat_id = ? "
            "AND component_id = ? AND user_id = ?",
            (authoritative["id"], chat_id, component_id, user_id),
        )
        removed = bool(getattr(cur, "rowcount", 0))
        if removed:
            # Feature 029: a deleted component's refs vanish from arrangements
            # (materialization would drop them anyway; pruning keeps stored
            # layouts honest for snapshots/timeline).
            try:
                for lay in self.live_layouts(chat_id, user_id):
                    if component_id in set(iter_layout_refs(lay["layout"])):
                        pruned = prune_layout_refs(lay["layout"], {component_id})
                        self.db.execute(
                            "UPDATE workspace_layout SET layout = ?, updated_at = ? "
                            "WHERE chat_id = ? AND user_id = ? AND layout_key = ?",
                            (json.dumps(pruned), _now_ms(), chat_id, user_id, lay["layout_key"]),
                        )
            except Exception:
                logger.debug("layout ref pruning failed on remove", exc_info=True)
            # Feature 055 (US4): a deleted component's refine history goes
            # with it (component_version has no saved_components FK).
            try:
                from orchestrator import artifact_versions
                artifact_versions.delete_for_component(self.db, chat_id, user_id, component_id)
            except Exception:
                logger.debug("version-history cascade failed on remove", exc_info=True)
        if removed:
            if not self.live_rows(chat_id, user_id):
                self.db.execute(
                    "UPDATE chats SET has_saved_components = FALSE WHERE id = ? AND user_id = ?",
                    (chat_id, user_id),
                )
        return removed

    # ── snapshots / timeline (D14, FR-030..FR-033) ───────────────────────
    def snapshot(self, chat_id: str, user_id: str, cause: str,
                 turn_message_id: Optional[int] = None) -> Optional[int]:
        """Record the full current workspace state. Returns the snapshot id."""
        if not chat_id:
            return None
        stage = self._publication_stage(chat_id, user_id)
        if stage is not None:
            # A timeline entry is an authoritative publication. Staged rows
            # are already durable under conversation_commit and must not leak
            # into that older visibility channel. Remember the requested
            # derivative snapshot and materialize it only after publication.
            stage.snapshot_cause = str(cause or "conversation_commit")[:128]
            return None
        components = self.live_components(chat_id, user_id)
        # Feature 029: arrangements snapshot alongside components so the
        # timeline can materialize historical designed states. NULL-tolerant
        # readers treat missing/NULL layouts as "render flat" (pre-029 rows).
        layouts = self.live_layouts(chat_id, user_id)
        self.db.execute(
            "INSERT INTO workspace_snapshot (chat_id, user_id, turn_message_id, cause, "
            "components, layouts, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, user_id, turn_message_id, cause, json.dumps(components),
             json.dumps(layouts) if layouts else None, _now_ms()),
        )
        # RealDictCursor rows; psycopg2 cursors expose lastrowid unreliably —
        # callers only need success/failure, the id is for tests/diagnostics.
        try:
            row = self.db.fetch_one(
                "SELECT id FROM workspace_snapshot WHERE chat_id = ? AND user_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (chat_id, user_id),
            )
            return row["id"] if row else None
        except Exception:
            return None

    def list_snapshots(self, chat_id: str, user_id: str, limit: int = 50,
                       offset: int = 0) -> List[Dict[str, Any]]:
        """Snapshot metadata for the timeline list (newest first; no payloads)."""
        rows = self.db.fetch_all(
            "SELECT id, chat_id, turn_message_id, cause, created_at "
            "FROM workspace_snapshot WHERE chat_id = ? AND user_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (chat_id, user_id, limit, offset),
        )
        return [dict(r) for r in rows]

    def count_snapshots(self, chat_id: str, user_id: str) -> int:
        row = self.db.fetch_one(
            "SELECT COUNT(*) as count FROM workspace_snapshot WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        return int(row["count"]) if row else 0

    def get_snapshot(self, snapshot_id: int, user_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.fetch_one(
            "SELECT * FROM workspace_snapshot WHERE id = ? AND user_id = ?",
            (snapshot_id, user_id),
        )
        if not row:
            return None
        try:
            components = json.loads(row["components"])
        except (json.JSONDecodeError, TypeError):
            components = []
        try:
            layouts = json.loads(row["layouts"]) if row.get("layouts") else []
        except (json.JSONDecodeError, TypeError):
            layouts = []
        return {
            "id": row["id"],
            "chat_id": row["chat_id"],
            "turn_message_id": row.get("turn_message_id"),
            "cause": row["cause"],
            "components": components,
            "layouts": layouts,
            "created_at": row["created_at"],
        }

    # ── async facade (event-loop-safe twins of the sync methods above) ───
    async def alive_rows(self, chat_id: str, user_id: str) -> List[Dict[str, Any]]:
        """Async twin of :meth:`live_rows`, run off the event loop."""
        return await asyncio.to_thread(self.live_rows, chat_id, user_id)

    async def alive_components(self, chat_id: str, user_id: str) -> List[Dict[str, Any]]:
        """Async twin of :meth:`live_components`, run off the event loop."""
        return await asyncio.to_thread(self.live_components, chat_id, user_id)

    async def aget_by_component_id(self, chat_id: str, user_id: str,
                                   component_id: str) -> Optional[Dict[str, Any]]:
        """Async twin of :meth:`get_by_component_id`, run off the event loop."""
        return await asyncio.to_thread(self.get_by_component_id, chat_id, user_id, component_id)

    async def aupsert(self, chat_id: str, user_id: str, components: List[Dict[str, Any]],
                      *, force_component_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Async twin of :meth:`upsert`, run off the event loop."""
        return await asyncio.to_thread(
            self.upsert, chat_id, user_id, components,
            force_component_id=force_component_id,
        )

    async def alive_layouts(self, chat_id: str, user_id: str) -> List[Dict[str, Any]]:
        """Async twin of :meth:`live_layouts`, run off the event loop."""
        return await asyncio.to_thread(self.live_layouts, chat_id, user_id)

    async def aupsert_layout(self, chat_id: str, user_id: str, layout_key: str,
                             layout: List[Dict[str, Any]]) -> bool:
        """Async twin of :meth:`upsert_layout`, run off the event loop."""
        return await asyncio.to_thread(self.upsert_layout, chat_id, user_id, layout_key, layout)

    async def aremove(self, chat_id: str, user_id: str, component_id: str) -> bool:
        """Async twin of :meth:`remove`, run off the event loop."""
        return await asyncio.to_thread(self.remove, chat_id, user_id, component_id)

    async def asnapshot(self, chat_id: str, user_id: str, cause: str,
                        turn_message_id: Optional[int] = None) -> Optional[int]:
        """Async twin of :meth:`snapshot`, run off the event loop."""
        return await asyncio.to_thread(self.snapshot, chat_id, user_id, cause, turn_message_id)

    async def alist_snapshots(self, chat_id: str, user_id: str, limit: int = 50,
                              offset: int = 0) -> List[Dict[str, Any]]:
        """Async twin of :meth:`list_snapshots`, run off the event loop."""
        return await asyncio.to_thread(self.list_snapshots, chat_id, user_id, limit, offset)

    async def acount_snapshots(self, chat_id: str, user_id: str) -> int:
        """Async twin of :meth:`count_snapshots`, run off the event loop."""
        return await asyncio.to_thread(self.count_snapshots, chat_id, user_id)

    async def aget_snapshot(self, snapshot_id: int, user_id: str) -> Optional[Dict[str, Any]]:
        """Async twin of :meth:`get_snapshot`, run off the event loop."""
        return await asyncio.to_thread(self.get_snapshot, snapshot_id, user_id)
