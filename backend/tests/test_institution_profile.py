"""Covers shared/institution_profile.py and the startup posture it feeds: statements signed by an institution key verify offline and every tamper is refused with a dotted code, and a missing or malformed institution key, issuer or approval stops the process with exit 78."""

from __future__ import annotations

import json
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from orchestrator.session_store import assert_production_posture
from shared import institution_profile as ip
from shared import node_identity as ni

KEY = ec.derive_private_key(0x1D2C3B4A5968778695A4B3C2D1E0F00F, ec.SECP256R1())
OTHER = ec.derive_private_key(0x0F1E2D3C4B5A69788796A5B4C3D2E1F0, ec.SECP256R1())
NOW = 1_800_000_000


def statement(key=KEY, *, kind="node", subject="astral-node:abc", issued=NOW - 10, until=NOW + 1000):
    return ip.sign_phi_approval(key, subject_kind=kind, subject=subject, issued_s=issued, not_after_s=until)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv(ni.PROFILE_ENV, "institution")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/a")
    monkeypatch.setenv(ip.KEY_ENV, ip.key_text(KEY))
    monkeypatch.delenv(ip.APPROVAL_ENV, raising=False)
    monkeypatch.delenv(ni.ISSUERS_ENV, raising=False)
    ni.reset_cache()
    yield tmp_path
    ni.reset_cache()


def test_a_signed_statement_verifies_against_the_institution_key():
    found = ip.verify_phi_approval(statement(), ip.key_text(KEY), subject_kind="node", subject="astral-node:abc", now_s=NOW)
    assert found["subject"] == "astral-node:abc"


def test_the_statement_has_exactly_the_contract_fields_and_a_low_s_raw_signature():
    signed = statement()
    assert set(signed) == ip.FIELDS and signed["kind"] == "phi" and signed["institution"] == ip.key_text(KEY)
    assert len(ip._unb64url(signed["sig"])) == 64


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda s: {**s, "extra": 1}, "attestation.malformed"),
        (lambda s: {k: v for k, v in s.items() if k != "sig"}, "attestation.malformed"),
        (lambda s: {**s, "kind": "identity"}, "attestation.malformed"),
        (lambda s: {**s, "subject_kind": "laptop"}, "attestation.malformed"),
        (lambda s: {**s, "subject": ""}, "attestation.malformed"),
        (lambda s: {**s, "issued_s": "1"}, "attestation.malformed"),
        (lambda s: {**s, "not_after_s": s["issued_s"]}, "attestation.malformed"),
        (lambda s: {**s, "institution": ip.key_text(OTHER)}, "attestation.unknown_authority"),
        (lambda s: {**s, "not_after_s": s["not_after_s"] + 1}, "attestation.bad_signature"),
        (lambda s: {**s, "sig": "AAAA"}, "attestation.bad_signature"),
        (lambda s: {**s, "sig": "!!!"}, "attestation.malformed"),
        (lambda s: {**s, "subject": "astral-node:other"}, "attestation.bad_signature"),
    ],
)
def test_a_tampered_statement_is_refused(change, code):
    with pytest.raises(ip.ApprovalError) as info:
        ip.verify_phi_approval(change(statement()), ip.key_text(KEY), subject_kind="node", subject="astral-node:abc", now_s=NOW)
    assert info.value.code == code


def test_a_statement_signed_by_another_key_is_refused_even_when_it_claims_the_institution():
    forged = {**statement(OTHER), "institution": ip.key_text(KEY)}
    with pytest.raises(ip.ApprovalError, match="attestation.bad_signature"):
        ip.verify_phi_approval(forged, ip.key_text(KEY), subject_kind="node", subject="astral-node:abc")


def test_a_statement_for_another_subject_or_kind_is_refused():
    for kind, subject in (("device", "astral-node:abc"), ("node", "astral-node:zzz")):
        with pytest.raises(ip.ApprovalError, match="attestation.wrong_subject"):
            ip.verify_phi_approval(statement(), ip.key_text(KEY), subject_kind=kind, subject=subject)


@pytest.mark.parametrize("now", [NOW - 100, NOW + 5000])
def test_a_statement_outside_its_window_is_expired(now):
    with pytest.raises(ip.ApprovalError, match="attestation.expired"):
        ip.verify_phi_approval(statement(), ip.key_text(KEY), subject_kind="node", subject="astral-node:abc", now_s=now)


def test_a_high_s_signature_is_refused():
    signed = statement()
    raw = ip._unb64url(signed["sig"])
    s = ip.P256_ORDER - int.from_bytes(raw[32:], "big")
    flipped = {**signed, "sig": ip._b64url(raw[:32] + s.to_bytes(32, "big"))}
    with pytest.raises(ip.ApprovalError, match="attestation.bad_signature"):
        ip.verify_phi_approval(flipped, ip.key_text(KEY), subject_kind="node", subject="astral-node:abc")


