"""Operator-rooted authentication for the LETS signed trust manifest."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from lets.canonical import b64url_decode
from lets.errors import SignatureError, ValidationError
from lets.manifest import ClusterManifest


OPERATOR_TRUST_API_VERSION: Final = "astraldeep.lets-operator-trust/v1"
OPERATOR_TRUST_ENV: Final = "LETS_MANIFEST_OPERATOR_KEYS_FILE"
MAX_OPERATOR_TRUST_BYTES: Final = 65_536
MAX_OPERATOR_KEYS: Final = 16


class OperatorTrustError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _strict_object(raw: bytes) -> dict[str, Any]:
    def pairs(values: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        raise OperatorTrustError("invalid_operator_trust_bundle") from None
    if not isinstance(value, dict):
        raise OperatorTrustError("invalid_operator_trust_bundle")
    return value


def load_operator_trust_bundle(path: Path) -> tuple[dict[str, bytes], int]:
    """Load exact Ed25519 operator anchors from a bounded regular file."""

    try:
        metadata = path.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > MAX_OPERATOR_TRUST_BYTES
        ):
            raise OSError
        raw = path.read_bytes()
    except (OSError, ValueError):
        raise OperatorTrustError("invalid_operator_trust_bundle") from None
    document = _strict_object(raw)
    if set(document) != {"api_version", "threshold", "keys"}:
        raise OperatorTrustError("invalid_operator_trust_bundle")
    if document.get("api_version") != OPERATOR_TRUST_API_VERSION:
        raise OperatorTrustError("invalid_operator_trust_bundle")
    threshold = document.get("threshold")
    keys = document.get("keys")
    if (
        type(threshold) is not int
        or threshold < 1
        or not isinstance(keys, list)
        or not 1 <= len(keys) <= MAX_OPERATOR_KEYS
        or threshold > len(keys)
    ):
        raise OperatorTrustError("invalid_operator_trust_bundle")
    trusted: dict[str, bytes] = {}
    material: set[bytes] = set()
    for item in keys:
        if (
            not isinstance(item, dict)
            or set(item) != {"key_id", "algorithm", "public_key"}
            or item.get("algorithm") != "Ed25519"
            or not isinstance(item.get("key_id"), str)
            or not isinstance(item.get("public_key"), str)
        ):
            raise OperatorTrustError("invalid_operator_trust_bundle")
        key_id = item["key_id"]
        try:
            public_key = b64url_decode(item["public_key"])
        except Exception:
            raise OperatorTrustError("invalid_operator_trust_bundle") from None
        if (
            not key_id
            or key_id in trusted
            or len(public_key) != 32
            or public_key in material
        ):
            raise OperatorTrustError("invalid_operator_trust_bundle")
        trusted[key_id] = public_key
        material.add(public_key)
    return trusted, threshold


def build_manifest_authenticator(
    environ: Mapping[str, str] | None = None,
) -> Callable[[bytes, Mapping[str, Any]], bool] | None:
    """Return an authenticator rooted only in the operator-mounted key file."""

    values = os.environ if environ is None else environ
    raw_path = values.get(OPERATOR_TRUST_ENV)
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    path = Path(raw_path.strip())
    if not path.is_absolute():
        raise OperatorTrustError("invalid_operator_trust_bundle")
    trusted, threshold = load_operator_trust_bundle(path.resolve(strict=True))
    allow_insecure = str(values.get("ASTRAL_ENV", "")).strip().lower() in {
        "development",
        "dev",
        "test",
    }

    def authenticate(_raw: bytes, document: Mapping[str, Any]) -> bool:
        try:
            manifest = ClusterManifest.from_dict(
                dict(document),
                allow_insecure_http=allow_insecure,
            )
            manifest.verify_signatures(trusted, threshold=threshold)
        except (SignatureError, ValidationError, TypeError, ValueError):
            return False
        return True

    return authenticate


__all__ = (
    "MAX_OPERATOR_KEYS",
    "MAX_OPERATOR_TRUST_BYTES",
    "OPERATOR_TRUST_API_VERSION",
    "OPERATOR_TRUST_ENV",
    "OperatorTrustError",
    "build_manifest_authenticator",
    "load_operator_trust_bundle",
)
