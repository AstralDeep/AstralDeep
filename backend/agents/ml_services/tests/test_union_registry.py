"""Tests for the ML Services union registry (mcp_tools.py, ml_services_agent.py): the
consolidated tool-name set, the service-prefixed verbs, schema/scope compatibility
with the predecessor agents, and the agent card's declarations.
"""

from agents.ml_services import mcp_tools
from agents.ml_services.mcp_server import MCPServer
from agents.ml_services.ml_services_agent import MlServicesAgent

EXPECTED_TOOL_NAMES = {
    "_credentials_check",
    "classify_submit_dataset",
    "classify_start_training_job",
    "classify_get_job_status",
    "classify_get_results",
    "classify_delete_dataset",
    "set_column_types",
    "get_ml_options",
    "propose_training_config",
    "get_output_log",
    "forecaster_submit_dataset",
    "forecaster_start_training_job",
    "forecaster_get_job_status",
    "forecaster_get_results",
    "forecaster_delete_dataset",
    "set_column_roles",
    "list_models",
    "chat_with_model",
    "create_embedding",
    "transcribe_audio",
}

PREFIXED_NAMES = {
    "classify_submit_dataset", "classify_start_training_job",
    "classify_get_job_status", "classify_get_results", "classify_delete_dataset",
    "forecaster_submit_dataset", "forecaster_start_training_job",
    "forecaster_get_job_status", "forecaster_get_results", "forecaster_delete_dataset",
}

EXPECTED_SCOPES = {
    "_credentials_check": "tools:read",
    "classify_submit_dataset": "tools:write",
    "set_column_types": "tools:write",
    "get_ml_options": "tools:read",
    "classify_start_training_job": "tools:write",
    "propose_training_config": "tools:read",
    "classify_get_job_status": "tools:read",
    "classify_get_results": "tools:read",
    "get_output_log": "tools:read",
    "classify_delete_dataset": "tools:write",
    "forecaster_submit_dataset": "tools:write",
    "set_column_roles": "tools:write",
    "forecaster_start_training_job": "tools:write",
    "forecaster_get_job_status": "tools:read",
    "forecaster_get_results": "tools:read",
    "forecaster_delete_dataset": "tools:write",
    "list_models": "tools:read",
    "chat_with_model": "tools:write",
    "create_embedding": "tools:write",
    "transcribe_audio": "tools:write",
}

_COLUMN_ROLES = [
    "not-included",
    "time-component",
    "grouping",
    "target",
    "past-covariates",
    "future-covariates",
    "static-covariates",
]

