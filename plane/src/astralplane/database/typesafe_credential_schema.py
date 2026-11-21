"""Per-user TypeSafe credential and data-sharing acknowledgment schema; stores the key
as ciphertext only, with a fingerprint so a late verification of a replaced key can't
mark the current one rejected. No system-wide counterpart exists, by design.
"""

TYPESAFE_CREDENTIAL_SCHEMA_STATEMENTS = (
    """
CREATE TABLE user_typesafe_credential (
    user_id TEXT PRIMARY KEY CHECK(char_length(user_id) BETWEEN 1 AND 512),
    api_key_enc BYTEA NOT NULL,
    key_fingerprint TEXT NOT NULL CHECK(key_fingerprint ~ '^[0-9a-f]{12}$'),
    last_verified_at TIMESTAMPTZ,
    last_verification_outcome TEXT NOT NULL DEFAULT 'unverified'
        CHECK(last_verification_outcome IN ('unverified','valid','rejected','unavailable')),
    last_outcome_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
""".strip(),
    """
CREATE TABLE user_data_sharing_acknowledgment (
    user_id TEXT PRIMARY KEY CHECK(char_length(user_id) BETWEEN 1 AND 512),
    notice_version TEXT NOT NULL CHECK(char_length(notice_version) BETWEEN 1 AND 64),
    acknowledged_at TIMESTAMPTZ NOT NULL,
    first_acknowledged_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT user_data_sharing_acknowledgment_order
        CHECK(acknowledged_at >= first_acknowledged_at)
)
""".strip(),
)
