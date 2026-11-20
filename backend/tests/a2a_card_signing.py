"""Signs A2A agent cards with a throwaway Ed25519 key so tests can exercise the card-signature gate."""

from __future__ import annotations

from typing import Any

from a2a.types import AgentCard
from a2a.utils.signing import create_agent_card_signer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from google.protobuf.json_format import MessageToDict, ParseDict
from jwt.algorithms import OKPAlgorithm


class TestSigner:
    __test__ = False

    def __init__(self, kid: str = "peer-key-1"):
        self.kid = kid
        self._private = Ed25519PrivateKey.generate()
        self._pem = self._private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        import json
        self.public_jwk: dict[str, Any] = json.loads(OKPAlgorithm.to_jwk(self._private.public_key()))
        self.public_jwk["kid"] = kid

    def sign(self, card: AgentCard, *, jku: str | None = None) -> AgentCard:
        header: dict[str, Any] = {"kid": self.kid, "alg": "EdDSA", "typ": "JOSE"}
        if jku:
            header["jku"] = jku
        return create_agent_card_signer(self._pem, header)(card)

    def sign_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        card = ParseDict(dict(payload), AgentCard(), ignore_unknown_fields=True)
        return MessageToDict(self.sign(card))
