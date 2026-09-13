"""Pure legacy skill capture using synthetic bytes, no user files or Plane stub."""
from dataclasses import FrozenInstanceError, replace
import hashlib
import json

import pytest

from orchestrator.user_skill_materialization import (
    LegacySkillFile, LegacySkillMaterializationError, prepare_legacy_skill_materialization,
)
from orchestrator.user_skills import Skill, UserSkillStore, owner_dir, render_markdown

OWNER = "synthetic-owner"
GOLDEN = b'''---
name: "Weekly status"
type: user_skill
owner: "synthetic-owner"
slug: weekly-status
command: status
applies_to: [always]
enabled: true
updated_at: 1700000000
---

## Instructions

Three bullets, then risks.
'''


def prepare(*files, owner=OWNER, reserved=("help", "weather")):
    return prepare_legacy_skill_materialization(owner, files=tuple(files), reserved_aliases=reserved)


def golden(content=GOLDEN, filename="weekly-status.md"):
    return LegacySkillFile(filename, content)


def test_exact_original_bytes_and_manifest_are_preserved():
    result = prepare(golden())
    item = result.entries[0]
    assert item.markdown == GOLDEN
    assert item.slug == "weekly-status" and item.legacy_updated_at == 1700000000
    assert item.format == "deep_owner_markdown_v1"
    assert item.definition.name == "Weekly status"
    assert item.definition.instructions == "Three bullets, then risks."
    assert item.definition.applies_to == ("always",)
    assert item.definition.alias == "status" and item.definition.enabled is True
    expected = {"version": 1, "kind": "owner_skill_legacy_manifest", "owner_id": OWNER,
                "files": [{"filename": "weekly-status.md", "sha256": hashlib.sha256(GOLDEN).hexdigest()}]}
    raw = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert result.manifest_bytes == raw
    assert result.manifest_digest == hashlib.sha256(raw).hexdigest()
    assert b"Three bullets" not in result.manifest_bytes
    assert "Three bullets" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        item.definition.name = "changed"
    with pytest.raises(FrozenInstanceError):
        result.entries = ()


def test_existing_writer_utf8_escaping_scoped_disabled_and_renamed_slug():
    skill = Skill("old-name-", 'Café "quoted" \\ format', "Explain café results.\nKeep\tsteps.",
                  ("web-research-1", "agent.A:1"), command="", enabled=False, updated_at=0)
    raw = render_markdown(skill, OWNER).encode()
    item = prepare(LegacySkillFile("old-name-.md", raw)).entries[0]
    assert item.markdown == raw and item.definition.instructions == skill.instructions
    assert item.slug == skill.slug and item.definition.applies_to == skill.applies_to
    assert item.definition.enabled is False and item.definition.alias == ""


def test_crlf_format_retains_exact_original_bytes_and_distinct_manifest():
    unix = prepare(golden())
    windows = prepare(golden(GOLDEN.replace(b"\n", b"\r\n")))
    assert windows.entries[0].definition == unix.entries[0].definition
    assert windows.entries[0].markdown != unix.entries[0].markdown
    assert windows.manifest_digest != unix.manifest_digest


@pytest.mark.parametrize("needle,replacement", [
    (b'owner: "synthetic-owner"', b'owner: "foreign-owner"'),
    (b"slug: weekly-status", b"slug: other"),
    (b"type: user_skill", b"type: skill_pack"),
    (b"command: status", b"command: /status"),
    (b"command: status", b"command: HELP"),
    (b"command: status", b"command: help"),
    (b"enabled: true", b"enabled: yes"),
    (b"updated_at: 1700000000", b"updated_at: 1.0"),
    (b"updated_at: 1700000000", b"updated_at: -1"),
    (b"updated_at: 1700000000", b"updated_at: 01"),
    (b"updated_at: 1700000000", b"updated_at: 9223372036854775808"),
    (b"applies_to: [always]", b"applies_to: [always, agent]"),
    (b"applies_to: [always]", b"applies_to: [agent, agent]"),
    (b"applies_to: [always]", b"applies_to: [../agent]"),
    (b"applies_to: [always]", b"applies_to: []"),
    (b"applies_to: [always]", b"applies_to: always"),
    (b"applies_to: [always]", b"applies_to: [a,b]"),
    (b"Three bullets, then risks.", b"short"),
    (b"Three bullets, then risks.", b" unsafe\x00value text"),
    (b'name: "Weekly status"', b'name: "x"'),
    (b'name: "Weekly status"', b'name: " bad name"'),
])
def test_malformed_or_conflicting_fields_are_not_normalized_away(needle, replacement):
    with pytest.raises(LegacySkillMaterializationError):
        prepare(golden(GOLDEN.replace(needle, replacement)))


@pytest.mark.parametrize("extra", [b"owner: \"synthetic-owner\"\n", b"command: status\n",
                                    b"unknown: 1\n"])
def test_duplicate_or_unknown_frontmatter_keys_refused(extra):
    with pytest.raises(LegacySkillMaterializationError):
        prepare(golden(GOLDEN.replace(b"type: user_skill\n", b"type: user_skill\n" + extra)))


@pytest.mark.parametrize("raw", [b'{"name":"A","name":"B"}', b'["Weekly status"]',
                                b'"Weekly \\u0073tatus"', b'"Weekly \\qstatus"'])
def test_json_objects_duplicate_keys_arrays_and_nonlegacy_string_escapes_refused(raw):
    with pytest.raises(LegacySkillMaterializationError):
        prepare(golden(GOLDEN.replace(b'"Weekly status"', raw)))


