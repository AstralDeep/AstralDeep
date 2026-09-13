"""The sole current owner-skill authority, backed by Plane revisions.

Legacy files are read only for an unmarked owner. All application consumers use
this facade; after cutover the retained files are recovery evidence, never a
second current catalog. The owner SQL fence linearizes application mutations;
file descriptor/fingerprint rechecks detect ordinary concurrent edits, but do
not claim a distributed transaction with an external filesystem editor.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import os
import stat
import threading
from uuid import uuid4

from audit.repository import AuditRepository
from audit.schemas import AuditEventCreate, AuditEventDTO
from orchestrator import user_skills as legacy
from orchestrator.human_request_authority import CurrentHumanCaller
from orchestrator.user_skill_materialization import (
    LegacySkillFile, LegacySkillMaterializationError, prepare_legacy_skill_materialization,
)
from persistent_agents.models import AssignmentError


class SkillCatalogError(AssignmentError):
    """A data-free conflict or refusal; authored values never enter diagnostics."""


@dataclass(frozen=True, repr=False)
class CurrentSkill(legacy.Skill):
    skill_id: str = ''
    revision: int = 0

    def __repr__(self):
        return '<CurrentSkill private>'

    def public(self):
        return {**super().public(), 'skill_id': self.skill_id, 'revision': self.revision}


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True, repr=False)
class _LegacyCapture:
    files: tuple
    directories: tuple
    missing: str | None


def capture_legacy_files(knowledge_dir, owner_id):
    """Bounded no-follow capture, including exact missing-directory evidence.

    A component observed present may never become an empty catalog on open
    failure. Absence is accepted only beneath a stable, open existing parent;
    both captures must retain the same directory identities and absent suffix.
    """
    descriptors = []
    identities = []
    links = []
    def verify():
        for fd, (parent, name), identity in zip(descriptors, links, identities):
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _fingerprint(os.fstat(fd)) != identity or _fingerprint(current) != identity:
                raise ValueError
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = None
        parts = (os.fspath(knowledge_dir), legacy.SUBDIR,
                 os.path.basename(legacy.owner_dir(knowledge_dir, owner_id)))
        for index, name in enumerate(parts):
            parent = fd
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if parent is None:
                    raise ValueError
                verify()
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    verify()
                    return _LegacyCapture((), tuple(identities), '/'.join(parts[index:]))
                raise ValueError
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError
            fd = os.open(name, flags, dir_fd=parent)
            descriptors.append(fd)
            links.append((parent, name))
            identities.append(_fingerprint(info))
            verify()
        names = sorted(os.listdir(fd))
        if len(names) > legacy.MAX_SKILLS:
            raise ValueError
        files = []
        for name in names:
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                info = os.fstat(file_fd)
                if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 32768 or info.st_nlink != 1:
                    raise ValueError
                raw = bytearray()
                while len(raw) <= 32768:
                    chunk = os.read(file_fd, min(8192, 32769 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                if (_fingerprint(os.fstat(file_fd)) != _fingerprint(info)
                        or _fingerprint(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _fingerprint(info)):
                    raise ValueError
                files.append(LegacySkillFile(name, bytes(raw)))
            finally:
                os.close(file_fd)
        verify()
        if names != sorted(os.listdir(fd)):
            raise ValueError
        return _LegacyCapture(tuple(files), tuple(identities), None)
    except (OSError, ValueError, LegacySkillMaterializationError):
        raise SkillCatalogError('skill_materialization_conflict', 409) from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


class UserSkillFacade:
    """Current human mutations and explicitly admitted, read-only turn guidance."""

    def __init__(self, orchestrator, knowledge_dir):
        from astralplane.repositories.guidance import SkillsRepository
        self.orch, self.knowledge_dir = orchestrator, os.fspath(knowledge_dir)
        plane = getattr(getattr(orchestrator, 'runtime_composition', None), 'plane', None)
        self.runtime = getattr(plane, 'runtime', None)
        self.repositories = getattr(self.runtime, 'repositories', None)
        self.repository = getattr(getattr(self.repositories, 'preferences', None), 'skills', None)
        self.audit = getattr(orchestrator, 'audit_repo', None)
        if type(self.repository) is not SkillsRepository or type(self.audit) is not AuditRepository:
            raise SkillCatalogError('skill_unavailable', 503)
        # SQL owner locking spans processes. This bounded local fence also keeps
        # this facade's filesystem captures together through marker insertion.
        self._capture_lock = threading.Lock()

    def _current(self, caller, *, read_only=False):
        from orchestrator.turn_guidance_authority import TurnGuidanceReader
        allowed = (CurrentHumanCaller, TurnGuidanceReader) if read_only else (CurrentHumanCaller,)
        if type(caller) not in allowed:
            raise SkillCatalogError('skill_authentication_required', 401)
        plane = getattr(getattr(self.orch, 'runtime_composition', None), 'plane', None)
        if (not legacy.enabled() or caller.runtime is not self.runtime
                or caller.repositories is not self.repositories or caller.audit_repo is not self.audit
                or getattr(plane, 'runtime', None) is not self.runtime
                or getattr(plane, 'repositories', None) is not self.repositories
                or self.runtime.repositories is not self.repositories
                or self.repositories.preferences.skills is not self.repository
                or getattr(self.orch, 'audit_repo', None) is not self.audit
                or self.audit._audit.plane_runtime is not self.runtime
                or self.audit._audit.repository is not self.repositories.audit):
            raise SkillCatalogError('skill_unavailable', 503)

    def _catalog(self, tx, caller, *, read_only=False):
        """Called only inside the original caller's bounded transaction."""
        from astralplane.repositories.guidance_models import LegacySkillEntry, SkillDefinition
        from orchestrator.slash_commands import reserved_names
        marker = self.repository.get_materialization(tx, owner_id=caller.owner_id)
        if marker is not None:
            return marker
        if not self._capture_lock.acquire(blocking=False):
            raise SkillCatalogError('skill_materialization_busy', 409)
        try:
            files = capture_legacy_files(self.knowledge_dir, caller.owner_id)
            prepared = prepare_legacy_skill_materialization(caller.owner_id, files=files.files,
                                                            reserved_aliases=tuple(sorted(reserved_names())))
            entries = tuple(LegacySkillEntry(str(uuid4()), e.slug,
                SkillDefinition(**asdict(e.definition)), e.markdown, e.format, e.legacy_updated_at)
                for e in prepared.entries)
            # materialize takes owner 79 before inspecting the marker. A second
            # exact capture after that wait prevents adopting changed file input.
            result = self.repository.materialize_legacy_skills(tx, owner_id=caller.owner_id,
                entries=entries, manifest_digest=prepared.manifest_digest)
            if not result.replayed:
                self._audit_materialization(tx, caller, result, read_only=read_only)
            if capture_legacy_files(self.knowledge_dir, caller.owner_id) != files:
                raise SkillCatalogError('skill_materialization_conflict', 409)
            self._current(caller, read_only=read_only)
            return result
        except LegacySkillMaterializationError:
            raise SkillCatalogError('skill_materialization_conflict', 409) from None
        finally:
            self._capture_lock.release()

    async def _transaction(self, caller, operation, *, delivery_check=None, read_only=False):
        from astralplane.repositories import (
            RepositoryConflictError, RepositoryDataError, RepositoryNotFoundError,
            RepositoryValidationError,
        )
        self._current(caller, read_only=read_only)
        def apply(tx, repositories):
            self._current(caller, read_only=read_only)
            if repositories is not self.repositories:
                raise SkillCatalogError('skill_unavailable', 503)
            try:
                self.repository.lock_owner(tx, owner_id=caller.owner_id)
                self._catalog(tx, caller, read_only=read_only)
                value = operation(tx)
            except RepositoryNotFoundError:
                raise SkillCatalogError('skill_not_found', 404) from None
            except RepositoryConflictError:
                raise SkillCatalogError('skill_conflict', 409) from None
            except RepositoryValidationError:
                raise SkillCatalogError('skill_invalid', 422) from None
            except RepositoryDataError:
                raise SkillCatalogError('skill_unavailable', 503) from None
            self._current(caller, read_only=read_only)
            return value
        result = await caller.transaction(apply, expected_orchestrator=self.orch)
        await caller.verify_delivery()
        if delivery_check is not None:
            def final(tx, repositories):
                self._current(caller, read_only=read_only)
                if repositories is not self.repositories:
                    raise SkillCatalogError('skill_unavailable', 503)
                self.repository.lock_owner(tx, owner_id=caller.owner_id)
                delivery_check(tx, result)
                self._current(caller, read_only=read_only)
            await caller.transaction(final, expected_orchestrator=self.orch)
        self._current(caller, read_only=read_only)
        return result

    def _skill(self, tx, owner_id, head):
        snapshot = self.repository.get_revision(tx, owner_id=owner_id,
            skill_id=head.skill_id, revision=head.revision)
        if (snapshot is None or snapshot.deleted
                or snapshot.definition_digest != head.definition_digest
                or self.repository.get(tx, owner_id=owner_id, skill_id=head.skill_id) != head):
            raise SkillCatalogError('skill_conflict', 409)
        definition = snapshot.definition
        updated_at = head.updated_at // 1000
        if snapshot.legacy_markdown is not None:
            from orchestrator.slash_commands import reserved_names
            exact = prepare_legacy_skill_materialization(owner_id,
                files=(LegacySkillFile(head.slug + '.md', snapshot.legacy_markdown),),
                reserved_aliases=tuple(sorted(reserved_names())))
            if asdict(exact.entries[0].definition) != asdict(definition):
                raise SkillCatalogError('skill_unavailable', 503)
            updated_at = exact.entries[0].legacy_updated_at
        return CurrentSkill(head.slug, definition.name, definition.instructions,
            definition.applies_to, definition.alias, definition.enabled, updated_at,
            head.skill_id, head.revision)

    async def list(self, *, caller):
        def read(tx):
            heads = self.repository.list(tx, owner_id=caller.owner_id, limit=20)
            result = tuple(self._skill(tx, caller.owner_id, head) for head in heads)
            return tuple(sorted(result, key=lambda value: value.name.lower())), heads
        def unchanged(tx, captured):
            if self.repository.list(tx, owner_id=caller.owner_id, limit=20) != captured[1]:
                raise SkillCatalogError('skill_conflict', 409)
        result = await self._transaction(caller, read, delivery_check=unchanged, read_only=True)
        return result[0]

    def _append_audit(self, tx, caller, event, *, read_only=False):
        from orchestrator.work_submit import _sync
        result = _sync(self.audit.insert_in_transaction(event, transaction=tx, plane_runtime=self.runtime))
        if type(result) is not AuditEventDTO:
            raise SkillCatalogError('skill_audit_unavailable', 503)
        self._current(caller, read_only=read_only)

    def _audit_materialization(self, tx, caller, result, *, read_only):
        now = datetime.now(timezone.utc)
        event = AuditEventCreate(actor_user_id=caller.owner_id, auth_principal=caller.owner_id,
            event_class='settings', action_type='user_skill_materialize',
            description='Owner legacy skill catalog materialized', outcome='success',
            correlation_id=str(uuid4()),
            outputs_meta={'count': len(result.mappings), 'manifest_digest': result.manifest_digest,
                          'skill_ids': sorted(value.skill_id for value in result.mappings)},
            started_at=now, completed_at=now)
        self._append_audit(tx, caller, event, read_only=read_only)

    def _audit(self, tx, caller, prepared):
        command = prepared.command
        now = datetime.now(timezone.utc)
        event = AuditEventCreate(actor_user_id=caller.owner_id, auth_principal=caller.owner_id,
            event_class='settings', action_type='user_skill_' + command.command,
            description='Owner skill revision command', correlation_id=command.skill_id,
            outcome='success', outputs_meta={'skill_id': command.skill_id,
                'command_id': command.command_id, 'revision': command.expected_revision + 1},
            started_at=now, completed_at=now)
        self._append_audit(tx, caller, event)

    async def _change(self, caller, build):
        self._current(caller)
        caller.require_write()
        def change(tx):
            command = build(tx)
            prepared = self.repository.prepare_change(tx, command=command)
            if not prepared.replayed:
                self._audit(tx, caller, prepared)
            result = self.repository.apply_change(tx, command=command)
            # A final failure aborts the outer transaction including audit.
            if result.replayed != prepared.replayed:
                raise SkillCatalogError('skill_conflict', 409)
            return result
        return await self._transaction(caller, change)

    async def save(self, *, caller, skill_id, command_id, expected_revision, name,
                   instructions, applies_to, command='', enabled=True, slug=''):
        from astralplane.repositories.guidance_models import SkillCommand, SkillDefinition
        from orchestrator.slash_commands import reserved_names
        self._current(caller)
        try:
            if any(type(value) is not str for value in (name, instructions, command, slug)):
                raise ValueError
            alias = command.strip().lstrip('/').lower()
            if alias in reserved_names():
                raise ValueError
            applies = legacy._normalise_applies(applies_to)
            # Do not silently truncate a newly submitted applicability selection.
            if type(applies_to) not in (str, list, tuple):
                raise ValueError
            if type(applies_to) in (list, tuple) and len(applies_to) > legacy.MAX_APPLIES_TO:
                raise ValueError
            if type(applies_to) is str and len([p for p in applies_to.replace('\n', ',').split(',') if p.strip()]) > legacy.MAX_APPLIES_TO:
                raise ValueError
            definition = SkillDefinition(name.strip(), instructions.strip(), applies, alias, enabled)
            intent = SkillCommand(caller.owner_id, skill_id, command_id,
                'create' if expected_revision == 0 else 'replace', expected_revision,
                legacy.slugify(name) if expected_revision == 0 else None, definition)
            if bool(slug) != bool(expected_revision):
                raise ValueError
        except (TypeError, ValueError):
            raise SkillCatalogError('skill_invalid', 422) from None
        return await self._change(caller, lambda tx: intent)

    async def set_enabled(self, *, caller, skill_id, command_id, expected_revision, enabled):
        from astralplane.repositories.guidance_models import SkillCommand
        self._current(caller)
        if type(enabled) is not bool:
            raise SkillCatalogError('skill_invalid', 422)
        def build(tx):
            snapshot = self.repository.get_revision(tx, owner_id=caller.owner_id,
                skill_id=skill_id, revision=expected_revision)
            if snapshot is None:
                raise SkillCatalogError('skill_not_found', 404)
            return SkillCommand(caller.owner_id, skill_id, command_id, 'replace', expected_revision,
                                definition=replace(snapshot.definition, enabled=enabled))
        return await self._change(caller, build)

    async def delete(self, *, caller, skill_id, command_id, expected_revision):
        from astralplane.repositories.guidance_models import SkillCommand
        self._current(caller)
        intent = SkillCommand(caller.owner_id, skill_id, command_id, 'delete', expected_revision)
        return await self._change(caller, lambda tx: intent)