ORIGINAL_SCHEMAS_UNCHANGED_NAMES = {
    "set_column_types": {
        "type": "object",
        "properties": {
            "report_uuid": {
                "type": "string",
                "description": "Report UUID returned by submit_dataset.",
            },
            "class_column": {
                "type": "string",
                "description": "Name of the class column.",
            },
            "column_types": {
                "type": "object",
                "description": (
                    "data_types map from submit_dataset._data.column_types "
                    "(e.g. {'col_a': 'integer', 'col_b': 'string'})."
                ),
                "additionalProperties": {"type": "string"},
            },
            "missing_strategy": {
                "type": "string",
                "enum": ["synthetic", "constant", "none"],
                "default": "synthetic",
                "description": (
                    "How to handle missing cells in columns that contain nulls. "
                    "'synthetic' is the script's default."
                ),
            },
            "fill_value": {
                "description": "Used only when missing_strategy='constant'.",
            },
            "excluded_columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Columns to mark checked=False (drop from training).",
            },
            "column_changes": {
                "type": "array",
                "description": (
                    "Optional escape hatch: a hand-built per-column list overrides "
                    "automatic building. Per-column config. Fields: 'column' (str, required), "
                    "'data_type' (str: 'integer'|'float'|'bool'|'string', required), "
                    "'checked' (bool, include in final dataset, required), "
                    "'missing' (null|'synthetic'|'constant', how to handle missing cells), "
                    "'fill_value' (any, used when missing='constant'), "
                    "'class' (bool, set True on exactly one entry to mark the class column)."
                ),
                "items": {"type": "object"},
            },
        },
        "required": ["report_uuid"],
    },
    "get_ml_options": {
        "type": "object",
        "properties": {
            "unsstate": {
                "type": "integer",
                "description": "0 for supervised (default), 1 for unsupervised.",
                "default": 0,
            },
        },
        "additionalProperties": False,
    },
    "propose_training_config": {
        "type": "object",
        "properties": {
            "report_uuid": {"type": "string", "description": "Report UUID from submit_dataset."},
            "class_column": {"type": "string", "description": "Name of the class column."},
            "supervised": {
                "type": "boolean",
                "description": "Default True. Off = unsupervised clustering.",
                "default": True,
            },
            "autodetermineclusters": {
                "type": "boolean",
                "description": "Only meaningful when supervised is False.",
                "default": False,
            },
            "unsstate": {
                "type": "integer",
                "description": (
                    "Optional get-ml-opts mode override; auto-derived from supervised."
                ),
            },
        },
        "required": ["report_uuid", "class_column"],
        "additionalProperties": False,
    },
    "get_output_log": {
        "type": "object",
        "properties": {"report_uuid": {"type": "string"}},
        "required": ["report_uuid"],
    },
    "set_column_roles": {
        "type": "object",
        "properties": {
            "uuid": {
                "type": "string",
                "description": "Dataset UUID returned by submit_dataset.",
            },
            "column_roles": {
                "type": "object",
                "description": (
                    "Map of column_name → role. Example: "
                    "{'Date': 'time-component', 'Volume': 'target', "
                    "'Rain': 'past-covariates', 'Temp': 'past-covariates'}."
                ),
                "additionalProperties": {
                    "type": "string",
                    "enum": _COLUMN_ROLES,
                },
            },
        },
        "required": ["uuid", "column_roles"],
    },
    "list_models": {"type": "object", "properties": {}, "additionalProperties": False},
    "chat_with_model": {
        "type": "object",
        "properties": {
            "model_id": {"type": "string", "description": "Identifier of a model served by the Router."},
            "messages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string", "enum": ["system", "user", "assistant"]},
                        "content": {"type": "string"},
                    },
                    "required": ["role", "content"],
                },
            },
            "options": {"type": "object", "description": "OpenAI-compatible parameters: temperature, max_tokens, etc."},
        },
        "required": ["model_id", "messages"],
    },
    "create_embedding": {
        "type": "object",
        "properties": {
            "model_id": {"type": "string", "description": "Identifier of an embedding-capable model."},
            "input": {
                "description": "Either a single string or a list of strings to embed.",
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
            },
        },
        "required": ["model_id", "input"],
    },
    "transcribe_audio": {
        "type": "object",
        "properties": {
            "model_id": {"type": "string", "description": "Identifier of a transcription-capable model (e.g. whisper-1)."},
            "file_handle": {"type": "string", "description": "AstralDeep attachment_id of the audio file."},
            "language": {"type": "string", "description": "Optional ISO-639-1 language hint (e.g. 'en')."},
        },
        "required": ["model_id", "file_handle"],
    },
}

PREFIXED_SCHEMA_REQUIRED = {
    "classify_submit_dataset": [],
    "classify_start_training_job": ["report_uuid", "class_column"],
    "classify_get_job_status": ["report_uuid"],
    "classify_get_results": ["report_uuid"],
    "classify_delete_dataset": ["report_uuid"],
    "forecaster_submit_dataset": [],
    "forecaster_start_training_job": ["uuid"],
    "forecaster_get_job_status": ["uuid"],
    "forecaster_get_results": ["uuid"],
    "forecaster_delete_dataset": ["uuid"],
}


def test_union_registry_completeness() -> None:
    assert set(mcp_tools.TOOL_REGISTRY.keys()) == EXPECTED_TOOL_NAMES


def test_exactly_the_collision_set_is_prefixed() -> None:
    names = set(mcp_tools.TOOL_REGISTRY.keys())
    assert PREFIXED_NAMES <= names
    for bare in ("submit_dataset", "start_training_job", "get_job_status",
                 "get_results", "delete_dataset"):
        assert bare not in names, f"bare collision verb {bare!r} must not be registered"
    for name in PREFIXED_NAMES:
        assert name.startswith(("classify_", "forecaster_"))


