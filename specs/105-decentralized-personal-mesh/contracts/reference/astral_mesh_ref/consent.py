"""Consent objects: a presence-key signature over the digest of one exact action or one policy document.
Tickets and the budget policy both use it, and the host that draws the prompt computes the digest.
"""

from __future__ import annotations

from typing import Any

from . import crypto
from .cj import b64url_decode
from .errors import MeshError

CONSENT_FIELDS = {"clock", "device", "digest", "kind", "sig", "v"}
CONSENT_KINDS = ("action", "policy", "grant")


def build(
    kind: str, digest: str, device: str, presence_scalar: int, clock: dict[str, int]
) -> dict[str, Any]:
    consent: dict[str, Any] = {
        "clock": clock,
        "device": device,
        "digest": digest,
        "kind": kind,
        "v": 1,
    }
    consent["sig"] = crypto.sign_object(
        "p256", presence_scalar, "astral.consent/v1", consent
    )
    return consent


def verify(
    consent: Any, kind: str, digest: str, presence_keys: dict[str, str]
) -> dict[str, Any]:
    if (
        not isinstance(consent, dict)
        or set(consent) != CONSENT_FIELDS
        or consent["v"] != 1
        or consent["kind"] not in CONSENT_KINDS
        or not isinstance(consent["device"], str)
        or not isinstance(consent["sig"], str)
    ):
        raise MeshError("consent.malformed", "fields")
    clock = consent["clock"]
    if (
        not isinstance(clock, dict)
        or set(clock) != {"ms", "n"}
        or any(type(v) is not int or v < 0 for v in clock.values())
    ):
        raise MeshError("consent.malformed", "clock")
    if consent["kind"] != kind or consent["digest"] != digest:
        raise MeshError(
            "consent.wrong_action",
            "kind or digest differs from what is being authorized",
        )
    key = presence_keys.get(consent["device"])
    if key is None:
        raise MeshError("consent.unknown_device", str(consent["device"]))
    if not crypto.verify_object(
        "p256", b64url_decode(key), "astral.consent/v1", consent
    ):
        raise MeshError("consent.bad_signature", "presence signature")
    return consent
