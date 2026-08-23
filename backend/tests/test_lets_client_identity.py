"""Per-request minted EdDSA service identity for the LETS warden client."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from lets.canonical import b64url_decode, b64url_encode
from lets.client import LETSClient
from lets.providers.generic import Ed25519JWTAuthenticator
from nacl.signing import SigningKey, VerifyKey

from orchestrator.lets_client import (
    LetsClientBoundaryError,
    LetsIdentityMinter,
    MintingLETSClient,
    create_lets_warden_client,
)
from orchestrator.lets_config import (
    AuthenticatedTrustManifest,
    LetsHostConfig,
    LetsIdentityConfig,
    SecretFileReference,
)

POLICY_DIGEST = "sha256:" + "1" * 64
MACHINE_DIGEST = "sha256:" + "2" * 64
SEED = bytes(range(32))
TENANT = "tenant-a"
ISSUER = "https://astral.example/lets-identity"
AUDIENCE = "astral-lets-warden"
KID = "astral-orch/ed25519-1"


def _identity(seed_path: Path, **changes: Any) -> LetsIdentityConfig:
    values: dict[str, Any] = {
        "seed_file": SecretFileReference(path=seed_path, size_bytes=32),
        "kid": KID,
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "subject": "astraldeep-orchestrator",
        "scopes": ("lets.lease.issue", "lets.lease.manage", "lets.branch.revoke"),
        "token_ttl_seconds": 120,
    }
    values.update(changes)
    return LetsIdentityConfig(**values)


def _config(**changes: object) -> LetsHostConfig:
    manifest = AuthenticatedTrustManifest(
        path=Path("C:/synthetic/trust-manifest.json"),
        sha256="3" * 64,
        tenant_id=TENANT,
        envelope_id="envelope-a",
        config_epoch=7,
        warden_id="warden-a",
        policy_digest=POLICY_DIGEST,
        machine_digest=MACHINE_DIGEST,
        max_lease_ttl_ns=120_000_000_000,
    )
    values: dict[str, object] = {
        "master_enabled": True,
        "mode": "enforce",
        "environment": "production",
        "governed_cohorts": ("server_dynamic", "byo_user"),
        "governed_agent_allowlist": (),
        "warden_origin": "https://warden.example",
        "service_token_file": None,
        "identity": _identity(Path("C:/synthetic/identity.seed")),
        "tenant_id": TENANT,
        "envelope_id": "envelope-a",
        "policy_digest": POLICY_DIGEST,
        "machine_digest": MACHINE_DIGEST,
        "default_allocation": (2, 2, 2, 2, 2, 2),
        "default_ttl_seconds": 60,
        "request_timeout_seconds": 2.5,
        "request_attempts": 2,
        "trust_manifest": manifest,
    }
    values.update(changes)
    return LetsHostConfig(**values)  # type: ignore[arg-type]


def _minter(**changes: Any) -> LetsIdentityMinter:
    return LetsIdentityMinter(
        seed=SEED,
        identity=_identity(Path("C:/synthetic/identity.seed"), **changes),
        tenant_id=TENANT,
    )


def _decode(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, str]:
    encoded_header, encoded_payload, encoded_signature = token.split(".")
    return (
        json.loads(b64url_decode(encoded_header)),
        json.loads(b64url_decode(encoded_payload)),
        b64url_decode(encoded_signature),
        f"{encoded_header}.{encoded_payload}",
    )


def _authenticator(tmp_path: Path, **changes: Any) -> Ed25519JWTAuthenticator:
    keys = tmp_path / "identity-keys.json"
    keys.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "kid": KID,
                        "public_key": b64url_encode(bytes(SigningKey(SEED).verify_key)),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    values: dict[str, Any] = {
        "key_file": keys,
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "tenant_id": TENANT,
        "clock_skew_s": 30,
        "max_lifetime_s": 900,
    }
    values.update(changes)
    return Ed25519JWTAuthenticator(**values)


# --- minter ---------------------------------------------------------------


def test_minted_token_has_exact_header_claims_and_verifies_against_seed() -> None:
    minter = _minter()
    now = 1_800_000_000

    token = minter.mint(now=now)
    header, claims, signature, signing_input = _decode(token)

    assert token.isascii() and token.count(".") == 2
    assert header == {"alg": "EdDSA", "kid": KID, "typ": "JWT"}
    assert list(header) == ["alg", "kid", "typ"]
    assert set(claims) == {"aud", "exp", "iat", "iss", "jti", "nbf", "scope", "sub", "tenant_id"}
    assert list(claims) == sorted(claims)
    assert claims["iss"] == ISSUER
    assert claims["aud"] == AUDIENCE
    assert claims["sub"] == "astraldeep-orchestrator"
    assert claims["tenant_id"] == TENANT
    assert claims["scope"] == "lets.lease.issue lets.lease.manage lets.branch.revoke"
    assert claims["iat"] == now - 1
    assert claims["nbf"] == claims["iat"]
    assert claims["exp"] == claims["iat"] + 120
    assert claims["iat"] <= claims["nbf"] <= claims["exp"]
    assert claims["jti"].startswith("astral-") and len(claims["jti"]) == 7 + 32
    assert len(signature) == 64
    VerifyKey(SigningKey(SEED).verify_key.encode()).verify(signing_input.encode("ascii"), signature)
    assert minter.public_key == bytes(SigningKey(SEED).verify_key)


def test_minted_token_is_accepted_by_the_generic_production_verifier(tmp_path: Path) -> None:
    authenticator = _authenticator(tmp_path)
    token = _minter().mint()

    context = authenticator.authenticate(
        SimpleNamespace(headers={"authorization": f"Bearer {token}"})
    )

    assert context.subject_id == "astraldeep-orchestrator"
    assert context.tenant_id == TENANT
    assert context.scopes == frozenset(
        {"lets.lease.issue", "lets.lease.manage", "lets.branch.revoke"}
    )
    assert context.authentication_method == "jwt-eddsa"


def test_generic_verifier_rejects_wrong_key_issuer_or_stale_token(tmp_path: Path) -> None:
    from lets.auth import AuthenticationError

    authenticator = _authenticator(tmp_path)
    other = LetsIdentityMinter(
        seed=b"\x42" * 32,
        identity=_identity(Path("C:/synthetic/other.seed")),
        tenant_id=TENANT,
    )
    for bad in (
        other.mint(),
        _minter(issuer="https://other.example").mint(),
        _minter().mint(now=int(time.time()) - 10_000),
        _minter(token_ttl_seconds=901).mint(),
    ):
        with pytest.raises(AuthenticationError):
            authenticator.authenticate(SimpleNamespace(headers={"authorization": f"Bearer {bad}"}))


def test_every_mint_is_unique_and_fresh() -> None:
    minter = _minter()
    first = _decode(minter.mint())[1]
    second = _decode(minter.mint())[1]

    assert first["jti"] != second["jti"]
    assert abs(first["iat"] - (int(time.time()) - 1)) <= 2


@pytest.mark.parametrize("seed", [b"", b"\x01" * 31, b"\x01" * 33, "not-bytes"])
def test_minter_refuses_bad_seed_material(seed: object) -> None:
    with pytest.raises(LetsClientBoundaryError, match="^credential_invalid$"):
        LetsIdentityMinter(
            seed=seed,  # type: ignore[arg-type]
            identity=_identity(Path("C:/synthetic/identity.seed")),
            tenant_id=TENANT,
        )


@pytest.mark.parametrize("ttl", [0, -1, 3601])
def test_minter_refuses_out_of_policy_ttl(ttl: int) -> None:
    with pytest.raises(LetsClientBoundaryError, match="^credential_invalid$"):
        _minter(token_ttl_seconds=ttl)


def test_minter_representation_and_errors_carry_no_material() -> None:
    minter = _minter()
    rendered = repr(minter) + str(minter)
    assert rendered == "<LetsIdentityMinter><LetsIdentityMinter>"
    assert SEED.hex() not in rendered
    assert not hasattr(minter, "__dict__")


# --- MintingLETSClient ----------------------------------------------------


def _recording_transport(seen: list[str | None], *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(
            status,
            json={"tenant_id": TENANT},
            headers={"content-type": "application/json"},
        )

    return httpx.MockTransport(handler)


def test_minting_client_refreshes_bearer_header_on_every_request() -> None:
    seen: list[str | None] = []
    client = MintingLETSClient(
        "https://warden.example",
        token_minter=_minter(),
        transport=_recording_transport(seen),
        total_timeout_s=2.0,
    )
    try:
        assert "authorization" not in client._client.headers
        client.info()
        client.info()
        client.info()
    finally:
        client.close()

    assert len(seen) == 3
    assert all(value is not None and value.startswith("Bearer ") for value in seen)
    assert len(set(seen)) == 3
    for value in seen:
        assert value is not None
        _, claims, _, _ = _decode(value.removeprefix("Bearer "))
        assert claims["aud"] == AUDIENCE


def test_minting_client_header_survives_deadline_client_recreation() -> None:
    seen: list[str | None] = []
    client = MintingLETSClient(
        "https://warden.example",
        token_minter=_minter(),
        transport=_recording_transport(seen),
        total_timeout_s=2.0,
    )
    try:
        client.info()
        original = client._client
        factory = client._client_factory
        assert factory is not None
        client._client = factory()  # same path as the watchdog-fired recreation
        assert client._client is not original
        assert "authorization" not in client._client.headers
        client.info()
    finally:
        client.close()

    assert len(seen) == 2 and None not in seen and seen[0] != seen[1]


def test_minting_client_refuses_a_static_token() -> None:
    with pytest.raises(ValueError):
        MintingLETSClient(
            "https://warden.example",
            token="static",
            token_minter=_minter(),
            transport=_recording_transport([]),
        )


def test_minting_client_is_a_lets_client() -> None:
    assert issubclass(MintingLETSClient, LETSClient)


# --- create_lets_warden_client -------------------------------------------


class CapturingFactory:
    def __init__(self) -> None:
        self.base_url: str | None = None
        self.keywords: dict[str, Any] = {}
        self.closed = 0

    def __call__(self, base_url: str, **keywords: Any) -> "CapturingFactory":
        self.base_url = base_url
        self.keywords = keywords
        return self

    def close(self) -> None:
        self.closed += 1


def test_factory_builds_minting_client_from_seed_and_never_a_static_token(
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "identity.seed"
    seed_path.write_bytes(SEED)
    factory = CapturingFactory()

    boundary = create_lets_warden_client(
        _config(identity=_identity(seed_path)),
        client_factory=factory,  # type: ignore[arg-type]
    )
    boundary.close()

    assert factory.base_url == "https://warden.example"
    assert factory.keywords["token"] is None
    minter = factory.keywords["token_minter"]
    assert isinstance(minter, LetsIdentityMinter)
    assert minter.public_key == bytes(SigningKey(SEED).verify_key)
    _, claims, _, _ = _decode(minter.mint())
    assert claims["tenant_id"] == TENANT
    assert factory.keywords["verify"] is True
    assert factory.keywords["cert"] is None
    assert factory.closed == 1


def test_default_factory_in_minted_mode_is_the_minting_client(tmp_path: Path) -> None:
    seed_path = tmp_path / "identity.seed"
    seed_path.write_bytes(SEED)

    boundary = create_lets_warden_client(_config(identity=_identity(seed_path)))
    try:
        assert isinstance(boundary._client, MintingLETSClient)
    finally:
        boundary.close()


def test_seed_reader_failures_are_redacted(tmp_path: Path) -> None:
    missing = tmp_path / "missing.seed"
    with pytest.raises(LetsClientBoundaryError, match="^credential_unavailable$") as raised:
        create_lets_warden_client(
            _config(identity=_identity(missing)),
            client_factory=CapturingFactory(),  # type: ignore[arg-type]
        )
    assert str(missing) not in repr(raised.value)

    for contents in (b"", b"\x01" * 31, b"\x01" * 33):
        short = tmp_path / "short.seed"
        short.write_bytes(contents)
        with pytest.raises(LetsClientBoundaryError, match="^credential_invalid$"):
            create_lets_warden_client(
                _config(identity=_identity(short)),
                client_factory=CapturingFactory(),  # type: ignore[arg-type]
            )

    def exploding(_reference: SecretFileReference) -> bytes:
        raise RuntimeError("synthetic-sensitive-marker")

    with pytest.raises(LetsClientBoundaryError, match="^credential_unavailable$") as raised:
        create_lets_warden_client(
            _config(),
            client_factory=CapturingFactory(),  # type: ignore[arg-type]
            seed_reader=exploding,
        )
    assert "synthetic-sensitive-marker" not in repr(raised.value)


def test_injected_seed_reader_is_used_and_static_reader_is_not(tmp_path: Path) -> None:
    calls: list[str] = []

    def read_seed(_reference: SecretFileReference) -> bytes:
        calls.append("seed")
        return SEED

    def read_token(_reference: SecretFileReference) -> str:
        calls.append("token")
        return "never"

    factory = CapturingFactory()
    create_lets_warden_client(
        _config(),
        client_factory=factory,  # type: ignore[arg-type]
        secret_reader=read_token,
        seed_reader=read_seed,
    ).close()

    assert calls == ["seed"]
    assert "token_minter" in factory.keywords


def test_static_token_path_is_unchanged(tmp_path: Path) -> None:
    token_path = tmp_path / "service-token"
    token_path.write_bytes(b"static-synthetic-value\n")
    factory = CapturingFactory()

    create_lets_warden_client(
        _config(
            identity=None,
            service_token_file=SecretFileReference(path=token_path, size_bytes=23),
        ),
        client_factory=factory,  # type: ignore[arg-type]
    ).close()

    assert factory.keywords["token"] == "static-synthetic-value"
    assert "token_minter" not in factory.keywords


def test_exactly_one_credential_source_is_required() -> None:
    token = SecretFileReference(path=Path("C:/synthetic/service-token"), size_bytes=1)
    both = _config(service_token_file=token)
    neither = _config(identity=None, service_token_file=None)
    for config in (both, neither):
        with pytest.raises(LetsClientBoundaryError, match="^client_not_configured$"):
            create_lets_warden_client(
                config,
                client_factory=CapturingFactory(),  # type: ignore[arg-type]
                secret_reader=lambda _reference: "token",
                seed_reader=lambda _reference: SEED,
            )


def test_identity_config_replace_keeps_frozen_semantics(tmp_path: Path) -> None:
    identity = _identity(tmp_path / "identity.seed")
    narrowed = replace(identity, scopes=("lets.lease.issue",))
    assert narrowed.scopes == ("lets.lease.issue",)
    with pytest.raises(AttributeError):
        identity.kid = "other"  # type: ignore[misc]
