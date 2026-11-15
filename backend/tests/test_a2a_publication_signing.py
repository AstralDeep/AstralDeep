"""Covers the node-key signature on the published-capability list and on the public A2A card, including the fail-closed personal-node posture."""

from __future__ import annotations

import os
import stat

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orchestrator import a2a_card_trust as trust
from orchestrator import a2a_publication_signing as signing
from orchestrator.a2a_orchestrator_executor import attach_publication_registry, build_orchestrator_a2a_card
from tests.test_a2a_publication_controls import _publish, publication_host  # noqa: F401


def _signer() -> signing.NodeKeySigner:
    return signing.NodeKeySigner(Ed25519PrivateKey.generate())


def _extension_list(card):
    extension = next(item for item in card.capabilities.extensions if item.uri == signing.EXTENSION_URI)
    return extension.params["list"]


def _write_key(directory, key, mode=0o600):
    path = directory / signing.KEY_FILE
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    path.chmod(mode)
    return path


def test_publication_list_round_trips_and_rejects_other_keys_and_tampering():
    signer, other = _signer(), _signer()
    entries = [{"capability": "weather-1", "audiences": ["tools:read"], "expires_at": None, "publication_id": "p1"}]
    token = signing.sign_publication_list(entries, signer)
    assert signing.verify_publication_list(token, signer.public_jwk()) == entries
    assert signing.sign_publication_list(entries, signer) == token
    header, payload, signature = token.split(".")
    forged = signing.sign_publication_list(entries + [{"capability": "extra"}], other).split(".")
    for bad in (token, f"{header}.{forged[1]}.{signature}", f"{header}.{payload}", "garbage", None):
        if bad == token:
            with pytest.raises(signing.PublicationSigningError):
                signing.verify_publication_list(bad, other.public_jwk())
        else:
            with pytest.raises(signing.PublicationSigningError):
                signing.verify_publication_list(bad, signer.public_jwk())
    with pytest.raises(signing.PublicationSigningError):
        signing.verify_publication_list(token, {"kty": "EC"})


def test_empty_default_card_is_signed_with_an_empty_list(publication_host):  # noqa: F811
    orch = publication_host()
    signer = _signer()
    orch.publication_signer = signer
    card = build_orchestrator_a2a_card(orch)
    assert {skill.id for skill in card.skills} == {"chat"}
    assert signing.verify_publication_list(_extension_list(card), signer.public_jwk()) == []
    trust.verify_card(card, trust.CardTrust(public_key=signer.public_jwk()))


def test_published_capability_appears_in_the_signed_list_and_card_verifies(publication_host):  # noqa: F811
    orch = publication_host()
    signer = _signer()
    orch.publication_signer = signer
    registry = attach_publication_registry(orch)
    record = _publish(registry, audiences=["tools:read"])
    card = build_orchestrator_a2a_card(orch)
    entries = signing.verify_publication_list(_extension_list(card), signer.public_jwk())
    assert [(e["capability"], e["audiences"], e["publication_id"]) for e in entries] == [
        ("weather-1", ["tools:read"], record.publication_id)]
    assert "weather-1" in {skill.id for skill in card.skills}
    first = trust.verify_card(card, trust.CardTrust(public_key=signer.public_jwk()))
    assert trust.verify_card(build_orchestrator_a2a_card(orch), trust.CardTrust(public_key=signer.public_jwk())) == first


def test_adding_a_skill_after_signing_breaks_the_card_signature(publication_host):  # noqa: F811
    orch = publication_host()
    signer = _signer()
    orch.publication_signer = signer
    card = build_orchestrator_a2a_card(orch)
    card.skills.add(id="draft-secret-9", name="draft", description="not published", tags=["x"])
    with pytest.raises(trust.CardRefused) as refusal:
        trust.verify_card(card, trust.CardTrust(public_key=signer.public_jwk()))
    assert refusal.value.code == trust.SIGNATURE_INVALID


def test_withdrawal_removes_the_capability_from_the_next_signed_list(publication_host):  # noqa: F811
    orch = publication_host()
    signer = _signer()
    orch.publication_signer = signer
    registry = attach_publication_registry(orch)
    record = _publish(registry)
    registry.withdraw(publication_id=record.publication_id, actor="admin-sub")
    card = build_orchestrator_a2a_card(orch)
    assert signing.verify_publication_list(_extension_list(card), signer.public_jwk()) == []
    assert {skill.id for skill in card.skills} == {"chat"}


def test_unpublished_capability_is_refused_at_admission(publication_host):  # noqa: F811
    orch = publication_host()
    orch.publication_signer = _signer()
    attach_publication_registry(orch)
    assert orch.a2a_publication_policy.admit("weather-1") is None
    _publish(orch.a2a_publications)
    assert orch.a2a_publication_policy.admit("weather-1") is not None
    assert orch.a2a_publication_policy.admit("web-research-1") is None


def test_personal_node_loads_its_key_from_the_data_directory(publication_host, monkeypatch, tmp_path):  # noqa: F811
    key = Ed25519PrivateKey.generate()
    _write_key(tmp_path, key)
    monkeypatch.setenv(signing.PROFILE_ENV, "personal")
    monkeypatch.setenv(signing.DATA_DIR_ENV, str(tmp_path))
    orch = publication_host()
    registry = attach_publication_registry(orch)
    _publish(registry)
    card = build_orchestrator_a2a_card(orch)
    loaded = signing.load_node_signer()
    assert loaded.key_id == orch.publication_signer.key_id
    assert "weather-1" in {skill.id for skill in card.skills}
    trust.verify_card(card, trust.CardTrust(public_key=loaded.public_jwk()))


@pytest.mark.parametrize("setup", ["missing", "group-readable", "garbage", "wrong-type"])
def test_personal_node_without_a_usable_key_publishes_nothing(publication_host, monkeypatch, tmp_path, setup):  # noqa: F811
    path = tmp_path / signing.KEY_FILE
    if setup == "group-readable":
        _write_key(tmp_path, Ed25519PrivateKey.generate(), 0o644)
    elif setup == "garbage":
        path.write_text("not a key")
        path.chmod(0o600)
    elif setup == "wrong-type":
        _write_key(tmp_path, ec.generate_private_key(ec.SECP256R1()))
    monkeypatch.setenv(signing.PROFILE_ENV, "personal")
    monkeypatch.setenv(signing.DATA_DIR_ENV, str(tmp_path))
    orch = publication_host()
    registry = attach_publication_registry(orch)
    _publish(registry)
    card = build_orchestrator_a2a_card(orch)
    assert {skill.id for skill in card.skills} == {"chat"}
    assert not card.signatures and not card.security_requirements
    assert orch.a2a_publication_policy.admit("weather-1") is None
    assert signing.can_publish(orch) is False
    with pytest.raises(signing.PublicationSigningError):
        signing.load_node_signer()


def test_central_deployment_without_a_key_keeps_unsigned_cards(publication_host, monkeypatch):  # noqa: F811
    monkeypatch.delenv(signing.PROFILE_ENV, raising=False)
    orch = publication_host()
    card = build_orchestrator_a2a_card(orch)
    assert not card.signatures
    assert signing.can_publish(orch) is True
    assert signing.signer_for(orch) is None


def test_key_file_must_be_owner_only_on_disk(tmp_path, monkeypatch):
    path = _write_key(tmp_path, Ed25519PrivateKey.generate())
    monkeypatch.setenv(signing.DATA_DIR_ENV, str(tmp_path))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert signing.load_node_signer().public_jwk()["kty"] == "OKP"
