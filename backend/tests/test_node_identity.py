"""Covers shared/node_identity.py: profile selection, key creation and permissions, EdDSA token minting and refusal, canonical owner ids and the single-owner binding."""

from __future__ import annotations

import base64
import json
import os
import stat

import pytest

from shared import node_identity as ni


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    monkeypatch.delenv(ni.ISSUERS_ENV, raising=False)
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/a/")
    ni.reset_cache()
    yield tmp_path / "node"
    ni.reset_cache()


def second_node(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "other"))
    return ni.load_node_identity()


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_profile_switch(monkeypatch):
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    assert not ni.is_personal()
    monkeypatch.setenv(ni.PROFILE_ENV, " Personal ")
    assert ni.is_personal()
    monkeypatch.setenv(ni.PROFILE_ENV, "institution")
    assert not ni.is_personal()


def test_key_is_generated_private_and_stable(node):
    identity = ni.load_node_identity()
    path = node / ni.KEY_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(node.stat().st_mode) == 0o700
    ni.reset_cache()
    assert ni.load_node_identity().node_id == identity.node_id
    assert identity.node_id.startswith(ni.NODE_ID_PREFIX)


def test_missing_key_without_create_is_refused(node):
    with pytest.raises(ni.NodeKeyError):
        ni.load_node_identity(create=False)
    assert not (node / ni.KEY_FILE).exists()


def test_world_readable_key_is_refused(node):
    ni.load_node_identity()
    ni.reset_cache()
    os.chmod(node / ni.KEY_FILE, 0o644)
    with pytest.raises(ni.NodeKeyError):
        ni.load_node_identity()


def test_garbage_and_wrong_type_keys_are_refused(node):
    ni.load_node_identity()
    ni.reset_cache()
    (node / ni.KEY_FILE).write_text("not a key")
    with pytest.raises(ni.NodeKeyError):
        ni.load_node_identity()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    (node / ni.KEY_FILE).write_bytes(pem)
    with pytest.raises(ni.NodeKeyError):
        ni.load_node_identity()


def test_unwritable_data_directory_is_reported(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(blocker / "sub"))
    ni.reset_cache()
    with pytest.raises(ni.NodeKeyError):
        ni.load_node_identity()


def test_token_round_trip_and_claims(node):
    identity = ni.load_node_identity()
    token = identity.mint_token(
        {"sub": "oidc.x.u", "scope": "tools:read"}, audience=["astral-agent-service"], lifetime=120, now=1000
    )
    claims = identity.verify_token(token, audience="astral-agent-service", now=1010)
    assert claims["iss"] == identity.node_id
    assert claims["aud"] == ["astral-agent-service", identity.node_id]
    assert claims["exp"] - claims["iat"] == 120
    assert ni.peek_algorithm(token) == "EdDSA"


def test_lifetime_is_capped_at_300_seconds(node):
    identity = ni.load_node_identity()
    claims = identity.verify_token(identity.mint_token({"sub": "s"}, lifetime=99999, now=1000), now=1001)
    assert claims["exp"] - claims["iat"] == ni.MAX_TOKEN_SECONDS


def test_expired_future_and_overlong_tokens_are_refused(node):
    identity = ni.load_node_identity()
    token = identity.mint_token({"sub": "s"}, lifetime=60, now=1000)
    with pytest.raises(ni.NodeTokenError):
        identity.verify_token(token, now=1000 + 60 + ni.CLOCK_SKEW_SECONDS)
    with pytest.raises(ni.NodeTokenError):
        identity.verify_token(token, now=1000 - ni.CLOCK_SKEW_SECONDS - 1)
    forged = identity.sign_token(
        {"iss": identity.node_id, "aud": [identity.node_id], "iat": 1000, "exp": 1000 + 301, "sub": "s"}
    )
    with pytest.raises(ni.NodeTokenError):
        identity.verify_token(forged, now=1001)
    missing = identity.sign_token({"iss": identity.node_id, "aud": [identity.node_id], "sub": "s"})
    with pytest.raises(ni.NodeTokenError):
        identity.verify_token(missing, now=1001)


