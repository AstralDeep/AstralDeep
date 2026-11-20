"""Owner-scoped persistence for skill catalogs and explicit notes, invalidating
dependent assignment/action guidance references under canonical lock order on
revision change. Used by orchestrator/user_skill_catalog.py and
repositories/assignments.py.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, replace

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.guidance_models import (
    MAX_REVISION,
    MAX_TIME,
    ExplicitNotePreparation,
    ExplicitNoteRecord,
    ExplicitNoteRetirementPreparation,
    ExplicitNoteTombstone,
    LegacySkillEntry,
    LegacySkillMapping,
    NoteExpiryCandidate,
    NoteExpiryCursor,
    NoteExpiryPage,
    SkillChangeResult,
    SkillCommand,
    SkillDefinition,
    SkillHead,
    SkillMaterialization,
    SkillPreparation,
    SkillReceipt,
    SkillRevisionRecord,
    canonical,
    digest,
    identifier,
    integer,
    legacy_directory_digest,
    legacy_manifest,
    owner,
    slug,
)


def _clock(query):
    return integer(
        query.fetch_one(
            "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint AS now_ms"
        )["now_ms"],
        maximum=MAX_TIME,
    )


def _lock_owner(tx, owner_id, *, retirement=False, skip_locked=False):
    owner(owner_id)
    if skip_locked:
        if not tx.fetch_one(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,79)) AS acquired", (owner_id,)
        )["acquired"]:
            return False
    else:
        tx.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner_id,))
    if retirement:
        return True
    state = tx.fetch_one(
        "SELECT state FROM astralplane_blob_owner_state WHERE owner_id=%s FOR UPDATE", (owner_id,)
    )
    if not retirement and state is not None and state["state"] != "active":
        raise RepositoryConflictError("guidance owner is retired")
    return True


def _lock_affected(tx, owner_id, kind, resource_id):
    from astralplane.repositories.assignments import AssignmentRepository

    return AssignmentRepository()._lock_guidance_dependants(tx, owner_id, kind, resource_id)


def _invalidate(tx, owner_id, kind, resource_id):
    from astralplane.repositories.assignments import AssignmentRepository

    AssignmentRepository()._invalidate_guidance_dependants(tx, owner_id, kind, resource_id)


def _skill_definition(value):
    if not isinstance(value, Mapping) or set(value) != {
        "name",
        "instructions",
        "applies_to",
        "alias",
        "enabled",
        "format_version",
    }:
        raise RepositoryDataError("invalid stored skill definition")
    value = dict(value)
    if type(value["applies_to"]) not in (list, tuple):
        raise RepositoryDataError("invalid stored skill applicability")
    value["applies_to"] = tuple(value["applies_to"])
    return SkillDefinition(**value)


def _head(row):
    if row is None:
        return None
    try:
        owner(row["owner_id"])
        identifier(str(row["skill_id"]))
        slug(row["slug"])
        integer(row["revision"], minimum=1)
        digest(row["definition_digest"])
        integer(row["created_at"], maximum=MAX_TIME)
        integer(row["updated_at"], minimum=row["created_at"], maximum=MAX_TIME)
        if row["deleted_at"] is not None:
            integer(row["deleted_at"], minimum=row["updated_at"], maximum=row["updated_at"])
            if row["enabled"] is not False:
                raise ValueError
        SkillDefinition(
            row["name"], "metadata check", tuple(row["applies_to"]), row["alias"], row["enabled"]
        )
        return SkillHead(
            **{
                **dict(row),
                "skill_id": str(row["skill_id"]),
                "applies_to": tuple(row["applies_to"]),
            }
        )
    except (KeyError, TypeError, ValueError, RepositoryValidationError) as exc:
        raise RepositoryDataError("invalid stored skill head") from exc


def _revision(row):
    if row is None:
        return None
    try:
        definition = _skill_definition(row["definition"])
        if (
            definition.definition_digest != row["definition_digest"]
            or type(row["deleted"]) is not bool
        ):
            raise ValueError
        owner(row["owner_id"])
        identifier(str(row["skill_id"]))
        integer(row["revision"], minimum=1)
        integer(row["created_at"], maximum=MAX_TIME)
        raw = None if row["legacy_markdown"] is None else bytes(row["legacy_markdown"])
        if raw is not None:
            if (
                len(raw) > 32768
                or not raw
                or hashlib.sha256(raw).hexdigest() != row["legacy_digest"]
                or row["revision"] != 1
            ):
                raise ValueError
            integer(row["legacy_updated_at"], maximum=MAX_TIME)
            raw.decode("utf-8")
        elif row["legacy_digest"] is not None or row["legacy_updated_at"] is not None:
            raise ValueError
        return SkillRevisionRecord(
            row["owner_id"],
            str(row["skill_id"]),
            row["revision"],
            definition,
            row["definition_digest"],
            row["created_at"],
            row["deleted"],
            raw,
            row["legacy_digest"],
        )
    except (KeyError, TypeError, ValueError, UnicodeError, RepositoryValidationError) as exc:
        raise RepositoryDataError("invalid stored skill revision") from exc


def _receipt(row):
    try:
        result = SkillReceipt(
            row["owner_id"],
            str(row["skill_id"]),
            str(row["command_id"]),
            row["command"],
            row["request_digest"],
            row["revision"],
            row["created_at"],
        )
        owner(result.owner_id)
        identifier(result.skill_id)
        identifier(result.command_id)
        digest(result.request_digest)
        integer(result.revision, minimum=1)
        integer(result.created_at, maximum=MAX_TIME)
        if result.command not in {"create", "replace", "delete"}:
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError, RepositoryValidationError) as exc:
        raise RepositoryDataError("invalid stored skill receipt") from exc


class SkillsRepository:
    def lock_owner(self, transaction, *, owner_id):
        _lock_owner(transaction, owner_id)

    def get(self, query, *, owner_id, skill_id, include_deleted=False):
        owner(owner_id)
        identifier(skill_id)
        if type(include_deleted) is not bool:
            raise RepositoryValidationError("include_deleted must be boolean")
        row = query.fetch_one(
            "SELECT * FROM owner_skill_head WHERE owner_id=%s AND skill_id=%s", (owner_id, skill_id)
        )
        head = _head(row)
        return head if head is None or head.deleted_at is None or include_deleted else None

    def get_by_slug(self, query, *, owner_id, slug):
        from astralplane.repositories.guidance_models import slug as validate_slug

        owner(owner_id)
        validate_slug(slug)
        return _head(
            query.fetch_one(
                (
                    "SELECT * FROM owner_skill_head WHERE owner_id=%s AND slug=%s AND delet"
                    "ed_at IS NULL"
                ),
                (owner_id, slug),
            )
        )

    def list(self, query, *, owner_id, include_disabled=True, limit=20):
        owner(owner_id)
        integer(limit, minimum=1, maximum=20)
        if type(include_disabled) is not bool:
            raise RepositoryValidationError("include_disabled must be boolean")
        return tuple(
            _head(row)
            for row in query.fetch_all(
                (
                    "SELECT * FROM owner_skill_head WHERE owner_id=%s AND deleted_at IS NUL"
                    "L AND (%s OR enabled) ORDER BY slug LIMIT %s"
                ),
                (owner_id, include_disabled, limit),
            )
        )

    def get_revision(self, query, *, owner_id, skill_id, revision):
        owner(owner_id)
        identifier(skill_id)
        integer(revision, minimum=1)
        return _revision(
            query.fetch_one(
                (
                    "SELECT * FROM owner_skill_revision WHERE owner_id=%s AND skill_id=%s A"
                    "ND revision=%s"
                ),
                (owner_id, skill_id, revision),
            )
        )

    def history(self, query, *, owner_id, skill_id, before_revision=None, limit=20):
        owner(owner_id)
        identifier(skill_id)
        integer(limit, minimum=1, maximum=100)
        if before_revision is not None:
            integer(before_revision, minimum=1)
        return tuple(
            _revision(row)
            for row in query.fetch_all(
                (
                    "SELECT * FROM owner_skill_revision WHERE owner_id=%s AND skill_id=%s A"
                    "ND (%s::bigint IS NULL OR revision<%s) ORDER BY revision DESC LIMIT %s"
                ),
                (owner_id, skill_id, before_revision, before_revision, limit),
            )
        )

    def prepare_change(self, transaction, *, command):
        if type(command) is not SkillCommand:
            raise RepositoryValidationError("typed skill command required")
        command = SkillCommand(
            **{
                **{k: v for k, v in asdict(command).items() if k != "definition"},
                "definition": None
                if command.definition is None
                else SkillDefinition(
                    **{**asdict(command.definition), "applies_to": command.definition.applies_to}
                ),
            }
        )
        _lock_owner(transaction, command.owner_id)
        old = transaction.fetch_one(
            (
                "SELECT owner_id,skill_id,command_id,command,request_digest,revision,cr"
                "eated_at FROM owner_skill_revision WHERE owner_id=%s AND command_id=%s"
            ),
            (command.owner_id, command.command_id),
        )
        if old is not None:
            receipt = _receipt(old)
            if (receipt.skill_id, receipt.command, receipt.request_digest) != (
                command.skill_id,
                command.command,
                command.request_digest,
            ):
                raise RepositoryConflictError("skill command identity was reused")
            head = self.get(
                transaction,
                owner_id=command.owner_id,
                skill_id=command.skill_id,
                include_deleted=True,
            )
            if head is None:
                raise RepositoryDataError("skill receipt has no head")
            return SkillPreparation(command, head, None, receipt, True, _clock(transaction))
        _lock_affected(transaction, command.owner_id, "skill", command.skill_id)
        head = _head(
            transaction.fetch_one(
                "SELECT * FROM owner_skill_head WHERE owner_id=%s AND skill_id=%s FOR UPDATE",
                (command.owner_id, command.skill_id),
            )
        )
        if command.command == "create":
            if head is not None or transaction.fetch_one(
                "SELECT skill_id FROM owner_skill_head WHERE skill_id=%s", (command.skill_id,)
            ):
                raise RepositoryConflictError("skill identity already exists")
            count = transaction.fetch_one(
                (
                    "SELECT count(*) AS count FROM owner_skill_head WHERE owner_id=%s AND d"
                    "eleted_at IS NULL"
                ),
                (command.owner_id,),
            )["count"]
            if count >= 20:
                raise RepositoryConflictError("skill catalog is full")
        elif head is None or head.deleted_at is not None:
            raise RepositoryNotFoundError("skill not found")
        elif head.revision != command.expected_revision:
            raise RepositoryConflictError("skill revision changed")
        if command.command != "delete" and command.expected_revision >= MAX_REVISION - 1:
            raise RepositoryConflictError("skill live revision exhausted")
        definition = command.definition
        if definition is not None:
            collision = transaction.fetch_one(
                (
                    "SELECT skill_id FROM owner_skill_head WHERE owner_id=%s AND skill_id<>"
                    "%s AND deleted_at IS NULL AND ((%s<>'' AND alias=%s) OR slug=%s)"
                ),
                (
                    command.owner_id,
                    command.skill_id,
                    definition.alias,
                    definition.alias,
                    command.slug or head.slug,
                ),
            )
            if collision is not None:
                raise RepositoryConflictError("skill slug or alias is already used")
        revision = (
            None
            if head is None
            else self.get_revision(
                transaction,
                owner_id=command.owner_id,
                skill_id=command.skill_id,
                revision=head.revision,
            )
        )
        if head is not None and (
            revision is None or revision.definition_digest != head.definition_digest
        ):
            raise RepositoryDataError("skill current snapshot does not match its head")
        return SkillPreparation(command, head, revision, None, False, _clock(transaction))

    def apply_change(self, transaction, *, command):
        with transaction.savepoint("guidance_skill_change"):
            preparation = self.prepare_change(transaction, command=command)
            command = preparation.command
            if preparation.replayed:
                return SkillChangeResult(preparation.head, None, preparation.receipt, True)
            definition = command.definition or preparation.revision.definition
            deleted = command.command == "delete"
            if deleted:
                definition = replace(definition, enabled=False)
            now = preparation.observed_at_ms
            if preparation.head is not None and now < preparation.head.updated_at:
                raise RepositoryConflictError("guidance database clock regressed")
            number = command.expected_revision + 1
            self._write_revision(
                transaction,
                owner_id=command.owner_id,
                skill_id=command.skill_id,
                number=number,
                definition=definition,
                now=now,
                deleted=deleted,
                command=command,
            )
            if preparation.head is None:
                transaction.execute(
                    (
                        "INSERT INTO owner_skill_head(owner_id,skill_id,slug,revision,name,alia"
                        "s,applies_to,enabled,definition_digest,created_at,updated_at) VALUES(%"
                        "s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)"
                    ),
                    (
                        command.owner_id,
                        command.skill_id,
                        command.slug,
                        number,
                        definition.name,
                        definition.alias,
                        canonical(definition.applies_to),
                        definition.enabled,
                        definition.definition_digest,
                        now,
                        now,
                    ),
                )
            else:
                transaction.execute(
                    (
                        "UPDATE owner_skill_head SET revision=%s,name=%s,alias=%s,applies_to=%s"
                        "::jsonb,enabled=%s,definition_digest=%s,updated_at=%s,deleted_at=%s WH"
                        "ERE owner_id=%s AND skill_id=%s"
                    ),
                    (
                        number,
                        definition.name,
                        definition.alias,
                        canonical(definition.applies_to),
                        definition.enabled,
                        definition.definition_digest,
                        now,
                        now if deleted else None,
                        command.owner_id,
                        command.skill_id,
                    ),
                )
                _invalidate(transaction, command.owner_id, "skill", command.skill_id)
            return SkillChangeResult(
                self.get(
                    transaction,
                    owner_id=command.owner_id,
                    skill_id=command.skill_id,
                    include_deleted=True,
                ),
                self.get_revision(
                    transaction,
                    owner_id=command.owner_id,
                    skill_id=command.skill_id,
                    revision=number,
                ),
                SkillReceipt(
                    command.owner_id,
                    command.skill_id,
                    command.command_id,
                    command.command,
                    command.request_digest,
                    number,
                    now,
                ),
                False,
            )

    @staticmethod
    def _write_revision(
        tx, *, owner_id, skill_id, number, definition, now, deleted=False, command=None, legacy=None
    ):
        tx.execute(
            (
                "INSERT INTO owner_skill_revision(owner_id,skill_id,revision,definition"
                ",definition_digest,created_at,deleted,command_id,command,request_diges"
                "t,legacy_markdown,legacy_digest,legacy_updated_at) VALUES(%s,%s,%s,%s:"
                ":jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            ),
            (
                owner_id,
                skill_id,
                number,
                canonical(asdict(definition)),
                definition.definition_digest,
                now,
                deleted,
                None if command is None else command.command_id,
                None if command is None else command.command,
                None if command is None else command.request_digest,
                None if legacy is None else legacy.markdown,
                None if legacy is None else hashlib.sha256(legacy.markdown).hexdigest(),
                None if legacy is None else legacy.legacy_updated_at,
            ),
        )

    def get_materialization(self, query, *, owner_id):
        owner(owner_id)
        row = query.fetch_one("SELECT * FROM owner_skill_catalog WHERE owner_id=%s", (owner_id,))
        if row is None:
            return None
        try:
            digest(row["manifest_digest"])
            digest(row["entry_digest"])
            integer(row["created_at"], maximum=MAX_TIME)
            mappings = tuple(LegacySkillMapping(**m) for m in row["mappings"])
            if len(mappings) > 20 or len({m.slug for m in mappings}) != len(mappings):
                raise ValueError
            for mapping in mappings:
                slug(mapping.slug)
                identifier(mapping.skill_id)
                integer(mapping.revision, minimum=1, maximum=1)
                digest(mapping.legacy_digest)
                digest(mapping.definition_digest)
            return SkillMaterialization(
                owner_id, row["manifest_digest"], mappings, row["created_at"], True
            )
        except (KeyError, TypeError, ValueError, RepositoryValidationError) as exc:
            raise RepositoryDataError("invalid skill materialization marker") from exc

    def materialize_legacy_skills(self, transaction, *, owner_id, entries, manifest_digest):
        owner(owner_id)
        digest(manifest_digest)
        if type(entries) is not tuple or any(
            type(entry) is not LegacySkillEntry for entry in entries
        ):
            raise RepositoryValidationError("typed legacy entries required")
        entries = tuple(
            LegacySkillEntry(
                entry.skill_id,
                entry.slug,
                SkillDefinition(
                    **{**asdict(entry.definition), "applies_to": entry.definition.applies_to}
                ),
                entry.markdown,
                entry.format,
                entry.legacy_updated_at,
            )
            for entry in entries
        )
        entry_digest = legacy_manifest(entries)
        if manifest_digest != legacy_directory_digest(owner_id, entries):
            raise RepositoryValidationError("legacy manifest does not match exact bytes")
        _lock_owner(transaction, owner_id)
        old = self.get_materialization(transaction, owner_id=owner_id)
        if old is not None:
            stored = transaction.fetch_one(
                "SELECT entry_digest FROM owner_skill_catalog WHERE owner_id=%s", (owner_id,)
            )
            if old.manifest_digest != manifest_digest or stored["entry_digest"] != entry_digest:
                raise RepositoryConflictError(
                    "skill catalog was already materialized from another capture"
                )
            return old
        if transaction.fetch_one(
            "SELECT skill_id FROM owner_skill_head WHERE owner_id=%s LIMIT 1", (owner_id,)
        ):
            raise RepositoryConflictError("unmarked skill catalog is not empty")
        with transaction.savepoint("guidance_materialization"):
            now = _clock(transaction)
            mappings = []
            for entry in sorted(entries, key=lambda e: e.slug):
                if transaction.fetch_one(
                    "SELECT skill_id FROM owner_skill_head WHERE skill_id=%s", (entry.skill_id,)
                ):
                    raise RepositoryConflictError("legacy skill identity already exists")
                self._write_revision(
                    transaction,
                    owner_id=owner_id,
                    skill_id=entry.skill_id,
                    number=1,
                    definition=entry.definition,
                    now=now,
                    legacy=entry,
                )
                d = entry.definition
                transaction.execute(
                    (
                        "INSERT INTO owner_skill_head(owner_id,skill_id,slug,revision,name,alia"
                        "s,applies_to,enabled,definition_digest,created_at,updated_at) VALUES(%"
                        "s,%s,%s,1,%s,%s,%s::jsonb,%s,%s,%s,%s)"
                    ),
                    (
                        owner_id,
                        entry.skill_id,
                        entry.slug,
                        d.name,
                        d.alias,
                        canonical(d.applies_to),
                        d.enabled,
                        d.definition_digest,
                        now,
                        now,
                    ),
                )
                mappings.append(
                    LegacySkillMapping(
                        entry.slug,
                        entry.skill_id,
                        1,
                        hashlib.sha256(entry.markdown).hexdigest(),
                        d.definition_digest,
                    )
                )
            transaction.execute(
                (
                    "INSERT INTO owner_skill_catalog(owner_id,manifest_digest,entry_digest,"
                    "mappings,created_at) VALUES(%s,%s,%s,%s::jsonb,%s)"
                ),
                (
                    owner_id,
                    manifest_digest,
                    entry_digest,
                    canonical([asdict(m) for m in mappings]),
                    now,
                ),
            )
            return SkillMaterialization(owner_id, manifest_digest, tuple(mappings), now, False)


def _note(row):
    if row is None:
        return None
    try:
        values = dict(row)
        values["note_id"] = str(values["note_id"])
        if values["deleted_at"] is not None:
            live = (
                "format_version",
                "category",
                "enabled",
                "created_at",
                "updated_at",
                "expires_at",
                "ciphertext",
            )
            if any(values.pop(key) is not None for key in live):
                raise ValueError
            return ExplicitNoteTombstone(**values)
        if values.pop("deleted_reason") is not None:
            raise ValueError
        values.pop("deleted_at")
        values["ciphertext"] = bytes(values["ciphertext"])
        return ExplicitNoteRecord(**values)
    except (KeyError, TypeError, ValueError, RepositoryValidationError) as exc:
        raise RepositoryDataError("invalid stored explicit note") from exc


class ExplicitNotesRepository:
    def lock_explicit_note_owner(self, transaction, *, owner_id):
        _lock_owner(transaction, owner_id)

    def get_explicit_note(self, query, *, owner_id, note_id, include_disabled=True):
        owner(owner_id)
        identifier(note_id)
        if type(include_disabled) is not bool:
            raise RepositoryValidationError("include_disabled must be boolean")
        note = _note(
            query.fetch_one(
                "SELECT * FROM explicit_note_current WHERE owner_id=%s AND note_id=%s",
                (owner_id, note_id),
            )
        )
        if type(note) is ExplicitNoteRecord:
            now = _clock(query)
            if (
                now < note.updated_at
                or (note.expires_at is not None and now >= note.expires_at)
                or (not include_disabled and not note.enabled)
            ):
                return None
        return note

    def list_explicit_notes(
        self, query, *, owner_id, after_id=None, limit=100, include_disabled=True
    ):
        owner(owner_id)
        integer(limit, minimum=1, maximum=100)
        if after_id is not None:
            identifier(after_id)
        if type(include_disabled) is not bool:
            raise RepositoryValidationError("include_disabled must be boolean")
        now = _clock(query)
        rows = query.fetch_all(
            (
                "SELECT * FROM explicit_note_current WHERE owner_id=%s AND deleted_at I"
                "S NULL AND updated_at<=%s AND (expires_at IS NULL OR expires_at>%s) AN"
                "D (%s OR enabled) AND (%s::uuid IS NULL OR note_id>%s::uuid) ORDER BY "
                "note_id LIMIT %s"
            ),
            (owner_id, now, now, include_disabled, after_id, after_id, limit),
        )
        now = _clock(query)
        notes = tuple(_note(row) for row in rows)
        return tuple(
            note
            for note in notes
            if note.updated_at <= now and (note.expires_at is None or now < note.expires_at)
        )

    def prepare_explicit_note(self, transaction, *, owner_id, note_id, expected_revision):
        owner(owner_id)
        identifier(note_id)
        integer(expected_revision, maximum=MAX_REVISION - 1)
        _lock_owner(transaction, owner_id)
        _lock_affected(transaction, owner_id, "note", note_id)
        current = _note(
            transaction.fetch_one(
                "SELECT * FROM explicit_note_current WHERE owner_id=%s AND note_id=%s FOR UPDATE",
                (owner_id, note_id),
            )
        )
        if (0 if current is None else current.revision) != expected_revision or type(
            current
        ) is ExplicitNoteTombstone:
            raise RepositoryConflictError("explicit note revision changed")
        if current is None and transaction.fetch_one(
            "SELECT note_id FROM explicit_note_current WHERE note_id=%s", (note_id,)
        ):
            raise RepositoryConflictError("explicit note identity already exists")
        now = _clock(transaction)
        if current is not None and (
            now < current.updated_at
            or (current.expires_at is not None and now >= current.expires_at)
        ):
            raise RepositoryConflictError("explicit note is expired or unavailable")
        return ExplicitNotePreparation(owner_id, note_id, expected_revision, current, now)

    def put_explicit_note(self, transaction, *, preparation, record):
        if (
            type(preparation) is not ExplicitNotePreparation
            or type(record) is not ExplicitNoteRecord
        ):
            raise RepositoryValidationError("typed explicit note preparation and record required")
        frozen_current = preparation.current
        if type(frozen_current) is ExplicitNoteRecord:
            frozen_current = ExplicitNoteRecord(**asdict(frozen_current))
        elif type(frozen_current) is ExplicitNoteTombstone:
            frozen_current = ExplicitNoteTombstone(**asdict(frozen_current))
        elif frozen_current is not None:
            raise RepositoryValidationError("invalid prepared explicit note")
        preparation = ExplicitNotePreparation(
            owner(preparation.owner_id),
            identifier(preparation.note_id),
            integer(preparation.expected_revision, maximum=MAX_REVISION - 1),
            frozen_current,
            integer(preparation.observed_at_ms, maximum=MAX_TIME),
        )
        record = ExplicitNoteRecord(**asdict(record))
        with transaction.savepoint("guidance_note_put"):
            current = self.prepare_explicit_note(
                transaction,
                owner_id=preparation.owner_id,
                note_id=preparation.note_id,
                expected_revision=preparation.expected_revision,
            )
            if current.current != preparation.current or (
                record.owner_id,
                record.note_id,
                record.revision,
                record.updated_at,
            ) != (
                preparation.owner_id,
                preparation.note_id,
                preparation.expected_revision + 1,
                preparation.observed_at_ms,
            ):
                raise RepositoryConflictError("explicit note preparation changed")
            if record.updated_at > current.observed_at_ms or (
                record.expires_at is not None and record.expires_at <= current.observed_at_ms
            ):
                raise RepositoryConflictError(
                    "explicit note is expired or from a future observation"
                )
            created = (
                preparation.observed_at_ms
                if current.current is None
                else current.current.created_at
            )
            if record.created_at != created:
                raise RepositoryConflictError("explicit note creation time changed")
            transaction.execute(
                (
                    "INSERT INTO explicit_note_current(owner_id,note_id,revision,format_ver"
                    "sion,category,enabled,created_at,updated_at,expires_at,ciphertext) VAL"
                    "UES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(owner_id,note_id) DO UP"
                    "DATE SET revision=EXCLUDED.revision,format_version=EXCLUDED.format_ver"
                    "sion,category=EXCLUDED.category,enabled=EXCLUDED.enabled,updated_at=EX"
                    "CLUDED.updated_at,expires_at=EXCLUDED.expires_at,ciphertext=EXCLUDED.c"
                    "iphertext"
                ),
                (
                    record.owner_id,
                    record.note_id,
                    record.revision,
                    record.format_version,
                    record.category,
                    record.enabled,
                    record.created_at,
                    record.updated_at,
                    record.expires_at,
                    record.ciphertext,
                ),
            )
            if current.current is not None:
                _invalidate(transaction, record.owner_id, "note", record.note_id)
            if record.expires_at is not None and _clock(transaction) >= record.expires_at:
                raise RepositoryConflictError("explicit note expired before commit")
            return record

    def forget_explicit_note(self, transaction, *, owner_id, note_id, expected_revision):
        return self._retire_explicit_note(
            transaction,
            owner_id=owner_id,
            note_id=note_id,
            expected_revision=expected_revision,
            reason="forgotten",
        )

    def expire_explicit_note(
        self, transaction, *, owner_id, note_id, expected_revision, skip_locked=False
    ):
        if type(skip_locked) is not bool:
            raise RepositoryValidationError("skip_locked must be boolean")
        return self._retire_explicit_note(
            transaction,
            owner_id=owner_id,
            note_id=note_id,
            expected_revision=expected_revision,
            reason="expired",
            skip_locked=skip_locked,
        )

    def prepare_explicit_note_retirement(
        self,
        transaction,
        *,
        owner_id,
        note_id,
        expected_revision,
        reason,
        skip_locked=False,
    ):
        owner(owner_id)
        identifier(note_id)
        integer(expected_revision, minimum=1, maximum=MAX_REVISION - 1)
        if type(reason) is not str or reason not in {"forgotten", "expired"}:
            raise RepositoryValidationError("invalid explicit note retirement reason")
        if type(skip_locked) is not bool:
            raise RepositoryValidationError("skip_locked must be boolean")
        if not _lock_owner(transaction, owner_id, retirement=True, skip_locked=skip_locked):
            return None
        _lock_affected(transaction, owner_id, "note", note_id)
        current = _note(
            transaction.fetch_one(
                "SELECT * FROM explicit_note_current WHERE owner_id=%s AND note_id=%s FOR UPDATE",
                (owner_id, note_id),
            )
        )
        now = _clock(transaction)
        if (
            type(current) is ExplicitNoteTombstone
            and current.revision == expected_revision + 1
            and current.deleted_reason == reason
        ):
            return ExplicitNoteRetirementPreparation(
                owner_id, note_id, expected_revision, reason, current, now, True
            )
        if current is None:
            raise RepositoryNotFoundError("explicit note not found")
        if type(current) is not ExplicitNoteRecord or current.revision != expected_revision:
            raise RepositoryConflictError("explicit note revision changed")
        if now < current.updated_at or (
            reason == "expired" and (current.expires_at is None or current.expires_at > now)
        ):
            raise RepositoryConflictError("explicit note is not eligible for retirement")
        return ExplicitNoteRetirementPreparation(
            owner_id, note_id, expected_revision, reason, current, now, False
        )

    def _retire_explicit_note(
        self,
        transaction,
        *,
        owner_id,
        note_id,
        expected_revision,
        reason,
        skip_locked=False,
    ):
        with transaction.savepoint("guidance_note_retire"):
            prepared = self.prepare_explicit_note_retirement(
                transaction,
                owner_id=owner_id,
                note_id=note_id,
                expected_revision=expected_revision,
                reason=reason,
                skip_locked=skip_locked,
            )
            if prepared is None:
                return None
            if prepared.replayed:
                return prepared.current
            now = prepared.observed_at_ms
            transaction.execute(
                "UPDATE explicit_note_current SET revision=%s,format_version=NULL,category=NULL,"
                "enabled=NULL,created_at=NULL,updated_at=NULL,expires_at=NULL,ciphertext=NULL,"
                "deleted_at=%s,deleted_reason=%s WHERE owner_id=%s AND note_id=%s",
                (expected_revision + 1, now, reason, owner_id, note_id),
            )
            _invalidate(transaction, owner_id, "note", note_id)
            return ExplicitNoteTombstone(owner_id, note_id, expected_revision + 1, now, reason)

    def page_expired_explicit_notes(self, query, *, limit=20, after=None, cutoff_ms=None):
        integer(limit, minimum=1, maximum=100)
        now = _clock(query)
        if cutoff_ms is None:
            if after is not None:
                raise RepositoryValidationError("expiry cursor requires its captured cutoff")
            cutoff_ms = now
        integer(cutoff_ms, maximum=now)
        if after is not None:
            if type(after) is not NoteExpiryCursor:
                raise RepositoryValidationError("typed expiry cursor required")
            integer(after.expires_at, maximum=cutoff_ms)
            owner(after.owner_id)
            identifier(after.note_id)
        rows = query.fetch_all(
            (
                "SELECT owner_id,note_id,revision,expires_at FROM explicit_note_current"
                " WHERE deleted_at IS NULL AND expires_at<=%s AND (%s::bigint IS NULL O"
                "R (expires_at,owner_id,note_id)>(%s,%s,%s::uuid)) ORDER BY expires_at,"
                "owner_id,note_id LIMIT %s"
            ),
            (
                cutoff_ms,
                None if after is None else after.expires_at,
                None if after is None else after.expires_at,
                None if after is None else after.owner_id,
                None if after is None else after.note_id,
                limit + 1,
            ),
        )
        result = []
        for row in rows[:limit]:
            owner(row["owner_id"])
            identifier(str(row["note_id"]))
            integer(row["revision"], minimum=1, maximum=MAX_REVISION - 1)
            integer(row["expires_at"], maximum=cutoff_ms)
            result.append(
                NoteExpiryCandidate(
                    row["owner_id"], str(row["note_id"]), row["revision"], row["expires_at"]
                )
            )
        last = result[-1] if len(rows) > limit else None
        return NoteExpiryPage(
            tuple(result),
            cutoff_ms,
            None
            if last is None
            else NoteExpiryCursor(last.expires_at, last.owner_id, last.note_id),
        )
