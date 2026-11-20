"""Tests for astralplane.repositories.guidance_models: skill/command/note definition
bounds, canonical UTF-8 digesting, immutable replay identities, and
guidance-index/manifest validation.
"""

import hashlib
import json
import uuid
from dataclasses import replace

import pytest

from astralplane.repositories import RepositoryValidationError
from astralplane.repositories.guidance_models import (
    MAX_REVISION,
    MAX_TIME,
    ExplicitNoteRecord,
    ExplicitNoteTombstone,
    GuidanceReference,
    LegacySkillEntry,
    SkillCommand,
    SkillDefinition,
    digest,
    identifier,
    integer,
    legacy_directory_digest,
    legacy_manifest,
    owner,
    slug,
    text,
)


def uid():
    return str(uuid.uuid4())


def definition(**changes):
    values = dict(
        name="Research",
        instructions="Read exact attributed excerpts.",
        applies_to=("research-1",),
        alias="read",
    )
    values.update(changes)
    return SkillDefinition(**values)


@pytest.mark.parametrize("value", [True, False, None, -1, 1.1, "1", MAX_REVISION + 1])
def test_integer_is_exact_bounded(value):
    with pytest.raises(RepositoryValidationError):
        integer(value)


@pytest.mark.parametrize(
    "value",
    [None, True, 1, "", "wrong", str(uuid.uuid1()), str(uuid.uuid4()).upper(), uuid.uuid4().hex],
)
def test_identity_is_canonical_uuid4(value):
    with pytest.raises(RepositoryValidationError):
        identifier(value)


@pytest.mark.parametrize("value", ["", None, True, " x", "x ", "x\x00", "x\ud800", "a" * 257])
def test_owner_bounds_are_literal_and_unambiguous(value):
    with pytest.raises(RepositoryValidationError):
        owner(value)


def test_unicode_owner_and_plain_text_preserve_exact_codepoints():
    assert owner("😀" * 256) == "😀" * 256
    assert text("exact\n\ttext", multiline=True) == "exact\n\ttext"
    assert integer(MAX_TIME, maximum=MAX_TIME) == MAX_TIME


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "x"),
        ("name", "x" * 61),
        ("name", "name\n"),
        ("instructions", "short"),
        ("instructions", "x" * 4001),
        ("instructions", "text\x00 more"),
        ("format_version", True),
        ("format_version", 2),
        ("enabled", 1),
        ("applies_to", []),
        ("applies_to", ("a",) * 2),
        ("applies_to", tuple(str(i) for i in range(9))),
        ("applies_to", ("agent with space",)),
        ("applies_to", ("a" * 65,)),
        ("applies_to", (None,)),
        ("alias", "UPPER"),
        ("alias", True),
        ("alias", "a" * 25),
        ("alias", "two words"),
    ],
)
def test_skill_definition_refuses_malformed_or_unbounded_fields(field, value):
    with pytest.raises(RepositoryValidationError):
        definition(**{field: value})


def test_skill_digest_is_canonical_utf8_and_detached_from_aliases():
    d = definition(name="研究者", instructions="原文を保持して公的な資料を読みます。")
    body = {
        "name": d.name,
        "instructions": d.instructions,
        "applies_to": list(d.applies_to),
        "alias": d.alias,
        "enabled": True,
        "format_version": 1,
    }
    assert (
        d.definition_digest
        == hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert d.instructions not in repr(d)
    assert replace(d, enabled=False).definition_digest != d.definition_digest


@pytest.mark.parametrize(
    "changes",
    [
        {"command": "unknown"},
        {"command": None},
        {"command": {}},
        {"expected_revision": True},
        {"expected_revision": 1},
        {"slug": None},
        {"slug": "MixedCase"},
        {"slug": "a" * 49},
        {"definition": {}},
        {"skill_id": str(uuid.uuid1())},
        {"command_id": "bad"},
    ],
)
def test_skill_command_refuses_ambiguous_create(changes):
    values = dict(
        owner_id="owner",
        skill_id=uid(),
        command_id=uid(),
        command="create",
        expected_revision=0,
        slug="research",
        definition=definition(),
    )
    values.update(changes)
    with pytest.raises(RepositoryValidationError):
        SkillCommand(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"command": "delete", "definition": definition()},
        {"command": "replace", "expected_revision": 0},
        {"command": "replace", "slug": "rename"},
    ],
)
def test_revision_commands_cannot_change_slug_or_omit_cas(changes):
    values = dict(
        owner_id="owner",
        skill_id=uid(),
        command_id=uid(),
        command="replace",
        expected_revision=1,
        definition=definition(),
    )
    values.update(changes)
    with pytest.raises(RepositoryValidationError):
        SkillCommand(**values)