def test_a_token_from_one_node_is_refused_by_another(node, tmp_path, monkeypatch):
    first = ni.load_node_identity()
    token = first.mint_token({"sub": "oidc.x.u"}, audience=["astral-agent-service"])
    other = second_node(tmp_path, monkeypatch)
    assert other.node_id != first.node_id
    with pytest.raises(ni.NodeTokenError):
        other.verify_token(token)


def test_a_token_minted_for_another_audience_is_refused(node, tmp_path, monkeypatch):
    first = ni.load_node_identity()
    other = second_node(tmp_path, monkeypatch)
    forged = first.sign_token(
        {"iss": first.node_id, "aud": [other.node_id], "iat": 1000, "exp": 1100, "sub": "s"}
    )
    with pytest.raises(ni.NodeTokenError):
        first.verify_token(forged, now=1001)
    relabeled = other.sign_token(
        {"iss": first.node_id, "aud": [first.node_id], "iat": 1000, "exp": 1100, "sub": "s"}
    )
    with pytest.raises(ni.NodeTokenError):
        first.verify_token(relabeled, now=1001)


def test_wrong_audience_algorithm_key_id_and_malformed_tokens_are_refused(node):
    identity = ni.load_node_identity()
    token = identity.mint_token({"sub": "s"}, audience=["a"], now=1000)
    with pytest.raises(ni.NodeTokenError):
        identity.verify_token(token, audience="b", now=1001)
    head, body, sig = token.split(".")
    for header in ({"alg": "HS256", "kid": identity.kid}, {"alg": "EdDSA", "kid": "other"}):
        swapped = b64(json.dumps(header).encode()) + "." + body + "." + sig
        with pytest.raises(ni.NodeTokenError):
            identity.verify_token(swapped, now=1001)
    tampered = head + "." + b64(json.dumps({"sub": "evil"}).encode()) + "." + sig
    with pytest.raises(ni.NodeTokenError):
        identity.verify_token(tampered, now=1001)
    for bad in ("", "a.b", "a.b.c.d", "!!.!!.!!", 7, "x" * 20000):
        with pytest.raises(ni.NodeTokenError):
            identity.verify_token(bad, now=1001)
    assert ni.peek_algorithm("nonsense") is None


def test_public_jwks_describes_the_node_key(node):
    identity = ni.load_node_identity()
    key = identity.jwks()["keys"][0]
    assert (key["kty"], key["crv"], key["alg"], key["kid"]) == ("OKP", "Ed25519", "EdDSA", identity.kid)
    assert "d" not in key


def test_derived_secret_is_stable_and_separated(node):
    identity = ni.load_node_identity()
    assert identity.derive_secret("a") == identity.derive_secret("a")
    assert identity.derive_secret("a") != identity.derive_secret("b")


def test_owner_id_depends_on_issuer_and_subject():
    first = ni.canonical_owner_id("https://idp.example/realms/a/", "user-1")
    assert first == ni.canonical_owner_id("https://idp.example/realms/a", "user-1")
    assert first != ni.canonical_owner_id("https://idp.example/realms/b", "user-1")
    assert first != ni.canonical_owner_id("https://idp.example/realms/a", "user-2")
    assert first.startswith("oidc.") and first.endswith(".user-1")


@pytest.mark.parametrize(
    "issuer,subject",
    [("", "u"), (None, "u"), ("https://i", ""), ("https://i", None), ("https://i", " u"), ("https://i", "a:b"), ("https://i", "x" * 300)],
)
def test_owner_id_refuses_unusable_input(issuer, subject):
    with pytest.raises(ni.NodeIdentityError):
        ni.canonical_owner_id(issuer, subject)


def test_accepted_issuers_merge_and_normalize(node, monkeypatch):
    monkeypatch.setenv(ni.ISSUERS_ENV, "https://two.example/realms/b/, https://idp.example/realms/a,,")
    assert ni.accepted_issuers(["https://three.example"]) == [
        "https://idp.example/realms/a",
        "https://three.example",
        "https://two.example/realms/b",
    ]