def test_the_configuration_loads_with_a_key_and_an_issuer(env):
    config = ip.load()
    assert config.key == ip.key_text(KEY) and config.issuers == ("https://idp.example/realms/a",)
    assert config.approval is None and config.current_approval() is None


def test_the_configuration_declares_a_verified_node_approval_while_it_is_current(env, monkeypatch):
    node_id = ni.load_node_identity().node_id
    path = env / "approval.json"
    path.write_text(json.dumps(statement(subject=node_id, issued=0, until=NOW)))
    monkeypatch.setenv(ip.APPROVAL_ENV, str(path))
    config = ip.load()
    assert config.current_approval(NOW - 1)["subject"] == node_id
    assert config.current_approval(NOW + 1) is None


@pytest.mark.parametrize("variable", [ip.KEY_ENV, "KEYCLOAK_AUTHORITY"])
def test_a_missing_key_or_issuer_stops_the_process_with_exit_78(env, monkeypatch, variable):
    monkeypatch.delenv(variable)
    with pytest.raises(SystemExit) as info:
        ip.enforce()
    assert info.value.code == 78


@pytest.mark.parametrize("key", ["not-a-key", "AAAA", ip.key_text(KEY)[:-4] + "AAAA"])
def test_a_malformed_key_stops_the_process_with_exit_78(env, monkeypatch, key):
    monkeypatch.setenv(ip.KEY_ENV, key)
    with pytest.raises(SystemExit) as info:
        ip.enforce()
    assert info.value.code == 78


@pytest.mark.parametrize("content", ["not json", "[]", json.dumps(statement(subject="astral-node:someone-else")), json.dumps(statement(OTHER, subject="x"))])
def test_a_bad_approval_file_stops_the_process_with_exit_78(env, monkeypatch, content):
    path = env / "approval.json"
    path.write_text(content)
    monkeypatch.setenv(ip.APPROVAL_ENV, str(path))
    with pytest.raises(SystemExit) as info:
        ip.enforce()
    assert info.value.code == 78


def test_a_missing_or_oversized_approval_file_stops_the_process_with_exit_78(env, monkeypatch):
    monkeypatch.setenv(ip.APPROVAL_ENV, str(env / "absent.json"))
    with pytest.raises(SystemExit):
        ip.enforce()
    big = env / "big.json"
    big.write_text(" " * 9000)
    monkeypatch.setenv(ip.APPROVAL_ENV, str(big))
    with pytest.raises(SystemExit):
        ip.enforce()


def test_the_startup_posture_refuses_an_institution_without_a_key_in_development_too(env, monkeypatch):
    monkeypatch.delenv(ip.KEY_ENV)
    with pytest.raises(SystemExit) as info:
        assert_production_posture()
    assert info.value.code == 78


def test_the_production_posture_accepts_a_configured_institution(env, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "production")
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", "x" * 44)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "y" * 44)
    monkeypatch.setenv("AUDIT_HMAC_SECRET", f"high-entropy-{uuid.uuid4()}")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "z" * 32)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    assert_production_posture()
    monkeypatch.delenv(ip.KEY_ENV)
    with pytest.raises(SystemExit) as info:
        assert_production_posture()
    assert info.value.code == 78


def test_other_profiles_are_not_asked_for_an_institution_key(env, monkeypatch):
    monkeypatch.delenv(ni.PROFILE_ENV)
    monkeypatch.delenv(ip.KEY_ENV)
    assert_production_posture()


def test_the_offline_tool_prints_the_public_key_and_signs_a_statement_a_node_accepts(tmp_path, capsys):
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    path = tmp_path / "institution.pem"
    path.write_bytes(KEY.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    assert ip.main(["public-key", "--key", str(path)]) == 0
    assert capsys.readouterr().out.strip() == ip.key_text(KEY)
    assert ip.main(["sign", "--key", str(path), "--kind", "node", "--subject", "astral-node:abc", "--days", "30"]) == 0
    signed = json.loads(capsys.readouterr().out)
    found = ip.verify_phi_approval(signed, ip.key_text(KEY), subject_kind="node", subject="astral-node:abc", now_s=signed["issued_s"])
    assert found["not_after_s"] - found["issued_s"] == 30 * 86400


def test_the_offline_tool_refuses_a_missing_or_wrong_kind_of_key_and_a_silly_lifetime(tmp_path, capsys):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    assert ip.main(["public-key", "--key", str(tmp_path / "absent.pem")]) == 1
    wrong = tmp_path / "ed.pem"
    wrong.write_bytes(ed25519.Ed25519PrivateKey.generate().private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    assert ip.main(["public-key", "--key", str(wrong)]) == 1
    good = tmp_path / "p256.pem"
    good.write_bytes(KEY.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    assert ip.main(["sign", "--key", str(good), "--kind", "node", "--subject", "n", "--days", "0"]) == 1
    assert "key" in capsys.readouterr().err
