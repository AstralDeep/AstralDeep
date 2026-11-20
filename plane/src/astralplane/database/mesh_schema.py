"""Neutral personal-mesh membership, enrollment, and revocation schema; stores only
public key material and opaque challenge/invitation digests, while possession proof,
IAM, and admission policy stay with the host.
"""

MESH_SCHEMA_STATEMENTS = (
    """
CREATE TABLE mesh_record (
    mesh_id TEXT PRIMARY KEY CHECK(char_length(mesh_id) BETWEEN 1 AND 128),
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    display_name TEXT NOT NULL CHECK(char_length(display_name) BETWEEN 1 AND 256),
    membership_epoch BIGINT NOT NULL DEFAULT 0 CHECK(membership_epoch >= 0),
    revocation_epoch BIGINT NOT NULL DEFAULT 0 CHECK(revocation_epoch >= 0),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
""".strip(),
    """
CREATE TABLE mesh_member (
    mesh_id TEXT NOT NULL REFERENCES mesh_record(mesh_id),
    member_id TEXT NOT NULL CHECK(char_length(member_id) BETWEEN 1 AND 128),
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    member_kind TEXT NOT NULL CHECK(member_kind IN ('device','agent','companion')),
    display_label TEXT CHECK(char_length(display_label) BETWEEN 1 AND 256),
    membership_epoch BIGINT NOT NULL CHECK(membership_epoch >= 1),
    member_status TEXT NOT NULL DEFAULT 'active'
        CHECK(member_status IN ('active','revoked','retired')),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version >= 1),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mesh_member_identity PRIMARY KEY (mesh_id, member_id)
)
""".strip(),
    """
CREATE TABLE mesh_public_identity (
    identity_id TEXT PRIMARY KEY CHECK(char_length(identity_id) BETWEEN 1 AND 128),
    mesh_id TEXT NOT NULL REFERENCES mesh_record(mesh_id),
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    member_id TEXT NOT NULL CHECK(char_length(member_id) BETWEEN 1 AND 128),
    algorithm TEXT NOT NULL CHECK(char_length(algorithm) BETWEEN 1 AND 64),
    public_key TEXT NOT NULL CHECK(char_length(public_key) BETWEEN 1 AND 8192),
    key_fingerprint TEXT NOT NULL CHECK(key_fingerprint ~ '^[0-9a-f]{64}$'),
    identity_state TEXT NOT NULL DEFAULT 'active'
        CHECK(identity_state IN ('active','rotated','revoked')),
    activated_epoch BIGINT CHECK(activated_epoch IS NULL OR activated_epoch >= 1),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mesh_public_identity_member FOREIGN KEY (mesh_id, member_id)
        REFERENCES mesh_member(mesh_id, member_id)
)
""".strip(),
    """
CREATE TABLE mesh_enrollment_challenge (
    challenge_id TEXT PRIMARY KEY CHECK(char_length(challenge_id) BETWEEN 1 AND 128),
    mesh_id TEXT NOT NULL REFERENCES mesh_record(mesh_id),
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    member_id TEXT NOT NULL CHECK(char_length(member_id) BETWEEN 1 AND 128),
    challenge_digest TEXT NOT NULL CHECK(challenge_digest ~ '^[0-9a-f]{64}$'),
    challenge_state TEXT NOT NULL DEFAULT 'pending'
        CHECK(challenge_state IN ('pending','proven','expired','cancelled')),
    issued_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL,
    proven_at BIGINT,
    record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version >= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mesh_enrollment_challenge_order CHECK(expires_at > issued_at)
)
""".strip(),
    """
CREATE TABLE mesh_enrollment_invitation (
    invitation_id TEXT PRIMARY KEY CHECK(char_length(invitation_id) BETWEEN 1 AND 128),
    mesh_id TEXT NOT NULL REFERENCES mesh_record(mesh_id),
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    member_kind TEXT NOT NULL CHECK(member_kind IN ('device','agent','companion')),
    member_label TEXT CHECK(char_length(member_label) BETWEEN 1 AND 256),
    invitation_digest TEXT NOT NULL CHECK(invitation_digest ~ '^[0-9a-f]{64}$'),
    invitation_state TEXT NOT NULL DEFAULT 'pending'
        CHECK(invitation_state IN ('pending','consumed','confirmed','expired','revoked')),
    issued_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL,
    consumed_at BIGINT,
    confirmed_at BIGINT,
    record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version >= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mesh_enrollment_invitation_order CHECK(expires_at > issued_at)
)
""".strip(),
    """
CREATE TABLE mesh_member_revocation (
    revocation_id TEXT PRIMARY KEY CHECK(char_length(revocation_id) BETWEEN 1 AND 128),
    mesh_id TEXT NOT NULL REFERENCES mesh_record(mesh_id),
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    member_id TEXT NOT NULL CHECK(char_length(member_id) BETWEEN 1 AND 128),
    revocation_epoch BIGINT NOT NULL CHECK(revocation_epoch >= 1),
    reason TEXT CHECK(char_length(reason) BETWEEN 1 AND 512),
    revoked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mesh_member_revocation_epoch UNIQUE (mesh_id, revocation_epoch)
)
""".strip(),
    """
CREATE INDEX mesh_member_owner_idx ON mesh_member (owner_id, mesh_id, membership_epoch)
""".strip(),
    """
CREATE INDEX mesh_public_identity_member_idx
    ON mesh_public_identity (owner_id, mesh_id, member_id)
""".strip(),
    """
CREATE INDEX mesh_enrollment_challenge_mesh_idx
    ON mesh_enrollment_challenge (mesh_id, challenge_state, expires_at)
""".strip(),
    """
CREATE INDEX mesh_enrollment_invitation_mesh_idx
    ON mesh_enrollment_invitation (mesh_id, invitation_state, expires_at)
""".strip(),
    """
CREATE INDEX mesh_member_revocation_mesh_idx
    ON mesh_member_revocation (mesh_id, revocation_epoch)
""".strip(),
)