def test_command_digest_binds_owner_expected_revision_and_full_definition():
    c = SkillCommand("owner", uid(), uid(), "replace", 1, definition=definition())
    assert (
        len(
            {
                c.request_digest,
                replace(c, owner_id="other").request_digest,
                replace(c, expected_revision=2).request_digest,
                replace(c, definition=replace(c.definition, enabled=False)).request_digest,
            }
        )
        == 4
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"format": "other"},
        {"definition": {}},
        {"markdown": b""},
        {"markdown": "text"},
        {"markdown": b"x" * 32769},
        {"markdown": b"\xff"},
        {"legacy_updated_at": True},
    ],
)
def test_legacy_geometry_refuses_invalid_capture(changes):
    values = dict(
        skill_id=uid(), slug="legacy", definition=definition(), markdown=b"exact\r\nlegacy"
    )
    values.update(changes)
    with pytest.raises(RepositoryValidationError):
        LegacySkillEntry(**values)


def test_manifest_is_filename_ordered_owner_bound_and_raw_bytes_exact():
    entries = tuple(
        LegacySkillEntry(uid(), s, definition(alias=""), ("raw-" + s).encode())
        for s in ("a", "a-", "a-b")
    )
    expected = {
        "version": 1,
        "kind": "owner_skill_legacy_manifest",
        "owner_id": "owner",
        "files": sorted(
            [
                {"filename": e.slug + ".md", "sha256": hashlib.sha256(e.markdown).hexdigest()}
                for e in entries
            ],
            key=lambda e: e["filename"],
        ),
    }
    assert (
        legacy_directory_digest("owner", entries)
        == hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert legacy_directory_digest("owner", entries) != legacy_directory_digest("other", entries)
    assert legacy_manifest(entries) == legacy_manifest(
        tuple(replace(e, skill_id=uid()) for e in entries)
    )
    assert legacy_manifest(entries) != legacy_manifest(
        (replace(entries[0], legacy_updated_at=1), *entries[1:])
    )


@pytest.mark.parametrize(
    "form", ["list", "too_many", "duplicate_slug", "duplicate_id", "duplicate_alias", "wrong_type"]
)
def test_manifest_refuses_conflicting_owner_catalog(form):
    a = LegacySkillEntry(uid(), "a", definition(), b"original")
    b = replace(a, skill_id=uid(), slug="b")
    entries = {
        "list": [a],
        "too_many": tuple(a for _ in range(21)),
        "duplicate_slug": (a, replace(a, skill_id=uid())),
        "duplicate_id": (a, replace(a, slug="b")),
        "duplicate_alias": (a, b),
        "wrong_type": (None,),
    }[form]
    with pytest.raises(RepositoryValidationError):
        legacy_manifest(entries)


def note(**changes):
    values = dict(
        owner_id="owner",
        note_id=uid(),
        revision=1,
        category="context",
        enabled=True,
        created_at=100,
        updated_at=100,
        ciphertext=b"opaque",
        expires_at=200,
    )
    values.update(changes)
    return ExplicitNoteRecord(**values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", True),
        ("revision", 0),
        ("revision", MAX_REVISION),
        ("category", "unknown"),
        ("category", {}),
        ("enabled", 1),
        ("created_at", -1),
        ("updated_at", 99),
        ("expires_at", 100),
        ("expires_at", True),
        ("format_version", 2),
        ("format_version", True),
        ("expires_at", MAX_REVISION + 1),
        ("ciphertext", b""),
        ("ciphertext", "opaque"),
        ("ciphertext", b"x" * 16385),
    ],
)
def test_live_note_geometry_and_final_revision_reserve(field, value):
    with pytest.raises(RepositoryValidationError):
        note(**{field: value})


def test_last_live_note_always_has_room_for_minimal_tombstone():
    last = note(revision=MAX_REVISION - 1)
    gone = ExplicitNoteTombstone(last.owner_id, last.note_id, MAX_REVISION, 200, "forgotten")
    assert gone.revision == last.revision + 1
    assert set(gone.__dataclass_fields__) == {
        "owner_id",
        "note_id",
        "revision",
        "deleted_at",
        "deleted_reason",
    }
    assert "opaque" not in repr(last)
    with pytest.raises(RepositoryValidationError):
        replace(gone, deleted_reason="other")
    with pytest.raises(RepositoryValidationError):
        replace(gone, revision=MAX_REVISION + 1)


@pytest.mark.parametrize("kind", ["source", None, {}, True])
def test_guidance_index_only_accepts_explicit_guidance_types(kind):
    with pytest.raises(RepositoryValidationError):
        GuidanceReference(kind, uid(), 1)


@pytest.mark.parametrize(
    "function,value",
    [(digest, "f" * 63), (digest, True), (slug, "_bad"), (slug, None), (text, 1), (text, "a\x7f")],
)
def test_misc_closed_fields(function, value):
    with pytest.raises(RepositoryValidationError):
        function(value)
