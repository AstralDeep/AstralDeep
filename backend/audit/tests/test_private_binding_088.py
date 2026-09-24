"""Tests for audit/pii.py: MAC key rotation and lookup, where active vs historical keys
stay exact-match with no case or alias fallback, and malformed key ids never silently
resolve.
"""

import hashlib
import hmac
import os
from dataclasses import FrozenInstanceError

import pytest

from audit.pii import (
    PrivateBindingUnavailable,
    _DEV_FALLBACK_SECRET,
    hmac_digest,
    private_binding_key,
)

KEY = "synthetic-only-binding-" + "0123456789abcdef" * 2
OTHER = "synthetic-rotation-binding-" + "fedcba9876543210" * 2


@pytest.fixture(autouse=True)
def clean_keys(monkeypatch):
    for name in os.environ:
        if name.startswith("AUDIT_HMAC_"):
            monkeypatch.delenv(name)


def test_active_mac_domain_and_private_repr(monkeypatch):
    monkeypatch.setenv("AUDIT_HMAC_SECRET", KEY)
    key = private_binding_key()
    assert key.key_id == "k1"
    derived = hmac.new(
        KEY.encode(), b"astral.work.private/key/v1\0k1", hashlib.sha256
    ).digest()
    expected = hmac.new(
        derived, b"astral.work.private/v1/input\0private", hashlib.sha256
    ).hexdigest()
    assert key.sign("input", b"private") == expected
    assert (
        len({key.sign(domain, b"private") for domain in ("input", "config", "result")})
        == 3
    )
    assert key.verify("input", b"private", expected)
    assert not key.verify("input", b"changed", expected)
    for invalid in (None, b"a" * 64, "A" * 64, "a", "\ud800" * 64):
        assert not key.verify("input", b"private", invalid)
    assert KEY not in repr(key) and repr(derived) not in repr(key)
    assert hmac_digest(b"private")[0] != expected
    with pytest.raises(FrozenInstanceError):
        key.key_id = "relabelled"


def test_historical_key_requires_exact_versioned_entry(monkeypatch):
    monkeypatch.setenv("AUDIT_HMAC_SECRET", KEY)
    original = private_binding_key().sign("input", b"one operation")
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "k2")
    monkeypatch.setenv("AUDIT_HMAC_SECRET", OTHER)
    with pytest.raises(PrivateBindingUnavailable):
        private_binding_key("k1")
    monkeypatch.setenv("AUDIT_HMAC_SECRET_K1", KEY)
    assert private_binding_key("k1").sign("input", b"one operation") == original
    assert private_binding_key().sign("input", b"one operation") != original
    monkeypatch.delenv("AUDIT_HMAC_SECRET_K1")
    with pytest.raises(PrivateBindingUnavailable):
        private_binding_key("k1")


def test_active_versioned_only_and_conflict(monkeypatch):
    monkeypatch.setenv("AUDIT_HMAC_SECRET_K1", KEY)
    selected = private_binding_key()
    monkeypatch.setenv("AUDIT_HMAC_SECRET", KEY)
    assert private_binding_key() == selected
    monkeypatch.setenv("AUDIT_HMAC_SECRET", OTHER)
    with pytest.raises(
        PrivateBindingUnavailable, match="^private_binding_unavailable$"
    ):
        private_binding_key()
    monkeypatch.setenv("AUDIT_HMAC_SECRET_K1", "")
    with pytest.raises(PrivateBindingUnavailable):
        private_binding_key()


@pytest.mark.parametrize(
    "secret",
    [
        None,
        "",
        "short",
        "a" * 31,
        "a" * 4097,
        "change-me",
        "dev-audit-hmac-secret-change-me-in-prod",
        _DEV_FALLBACK_SECRET.decode(),
        " " + KEY,
        KEY + " ",
        KEY + "\n",
        KEY + "\x7f",
        KEY + "\ud800",
    ],
)
def test_missing_placeholder_or_malformed_key_never_falls_back(monkeypatch, secret):
    if secret is not None:
        if "\ud800" in secret:
            original = os.getenv
            monkeypatch.setattr(
                os,
                "getenv",
                lambda name, default=None: (
                    secret if name == "AUDIT_HMAC_SECRET" else original(name, default)
                ),
            )
        else:
            monkeypatch.setenv("AUDIT_HMAC_SECRET", secret)
    with pytest.raises(
        PrivateBindingUnavailable, match="^private_binding_unavailable$"
    ):
        private_binding_key()


@pytest.mark.parametrize(
    "identity",
    ["", "K1", "k-1", " k1", "k1 ", "1key", "k" * 33, "é", "k1\n", "k1\x00", 1, True],
)
def test_key_ids_have_no_case_or_lookup_alias(monkeypatch, identity):
    monkeypatch.setenv("AUDIT_HMAC_SECRET", KEY)
    monkeypatch.setenv("AUDIT_HMAC_SECRET_K1", KEY)
    with pytest.raises(PrivateBindingUnavailable):
        private_binding_key(identity)


def test_malformed_active_id_also_refuses_historical_lookup(monkeypatch):
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "K2")
    monkeypatch.setenv("AUDIT_HMAC_SECRET_K1", KEY)
    with pytest.raises(PrivateBindingUnavailable):
        private_binding_key("k1")


@pytest.mark.parametrize(
    "domain,payload",
    [
        ("unknown", b"x"),
        (None, b"x"),
        ("input", "text"),
        ("input", bytearray(b"x")),
        ("input", b"x" * (2 * 1024 * 1024 + 1)),
    ],
)
def test_domains_and_private_bytes_are_closed(monkeypatch, domain, payload):
    monkeypatch.setenv("AUDIT_HMAC_SECRET", KEY)
    with pytest.raises(PrivateBindingUnavailable):
        private_binding_key().sign(domain, payload)


def test_old_audit_dev_and_unknown_historical_fallback_are_unchanged(monkeypatch):
    first = hmac_digest(b"old", "retired")
    assert first == hmac_digest(b"old", "retired")
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "legacy-short-key")
    assert hmac_digest(b"old", "retired") != first
    assert hmac_digest(b"old", "retired")[0] == hmac_digest(b"old", "active")[0]
    with pytest.raises(PrivateBindingUnavailable):
        private_binding_key("retired")
