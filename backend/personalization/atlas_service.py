"""Authorized Atlas page lifecycle over AstralPlane's qualified personalization facade:
mutations validate input, re-check owner/project authorization and the PHI gate, seal
the body with the host key, then audit and write page heads, bounded revision chains,
and graph links inside one Plane transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from astralplane.repositories import RepositoryConflictError, RepositoryNotFoundError
from astralplane.repositories.preferences import MemoryRecord
from cryptography.fernet import Fernet

from audit.repository import AuditRepository
from audit.schemas import AuditEventCreate
from orchestrator.plane_repository_context import PlaneRepositoryContext, repository_from
from personalization.atlas_pages import (
    KIND_PAGE,
    KIND_REVISION,
    MAX_HISTORY,
    MAX_LINKS,
    MAX_REVISION,
    PageCipher,
    PageSealed,
    AtlasPageError,
    canonical_page_id,
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
from personalization.phi_gate import PHIGate, get_phi_gate

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

    def create_page(self, *, owner_id: str, page: PageInput) -> PageSummary:
        caller = self._author(owner_id)
        project_id = normalize_project(page.project_id)
        slug = normalize_slug(page.slug)
        title = normalize_title(page.title)
        body = normalize_body(page.body)
        sources = normalize_sources(page.sources)
        page_id = page_identity(caller, project_id, slug)
        self._gate(title, body, *sources)
        sealed = self._sealed(caller, project_id, page_id, slug, title, 1, sources, body)
        envelope = envelope_bytes(sealed, kind=KIND_PAGE, prev_revision_id=None)
        now = _now_ms()

        def accept(transaction):
            existing = self.pages.repository.get_memory(
                transaction, owner_id=caller, memory_id=page_id
            )
            if existing is not None:
                raise AtlasConflict("page_already_exists", current_revision=_revision_of(existing))
            targets = self._resolve_link_targets(
                transaction, caller, project_id, page_id, page.links
            )
            self._audit_in_transaction(
                transaction,
                owner_id=caller,
                action="create",
                description=f"Created Atlas page {slug}",
                detail=f"project={project_id or 'global'} revision=1",
            )
            row = self.pages.repository.create_memory(
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
            row, envelope = self._head(transaction, caller, page_id, project)
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
            self._gate(next_title, next_body, *next_sources)
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
            snapshot = self.pages.repository.create_memory(
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
            self.pages.repository.supersede_memory(
                transaction,
                owner_id=caller,
                memory_id=snapshot.memory_id,
                superseded_at=now,
                replacement_id=page_id,
            )
            head_envelope = envelope_bytes(sealed, kind=KIND_PAGE, prev_revision_id=snapshot.memory_id)
            try:
                updated = self.pages.repository.update_memory_value(
                    transaction,
                    owner_id=caller,
                    memory_id=page_id,
                    value=head_envelope,
                    signature=envelope_fingerprint(parse_envelope(head_envelope) or {}),
                    updated_at=_after_ms(now),
                    expected_updated_at=row.updated_at or 0,
                )
            except RepositoryConflictError:
                current = self.pages.repository.get_memory(
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
        caller = self._author(owner_id)
        project = normalize_project(project_id)
        if not isinstance(limit, int) or not 1 <= limit <= _MAX_LIST:
            raise AtlasPageError("limit_out_of_bounds")

        def accept(transaction):
            rows = self.pages.repository.list_memory(
                transaction,
                owner_id=caller,
                project_id=project,
                include_global=False,
                global_only=project is None,
                limit=_MAX_LIST,
            )
            summaries: list[PageSummary] = []
            for row in rows:
                envelope = parse_envelope(row.value)
                if envelope is None or envelope.get("kind") != KIND_PAGE:
                    continue
                if envelope.get("owner_id") != caller:
                    continue
                summaries.append(self._summary(row, envelope))
                if len(summaries) >= limit:
                    break
            return tuple(summaries)

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
