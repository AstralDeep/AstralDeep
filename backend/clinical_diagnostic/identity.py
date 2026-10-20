"""Validates explicit non-secret operator observations of the measured backend model.
These inputs describe provenance; they never configure or authenticate the product.
"""

from dataclasses import dataclass, asdict
import json
from urllib.parse import urlsplit


def public_url(value, schemes=("https", "http", "wss", "ws")):
    if not isinstance(value, str) or any(character.isspace() for character in value):
        raise ValueError("endpoint must be an absolute credential-free URL")
    parsed = urlsplit(value)
    parsed.port
    if (parsed.scheme not in schemes or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or len(value) > 2048):
        raise ValueError("endpoint must be an absolute credential-free URL without query or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class AstralModelObservation:
    provider: str
    model: str
    base_url: str
    verification_reference: str

    def __post_init__(self):
        for name in ("provider", "model", "verification_reference"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError("model observation fields must be non-empty bounded strings")
        public_url(self.base_url, ("http", "https"))

    def to_dict(self):
        return {"status": "operator_verified_input", **asdict(self)}


def load_model_observation(path):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or set(payload) != {
            "provider", "model", "base_url", "verification_reference"}:
        raise ValueError("model observation requires only provider, model, base_url, verification_reference")
    return AstralModelObservation(**payload)
