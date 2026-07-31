"""Feature 064 Phase A schema-dialect and offline-safety tests."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.base_agent import BaseA2AAgent
from shared.schema_validation import (
    JSON_SCHEMA_2020_12,
    ToolSchemaError,
    validate_tool_schema,
)


def test_absent_dialect_means_2020_12_without_mutating_input() -> None:
    original = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    before = copy.deepcopy(original)

    validated = validate_tool_schema(
        original,
        "search",
        require_object_root=True,
    )

    assert original == before
    assert validated["$schema"] == JSON_SCHEMA_2020_12


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        (
            {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"},
            "unsupported JSON Schema dialect",
        ),
        (
            {
                "type": "object",
                "properties": {"query": {"type": "string", "required": True}},
            },
            "required",
        ),
        ({"type": "string"}, "root type must be object"),
    ],
)
def test_invalid_input_schema_names_tool_and_reason(schema: dict, reason: str) -> None:
    with pytest.raises(ToolSchemaError) as caught:
        validate_tool_schema(schema, "unsafe_tool", require_object_root=True)

    assert "unsafe_tool" in str(caught.value)
    assert reason in str(caught.value)


def test_network_ref_is_refused_without_outbound_resolution_in_ten_attempts() -> None:
    schema = {
        "type": "object",
        "properties": {
            "payload": {"$ref": "https://schemas.example.invalid/payload.json"}
        },
    }

    for _ in range(10):
        with pytest.raises(ToolSchemaError, match=r"network or external \$ref"):
            validate_tool_schema(schema, "network_ref", require_object_root=True)


def test_unresolved_local_ref_is_refused() -> None:
    with pytest.raises(ToolSchemaError, match=r"unresolvable local \$ref"):
        validate_tool_schema(
            {
                "type": "object",
                "properties": {"value": {"$ref": "#/$defs/missing"}},
                "$defs": {},
            },
            "local_ref",
            require_object_root=True,
        )


def test_schema_depth_and_subschema_counts_are_bounded() -> None:
    deep: dict = {"type": "string"}
    for _ in range(34):
        deep = {"type": "object", "properties": {"next": deep}}
    with pytest.raises(ToolSchemaError, match="depth exceeds"):
        validate_tool_schema(deep, "deep", require_object_root=True)

    wide = {
        "type": "object",
        "properties": {
            f"field_{index}": {"type": "string"} for index in range(513)
        },
    }
    with pytest.raises(ToolSchemaError, match="subschemas"):
        validate_tool_schema(wide, "wide", require_object_root=True)


def test_agent_card_threads_explicit_output_schema_or_null() -> None:
    fake_agent = SimpleNamespace(
        mcp_server=SimpleNamespace(
            tools={
                "derived": {
                    "description": "Derived output",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {
                        "type": "object",
                        "properties": {"value": {"type": "number"}},
                    },
                    "scope": "tools:read",
                },
                "opaque": {
                    "description": "Opaque output",
                    "input_schema": {"type": "object", "properties": {}},
                    "scope": "tools:read",
                },
            }
        ),
        skill_tags=[],
        card_metadata={},
        _public_key_jwk={"kty": "EC"},
        service_name="Test",
        description="Test",
        agent_id="test-1",
    )

    card = BaseA2AAgent._build_agent_card(fake_agent)
    schemas = {skill.id: skill.output_schema for skill in card.skills}

    assert schemas["derived"]["type"] == "object"
    assert schemas["opaque"] is None


@pytest.mark.parametrize(
    ("schema", "kwargs", "reason"),
    [
        ({"type": "object"}, {"tool_name": ""}, "tool name is required"),
        ([], {}, "schema must be an object"),
        ({"type": "object", "const": float("nan")}, {}, "finite JSON values"),
        ({"type": "object", "$ref": "other.json"}, {}, "non-local \\$ref"),
        ({"type": "object", "$ref": "#anchor"}, {}, "unsupported local \\$ref"),
        ({"type": "mystery"}, {}, "invalid type declaration"),
        ({"type": []}, {}, "invalid type declaration"),
        ({"type": "object", "required": ["x", "x"]}, {}, "unique strings"),
        ({"type": "object", "required": [1]}, {}, "unique strings"),
        ({"type": "object", "$ref": 3}, {}, "must be a string"),
        ({"type": "object", "properties": []}, {}, "must be an object"),
        ({"type": "object", "properties": {1: {"type": "string"}}}, {}, "names .* strings"),
        ({"type": "object", "items": 4}, {}, "subschema .* object or boolean"),
        ({"type": "object", "allOf": []}, {}, "non-empty array"),
        ({"type": "object", "oneOf": "bad"}, {}, "non-empty array"),
    ],
)
def test_schema_validation_rejects_each_unsafe_shape(
    schema: object,
    kwargs: dict[str, str],
    reason: str,
) -> None:
    with pytest.raises(ToolSchemaError, match=reason):
        validate_tool_schema(
            schema,
            kwargs.get("tool_name", "edge_case"),
            require_object_root=False,
        )


def test_schema_validation_accepts_boolean_subschemas_and_local_pointer_arrays() -> None:
    schema = {
        "type": "object",
        "$defs": {"choices": {"prefixItems": [{"type": "string"}]}},
        "properties": {
            "value": {"$ref": "#/$defs/choices/prefixItems/0"},
            "anything": True,
        },
        "additionalProperties": False,
        "allOf": [{"type": ["object", "null"]}],
    }

    assert validate_tool_schema(
        schema,
        "local_array_ref",
        require_object_root=True,
    )["$schema"] == JSON_SCHEMA_2020_12
    assert validate_tool_schema(
        {"type": "object", "$ref": "#"},
        "root_ref",
        require_object_root=True,
    )["$ref"] == "#"


def test_schema_validation_enforces_encoded_size_limit() -> None:
    with pytest.raises(ToolSchemaError, match="9-byte limit"):
        validate_tool_schema(
            {"type": "object"},
            "large",
            require_object_root=True,
            max_bytes=9,
        )


@pytest.mark.asyncio
async def test_agent_registration_refuses_invalid_schema_before_state_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator.orchestrator import Orchestrator
    from shared.protocol import AgentCard, AgentSkill, RegisterAgent

    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("AGENT_API_KEY", "")
    fake = SimpleNamespace(agents={}, agent_cards={})
    websocket = SimpleNamespace(close=AsyncMock())
    card = AgentCard(
        name="Unsafe",
        description="",
        agent_id="unsafe-1",
        skills=[
            AgentSkill(
                id="bad",
                name="Bad",
                description="",
                input_schema={"type": "string"},
            )
        ],
    )

    await Orchestrator.register_agent(
        fake,
        websocket,
        RegisterAgent(agent_card=card),
    )

    assert fake.agents == {} and fake.agent_cards == {}
    websocket.close.assert_awaited_once_with(code=1008, reason="invalid tool schema")