def test_owner_binding_is_first_come_and_then_fixed(node):
    assert ni.bound_owner() is None
    ni.enforce_owner("oidc.a.one")
    assert ni.bound_owner() == "oidc.a.one"
    assert stat.S_IMODE((node / ni.OWNER_FILE).stat().st_mode) == 0o600
    ni.enforce_owner("oidc.a.one")
    with pytest.raises(ni.OwnerMismatch):
        ni.enforce_owner("oidc.a.two")


def test_owner_from_environment_wins(node, monkeypatch):
    monkeypatch.setenv(ni.OWNER_ENV, "oidc.a.fixed")
    with pytest.raises(ni.OwnerMismatch):
        ni.enforce_owner("oidc.a.other")
    ni.enforce_owner("oidc.a.fixed")
    assert not (node / ni.OWNER_FILE).exists()


def test_enforce_owner_is_a_no_op_outside_the_personal_profile(tmp_path, monkeypatch):
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "n"))
    ni.enforce_owner("anyone")
    assert not (tmp_path / "n").exists()


def test_unwritable_owner_binding_is_reported(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(blocker / "sub"))
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    with pytest.raises(ni.NodeIdentityError):
        ni.bind_owner("oidc.a.one")


def test_principal_hash_is_stable():
    assert ni.owner_principal_hash("o") == ni.owner_principal_hash("o")
    assert ni.owner_principal_hash("o").startswith("sha256:")


def test_mesh_owner_binding_cannot_hide_an_existing_file_behind_environment(node, monkeypatch):
    ni.bind_owner("oidc.old.owner")
    mesh = "sha256:" + "a" * 64
    monkeypatch.setenv(ni.OWNER_ENV, mesh)
    with pytest.raises(ni.OwnerMismatch):
        ni.bind_mesh_owner(mesh)
    assert (node / ni.OWNER_FILE).read_text() == "oidc.old.owner"


def test_concurrent_owner_binding_publishes_one_complete_owner_and_preserves_the_winner(node, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    ready = threading.Barrier(12)
    publishing = threading.Barrier(12)
    link = os.link

    def publish(*args):
        publishing.wait()
        return link(*args)

    monkeypatch.setattr(os, "link", publish)

    def bind(index):
        ready.wait()
        return ni.bind_owner("sha256:" + f"{index:064x}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        owners = list(pool.map(bind, range(12)))
    assert len(set(owners)) == 1
    assert ni.bound_owner() == owners[0]
    assert len(owners[0]) == 71
    assert stat.S_IMODE((node / ni.OWNER_FILE).stat().st_mode) == 0o600


@pytest.mark.parametrize("mesh", [None, "", "mesh-1", "sha256:" + "A" * 64])
def test_mesh_owner_binding_rejects_unusable_principal(node, mesh):
    with pytest.raises(ni.NodeIdentityError):
        ni.bind_mesh_owner(mesh)
    assert not (node / ni.OWNER_FILE).exists()


@pytest.mark.parametrize("operation", ["bind_owner", "bind_mesh_owner"])
def test_empty_existing_owner_file_is_a_refusal_not_permission_to_rebind(node, operation):
    node.mkdir(parents=True)
    (node / ni.OWNER_FILE).write_text("")
    with pytest.raises(ni.NodeIdentityError):
        getattr(ni, operation)("sha256:" + "a" * 64)
    assert (node / ni.OWNER_FILE).read_text() == ""


@pytest.mark.parametrize("failure", ["read", "link", "empty_race"])
def test_owner_file_failure_never_returns_success_or_leaves_temporary_keys(node, monkeypatch, failure):
    from pathlib import Path

    mesh = "sha256:" + "a" * 64
    if failure == "read":
        read = Path.read_text

        def denied(path, *args, **kwargs):
            if path == node / ni.OWNER_FILE:
                raise PermissionError("denied")
            return read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)
    else:
        def denied(*_args):
            if failure == "empty_race":
                (node / ni.OWNER_FILE).write_text("")
                raise FileExistsError()
            raise PermissionError("denied")

        monkeypatch.setattr(os, "link", denied)
    with pytest.raises(ni.NodeIdentityError):
        ni.bind_mesh_owner(mesh)
    assert not list(node.glob(".owner-*"))
