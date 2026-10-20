"""Authorized Atlas page lifecycle and owner-approved source promotion over AstralPlane's
qualified personalization facade: mutations validate input, re-check owner/project
authorization and the PHI gate, seal the body with the host key, then audit and write page
heads, bounded revision chains, and graph links inside one Plane transaction.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

from astralplane.repositories import RepositoryConflictError, RepositoryNotFoundError
from astralplane.repositories.preferences import MemoryEnvelopeCursor, MemoryRecord
from cryptography.fernet import Fernet

from audit.repository import AuditRepository
from audit.schemas import AuditEventCreate
from orchestrator.plane_repository_context import PlaneRepositoryContext, repository_from
from personalization.atlas_pages import (
    KIND_PAGE,
    KIND_REVISION,
    FORMAT,
    MAX_HISTORY,
    MAX_LINKS,
    MAX_REVISION,
    PageCipher,
    PageSealed,
    AtlasPageError,
    canonical_page_id,
    decode_page_cursor,
    encode_page_cursor,
    envelope_bytes,
    envelope_fingerprint,
    normalize_body,
    normalize_project,
    normalize_slug,
    normalize_sources,
    normalize_title,
    page_identity,
    page_keywords,
    parse_envelope,
    revision_identity,
)
from personalization.atlas_promotions import (
    MAX_PROMOTION_SOURCES,
    PROMOTION_TTL_MS,
    PromotionApplied,
    PromotionDestination,
    PromotionProposal,
    PromotionSource,
    PromotionSourceInput,
    canonical_source_identifier,
    normalize_source_kind,
    normalize_source_revision,
    open_proposal,
    promotion_body,
    promotion_source_references,
    promotion_title,
    proposal_digest as proposal_payload_digest,
    seal_proposal,
    source_snapshot,
)
from personalization.explicit_notes import (
    EncryptedExplicitNote,
    ExplicitNoteCipher,
    ExplicitNoteMetadata,
    ExplicitNoteUnavailable,
)
from personalization.phi_gate import PHIGate, get_phi_gate
from personalization.repository import MEMORY_CATEGORIES

_PAGE_CATEGORY = "context"
_MEMORY_SOURCE = "explicit"
_MAX_LIST = 100
_EVENT_CLASS = "memory"


class AtlasUnavailable(RuntimeError):
    def __init__(self, code: str = "atlas_unavailable"):
        super().__init__(code)
        self.code = code


class AtlasNotFound(RuntimeError):
    def __init__(self, code: str = "page_not_found"):
        super().__init__(code)
        self.code = code


class AtlasConflict(RuntimeError):
    def __init__(self, code: str, *, current_revision: int | None = None):
        super().__init__(code)
        self.code = code
        self.current_revision = current_revision


class AtlasRefused(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _after_ms(previous: int) -> int:
    return max(_now_ms(), previous + 1)


def _from_ms(moment: int) -> datetime:
    return datetime.fromtimestamp(moment / 1000, tz=timezone.utc)


@dataclass(frozen=True, slots=True)
class PageInput:
    slug: str
    title: str
    body: str
    sources: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class PageSummary:
    page_id: str
    owner_id: str
    project_id: str | None
    slug: str
    title: str
    revision: int
    sources: tuple[str, ...]
    created_at: int
    updated_at: int
    fingerprint: str | None = None
    body: str | None = None
    deleted: bool = False
    revisions_removed: int = 0
    links_removed: int = 0


@dataclass(frozen=True, slots=True)
class PageBatch:
    pages: tuple[PageSummary, ...]
    next_cursor: str | None


class AtlasPageService:
    def __init__(
        self,
        *,
        plane_runtime,
        plane_repositories=None,
        audit_repo: AuditRepository | None,
        fernet: Fernet | None,
        phi_gate: PHIGate | None = None,
    ):
        self.runtime = plane_runtime
        repositories = plane_repositories or getattr(plane_runtime, "repositories", None)
        if repositories is None or audit_repo is None or type(audit_repo) is not AuditRepository:
            raise AtlasUnavailable("atlas_plane_unbound")
        if audit_repo._audit.plane_runtime is not plane_runtime:
            raise AtlasUnavailable("atlas_audit_unbound")
        preferences, runtime = repository_from(
            "preferences", plane_runtime=plane_runtime, repositories=repositories
        )
        graph, graph_runtime = repository_from(
            "personalization_graph", plane_runtime=plane_runtime, repositories=repositories
        )
        if runtime is not plane_runtime or graph_runtime is not plane_runtime:
            raise AtlasUnavailable("atlas_plane_unbound")
        self.audit = audit_repo
        self.pages = PlaneRepositoryContext(
            repository=preferences.personalization, plane_runtime=runtime
        )
        self.graph = PlaneRepositoryContext(repository=graph, plane_runtime=runtime)
        if not isinstance(fernet, Fernet):
            raise AtlasUnavailable("atlas_cipher_unbound")
        self.cipher = PageCipher(fernet)
        try:
            self.note_cipher = ExplicitNoteCipher(fernet)
        except ExplicitNoteUnavailable:
            raise AtlasUnavailable("atlas_cipher_unbound") from None
        self.notes = self.pages.repository
        self.phi = phi_gate or get_phi_gate()

    def _author(self, owner_id: object) -> str:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise AtlasUnavailable("atlas_caller_unverified")
        return owner_id

    def _gate(self, *texts: str) -> None:
        for text in texts:
            if text and self.phi.contains_phi(text):
                raise AtlasRefused("page_phi_refused")

    def _audit_in_transaction(
        self, transaction, *, owner_id: str, action: str, description: str, detail: str
    ) -> None:
        moment = _now_ms()
        try:
            self.audit.insert_in_transaction(
                AuditEventCreate(
                    actor_user_id=owner_id,
                    auth_principal=owner_id,
                    event_class=_EVENT_CLASS,
                    action_type=f"atlas_page_{action}",
                    description=description[:1024],
                    correlation_id=str(uuid4()),
                    outcome="success",
                    outcome_detail=detail[:2048],
                    started_at=_from_ms(moment),
                    completed_at=_from_ms(moment),
                ),
                transaction=transaction,
                plane_runtime=self.runtime,
            )
        except AtlasUnavailable:
            raise
        except Exception:
            raise AtlasUnavailable("atlas_audit_unavailable") from None

    def _sealed(
        self,
        owner_id: str,
        project_id: str | None,
        page_id: str,
        slug: str,
        title: str,
        revision: int,
        sources: tuple[str, ...],
        body: str,
    ) -> PageSealed:
        draft = PageSealed(
            page_id=page_id,
            owner_id=owner_id,
            project_id=project_id,
            slug=slug,
            title=title,
            revision=revision,
            sources=sources,
            ciphertext="",
        )
        return PageSealed(
            page_id=draft.page_id,
            owner_id=draft.owner_id,
            project_id=draft.project_id,
            slug=draft.slug,
            title=draft.title,
            revision=draft.revision,
            sources=draft.sources,
            ciphertext=self.cipher.seal(draft, body),
        )

    def _opened(self, envelope: dict) -> str:
        sealed = PageSealed(
            page_id=envelope["page_id"],
            owner_id=envelope["owner_id"],
            project_id=envelope.get("project_id"),
            slug=envelope.get("slug") or "",
            title=envelope.get("title") or "",
            revision=envelope["revision"],
            sources=tuple(envelope.get("sources") or ()),
            ciphertext=envelope["body"],
        )
        return self.cipher.open(sealed, envelope["body"])

    def _head(
        self, transaction, owner_id: str, page_id: str, project_id: str | None
    ) -> tuple[MemoryRecord, dict]:
        row = self.pages.repository.get_memory(
            transaction, owner_id=owner_id, memory_id=page_id
        )
        if row is None or row.superseded_at is not None:
            raise AtlasNotFound()
        envelope = parse_envelope(row.value)
        if envelope is None or envelope.get("kind") != KIND_PAGE:
            raise AtlasConflict("page_identity_is_not_an_atlas_page")
        if envelope.get("owner_id") != owner_id or envelope.get("project_id") != project_id:
            raise AtlasNotFound()
        return row, envelope

    def _revision_chain(
        self, transaction, owner_id: str, page_id: str, cursor: object, *, limit: int
    ) -> list[tuple[MemoryRecord, dict]]:
        chain: list[tuple[MemoryRecord, dict]] = []
        while isinstance(cursor, str) and cursor and len(chain) < limit:
            row = self.pages.repository.get_memory(
                transaction, owner_id=owner_id, memory_id=cursor
            )
            if row is None:
                break
            revision = parse_envelope(row.value)
            if (
                revision is None
                or revision.get("kind") != KIND_REVISION
                or revision.get("page_id") != page_id
            ):
                break
            chain.append((row, revision))
            cursor = revision.get("prev_revision_id")
        return chain

    def _prune_history(
        self, transaction, owner_id: str, chain: list[tuple[MemoryRecord, dict]]
    ) -> int:
        removed = 0
        for row, _revision in chain[MAX_HISTORY:]:
            if self.pages.repository.delete_memory(
                transaction,
                owner_id=owner_id,
                memory_id=row.memory_id,
                expected_updated_at=row.updated_at,
            ):
                removed += 1
        return removed

    def _resolve_link_targets(
        self,
        transaction,
        owner_id: str,
        project_id: str | None,
        page_id: str,
        links: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(links) > MAX_LINKS:
            raise AtlasPageError("links_out_of_bounds")
        targets: list[str] = []
        for entry in links:
            if not isinstance(entry, str) or not entry.strip():
                raise AtlasPageError("link_invalid")
            resolved = canonical_page_id(entry.strip())
            if resolved is None:
                resolved = page_identity(owner_id, project_id, normalize_slug(entry))
            if resolved == page_id:
                raise AtlasPageError("link_self_reference")
            target = self.pages.repository.get_memory(
                transaction, owner_id=owner_id, memory_id=resolved
            )
            if target is None or target.superseded_at is not None:
                raise AtlasNotFound("link_target_not_found")
            target_envelope = parse_envelope(target.value)
            if target_envelope is None or target_envelope.get("kind") != KIND_PAGE:
                raise AtlasPageError("link_target_invalid")
            if target_envelope.get("project_id") != project_id:
                raise AtlasNotFound("link_target_not_found")
            targets.append(resolved)
        return tuple(dict.fromkeys(targets))

    def _reconcile_links(
        self, transaction, owner_id: str, page_id: str, wanted: tuple[str, ...]
    ) -> tuple[int, int]:
        current = set(
            self.graph.repository.linked_ids(
                transaction, owner_id=owner_id, memory_id=page_id, limit=_MAX_LIST
            )
        )
        targets = set(wanted)
        added = 0
        for target in sorted(targets - current):
            self.graph.repository.add_link(
                transaction,
                owner_id=owner_id,
                memory_id=page_id,
                linked_id=target,
                created_at=_now_ms(),
            )
            added += 1
        removed = 0
        for stale in sorted(current - targets):
            self.graph.repository.remove_link(
                transaction, owner_id=owner_id, memory_id=page_id, linked_id=stale
            )
            removed += 1
        return added, removed

    def _summary(
        self, row: MemoryRecord, envelope: dict, *, body: str | None = None
    ) -> PageSummary:
        return PageSummary(
            page_id=envelope["page_id"],
            owner_id=envelope["owner_id"],
            project_id=envelope.get("project_id"),
            slug=envelope.get("slug") or "",
            title=envelope.get("title") or "",
            revision=envelope["revision"],
            sources=tuple(envelope.get("sources") or ()),
            created_at=row.created_at or 0,
            updated_at=row.updated_at or 0,
            fingerprint=envelope_fingerprint(envelope),
            body=body,
        )

    def _memory_row(
        self,
        *,
        memory_id: str,
        owner_id: str,
        envelope: str,
        project_id: str | None,
        created_at: int,
        updated_at: int,
        keywords: str | None,
    ) -> MemoryRecord:
        return MemoryRecord(
            memory_id=memory_id,
            owner_id=owner_id,
            category=_PAGE_CATEGORY,
            value=envelope,
            source=_MEMORY_SOURCE,
            salience=0.0,
            created_at=created_at,
            updated_at=updated_at,
            superseded_by=None,
            superseded_at=None,
            keywords=keywords,
            signature=envelope_fingerprint(parse_envelope(envelope) or {}),
            valid_from=None,
            valid_to=None,
            ingested_at=None,
            recall_count=0,
            last_recalled_at=None,
            project_id=project_id,
        )

    def _create_locked(
        self, transaction, *, caller: str, project_id: str | None, page_id: str,
        slug: str, title: str, body: str, sources: tuple[str, ...], links: tuple[str, ...],
    ) -> PageSummary:
        existing = self.notes.get_memory(
            transaction, owner_id=caller, memory_id=page_id
        )
        if existing is not None:
            raise AtlasConflict("page_already_exists", current_revision=_revision_of(existing))
        targets = self._resolve_link_targets(
            transaction, caller, project_id, page_id, links
        )
        sealed = self._sealed(caller, project_id, page_id, slug, title, 1, sources, body)
        envelope = envelope_bytes(sealed, kind=KIND_PAGE, prev_revision_id=None)
        now = _now_ms()
        self._audit_in_transaction(
            transaction,
            owner_id=caller,
            action="create",
            description=f"Created Atlas page {slug}",
            detail=f"project={project_id or 'global'} revision=1",
        )
        row = self.notes.create_memory(
            transaction,
            self._memory_row(
                memory_id=page_id,
                owner_id=caller,
                envelope=envelope,
                project_id=project_id,
                created_at=now,
                updated_at=now,
                keywords=page_keywords(slug, title),
            ),
        )
        if targets:
            self._reconcile_links(transaction, caller, page_id, targets)
        parsed = parse_envelope(row.value)
        if parsed is None:
            raise AtlasUnavailable("atlas_page_unreadable")
        return self._summary(row, parsed)

    def create_page(self, *, owner_id: str, page: PageInput) -> PageSummary:
        caller = self._author(owner_id)
        project_id = normalize_project(page.project_id)
        slug = normalize_slug(page.slug)
        title = normalize_title(page.title)
        body = normalize_body(page.body)
        sources = normalize_sources(page.sources)
        page_id = page_identity(caller, project_id, slug)
        self._gate(title, body, *sources)

        def accept(transaction):
            return self._create_locked(
                transaction,
                caller=caller,
                project_id=project_id,
                page_id=page_id,
                slug=slug,
                title=title,
                body=body,
                sources=sources,
                links=page.links,
            )

        return self._run(accept)

    def get_page(
        self,
        *,
        owner_id: str,
        project_id: str | None,
        reference: str,
        revision: int | None = None,
    ) -> PageSummary:
        caller = self._author(owner_id)
        project = normalize_project(project_id)
        page_id = self._resolve_reference(caller, project, reference)
        if revision is not None and (not isinstance(revision, int) or revision < 1):
            raise AtlasPageError("revision_invalid")

        def accept(transaction):
            row, envelope = self._head(transaction, caller, page_id, project)
            if revision is not None and revision != envelope["revision"]:
                chain = self._revision_chain(
                    transaction, caller, page_id, envelope.get("prev_revision_id"),
                    limit=MAX_HISTORY,
                )
                match = next(
                    (
                        candidate
                        for candidate in chain
                        if candidate[1]["revision"] == revision
                    ),
                    None,
                )
                if match is None:
                    raise AtlasNotFound("revision_not_found")
                row, envelope = match
            return self._summary(row, envelope, body=self._opened(envelope))

        return self._run(accept)

    def _update_locked(
        self, transaction, *, caller: str, project_id: str | None, page_id: str,
        title: str | None, body: str | None, sources: tuple[str, ...] | None,
        links: tuple[str, ...] | None, expected_revision: int,
        gate_sources: bool = True,
    ) -> PageSummary:
        row, envelope = self._head(transaction, caller, page_id, project_id)
        if envelope["revision"] != expected_revision:
            raise AtlasConflict(
                "page_revision_conflict", current_revision=envelope["revision"]
            )
        next_title = (
            title if title is not None else normalize_title(envelope.get("title") or "")
        )
        next_sources = (
            normalize_sources(sources)
            if sources is not None
            else tuple(envelope.get("sources") or ())
        )
        next_body = body if body is not None else self._opened(envelope)
        if gate_sources:
            self._gate(next_title, next_body, *next_sources)
        else:
            self._gate(next_title, next_body)
        next_revision = envelope["revision"] + 1
        if next_revision > MAX_REVISION:
            raise AtlasConflict(
                "page_revision_exhausted", current_revision=envelope["revision"]
            )
        sealed = self._sealed(
            caller,
            envelope.get("project_id"),
            page_id,
            envelope.get("slug") or "",
            next_title,
            next_revision,
            next_sources,
            next_body,
        )
        snapshot_id = revision_identity(page_id, envelope["revision"], uuid4().hex)
        snapshot_envelope = envelope_bytes(
            _snapshot_sealed(sealed, envelope),
            kind=KIND_REVISION,
            prev_revision_id=envelope.get("prev_revision_id"),
        )
        now = _after_ms(row.updated_at or 0)
        self._audit_in_transaction(
            transaction,
            owner_id=caller,
            action="update",
            description=f"Updated Atlas page {sealed.slug}",
            detail=f"revision={next_revision}",
        )
        snapshot = self.notes.create_memory(
            transaction,
            self._memory_row(
                memory_id=snapshot_id,
                owner_id=caller,
                envelope=snapshot_envelope,
                project_id=row.project_id,
                created_at=row.updated_at or now,
                updated_at=now,
                keywords=row.keywords,
            ),
        )
        self.notes.supersede_memory(
            transaction,
            owner_id=caller,
            memory_id=snapshot.memory_id,
            superseded_at=now,
            replacement_id=page_id,
        )
        head_envelope = envelope_bytes(sealed, kind=KIND_PAGE, prev_revision_id=snapshot.memory_id)
        try:
            updated = self.notes.update_memory_value(
                transaction,
                owner_id=caller,
                memory_id=page_id,
                value=head_envelope,
                signature=envelope_fingerprint(parse_envelope(head_envelope) or {}),
                updated_at=_after_ms(now),
                expected_updated_at=row.updated_at or 0,
            )
        except RepositoryConflictError:
            current = self.notes.get_memory(
                transaction, owner_id=caller, memory_id=page_id
            )
            raise AtlasConflict(
                "page_revision_conflict", current_revision=_revision_of(current)
            ) from None
        if updated is None:
            raise AtlasNotFound()
        if links is not None:
            wanted = self._resolve_link_targets(
                transaction, caller, envelope.get("project_id"), page_id, links
            )
            self._reconcile_links(transaction, caller, page_id, wanted)
        chain = self._revision_chain(
            transaction, caller, page_id, snapshot.memory_id, limit=MAX_HISTORY + 1
        )
        self._prune_history(transaction, caller, chain)
        parsed = parse_envelope(updated.value)
        if parsed is None:
            raise AtlasUnavailable("atlas_page_unreadable")
        return self._summary(updated, parsed, body=next_body)

    def update_page(
        self,
        *,
        owner_id: str,
        project_id: str | None,
        reference: str,
        title: str | None,
        body: str | None,
        sources: tuple[str, ...] | None,
        links: tuple[str, ...] | None,
        expected_revision: int,
    ) -> PageSummary:
        caller = self._author(owner_id)
        project = normalize_project(project_id)
        page_id = self._resolve_reference(caller, project, reference)
        if not isinstance(expected_revision, int) or expected_revision < 1:
            raise AtlasPageError("expected_revision_invalid")
        if title is None and body is None and sources is None and links is None:
            raise AtlasPageError("page_update_empty")
        if title is not None:
            title = normalize_title(title)
        if body is not None:
            body = normalize_body(body)

        def accept(transaction):
            return self._update_locked(
                transaction,
                caller=caller,
                project_id=project,
                page_id=page_id,
                title=title,
                body=body,
                sources=sources,
                links=links,
                expected_revision=expected_revision,
            )

        return self._run(accept)

    def _audit_promotion_in_transaction(
        self,
        transaction,
        *,
        owner_id: str,
        action: str,
        proposal_id: str,
        sources: tuple[PromotionSource, ...],
        destination: PromotionDestination,
        event_id: str | None = None,
    ) -> None:
        moment = _now_ms()
        try:
            self.audit.insert_in_transaction(
                AuditEventCreate(
                    event_id=event_id,
                    actor_user_id=owner_id,
                    auth_principal=owner_id,
                    event_class=_EVENT_CLASS,
                    action_type=f"atlas_promotion_{action}",
                    description=f"Atlas source promotion {action}",
                    correlation_id=proposal_id,
                    outcome="success",
                    outputs_meta={
                        "proposal_id": proposal_id,
                        "action": action,
                        "page_id": destination.page_id,
                        "page_revision": destination.revision,
                        "sources": [
                            {
                                "kind": source.kind,
                                "id": source.source_id,
                                "revision": source.revision,
                                "digest": source.digest,
                            }
                            for source in sources
                        ],
                    },
                    started_at=_from_ms(moment),
                    completed_at=_from_ms(moment),
                ),
                transaction=transaction,
                plane_runtime=self.runtime,
            )
        except AtlasUnavailable:
            raise
        except Exception:
            raise AtlasUnavailable("atlas_audit_unavailable") from None

    @staticmethod
    def _source_inputs(values: object) -> tuple[PromotionSourceInput, ...]:
        if (not isinstance(values, (list, tuple))
                or not 1 <= len(values) <= MAX_PROMOTION_SOURCES):
            raise AtlasPageError("promotion_sources_out_of_bounds")
        inputs: list[PromotionSourceInput] = []
        for value in values:
            if isinstance(value, PromotionSourceInput):
                item = value
            elif isinstance(value, dict) and set(value) <= {"kind", "id", "source_id", "revision"}:
                item = PromotionSourceInput(
                    kind=value.get("kind"),
                    source_id=value.get("id", value.get("source_id")),
                    revision=value.get("revision"),
                )
            else:
                raise AtlasPageError("promotion_source_invalid")
            kind = normalize_source_kind(item.kind)
            source_id = canonical_source_identifier(item.source_id)
            revision = normalize_source_revision(item.revision, required=False)
            if (kind, source_id) in {(entry.kind, entry.source_id) for entry in inputs}:
                raise AtlasPageError("promotion_source_duplicate")
            inputs.append(PromotionSourceInput(kind, source_id, revision))
        return tuple(inputs)

    def _load_note_source(
        self, transaction, *, owner_id: str, source: PromotionSourceInput, now: int
    ) -> PromotionSource:
        from astralplane.repositories.guidance_models import (
            ExplicitNoteRecord,
            ExplicitNoteTombstone,
        )

        record = self.notes.get_explicit_note(
            transaction, owner_id=owner_id, note_id=source.source_id
        )
        if record is None or type(record) is ExplicitNoteTombstone:
            raise AtlasNotFound("promotion_source_not_found")
        if type(record) is not ExplicitNoteRecord:
            raise AtlasUnavailable("promotion_source_unreadable")
        if source.revision is not None and record.revision != source.revision:
            raise AtlasConflict("promotion_source_stale", current_revision=record.revision)
        if not record.enabled:
            raise AtlasConflict("promotion_source_disabled")
        if record.expires_at is not None and now >= record.expires_at:
            raise AtlasConflict("promotion_source_expired")
        metadata = ExplicitNoteMetadata(
            **{key: value for key, value in asdict(record).items() if key != "ciphertext"}
        )
        try:
            opened = self.note_cipher.open(
                EncryptedExplicitNote(metadata, record.ciphertext),
                owner_id=owner_id,
                now_ms=now,
                require_enabled=True,
            )
        except ExplicitNoteUnavailable:
            raise AtlasUnavailable("promotion_source_unreadable") from None
        return source_snapshot(
            kind="note",
            source_id=record.note_id,
            revision=record.revision,
            category=record.category,
            value=opened.value,
        )

    def _load_memory_source(
        self,
        transaction,
        *,
        owner_id: str,
        project_id: str | None,
        source: PromotionSourceInput,
        now: int,
    ) -> PromotionSource:
        record = self.notes.get_memory(
            transaction, owner_id=owner_id, memory_id=source.source_id
        )
        if record is None:
            raise AtlasNotFound("promotion_source_not_found")
        if record.superseded_at is not None or record.superseded_by is not None:
            raise AtlasConflict("promotion_source_stale")
        if record.valid_to is not None and now >= record.valid_to:
            raise AtlasConflict("promotion_source_stale")
        if record.valid_from is not None and now < record.valid_from:
            raise AtlasConflict("promotion_source_stale")
        if record.category not in MEMORY_CATEGORIES or parse_envelope(record.value) is not None:
            raise AtlasPageError("promotion_source_invalid")
        if record.project_id not in (None, project_id):
            raise AtlasPageError("promotion_source_scope_mismatch")
        if source.revision is not None and record.updated_at != source.revision:
            raise AtlasConflict("promotion_source_stale")
        return source_snapshot(
            kind="memory",
            source_id=record.memory_id,
            revision=record.updated_at,
            category=record.category,
            value=record.value,
        )

    def _load_promotion_sources(
        self,
        transaction,
        *,
        owner_id: str,
        project_id: str | None,
        sources: tuple[PromotionSourceInput, ...],
        now: int,
    ) -> tuple[PromotionSource, ...]:
        loaded: list[PromotionSource] = []
        for source in sources:
            if source.kind == "note":
                loaded.append(
                    self._load_note_source(
                        transaction, owner_id=owner_id, source=source, now=now
                    )
                )
            else:
                loaded.append(
                    self._load_memory_source(
                        transaction,
                        owner_id=owner_id,
                        project_id=project_id,
                        source=source,
                        now=now,
                    )
                )
        self._gate(*(source.value for source in loaded))
        return tuple(loaded)

    def _promotion_destination(
        self, transaction, *, caller: str, project_id: str | None, slug: str
    ) -> tuple[str, str, dict | None, MemoryRecord | None]:
        page_id = page_identity(caller, project_id, slug)
        row = self.notes.get_memory(
            transaction, owner_id=caller, memory_id=page_id
        )
        if row is None or row.superseded_at is not None:
            return page_id, "create", None, None
        row, envelope = self._head(transaction, caller, page_id, project_id)
        return page_id, "append", envelope, row

    def propose_promotion(
        self,
        *,
        owner_id: str,
        project_id: str | None,
        sources: object,
        slug: str,
        title: str | None = None,
        mode: str = "auto",
        expected_revision: int | None = None,
    ) -> PromotionProposal:
        caller = self._author(owner_id)
        project = normalize_project(project_id)
        inputs = self._source_inputs(sources)
        normalized_slug = normalize_slug(slug)
        if mode not in {"auto", "create", "append"}:
            raise AtlasPageError("promotion_mode_invalid")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 1
        ):
            raise AtlasPageError("promotion_destination_revision_invalid")
        requested_title = normalize_title(title) if title is not None else None
        created = _now_ms()
        proposal_id = str(uuid4())

        def accept(transaction):
            now = _now_ms()
            loaded = self._load_promotion_sources(
                transaction,
                owner_id=caller,
                project_id=project,
                sources=inputs,
                now=now,
            )
            page_id, action, envelope, row = self._promotion_destination(
                transaction, caller=caller, project_id=project, slug=normalized_slug
            )
            if action == "append":
                if mode == "create":
                    raise AtlasConflict(
                        "page_already_exists", current_revision=envelope["revision"]
                    )
                if expected_revision is not None and envelope["revision"] != expected_revision:
                    raise AtlasConflict(
                        "page_revision_conflict", current_revision=envelope["revision"]
                    )
                existing_title = normalize_title(envelope.get("title") or "")
                if requested_title is not None and requested_title != existing_title:
                    raise AtlasPageError("promotion_destination_title_mismatch")
                page_title = existing_title
                existing_body = self._opened(envelope)
                references = promotion_source_references(
                    loaded, tuple(envelope.get("sources") or ())
                )
                destination = PromotionDestination(
                    page_id=page_id,
                    slug=normalized_slug,
                    revision=envelope["revision"],
                    fingerprint=envelope_fingerprint(envelope),
                )
            else:
                if mode == "append":
                    raise AtlasNotFound("promotion_destination_not_found")
                if expected_revision is not None:
                    raise AtlasPageError("promotion_destination_revision_invalid")
                page_title = promotion_title(loaded, requested_title)
                existing_body = None
                references = promotion_source_references(loaded)
                destination = PromotionDestination(
                    page_id=page_id,
                    slug=normalized_slug,
                    revision=None,
                    fingerprint=None,
                )
            body = promotion_body(loaded, title=page_title, existing_body=existing_body)
            self._gate(page_title, body)
            payload = {
                "v": 1,
                "format": "astral.atlas-promotion/v1",
                "proposal_id": proposal_id,
                "owner": caller,
                "project": project,
                "action": action,
                "sources": [
                    {
                        "kind": source.kind,
                        "id": source.source_id,
                        "revision": source.revision,
                        "category": source.category,
                        "digest": source.digest,
                    }
                    for source in loaded
                ],
                "destination": {
                    "page_id": destination.page_id,
                    "slug": destination.slug,
                    "revision": destination.revision,
                    "fingerprint": destination.fingerprint,
                },
                "title": page_title,
                "body": body,
                "source_references": list(references),
                "created": created,
                "expires": created + PROMOTION_TTL_MS,
            }
            digest = proposal_payload_digest(payload)
            token = seal_proposal(self.cipher._fernet, payload)
            self._audit_promotion_in_transaction(
                transaction,
                owner_id=caller,
                action="propose",
                proposal_id=proposal_id,
                sources=loaded,
                destination=destination,
            )
            return PromotionProposal(
                proposal_id=proposal_id,
                proposal_digest=digest,
                approval_token=token,
                expires_at=payload["expires"],
                owner_id=caller,
                project_id=project,
                action=action,
                sources=loaded,
                destination=destination,
                title=page_title,
                body=body,
                source_references=references,
            )

        return self._run(accept)

    def approve_promotion(
        self,
        *,
        owner_id: str,
        project_id: str | None,
        proposal_id: str,
        approval_token: str,
        proposal_digest: str | None = None,
        approved: bool = True,
    ) -> PromotionApplied:
        caller = self._author(owner_id)
        project = normalize_project(project_id)
        if not isinstance(proposal_id, str) or not proposal_id:
            raise AtlasPageError("promotion_approval_invalid")
        try:
            if str(UUID(proposal_id)) != proposal_id:
                raise ValueError
        except (ValueError, TypeError):
            raise AtlasPageError("promotion_approval_invalid") from None
        if approved is not True:
            raise AtlasConflict("promotion_not_approved")
        now = _now_ms()
        payload = open_proposal(
            self.cipher._fernet,
            approval_token,
            owner_id=caller,
            project_id=project,
            now_ms=now,
        )
        if payload["proposal_id"] != proposal_id:
            raise AtlasPageError("promotion_approval_invalid")
        digest = proposal_payload_digest(payload)
        if proposal_digest is not None and proposal_digest != digest:
            raise AtlasPageError("promotion_approval_invalid")
        inputs = tuple(
            PromotionSourceInput(source["kind"], source["id"], source["revision"])
            for source in payload["sources"]
        )
        expected_sources = tuple(payload["sources"])
        destination = PromotionDestination(**payload["destination"])
        if page_identity(caller, project, destination.slug) != destination.page_id:
            raise AtlasPageError("promotion_approval_invalid")

        def accept(transaction):
            if self.audit._audit.repository.get(
                transaction, chain_id=caller, event_id=proposal_id
            ) is not None:
                raise AtlasConflict("promotion_already_applied")
            loaded = self._load_promotion_sources(
                transaction,
                owner_id=caller,
                project_id=project,
                sources=inputs,
                now=_now_ms(),
            )
            if len(loaded) != len(expected_sources):
                raise AtlasConflict("promotion_source_stale")
            for source, expected in zip(loaded, expected_sources):
                if (
                    source.kind != expected["kind"]
                    or source.source_id != expected["id"]
                    or source.revision != expected["revision"]
                    or source.category != expected["category"]
                    or source.digest != expected["digest"]
                ):
                    raise AtlasConflict("promotion_source_stale")
            title = normalize_title(payload["title"])
            if payload["action"] == "create":
                if self.notes.get_memory(
                    transaction, owner_id=caller, memory_id=destination.page_id
                ) is not None:
                    raise AtlasConflict("page_already_exists")
                references = promotion_source_references(loaded)
                if tuple(payload["source_references"]) != references:
                    raise AtlasPageError("promotion_approval_invalid")
                body = normalize_body(payload["body"])
                self._gate(title, body)
                page = self._create_locked(
                    transaction,
                    caller=caller,
                    project_id=project,
                    page_id=destination.page_id,
                    slug=destination.slug,
                    title=title,
                    body=body,
                    sources=references,
                    links=(),
                )
                page = replace(page, body=body)
            else:
                row, envelope = self._head(
                    transaction, caller, destination.page_id, project
                )
                if (
                    destination.revision is not None
                    and envelope["revision"] != destination.revision
                ) or envelope_fingerprint(envelope) != destination.fingerprint:
                    raise AtlasConflict(
                        "promotion_destination_stale",
                        current_revision=envelope["revision"],
                    )
                existing_title = normalize_title(envelope.get("title") or "")
                if existing_title != title:
                    raise AtlasConflict("promotion_destination_stale")
                existing_body = self._opened(envelope)
                references = promotion_source_references(
                    loaded, tuple(envelope.get("sources") or ())
                )
                if tuple(payload["source_references"]) != references:
                    raise AtlasPageError("promotion_approval_invalid")
                body = promotion_body(loaded, title=title, existing_body=existing_body)
                if body != normalize_body(payload["body"]):
                    raise AtlasConflict("promotion_destination_stale")
                page = self._update_locked(
                    transaction,
                    caller=caller,
                    project_id=project,
                    page_id=destination.page_id,
                    title=None,
                    body=body,
                    sources=references,
                    links=None,
                    expected_revision=envelope["revision"],
                    gate_sources=False,
                )
            self._audit_promotion_in_transaction(
                transaction,
                owner_id=caller,
                action="apply",
                proposal_id=proposal_id,
                sources=loaded,
                destination=PromotionDestination(
                    page_id=page.page_id,
                    slug=page.slug,
                    revision=page.revision,
                    fingerprint=page.fingerprint,
                ),
                event_id=proposal_id,
            )
            return PromotionApplied(proposal_id, page, references)

        return self._run(accept)

    def delete_page(
        self,
        *,
        owner_id: str,
        project_id: str | None,
        reference: str,
        expected_revision: int,
    ) -> PageSummary:
        caller = self._author(owner_id)
        project = normalize_project(project_id)
        page_id = self._resolve_reference(caller, project, reference)
        if not isinstance(expected_revision, int) or expected_revision < 1:
            raise AtlasPageError("expected_revision_invalid")

        def accept(transaction):
            row, envelope = self._head(transaction, caller, page_id, project)
            if envelope["revision"] != expected_revision:
                raise AtlasConflict(
                    "page_revision_conflict", current_revision=envelope["revision"]
                )
            chain = self._revision_chain(
                transaction, caller, page_id, envelope.get("prev_revision_id"),
                limit=MAX_HISTORY,
            )
            neighbors = self.graph.repository.linked_ids(
                transaction, owner_id=caller, memory_id=page_id, limit=_MAX_LIST
            )
            links_removed = 0
            for neighbor in neighbors:
                if self.graph.repository.remove_link(
                    transaction, owner_id=caller, memory_id=page_id, linked_id=neighbor
                ):
                    links_removed += 1
            self._audit_in_transaction(
                transaction,
                owner_id=caller,
                action="delete",
                description=f"Deleted Atlas page {envelope.get('slug') or page_id}",
                detail=f"revision={envelope['revision']} revisions_removed={len(chain)}",
            )
            revisions_removed = 0
            for snapshot, _revision in chain:
                if self.pages.repository.delete_memory(
                    transaction,
                    owner_id=caller,
                    memory_id=snapshot.memory_id,
                    expected_updated_at=snapshot.updated_at,
                ):
                    revisions_removed += 1
            if not self.pages.repository.delete_memory(
                transaction,
                owner_id=caller,
                memory_id=page_id,
                expected_updated_at=row.updated_at,
            ):
                current = self.pages.repository.get_memory(
                    transaction, owner_id=caller, memory_id=page_id
                )
                raise AtlasConflict(
                    "page_revision_conflict", current_revision=_revision_of(current)
                )
            return PageSummary(
                page_id=page_id,
                owner_id=caller,
                project_id=envelope.get("project_id"),
                slug=envelope.get("slug") or "",
                title=envelope.get("title") or "",
                revision=envelope["revision"],
                sources=tuple(envelope.get("sources") or ()),
                created_at=row.created_at or 0,
                updated_at=row.updated_at or 0,
                deleted=True,
                revisions_removed=revisions_removed,
                links_removed=links_removed,
            )

        return self._run(accept)

    def list_pages(
        self, *, owner_id: str, project_id: str | None, limit: int = 50
    ) -> tuple[PageSummary, ...]:
        return self.list_page_batch(owner_id=owner_id, project_id=project_id, limit=limit).pages

    def list_page_batch(
        self, *, owner_id: str, project_id: str | None, limit: int = 50,
        cursor: str | None = None,
    ) -> PageBatch:
        caller = self._author(owner_id)
        project = normalize_project(project_id)
        if type(limit) is not int or not 1 <= limit <= _MAX_LIST:
            raise AtlasPageError("limit_out_of_bounds")
        decoded = decode_page_cursor(cursor, caller, project)
        continuation = (MemoryEnvelopeCursor(caller, project, FORMAT, KIND_PAGE, *decoded)
                        if decoded is not None else None)

        def accept(transaction):
            page = self.pages.repository.list_memory_envelope_page(
                transaction,
                owner_id=caller,
                project_id=project,
                envelope_format=FORMAT,
                envelope_kind=KIND_PAGE,
                cursor=continuation,
                limit=limit,
            )
            summaries: list[PageSummary] = []
            for row in page.records:
                envelope = parse_envelope(row.value)
                if (envelope is None or envelope.get("kind") != KIND_PAGE
                        or envelope.get("owner_id") != caller
                        or envelope.get("project_id") != project
                        or envelope.get("page_id") != row.memory_id):
                    raise AtlasUnavailable("atlas_page_unreadable")
                self._opened(envelope)
                summaries.append(self._summary(row, envelope))
            next_cursor = (encode_page_cursor(caller, project, page.next_cursor.created_at,
                                             page.next_cursor.memory_id)
                           if page.next_cursor is not None else None)
            return PageBatch(tuple(summaries), next_cursor)

        return self._run(accept)

    def page_history(
        self, *, owner_id: str, project_id: str | None, reference: str
    ) -> tuple[PageSummary, ...]:
        caller = self._author(owner_id)
        project = normalize_project(project_id)
        page_id = self._resolve_reference(caller, project, reference)

        def accept(transaction):
            row, envelope = self._head(transaction, caller, page_id, project)
            entries = [self._summary(row, envelope)]
            for snapshot, revision in self._revision_chain(
                transaction, caller, page_id, envelope.get("prev_revision_id"),
                limit=MAX_HISTORY,
            ):
                entries.append(self._summary(snapshot, revision))
            return tuple(entries)

        return self._run(accept)

    def page_links(
        self, *, owner_id: str, project_id: str | None, reference: str
    ) -> tuple[PageSummary, ...]:
        caller = self._author(owner_id)
        project = normalize_project(project_id)
        page_id = self._resolve_reference(caller, project, reference)

        def accept(transaction):
            self._head(transaction, caller, page_id, project)
            neighbors = self.graph.repository.linked_ids(
                transaction, owner_id=caller, memory_id=page_id, limit=_MAX_LIST
            )
            summaries: list[PageSummary] = []
            for neighbor in neighbors:
                row = self.pages.repository.get_memory(
                    transaction, owner_id=caller, memory_id=neighbor
                )
                if row is None or row.superseded_at is not None:
                    continue
                envelope = parse_envelope(row.value)
                if envelope is None or envelope.get("kind") != KIND_PAGE:
                    continue
                summaries.append(self._summary(row, envelope))
            return tuple(summaries)

        return self._run(accept)

    def _resolve_reference(
        self, owner_id: str, project_id: str | None, reference: object
    ) -> str:
        if not isinstance(reference, str) or not reference.strip():
            raise AtlasPageError("page_reference_invalid")
        reference = reference.strip()
        resolved = canonical_page_id(reference)
        if resolved is not None:
            return resolved
        return page_identity(owner_id, project_id, normalize_slug(reference))

    def _run(self, accept):
        try:
            with self.runtime.transaction() as transaction:
                return accept(transaction)
        except AtlasPageError:
            raise
        except (AtlasNotFound, AtlasConflict, AtlasUnavailable, AtlasRefused):
            raise
        except RepositoryNotFoundError:
            raise AtlasNotFound() from None
        except RepositoryConflictError:
            raise AtlasConflict("page_revision_conflict") from None
        except Exception:
            raise AtlasUnavailable("atlas_storage_failed") from None


def _snapshot_sealed(next_sealed: PageSealed, previous_envelope: dict) -> PageSealed:
    return PageSealed(
        page_id=next_sealed.page_id,
        owner_id=next_sealed.owner_id,
        project_id=next_sealed.project_id,
        slug=next_sealed.slug,
        title=previous_envelope.get("title") or "",
        revision=previous_envelope["revision"],
        sources=tuple(previous_envelope.get("sources") or ()),
        ciphertext=previous_envelope["body"],
    )


def _revision_of(row: MemoryRecord | None) -> int | None:
    if row is None:
        return None
    envelope = parse_envelope(row.value)
    if envelope is None:
        return None
    revision = envelope.get("revision")
    return revision if isinstance(revision, int) else None


__all__ = (
    "AtlasConflict",
    "AtlasNotFound",
    "AtlasRefused",
    "AtlasUnavailable",
    "AtlasPageService",
    "PageInput",
    "PageSummary",
)
