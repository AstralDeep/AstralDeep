"""Generates the credential item vectors from fixed public test ids and obviously fake secret values, running every case through credential_items.py.
It reuses the connection records of generate_connection_vectors.py, and `--check` fails when the committed file differs and writes nothing.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
credential_items = importlib.import_module("credential_items")
gen = importlib.import_module("generate_connection_vectors")

VECTORS = HERE.parent / "vectors"
SPEC = "106/contracts/credential-items.md"
RID = gen.RID
KEY_ITEM = "connection.key." + RID
PROVIDER = {
    "base_url": "https://models.research.example/v1",
    "keys": ["fake-key-1"],
    "models": ["m1"],
    "order": 0,
}


def secrets(**values: str) -> dict[str, Any]:
    return {"v": 1, "secrets": values}


def generate() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []

    def add(name: str, kind: str, case_input: dict[str, Any]) -> None:
        cases.append(
            {
                "name": name,
                "kind": kind,
                "input": case_input,
                "expect": credential_items.run(kind, case_input),
            }
        )

    for name, item in (
        ("connection_key", KEY_ITEM),
        ("provider", "model.provider.lab-models"),
        ("legacy_server", "remote.mcp.lab-tools"),
        ("other", "analytics.consent"),
        ("single_character", "a"),
        ("dots_underscores_and_dashes", "a.b_c-d"),
        ("length_128", "a" * 128),
        ("length_129", "a" * 129),
        ("empty", ""),
        ("leading_dot", ".a"),
        ("leading_dash", "-a"),
        ("uppercase", "Model.provider.x"),
        ("space", "a b"),
        ("slash", "a/b"),
        ("not_text", 5),
        ("connection_key_uppercase_hex", "connection.key." + RID.upper()),
        ("connection_key_31_hex", "connection.key." + RID[:31]),
        ("connection_key_33_hex", "connection.key." + RID + "0"),
        ("connection_key_without_id", "connection.key."),
        ("connection_state_family_is_other", "connection.state.x"),
    ):
        add(f"name_{name}", "credential_name", {"name": item})

    def write(name: str, item: Any, value: Any, source: str = "secure_field") -> None:
        add(
            f"write_{name}",
            "credential_write",
            {"name": item, "source": source, "value": value},
        )

    pad = "x" * (65536 - 10)
    for name, item, value in (
        ("key_single_valid", KEY_ITEM, secrets(key="fake-key-1")),
        ("key_env_names_valid", KEY_ITEM, secrets(FILES_TOKEN="fake-1", REGION="eu")),
        (
            "key_32_secrets_valid",
            KEY_ITEM,
            {"v": 1, "secrets": {f"K{i:02d}": "x" for i in range(32)}},
        ),
        (
            "key_33_secrets",
            KEY_ITEM,
            {"v": 1, "secrets": {f"K{i:02d}": "x" for i in range(33)}},
        ),
        ("key_secret_4096_valid", KEY_ITEM, secrets(key="k" * 4096)),
        ("key_secret_4097", KEY_ITEM, secrets(key="k" * 4097)),
        ("key_secret_with_spaces_valid", KEY_ITEM, secrets(key="a b c")),
        ("key_secret_empty", KEY_ITEM, secrets(key="")),
        ("key_secret_newline", KEY_ITEM, secrets(key="a\nb")),
        ("key_secret_zero_width_joiner", KEY_ITEM, secrets(key="a‍b")),
        ("key_secret_not_text", KEY_ITEM, {"v": 1, "secrets": {"key": 5}}),
        ("key_secret_name_64_valid", KEY_ITEM, secrets(**{"A" * 64: "x"})),
        ("key_secret_name_65", KEY_ITEM, secrets(**{"A" * 65: "x"})),
        ("key_secret_name_leading_digit", KEY_ITEM, secrets(**{"1A": "x"})),
        ("key_secret_name_with_dash", KEY_ITEM, secrets(**{"a-b": "x"})),
        ("key_no_secrets", KEY_ITEM, secrets()),
        ("key_secrets_not_object", KEY_ITEM, {"v": 1, "secrets": ["x"]}),
        ("key_version_2", KEY_ITEM, {"v": 2, "secrets": {"key": "x"}}),
        ("key_version_boolean", KEY_ITEM, {"v": True, "secrets": {"key": "x"}}),
        ("key_extra_field", KEY_ITEM, {"v": 1, "secrets": {"key": "x"}, "note": "x"}),
        ("key_missing_version", KEY_ITEM, {"secrets": {"key": "x"}}),
        ("key_plain_value_not_object", KEY_ITEM, "fake-key-1"),
        ("key_list_not_object", KEY_ITEM, ["x"]),
        ("key_null", KEY_ITEM, None),
        ("key_float", KEY_ITEM, {"v": 1.5, "secrets": {"key": "x"}}),
        ("provider_valid", "model.provider.lab-models", PROVIDER),
        (
            "legacy_server_valid",
            "remote.mcp.lab-tools",
            {"endpoint": "https://mcp.research.example/mcp", "bearer": "fake-bearer"},
        ),
        ("other_valid", "analytics.consent", {"granted": True}),
        ("empty_object_valid", "analytics.consent", {}),
        ("size_65536_valid", "analytics.pad", {"pad": pad}),
        ("size_65537", "analytics.pad", {"pad": pad + "x"}),
        ("size_before_token", "analytics.pad", {"pad": pad + "x", "access_token": "x"}),
        (
            "token_access",
            "model.provider.lab-models",
            {**PROVIDER, "access_token": "fake"},
        ),
        (
            "token_refresh",
            "model.provider.lab-models",
            {**PROVIDER, "refresh_token": "fake"},
        ),
        ("token_id", "model.provider.lab-models", {**PROVIDER, "id_token": "fake"}),
        (
            "token_uppercase_name",
            "model.provider.lab-models",
            {**PROVIDER, "Access_Token": "fake"},
        ),
        (
            "token_nested_object",
            "model.provider.lab-models",
            {**PROVIDER, "auth": {"oauth": {"refresh_token": "fake"}}},
        ),
        (
            "token_inside_list",
            "model.provider.lab-models",
            {**PROVIDER, "accounts": [{"id_token": "fake"}]},
        ),
        (
            "token_in_legacy_server",
            "remote.mcp.lab-tools",
            {"endpoint": "https://mcp.research.example/mcp", "access_token": "fake"},
        ),
        ("token_in_other_family", "analytics.consent", {"refresh_token": "fake"}),
        (
            "token_in_key_item_before_shape",
            KEY_ITEM,
            {"v": 1, "secrets": {"key": "x"}, "access_token": "fake"},
        ),
        (
            "token_name_as_a_secret_name_is_allowed",
            KEY_ITEM,
            secrets(ACCESS_TOKEN="x", id_token="y"),
        ),
        (
            "token_word_in_a_value_is_allowed",
            "analytics.consent",
            {"note": "access_token"},
        ),
        (
            "token_lookalike_field_is_allowed",
            "analytics.consent",
            {"access_tokens": 1, "token": "x"},
        ),
        ("not_an_object_list", "analytics.consent", ["x"]),
        ("not_an_object_text", "model.provider.x", "fake"),
        ("not_an_object_null", "model.provider.x", None),
        ("name_malformed_before_value", "Bad Name", ["x"]),
    ):
        write(name, item, value)
    for source in ("tool_output", "model_output", "import", "sync", ""):
        write(
            f"source_{source or 'empty'}_refused",
            KEY_ITEM,
            secrets(key="fake-key-1"),
            source,
        )
    write("source_checked_before_name", "Bad Name", secrets(key="x"), "tool_output")

    for name, value, item_value in (
        ("http_api_key", gen.http_api_record(RID), secrets(key="fake-key-1")),
        ("http_api_other_secret_name", gen.http_api_record(RID), secrets(token="x")),
        ("http_api_two_secrets", gen.http_api_record(RID), secrets(key="x", extra="y")),
        (
            "mcp_http_key_scheme",
            gen.record(
                RID,
                auth={"scheme": "key", "issuer": None, "scopes": [], "resource": None},
                egress=["mcp.research.example"],
            ),
            secrets(key="x"),
        ),
        (
            "a2a_key_scheme",
            gen.a2a_full_record(RID, auth={"scheme": "key"}),
            secrets(key="x"),
        ),
        (
            "stdio_env_names",
            gen.stdio_record(RID, env=["A_TOKEN", "B_TOKEN"]),
            secrets(A_TOKEN="x", B_TOKEN="y"),
        ),
        (
            "stdio_missing_env_name",
            gen.stdio_record(RID, env=["A_TOKEN", "B_TOKEN"]),
            secrets(A_TOKEN="x"),
        ),
        (
            "stdio_extra_env_name",
            gen.stdio_record(RID),
            secrets(FILES_TOKEN="x", OTHER="y"),
        ),
        (
            "stdio_no_env_has_no_key_item",
            gen.stdio_record(RID, env=[]),
            secrets(key="x"),
        ),
        ("oauth_record_takes_no_key", gen.record(RID), secrets(key="x")),
        (
            "a2a_oauth_takes_no_key",
            gen.a2a_full_record(
                RID,
                auth={"scheme": "oauth", "issuer": gen.A2A_AS, "scopes": ["read"]},
                egress=["agent.example.org", "login.example.org"],
            ),
            secrets(key="x"),
        ),
        ("a2a_none_takes_no_key", gen.a2a_full_record(RID), secrets(key="x")),
        ("model_uses_a_provider_item", gen.model_record(RID), secrets(key="x")),
        (
            "token_field_refused_first",
            gen.http_api_record(RID),
            {"v": 1, "secrets": {"key": "x"}, "refresh_token": "fake"},
        ),
        (
            "value_shape_before_mismatch",
            gen.http_api_record(RID),
            {"v": 1, "secrets": {}},
        ),
    ):
        add(
            f"binding_{name}",
            "credential_binding",
            {"record": value, "value": item_value},
        )

    return {
        "format": "astral.vectors/v1",
        "name": "credential-items-v1",
        "spec": SPEC,
        "cases": cases,
    }


def render() -> dict[str, str]:
    return {
        "credential-items-v1": json.dumps(generate(), indent=2, sort_keys=True) + "\n"
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    rendered = render()
    differs = [
        name
        for name, content in rendered.items()
        if not (VECTORS / f"{name}.json").exists()
        or (VECTORS / f"{name}.json").read_text(encoding="utf-8") != content
    ]
    if args.check:
        if differs:
            raise SystemExit(", ".join(differs) + " vectors differ")
        return
    for name, content in rendered.items():
        (VECTORS / f"{name}.json").write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
