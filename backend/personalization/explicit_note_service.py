"""Owner-encrypted note storage on Plane's personalization facade -- only ciphertext or
an erasure tombstone is durable; notes are ephemeral guidance, never audit or
citation content. Exposed via orchestrator's projection_surfaces/guidance.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import logging
import time
from uuid import UUID

from persistent_agents.models import AssignmentError
from personalization.explicit_notes import (
    EncryptedExplicitNote, ExplicitNoteCipher, ExplicitNoteMetadata,
    ExplicitNoteUnavailable, MAX_COUNTER, MAX_LIVE_NOTE_REVISION, NOTE_CATEGORIES,
    OpenedExplicitNote, normalize_note_value,
)

logger = logging.getLogger("Orchestrator.ExplicitNotes")


@dataclass(frozen=True, slots=True)
class ExplicitNoteCommand:
    command: str
    note_id: str
    expected_revision: int
    version: int = 1
    category: str | None = None
    value: str | None = field(default=None, repr=False)
    enabled: bool | None = None
    expires_at: int | None = None


@dataclass(frozen=True, slots=True)
class ExplicitNotePage:
    notes: tuple[OpenedExplicitNote, ...] = field(repr=False)
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ExplicitNoteExpiryBatch:
    scanned: int
    erased: int
    skipped: int
    cycle_complete: bool


def _identifier(value):
    parsed = UUID(value) if type(value) is str else None
    if parsed is None or parsed.version != 4 or str(parsed) != value:
        raise ValueError
    return value


def _integer(value, minimum=0, maximum=MAX_COUNTER):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError
    return value


def _now():
    return time.time_ns() // 1_000_000


def _unavailable():
    raise AssignmentError("explicit_note_unavailable", 503)


class ExplicitNoteService:
    def __init__(self, orchestrator):
        from audit.repository import AuditRepository
        from orchestrator.credential_manager import CredentialManager
        from orchestrator.human_request_authority import HumanRequestBoundary
        self.orch = orchestrator
        self.boundary = getattr(orchestrator, "human_request_boundary", None)
        self.credentials = getattr(orchestrator, "credential_manager", None)
        self.audit = getattr(orchestrator, "audit_repo", None)
        if (type(self.boundary) is not HumanRequestBoundary
                or type(self.credentials) is not CredentialManager
                or type(self.audit) is not AuditRepository):
            _unavailable()
        self.runtime = self.boundary.plane_runtime
        self.repositories = self.boundary.repositories
        self.preferences = self.repositories.preferences
        self.notes = self.preferences.personalization
        self.audit_context = self.audit._audit
        self.credential_context = self.credentials._credentials
        self.fernet = self.credentials._fernet
        try:
            self.cipher = ExplicitNoteCipher(self.fernet)
        except ExplicitNoteUnavailable:
            _unavailable()
        self.adapter = self.boundary.adapter
        self._expiry_lock = asyncio.Lock()
        self._expiry_cursor = None
        self._expiry_cutoff = None
        self._privacy_slots = asyncio.Semaphore(2)
        self._privacy_tasks = set()
        self._current()

    def _current(self, caller=None):
        from orchestrator.human_request_authority import CurrentHumanCaller
        plane = getattr(getattr(self.orch, "runtime_composition", None), "plane", None)
        if (getattr(self.orch, "human_request_boundary", None) is not self.boundary
                or self.boundary.closed or self.boundary.orchestrator is not self.orch
                or self.boundary.plane_runtime is not self.runtime
                or self.boundary.repositories is not self.repositories
                or self.boundary.adapter is not self.adapter
                or self.adapter.repositories is not self.repositories
                or getattr(plane, "runtime", None) is not self.runtime
                or getattr(plane, "repositories", None) is not self.repositories
                or self.runtime.repositories is not self.repositories
                or self.repositories.preferences is not self.preferences
                or self.preferences.personalization is not self.notes
                or getattr(self.orch, "credential_manager", None) is not self.credentials
                or self.credentials._credentials is not self.credential_context
                or self.credential_context.plane_runtime is not self.runtime
                or self.credential_context.repository is not self.repositories.credentials
                or self.credentials._fernet is not self.fernet or self.cipher._fernet is not self.fernet
                or getattr(self.orch, "audit_repo", None) is not self.audit
                or self.boundary.audit_repo is not self.audit
                or self.audit._audit is not self.audit_context
                or self.audit_context.plane_runtime is not self.runtime
                or self.audit_context.repository is not self.repositories.audit):
            _unavailable()
        if caller is not None and (type(caller) is not CurrentHumanCaller
                or caller.plane_runtime is not self.runtime or caller.repositories is not self.repositories
                or caller.audit_repo is not self.audit or caller._binding.boundary is not self.boundary):
            raise AssignmentError("explicit_note_authentication_required", 401)

    @staticmethod
    def snapshot(body):
        try:
            if type(body) is not ExplicitNoteCommand:
                raise ValueError
            body = replace(body)
            _integer(body.version, 1, 1)
            _identifier(body.note_id)
            _integer(body.expected_revision, 0, MAX_LIVE_NOTE_REVISION)
            if type(body.command) is not str or body.command not in {"save", "set_enabled", "forget"}:
                raise ValueError
            if body.command == "save":
                if (type(body.category) is not str or body.category not in NOTE_CATEGORIES
                        or type(body.enabled) is not bool):
                    raise ValueError
                body = replace(body, value=normalize_note_value(body.value))
                if body.expires_at is not None:
                    _integer(body.expires_at)
            else:
                if (body.expected_revision == 0 or body.value is not None
                        or body.category is not None or body.expires_at is not None
                        or (body.command == "set_enabled" and type(body.enabled) is not bool)
                        or (body.command == "forget" and body.enabled is not None)):
                    raise ValueError
            return body
        except (ValueError, TypeError, AttributeError):
            raise AssignmentError("explicit_note_command_invalid", 422) from None

    async def _transaction(self, caller, callback):
        self._current(caller)
        if caller is None:
            raise AssignmentError("explicit_note_authentication_required", 401)

        def guarded(tx, repositories):
            self._current(caller)
            if repositories is not self.repositories:
                _unavailable()
            self.notes.lock_explicit_note_owner(tx, owner_id=caller.owner_id)
            result = callback(tx, self.notes)
            self._current(caller)
            return result

        return await caller.transaction(guarded, expected_orchestrator=self.orch)

    def _encrypted(self, record):
        from astralplane.repositories.guidance_models import ExplicitNoteRecord
        if type(record) is not ExplicitNoteRecord:
            raise AssignmentError("explicit_note_not_found", 404)
        try:
            metadata = ExplicitNoteMetadata(**{key: value for key, value in asdict(record).items()
                                               if key != "ciphertext"})
            return EncryptedExplicitNote(metadata, record.ciphertext)
        except ExplicitNoteUnavailable:
            _unavailable()

    def _open(self, record, owner_id):
        try:
            return self.cipher.open(self._encrypted(record), owner_id=owner_id,
                                    now_ms=_now(), require_enabled=False)
        except ExplicitNoteUnavailable:
            _unavailable()

    def _audit(self, tx, *, owner_id, principal, action, record):
        from audit.schemas import AuditEventCreate, AuditEventDTO
        from orchestrator.work_submit import _sync
        now = datetime.now(timezone.utc)
        event = AuditEventCreate(actor_user_id=owner_id, auth_principal=principal,
            event_class="settings", action_type="explicit_note_" + action,
            description="Private note metadata change", correlation_id=record.note_id,
            outcome="success", outputs_meta={"note_id": record.note_id, "revision": record.revision},
            started_at=now, completed_at=now)
        try:
            saved = _sync(self.audit.insert_in_transaction(
                event, transaction=tx, plane_runtime=self.runtime))
            if type(saved) is not AuditEventDTO:
                raise ValueError
        except Exception:
            raise AssignmentError("explicit_note_audit_unavailable", 503) from None
        self._current()

    async def _privacy(self, caller, value):
        from personalization.phi_gate import get_phi_gate

        def scan():
            return get_phi_gate().contains_phi(value)

        def finished(task):
            self._privacy_tasks.discard(task)
            self._privacy_slots.release()
            if not task.cancelled():
                task.exception()

        try:
            async with asyncio.timeout_at(caller._deadline):
                await self._privacy_slots.acquire()
                task = asyncio.create_task(asyncio.to_thread(scan), name="explicit-note-privacy")
                self._privacy_tasks.add(task)
                task.add_done_callback(finished)
                refused = await asyncio.shield(task)
        except TimeoutError:
            raise AssignmentError("explicit_note_privacy_timeout", 408) from None
        except Exception:
            raise AssignmentError("explicit_note_privacy_unavailable", 503) from None
        self._current(caller)
        if refused is not False:
            raise AssignmentError("explicit_note_sensitive_content_refused", 422)

    async def command(self, *, caller, body):
        from astralplane.repositories.guidance_models import ExplicitNoteRecord, ExplicitNoteTombstone
        self._current(caller)
        if caller is None:
            raise AssignmentError("explicit_note_authentication_required", 401)
        caller.require_write()
        command = self.snapshot(body)
        if command.command == "save":
            await self._privacy(caller, command.value)

        def accept(tx, repository):
            if command.command == "forget":
                prepared = repository.prepare_explicit_note_retirement(tx, owner_id=caller.owner_id,
                    note_id=command.note_id, expected_revision=command.expected_revision, reason="forgotten")
                result = repository.forget_explicit_note(tx, owner_id=caller.owner_id,
                    note_id=command.note_id, expected_revision=command.expected_revision)
                if not prepared.replayed:
                    self._audit(tx, owner_id=caller.owner_id, principal=caller.owner_id,
                                action="forget", record=result)
                return result
            prepared = repository.prepare_explicit_note(tx, owner_id=caller.owner_id,
                note_id=command.note_id, expected_revision=command.expected_revision)
            current = prepared.current
            now = prepared.observed_at_ms
            try:
                if command.command == "save":
                    metadata = ExplicitNoteMetadata(owner_id=caller.owner_id, note_id=command.note_id,
                        revision=command.expected_revision+1, format_version=1, category=command.category,
                        enabled=command.enabled, created_at=now if current is None else current.created_at,
                        updated_at=now, expires_at=command.expires_at)
                else:
                    metadata = replace(self._encrypted(current).metadata,
                        revision=command.expected_revision+1, enabled=command.enabled, updated_at=now)
                encrypted = (self.cipher.seal(metadata, command.value) if current is None else
                    self.cipher.revise(self._encrypted(current), metadata, owner_id=caller.owner_id,
                                       now_ms=now, value=command.value))
            except ExplicitNoteUnavailable:
                raise AssignmentError("explicit_note_current_value_unavailable", 409) from None
            record = ExplicitNoteRecord(**asdict(encrypted.metadata), ciphertext=encrypted.ciphertext)
            result = repository.put_explicit_note(tx, preparation=prepared, record=record)
            self._audit(tx, owner_id=caller.owner_id, principal=caller.owner_id,
                        action=command.command, record=result)
            if repository.get_explicit_note(tx, owner_id=caller.owner_id,
                                            note_id=command.note_id) != result:
                raise AssignmentError("explicit_note_changed", 409)
            return result

        result = await self._transaction(caller, accept)
        await caller.verify_delivery()
        self._current(caller)
        # Returns metadata only; never plaintext or ciphertext
        if type(result) is ExplicitNoteTombstone:
            return result
        return self._encrypted(result).metadata

    async def _deliver(self, caller, records):
        await caller.verify_delivery()

        def current(tx, repository):
            for record in records:
                if repository.get_explicit_note(tx, owner_id=caller.owner_id,
                                                note_id=record.note_id) != record:
                    raise AssignmentError("explicit_note_changed", 409)
            opened = tuple(self._open(record, caller.owner_id) for record in records)
            expiring = tuple(record for record in records if record.expires_at is not None)
            if expiring:
                earliest = min(expiring, key=lambda record: record.expires_at)
                if repository.get_explicit_note(tx, owner_id=caller.owner_id,
                                                note_id=earliest.note_id) != earliest:
                    raise AssignmentError("explicit_note_changed", 409)
            return opened

        opened = await self._transaction(caller, current)
        now = _now()
        if any(note.metadata.updated_at > now or (
                note.metadata.expires_at is not None and note.metadata.expires_at <= now)
                for note in opened):
            raise AssignmentError("explicit_note_changed", 409)
        return opened

    async def get(self, *, caller, note_id):
        try:
            note_id = _identifier(note_id)
        except (ValueError, TypeError, AttributeError):
            raise AssignmentError("explicit_note_query_invalid", 422) from None
        record = await self._transaction(caller, lambda tx, repository:
            repository.get_explicit_note(tx, owner_id=caller.owner_id, note_id=note_id))
        self._encrypted(record)
        return (await self._deliver(caller, (record,)))[0]

    async def list(self, *, caller, after_id=None, limit=50, search=""):
        try:
            _integer(limit, 1, 100)
            if after_id is not None:
                after_id = _identifier(after_id)
            if type(search) is not str or len(search.encode("utf-8")) > 256:
                raise ValueError
            search = normalize_note_value(search).casefold() if search.strip() else ""
        except (ValueError, TypeError, AttributeError):
            raise AssignmentError("explicit_note_query_invalid", 422) from None
        records = await self._transaction(caller, lambda tx, repository:
            repository.list_explicit_notes(tx, owner_id=caller.owner_id,
                                            after_id=after_id, limit=limit, include_disabled=True))
        opened = await self._deliver(caller, records)
        matches = tuple(note for note in opened if not search or search in note.value.casefold())
        return ExplicitNotePage(matches, records[-1].note_id if len(records) == limit else None)

    async def verify_snapshot(self, *, caller, notes):
        self._current(caller)
        try:
            if caller is None or type(notes) is not tuple or len(notes) > 100:
                raise ValueError
            snapshots = []
            for note in notes:
                if type(note) is not OpenedExplicitNote or type(note.metadata) is not ExplicitNoteMetadata:
                    raise ValueError
                copied = OpenedExplicitNote(replace(note.metadata), note.value)
                if copied.metadata.owner_id != caller.owner_id or normalize_note_value(copied.value) != copied.value:
                    raise ValueError
                snapshots.append(copied)
            expected = tuple(snapshots)
            if len({note.metadata.note_id for note in expected}) != len(expected):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise AssignmentError("explicit_note_snapshot_invalid", 422) from None

        records = await self._transaction(caller, lambda tx, repository: tuple(
            repository.get_explicit_note(tx, owner_id=caller.owner_id, note_id=note.metadata.note_id)
            for note in expected))
        for record, note in zip(records, expected):
            if self._encrypted(record).metadata != note.metadata:
                raise AssignmentError("explicit_note_changed", 409)
        if await self._deliver(caller, records) != expected:
            raise AssignmentError("explicit_note_changed", 409)

    async def expire_batch(self, *, limit=20):
        from astralplane.repositories import RepositoryConflictError, RepositoryNotFoundError
        try:
            _integer(limit, 1, 100)
        except ValueError:
            raise AssignmentError("explicit_note_expiry_limit_invalid", 422) from None
        self._current()

        async def transaction(operation):
            def guarded(tx):
                self._current()
                self.repositories.history.sessions.bound_request_execution_waits(tx)
                result = operation(tx)
                self._current()
                return result
            result = await self.adapter.run_in_transaction(guarded)
            self._current()
            return result

        async with asyncio.timeout(15), self._expiry_lock:
            page = await transaction(lambda tx: self.notes.page_expired_explicit_notes(tx,
                limit=limit, after=self._expiry_cursor, cutoff_ms=self._expiry_cutoff))
            erased = skipped = 0
            for candidate in page.records:
                def erase(tx):
                    prepared = self.notes.prepare_explicit_note_retirement(tx,
                        owner_id=candidate.owner_id, note_id=candidate.note_id,
                        expected_revision=candidate.revision, reason="expired", skip_locked=True)
                    if prepared is None or prepared.replayed:
                        return False
                    record = self.notes.expire_explicit_note(tx, owner_id=candidate.owner_id,
                        note_id=candidate.note_id, expected_revision=candidate.revision, skip_locked=True)
                    if record is None:
                        _unavailable()
                    self._audit(tx, owner_id=candidate.owner_id, principal="system:explicit-note-expiry",
                                action="expire", record=record)
                    return True
                try:
                    changed = await transaction(erase)
                except (RepositoryConflictError, RepositoryNotFoundError):
                    changed = False
                erased += int(changed)
                skipped += int(not changed)
            self._expiry_cursor = page.next_cursor
            self._expiry_cutoff = page.cutoff_ms if page.next_cursor is not None else None
            return ExplicitNoteExpiryBatch(len(page.records), erased, skipped, page.next_cursor is None)

    async def expiry_loop(self):
        while True:
            self._current()
            interval = 30
            try:
                batch = await self.expire_batch(limit=20)
                if not batch.cycle_complete:
                    interval = 1
            except Exception as exc:
                # Log only the exception type, never repository detail
                logger.warning("explicit_note_expiry_retry reason=%s", type(exc).__name__)
            await asyncio.sleep(interval)
