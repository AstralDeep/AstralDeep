"""Reference for the content rule of credential items: names and families, the size limit, the refusal of sign-in tokens, the shape of connection key items and which secrets a record needs.
It reuses the preserved 105 reference for LETS-CJ/1 and the caller ticket label table, and vectors/credential-items-v1.json pins it for the Kernel and every other writer.
"""

from __future__ import annotations

import re
from typing import Any

import caller_ticket
import connection
from astral_mesh_ref.cj import canonical
from astral_mesh_ref.errors import MeshError

MAX_NAME = 128
MAX_VALUE_BYTES = 65536
MAX_SECRETS = 32
MAX_SECRET = 4096
SOURCE = "secure_field"
TOKEN_FIELDS = ("access_token", "refresh_token", "id_token")
KEY_SECRET = "key"
CODES = (
    "credential.malformed",
    "credential.too_large",
    "credential.sign_in_token",
    "credential.key_mismatch",
    "credential.source_refused",
)

NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")
KEY_ITEM = re.compile(r"connection\.key\.[0-9a-f]{32}")
SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
PROVIDER_PREFIX = "model.provider."
LEGACY_PREFIX = "remote.mcp."


def _fail(code: str, detail: str = "") -> MeshError:
    return MeshError(code, detail)


def family(name: Any) -> str:
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= MAX_NAME
        or not NAME.fullmatch(name)
    ):
        raise _fail("credential.malformed", "name")
    if name.startswith(connection.KEY_ITEM_PREFIX):
        if not KEY_ITEM.fullmatch(name):
            raise _fail("credential.malformed", "name")
        return "connection_key"
    if name.startswith(PROVIDER_PREFIX):
        return "provider"
    if name.startswith(LEGACY_PREFIX):
        return "legacy_server"
    return "other"


def has_token_field(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (isinstance(k, str) and k.lower() in TOKEN_FIELDS) or has_token_field(v)
            for k, v in value.items()
        )
    if isinstance(value, list):
        return any(has_token_field(item) for item in value)
    return False


def _secret_ok(secret: Any) -> bool:
    return (
        isinstance(secret, str)
        and 1 <= len(secret) <= MAX_SECRET
        and not any(caller_ticket.refused_in_label(c) for c in secret)
    )


def _key_shape(value: dict[str, Any]) -> dict[str, Any]:
    secrets = value.get("secrets")
    if (
        set(value) != {"v", "secrets"}
        or type(value["v"]) is not int
        or value["v"] != 1
        or not isinstance(secrets, dict)
        or not 1 <= len(secrets) <= MAX_SECRETS
        or not all(
            SECRET_NAME.fullmatch(n) and _secret_ok(s) for n, s in secrets.items()
        )
    ):
        raise _fail("credential.malformed", "key item")
    return {"secrets": sorted(secrets)}


def check_value(name: Any, value: Any) -> dict[str, Any]:
    kind = family(name)
    if not isinstance(value, dict):
        raise _fail("credential.malformed", "value")
    try:
        size = len(canonical(value))
    except MeshError as error:
        raise _fail("credential.malformed", error.code) from error
    if size > MAX_VALUE_BYTES:
        raise _fail("credential.too_large")
    scanned = (
        {k: v for k, v in value.items() if k != "secrets"}
        if kind == "connection_key"
        else value
    )
    if has_token_field(scanned):
        raise _fail("credential.sign_in_token")
    detail = _key_shape(value) if kind == "connection_key" else {}
    return {"family": kind, **detail}


def write(case_input: dict[str, Any]) -> dict[str, Any]:
    if case_input["source"] != SOURCE:
        raise _fail("credential.source_refused", str(case_input["source"]))
    return check_value(case_input["name"], case_input["value"])


def required_secrets(record: dict[str, Any]) -> list[str]:
    kind = record["kind"]
    if kind == "mcp-stdio":
        return list(record["env"])
    keyed = kind == "http-api" or (
        kind in ("mcp-http", "a2a") and record["auth"]["scheme"] == "key"
    )
    if not keyed:
        raise _fail("credential.malformed", "record takes no key item")
    return [KEY_SECRET]


def binding(case_input: dict[str, Any]) -> dict[str, Any]:
    record = case_input["record"]
    needed = required_secrets(record)
    shape = check_value(connection.KEY_ITEM_PREFIX + record["id"], case_input["value"])
    if shape["secrets"] != sorted(needed):
        raise _fail("credential.key_mismatch")
    return {"secrets": shape["secrets"]}


def _guard(call: Any) -> dict[str, Any]:
    try:
        return {"ok": call()}
    except MeshError as error:
        return {"error": error.code}


def run(kind: str, case_input: dict[str, Any]) -> dict[str, Any]:
    calls = {
        "credential_name": lambda: {"family": family(case_input["name"])},
        "credential_write": lambda: write(case_input),
        "credential_binding": lambda: binding(case_input),
    }
    return _guard(calls[kind])
