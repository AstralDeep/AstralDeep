"""Tests for audit/pii.py and audit/schemas.py: extension normalization,
filename/payload stripping, HMAC digest determinism, and AuditEventCreate's rejection
of oversize or payload-shaped input.
"""

from __future__ import annotations

import os

import pytest


def test_normalize_extension_keeps_lowercase_alphanumeric():
    from audit.pii import normalize_extension
    assert normalize_extension("scan.DCM") == "dcm"
    assert normalize_extension("Report.pdf") == "pdf"
    assert normalize_extension("ARCHIVE.tar") == "tar"


def test_normalize_extension_rejects_pathological_inputs():
    from audit.pii import normalize_extension
    assert normalize_extension("") is None
    assert normalize_extension(None) is None
    assert normalize_extension("no_dot") is None or len(normalize_extension("no_dot") or "") <= 16
    assert normalize_extension("file." + "a" * 17) is None
    assert normalize_extension("file.tar.gz!") is None


def test_strip_filename_drops_filename_keys_and_payload_keys():
    from audit.pii import strip_filename
    cleaned = strip_filename({
        "filename": "patient_smith_mri.dcm",
        "original_name": "ignored.txt",
        "size_bytes": 1024,
        "content": "PHI HERE",
        "method": "upload",
    })
    assert "filename" not in cleaned
    assert "original_name" not in cleaned
    assert "content" not in cleaned
    assert cleaned["size_bytes"] == 1024
    assert cleaned["method"] == "upload"
    assert cleaned["extension"] == "dcm"


def test_strip_filename_handles_non_dict_input():
    from audit.pii import strip_filename
    assert strip_filename(None) == {}  # type: ignore[arg-type]
    assert strip_filename([]) == {}  # type: ignore[arg-type]


def test_hmac_digest_is_deterministic_per_key():
    from audit.pii import hmac_digest
    a, kid_a = hmac_digest(b"hello", key_id="k1")
    b, kid_b = hmac_digest(b"hello", key_id="k1")
    assert a == b and kid_a == kid_b == "k1"


def test_hmac_digest_changes_with_key():
    from audit.pii import hmac_digest
    os.environ["AUDIT_HMAC_SECRET_KX"] = "alternate-secret-for-test"
    a, _ = hmac_digest(b"same", key_id="k1")
    b, _ = hmac_digest(b"same", key_id="kx")
    assert a != b


def test_hmac_digest_rejects_non_bytes():
    from audit.pii import hmac_digest
    with pytest.raises(TypeError):
        hmac_digest("not bytes")  # type: ignore[arg-type]


def test_audit_event_create_drops_payload_shaped_fields():
    from datetime import datetime, timezone
    from audit.schemas import AuditEventCreate

    ev = AuditEventCreate(
        actor_user_id="u",
        auth_principal="u",
        event_class="auth",
        action_type="auth.test",
        description="test",
        correlation_id="c",
        outcome="success",
        inputs_meta={
            "method": "password",
            "filename": "patient.dcm",
            "content": "PHI",
            "body": "more PHI",
        },
        started_at=datetime.now(timezone.utc),
    )
    assert "filename" not in ev.inputs_meta
    assert "content" not in ev.inputs_meta
    assert "body" not in ev.inputs_meta
    assert ev.inputs_meta["method"] == "password"
    assert ev.inputs_meta["extension"] == "dcm"


def test_audit_event_create_rejects_raw_bytes():
    from datetime import datetime, timezone
    from audit.schemas import AuditEventCreate
    with pytest.raises(Exception):
        AuditEventCreate(
            actor_user_id="u",
            auth_principal="u",
            event_class="auth",
            action_type="t",
            description="t",
            correlation_id="c",
            outcome="success",
            inputs_meta={"unrelated": b"raw"},
            started_at=datetime.now(timezone.utc),
        )


def test_audit_event_create_rejects_oversize_meta():
    from datetime import datetime, timezone
    from audit.schemas import AuditEventCreate
    big = "x" * 5000
    with pytest.raises(Exception):
        AuditEventCreate(
            actor_user_id="u",
            auth_principal="u",
            event_class="auth",
            action_type="t",
            description="t",
            correlation_id="c",
            outcome="success",
            inputs_meta={"blob_len_label": big},
            started_at=datetime.now(timezone.utc),
        )


def test_audit_event_create_rejects_unknown_event_class():
    from datetime import datetime, timezone
    from audit.schemas import AuditEventCreate
    with pytest.raises(Exception):
        AuditEventCreate(
            actor_user_id="u",
            auth_principal="u",
            event_class="bogus",
            action_type="t",
            description="t",
            correlation_id="c",
            outcome="success",
            started_at=datetime.now(timezone.utc),
        )
