"""Exercises malformed intake values and canonical authoring identities in skill_import.
Catalog persistence and caller authorization are covered by the real-Plane facade suite.
"""

import asyncio
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from orchestrator import skill_import


def skill(*, name="Demo Skill", description="Synthetic description", license="MIT",
          body="Use the supplied synthetic instructions."):
    return f"---\nname: {name}\ndescription: {description}\nlicense: {license}\n---\n{body}"


@pytest.mark.parametrize("field", ["name", "description", "license"])
def test_mapped_frontmatter_lists_raise_typed_error(field):
    with pytest.raises(skill_import.SkillImportError) as caught:
        skill_import.parse_agent_skill(skill(**{field: "\n  - malformed"}))
    assert caught.value.code == "invalid_field_type"


def test_lone_surrogate_raises_typed_encoding_error():
    with pytest.raises(skill_import.SkillImportError) as caught:
        skill_import.parse_agent_skill(skill(description="invalid \ud800"))
    assert caught.value.code == "invalid_encoding"


@pytest.mark.parametrize("field", ["name", "description", "license", "body"])
@pytest.mark.parametrize("control", ["\x00", "\x07"])
def test_control_characters_raise_typed_field_error(field, control):
    with pytest.raises(skill_import.SkillImportError) as caught:
        skill_import.parse_agent_skill(skill(**{field: "Synthetic" + control + "value"}))
    assert caught.value.code == "invalid_field_value"


@pytest.mark.parametrize("changes", [{"name": "x"}, {"body": ""}, {"body": "short"}])
def test_values_below_catalog_minimum_raise_typed_error(changes):
    with pytest.raises(skill_import.SkillImportError) as caught:
        skill_import.parse_agent_skill(skill(**changes))
    assert caught.value.code == "invalid_field_value"


def test_caller_body_limit_cannot_exceed_catalog_maximum():
    with pytest.raises(skill_import.SkillImportError) as caught:
        skill_import.parse_agent_skill(skill(body="x" * 4001), max_instructions_chars=5000)
    assert caught.value.code == "instructions_too_long"


@pytest.mark.parametrize("limit", ["max_bytes", "max_field_chars", "max_yaml_depth",
                                   "max_instructions_chars"])
@pytest.mark.parametrize("value", [None, "10", True, -1, 1.5])
def test_malformed_limits_raise_typed_error(limit, value):
    with pytest.raises(skill_import.SkillImportError) as caught:
        skill_import.parse_agent_skill(skill(), **{limit: value})
    assert caught.value.code == "invalid_limit"


@pytest.mark.parametrize("value", ["", "cmd-1", "1cb62afbadff4477b695ef97a18a9130",
                                   "1CB62AFB-ADFF-4477-B695-EF97A18A9130",
                                   "00000000-0000-1000-8000-000000000000", [], 42])
def test_invalid_command_identity_is_typed_and_never_reaches_save(value):
    facade = AsyncMock()
    with pytest.raises(skill_import.SkillImportError) as caught:
        asyncio.run(skill_import.import_skill_text(facade, object(), skill(), command_id=value))
    assert caught.value.code == "invalid_command_id"
    facade.save.assert_not_awaited()


def test_generated_catalog_identities_are_distinct_canonical_uuid4():
    facade = AsyncMock()
    asyncio.run(skill_import.import_skill_text(facade, object(), skill()))
    values = facade.save.call_args.kwargs
    assert values["skill_id"] != values["command_id"]
    for key in ("skill_id", "command_id"):
        parsed = UUID(values[key])
        assert parsed.version == 4 and str(parsed) == values[key]


@pytest.mark.parametrize("transform", [str, lambda value: value.encode(),
                                      lambda value: bytearray(value.encode())])
def test_scalar_unicode_and_instruction_whitespace_are_preserved(transform):
    parsed = skill_import.parse_agent_skill(transform(skill(
        name="Résumé", description="Unicode ✓", body="Line one.\n\tLine two.")))
    assert parsed.name == "Résumé"
    assert parsed.description == "Unicode ✓"
    assert parsed.instructions == "Line one.\n\tLine two."
