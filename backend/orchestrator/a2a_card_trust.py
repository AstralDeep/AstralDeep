"""Holds the owner-pinned signing keys for external A2A agents and verifies a fetched agent card against them.
orchestrator.py calls it before registering an A2A peer and again on every rediscovery, and audits each refusal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

import jwt
from a2a.types import AgentCard
from a2a.utils.signing import (
    InvalidSignaturesError,
    NoSignatureError,
    SignatureVerificationError,
    create_signature_verifier,
)
from google.protobuf.json_format import MessageToDict
from jwt.exceptions import PyJWTError

from shared import external_http

logger = logging.getLogger("Orchestrator.A2ACardTrust")

ALGORITHMS = ["EdDSA", "ES256", "ES384", "ES512", "RS256", "RS384", "RS512", "PS256", "PS384", "PS512"]
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PRIVATE_JWK_FIELDS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})
_MAX_JWKS_BYTES = 64 * 1024
_MAX_JWKS_KEYS = 20

UNSIGNED = "card_unsigned"
SIGNATURE_INVALID = "card_signature_invalid"
CHANGED = "card_changed_after_approval"
NO_PINNED_KEY = "no_pinned_key"
KEY_UNAVAILABLE = "pinned_key_unavailable"

JwksFetcher = Callable[[str], list[dict[str, Any]]]


class CardRefused(Exception):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class CardTrust:
    jwks_url: str | None = None
    public_key: dict[str, Any] | None = None
    card_sha256: str | None = None

    @staticmethod
    def from_mapping(value: Any) -> "CardTrust":
        if type(value) is not dict:
            raise CardRefused(NO_PINNED_KEY, "Pinned key settings must be an object.")
        jwks_url = value.get("jwks_url") or None
        public_key = value.get("public_key") or None
        digest = value.get("card_sha256") or None
        if jwks_url is not None:
            parts = urlsplit(jwks_url) if type(jwks_url) is str else None
            if (parts is None or parts.scheme != "https" or not parts.hostname
                    or parts.username or parts.password or parts.fragment):
                raise CardRefused(NO_PINNED_KEY, "The pinned JWKS URL must be a credential-free HTTPS URL.")
        if public_key is not None:
            if (type(public_key) is not dict or type(public_key.get("kty")) is not str
                    or _PRIVATE_JWK_FIELDS & set(public_key)):
                raise CardRefused(NO_PINNED_KEY, "The pinned key must be a public JWK without private members.")
            try:
                jwt.PyJWK.from_dict(public_key)
            except PyJWTError:
                raise CardRefused(NO_PINNED_KEY, "The pinned key is not a usable public JWK.") from None
        if jwks_url is None and public_key is None:
            raise CardRefused(NO_PINNED_KEY, "Pin a JWKS URL or a public key to register an A2A agent.")
        if digest is not None and (type(digest) is not str or not _DIGEST.fullmatch(digest)):
            raise CardRefused(NO_PINNED_KEY, "The approved card digest must be sha256: and 64 hex digits.")
        return CardTrust(jwks_url, public_key, digest)


def card_digest(card: AgentCard) -> str:
    body = MessageToDict(card)
    body.pop("signatures", None)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fetch_jwks(url: str) -> list[dict[str, Any]]:
    response = external_http.request(
        "GET", url, api_key="", timeout=5, max_response_bytes=_MAX_JWKS_BYTES,
        extra_headers={"Accept": "application/json"},
    )
    body = response.json()
    keys = body.get("keys") if type(body) is dict else None
    if type(keys) is not list or not 0 < len(keys) <= _MAX_JWKS_KEYS:
        raise ValueError("JWKS has no usable keys")
    return [key for key in keys if type(key) is dict and not _PRIVATE_JWK_FIELDS & set(key)]


def verify_card(card: AgentCard, trust: CardTrust, *, jwks_fetcher: JwksFetcher = fetch_jwks) -> str:
    problems: list[str] = []
    jwks_cache: dict[str, list[dict[str, Any]]] = {}

    def candidates(kid: str | None, jku: str | None) -> list[dict[str, Any]]:
        if jku and trust.jwks_url and jku != trust.jwks_url:
            problems.append("the signature names a key set that is not the pinned one")
            return []
        if jku and not trust.jwks_url:
            problems.append("the signature names a key set but only an inline key is pinned")
            return []
        keys: list[dict[str, Any]] = []
        if trust.public_key:
            keys.append(trust.public_key)
        if trust.jwks_url:
            if trust.jwks_url not in jwks_cache:
                try:
                    jwks_cache[trust.jwks_url] = jwks_fetcher(trust.jwks_url)
                except Exception as error:  # noqa: BLE001
                    problems.append(f"the pinned key set could not be fetched ({type(error).__name__})")
                    jwks_cache[trust.jwks_url] = []
            keys.extend(jwks_cache[trust.jwks_url])
        return [key for key in keys if kid is None or key.get("kid") in (None, kid)]

    def key_provider(kid: str | None, jku: str | None):
        matching = candidates(kid, jku)
        if not matching:
            problems.append("no pinned key matches the signature key id")
            raise jwt.InvalidKeyError("no pinned key")
        if len(matching) > 1:
            raise jwt.InvalidKeyError("ambiguous pinned key")
        return jwt.PyJWK.from_dict(matching[0])

    verifier = create_signature_verifier(key_provider, ALGORITHMS)
    try:
        verifier(card)
    except NoSignatureError:
        raise CardRefused(UNSIGNED, "The agent card is not signed.") from None
    except (InvalidSignaturesError, SignatureVerificationError):
        if any("could not be fetched" in item for item in problems):
            raise CardRefused(KEY_UNAVAILABLE, "The pinned key set could not be fetched, so the card was not accepted.") from None
        detail = f" ({problems[0]})" if problems else ""
        raise CardRefused(SIGNATURE_INVALID, "The agent card signature does not verify against the pinned key" + detail + ".") from None
    return card_digest(card)


class CardTrustRegistry:
    def __init__(self) -> None:
        self._trust: dict[str, CardTrust] = {}
        self._approved: dict[str, str] = {}

    @staticmethod
    def _key(base_url: str) -> str:
        return base_url.strip().rstrip("/")

    def pin(self, base_url: str, trust: CardTrust) -> None:
        key = self._key(base_url)
        self._trust[key] = trust
        if trust.card_sha256:
            self._approved[key] = trust.card_sha256
        else:
            self._approved.pop(key, None)

    def forget(self, base_url: str) -> None:
        self._trust.pop(self._key(base_url), None)
        self._approved.pop(self._key(base_url), None)

    def trust_for(self, base_url: str) -> CardTrust | None:
        return self._trust.get(self._key(base_url))

    def admit(self, base_url: str, card: AgentCard, *, jwks_fetcher: JwksFetcher = fetch_jwks) -> str:
        key = self._key(base_url)
        trust = self._trust.get(key)
        if trust is None:
            raise CardRefused(NO_PINNED_KEY, "No signing key was pinned for this agent. Register it with a JWKS URL or public key.")
        digest = verify_card(card, trust, jwks_fetcher=jwks_fetcher)
        approved = self._approved.get(key)
        if approved is None:
            self._approved[key] = digest
        elif approved != digest:
            raise CardRefused(CHANGED, "The agent card changed since it was approved. Register the agent again to approve the new card.")
        return digest


def registry_from_environment(raw: str) -> CardTrustRegistry:
    registry = CardTrustRegistry()
    if not raw.strip():
        return registry
    try:
        pinned = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("A2A_EXTERNAL_AGENT_KEYS is not valid JSON, so no external A2A agent has a pinned key")
        return registry
    if type(pinned) is not dict:
        logger.error("A2A_EXTERNAL_AGENT_KEYS must be a JSON object, so no external A2A agent has a pinned key")
        return registry
    for base_url, settings in pinned.items():
        try:
            registry.pin(str(base_url), CardTrust.from_mapping(settings))
        except CardRefused as refusal:
            logger.error("Pinned key for %s ignored: %s", base_url, refusal.reason)
    return registry