@pytest.mark.parametrize("filename", ["../weekly-status.md", "/weekly-status.md", "WEEKLY.md",
                                     "x\\weekly-status.md", "weekly-status.md.tmp", "weekly-status.json",
                                     "--bad.md", "a--b.md", "a" * 49 + ".md", ""])
def test_filename_is_closed_not_a_path(filename):
    with pytest.raises(LegacySkillMaterializationError):
        prepare(golden(filename=filename))


@pytest.mark.parametrize("raw", [b"", b"x" * 32769, b"\xff", b"{}", b"---\nname: x\n", GOLDEN[:-1],
                                GOLDEN.replace(b"\n", b"\r", 1), GOLDEN.replace(b"\n", b"\r\n", 1),
                                GOLDEN.replace(b"## Instructions", b"## Other")])
def test_file_shape_encoding_and_raw_bounds_refuse(raw):
    with pytest.raises(LegacySkillMaterializationError):
        prepare(golden(raw))


def test_definition_boundaries_not_truncated():
    maximum = Skill("skill", "é" * 60, "😀" * 4000, tuple(f"a{i}" for i in range(8)), "a" * 24)
    raw = render_markdown(maximum, OWNER).encode()
    item = prepare(LegacySkillFile("skill.md", raw)).entries[0]
    assert item.definition.instructions == maximum.instructions
    for changes in ({"name": "a" * 61}, {"instructions": "x" * 4001}, {"command": "a" * 25},
                    {"applies_to": tuple(f"a{i}" for i in range(9))}):
        bad = render_markdown(replace(maximum, **changes), OWNER).encode()
        with pytest.raises(LegacySkillMaterializationError):
            prepare(LegacySkillFile("skill.md", bad))


def test_owner_is_exact_current_human_bound():
    owner = "😀" * 256
    source = render_markdown(Skill("skill", "Valid name", "Long enough instructions.", ("always",)), owner).encode()
    assert prepare(LegacySkillFile("skill.md", source), owner=owner).owner_id == owner
    for bad in ("", None, " x", "x" * 257, "\ud800", "a\nowner"):
        with pytest.raises(LegacySkillMaterializationError):
            prepare(golden(), owner=bad)


def test_twenty_and_empty_catalogs_are_deterministic_and_owner_bound():
    files = tuple(LegacySkillFile(f"s{i}.md", render_markdown(
        Skill(f"s{i}", f"Skill {i}", "Long enough instructions.", ("always",)), OWNER).encode()) for i in range(21))
    one = prepare(*files[:20])
    two = prepare(*reversed(files[:20]))
    assert one == two and len(one.entries) == 20
    assert prepare().entries == ()
    assert prepare(owner="other").manifest_digest != prepare().manifest_digest
    with pytest.raises(LegacySkillMaterializationError):
        prepare(*files)


def test_raw_manifest_orders_filenames_even_when_slug_prefix_order_differs():
    files = tuple(LegacySkillFile(slug + ".md", render_markdown(
        Skill(slug, "Skill " + slug, "Long enough instructions.", ("always",),
              updated_at=2**63 - 1), OWNER).encode()) for slug in ("a", "a-", "a-b"))
    captured = prepare(*files)
    assert [entry.slug for entry in captured.entries] == ["a-", "a-b", "a"]
    assert [row["filename"] for row in json.loads(captured.manifest_bytes)["files"]] == ["a-.md", "a-b.md", "a.md"]
    assert all(entry.legacy_updated_at == 2**63 - 1 for entry in captured.entries)


def test_duplicate_filename_and_disabled_alias_conflict_refused():
    with pytest.raises(LegacySkillMaterializationError):
        prepare(golden(), golden())
    other = render_markdown(Skill("other", "Other skill", "Long enough instructions.",
                                   ("always",), "status", enabled=False), OWNER).encode()
    with pytest.raises(LegacySkillMaterializationError):
        prepare(golden(), LegacySkillFile("other.md", other))


def test_inputs_are_bounded_immutable_shapes_and_errors_are_private(caplog):
    for files, reserved in [([golden()], ()), (({},), ()), ((golden(),), ["help"]),
                            ((golden(),), ("Bad",)), ((golden(),), ("help", "help")),
                            ((golden(),), tuple(f"a{i}" for i in range(129)))]:
        with pytest.raises(LegacySkillMaterializationError) as exc:
            prepare_legacy_skill_materialization(OWNER, files=files, reserved_aliases=reserved)
        assert str(exc.value) == "legacy_skill_materialization_conflict"
    assert "Three bullets" not in caplog.text
    assert caplog.text == ""


def test_actual_current_store_files_are_prepared_without_rewriting_or_history(tmp_path):
    from pathlib import Path

    store = UserSkillStore(str(tmp_path))
    original = store.save(OWNER, name="A" * 47 + " XX", instructions="Long enough instructions.",
                          applies_to="web-research-1, summarizer-1", command="my-status")
    store.set_enabled(OWNER, original.slug, False)
    paths = tuple(Path(owner_dir(str(tmp_path), OWNER)).glob("*.md"))
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    captured = prepare(*(LegacySkillFile(p.name, p.read_bytes()) for p in paths))
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    assert before == after
    assert captured.entries[0].slug == original.slug and original.slug.endswith("-")
    assert captured.entries[0].definition.enabled is False
    assert captured.entries[0].definition.applies_to == original.applies_to
    assert not hasattr(captured.entries[0], "revision")
    assert store.get(OWNER, original.slug).instructions == captured.entries[0].definition.instructions
