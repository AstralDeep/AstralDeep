"""Owner-scoped approval verification credential and consumed-challenge schema; stores
only public key material and opaque challenge digests, while biometric verification,
possession proof, and approval policy stay with the host. The guarded migration registry
installs these objects before any credential can be enrolled or consumed.
"""

VERIFICATION_CREDENTIAL_SCHEMA_STATEMENTS = (
    """
CREATE TABLE verification_credential (
    credential_id TEXT PRIMARY KEY CHECK(char_length(credential_id) BETWEEN 1 AND 128),
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    device_id TEXT NOT NULL CHECK(char_length(device_id) BETWEEN 1 AND 128),
    algorithm TEXT NOT NULL CHECK(char_length(algorithm) BETWEEN 1 AND 64),
    key_format TEXT NOT NULL CHECK(key_format IN ('sec1','spki')),
    public_key TEXT NOT NULL CHECK(char_length(public_key) BETWEEN 1 AND 8192),
    credential_state TEXT NOT NULL DEFAULT 'active'
        CHECK(credential_state IN ('active','revoked')),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version >= 1),
    enrolled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT verification_credential_state_shape CHECK (
        (credential_state = 'active' AND revoked_at IS NULL)
        OR (credential_state = 'revoked' AND revoked_at IS NOT NULL)
    )
)
""".strip(),
    """
CREATE TABLE verification_challenge_consumption (
    consumption_id TEXT PRIMARY KEY CHECK(char_length(consumption_id) BETWEEN 1 AND 128),
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    challenge_digest TEXT NOT NULL CHECK(challenge_digest ~ '^[0-9a-f]{64}$'),
    consumed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT verification_challenge_consumption_digest
        UNIQUE (owner_id, challenge_digest)
)
""".strip(),
    """
CREATE INDEX verification_credential_owner_idx
    ON verification_credential (owner_id, credential_state, credential_id)
""".strip(),
    """
CREATE INDEX verification_challenge_consumption_owner_idx
    ON verification_challenge_consumption (owner_id, consumed_at)
""".strip(),
)