def test_scopes_byte_compatible() -> None:
    for name, scope in EXPECTED_SCOPES.items():
        assert mcp_tools.TOOL_REGISTRY[name]["scope"] == scope, name


def test_unchanged_name_tools_keep_original_input_schemas() -> None:
    for name, expected in ORIGINAL_SCHEMAS_UNCHANGED_NAMES.items():
        actual = mcp_tools.TOOL_REGISTRY[name]["input_schema"]
        assert actual == expected, f"input_schema drift for unchanged tool {name!r}"


def test_prefixed_tools_keep_original_schema_structure() -> None:
    for name, required in PREFIXED_SCHEMA_REQUIRED.items():
        schema = mcp_tools.TOOL_REGISTRY[name]["input_schema"]
        assert schema.get("required") == required, name
        assert schema.get("type") == "object", name
        for key in required:
            assert key in schema.get("properties", {}), f"{name} missing property {key}"


def test_classify_start_training_job_schema_matches_original() -> None:
    schema = mcp_tools.TOOL_REGISTRY["classify_start_training_job"]["input_schema"]
    assert set(schema["properties"].keys()) == {
        "report_uuid", "class_column", "models_to_train", "parameter_overrides",
        "parameter_tune", "supervised", "autodetermineclusters", "unsstate",
    }
    assert schema["properties"]["models_to_train"]["default"] == ["randomforest", "gradientboosting"]
    assert schema["properties"]["parameter_tune"]["default"] is False
    assert schema["properties"]["supervised"]["default"] is True
    assert schema["additionalProperties"] is False


def test_delete_tools_keep_external_target_metadata() -> None:
    assert mcp_tools.TOOL_REGISTRY["classify_delete_dataset"]["metadata"] == \
        {"external_target": "CLASSify"}
    assert mcp_tools.TOOL_REGISTRY["forecaster_delete_dataset"]["metadata"] == \
        {"external_target": "Forecaster"}


def test_union_long_running_tools() -> None:
    assert mcp_tools.LONG_RUNNING_TOOLS == {
        "classify_start_training_job", "forecaster_start_training_job",
    }


def test_every_tool_well_formed() -> None:
    for name, spec in mcp_tools.TOOL_REGISTRY.items():
        assert callable(spec.get("function")), name
        assert spec.get("description"), name
        assert isinstance(spec.get("input_schema"), dict), name
        assert spec.get("scope") in ("tools:read", "tools:write"), name


def test_mcp_server_lists_union_registry() -> None:
    server = MCPServer()
    listed = {t["name"] for t in server.get_tool_list()}
    assert listed == EXPECTED_TOOL_NAMES


def test_propose_training_config_submit_template_targets_prefixed_verb() -> None:
    import inspect
    from agents.ml_services import classify_tools
    src = inspect.getsource(classify_tools.propose_training_config)
    assert "call classify_start_training_job" in src


def test_agent_identity() -> None:
    assert MlServicesAgent.agent_id == "ml-services-1"
    assert MlServicesAgent.service_name == "ML Services"
    assert MlServicesAgent.skill_tags == [
        "machine-learning", "classification", "timeseries", "embeddings", "transcription",
    ]
    assert "forecast" not in MlServicesAgent.skill_tags


def test_agent_card_declares_three_optional_bundles_with_existing_keys() -> None:
    entries = MlServicesAgent.card_metadata["required_credentials"]
    keys = [e["key"] for e in entries]
    assert keys == [
        "CLASSIFY_URL", "CLASSIFY_API_KEY",
        "FORECASTER_URL", "FORECASTER_API_KEY",
        "LLM_FACTORY_URL", "LLM_FACTORY_API_KEY",
    ]
    assert all(e["required"] is False for e in entries)
    for e in entries:
        assert e.get("label")
        assert e.get("description")
        assert e.get("type") == "api_key"


def test_agent_card_long_running_tools_use_prefixed_names() -> None:
    assert MlServicesAgent.card_metadata["long_running_tools"] == [
        "classify_start_training_job", "forecaster_start_training_job",
    ]
