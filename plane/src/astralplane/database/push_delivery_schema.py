"""Defines owner-scoped opaque push destinations and bounded claimed delivery records.
Token custody, provider selection, encryption, and delivery attempts stay with the host;
the guarded migration registry installs these tables without provider calls.
"""

PUSH_DELIVERY_SCHEMA_STATEMENTS = (
    """
CREATE TABLE notification_device_destination (
    destination_id UUID PRIMARY KEY,
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    destination_key TEXT NOT NULL CHECK(char_length(destination_key) BETWEEN 1 AND 512),
    token_format TEXT,
    token_ciphertext BYTEA,
    token_sha256 CHAR(64),
    token_version BIGINT NOT NULL CHECK(token_version BETWEEN 1 AND 9007199254740991),
    revision BIGINT NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991),
    created_at BIGINT NOT NULL CHECK(created_at BETWEEN 0 AND 9007199254740991),
    updated_at BIGINT NOT NULL CHECK(updated_at BETWEEN 0 AND 9007199254740991),
    revoked_at BIGINT CHECK(revoked_at IS NULL OR revoked_at BETWEEN 0 AND 9007199254740991),
    deleted_at BIGINT CHECK(deleted_at IS NULL OR deleted_at BETWEEN 0 AND 9007199254740991),
    CONSTRAINT notification_device_destination_owner_key
        UNIQUE(owner_id, destination_key),
    CONSTRAINT notification_device_destination_owner_identity
        UNIQUE(destination_id, owner_id),
    CONSTRAINT notification_device_destination_token_shape CHECK (
        (
            deleted_at IS NULL
            AND token_format IS NOT NULL
            AND token_ciphertext IS NOT NULL
            AND token_sha256 IS NOT NULL
        )
        OR (
            deleted_at IS NOT NULL
            AND token_format IS NULL
            AND token_ciphertext IS NULL
            AND token_sha256 IS NULL
        )
    ),
    CONSTRAINT notification_device_destination_token_format CHECK (
        token_format IS NULL OR char_length(token_format) BETWEEN 1 AND 128
    ),
    CONSTRAINT notification_device_destination_token_ciphertext CHECK (
        token_ciphertext IS NULL OR octet_length(token_ciphertext) BETWEEN 1 AND 65536
    ),
    CONSTRAINT notification_device_destination_token_digest CHECK (
        token_sha256 IS NULL OR token_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT notification_device_destination_time_order CHECK (
        updated_at >= created_at
        AND (revoked_at IS NULL OR revoked_at >= created_at)
        AND (deleted_at IS NULL OR deleted_at >= created_at)
        AND (deleted_at IS NULL OR revoked_at IS NULL OR deleted_at >= revoked_at)
        AND (deleted_at IS NULL OR deleted_at = updated_at)
        AND (
            deleted_at IS NOT NULL
            OR updated_at >= COALESCE(revoked_at, created_at)
        )
    )
)
""".strip(),
    """
CREATE INDEX notification_device_destination_owner_live
ON notification_device_destination (owner_id, created_at DESC, destination_id DESC)
WHERE deleted_at IS NULL
""".strip(),
    """
CREATE TABLE notification_push_delivery (
    delivery_id UUID PRIMARY KEY,
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    entry_id UUID NOT NULL,
    destination_id UUID NOT NULL,
    delivery_key TEXT NOT NULL CHECK(char_length(delivery_key) BETWEEN 1 AND 512),
    event_kind TEXT NOT NULL
        CHECK(event_kind IN ('completion','failure','required_action')),
    content_format TEXT NOT NULL CHECK(char_length(content_format) BETWEEN 1 AND 128),
    content_ciphertext BYTEA NOT NULL
        CHECK(octet_length(content_ciphertext) BETWEEN 1 AND 65536),
    content_sha256 CHAR(64) NOT NULL
        CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
    token_version BIGINT NOT NULL CHECK(token_version BETWEEN 1 AND 9007199254740991),
    destination_revision BIGINT NOT NULL
        CHECK(destination_revision BETWEEN 1 AND 9007199254740991),
    status TEXT NOT NULL CHECK(
        status IN (
            'pending','claimed','retry','delivered','dead_letter','superseded'
        )
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 100),
    max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 100),
    available_at BIGINT NOT NULL CHECK(available_at BETWEEN 0 AND 9007199254740991),
    lease_owner TEXT CHECK(
        lease_owner IS NULL OR char_length(lease_owner) BETWEEN 1 AND 512
    ),
    claim_id UUID,
    lease_expires_at BIGINT
        CHECK(lease_expires_at IS NULL OR lease_expires_at BETWEEN 0 AND 9007199254740991),
    claimed_at BIGINT
        CHECK(claimed_at IS NULL OR claimed_at BETWEEN 0 AND 9007199254740991),
    acknowledged_at BIGINT
        CHECK(acknowledged_at IS NULL OR acknowledged_at BETWEEN 0 AND 9007199254740991),
    last_error_code TEXT CHECK(
        last_error_code IS NULL OR (
            char_length(last_error_code) BETWEEN 1 AND 128
            AND last_error_code ~ '^[a-z][a-z0-9_.-]*$'
        )
    ),
    created_at BIGINT NOT NULL CHECK(created_at BETWEEN 0 AND 9007199254740991),
    updated_at BIGINT NOT NULL CHECK(updated_at BETWEEN 0 AND 9007199254740991),
    version BIGINT NOT NULL DEFAULT 0
        CHECK(version BETWEEN 0 AND 9007199254740991),
    CONSTRAINT notification_push_delivery_owner_key UNIQUE(owner_id, delivery_key),
    CONSTRAINT notification_push_delivery_owner_identity UNIQUE(delivery_id, owner_id),
    CONSTRAINT notification_push_delivery_entry
        FOREIGN KEY (entry_id, owner_id)
        REFERENCES notification_inbox_entry(entry_id, owner_id),
    CONSTRAINT notification_push_delivery_destination
        FOREIGN KEY (destination_id, owner_id)
        REFERENCES notification_device_destination(destination_id, owner_id),
    CONSTRAINT notification_push_delivery_attempts
        CHECK(attempt_count <= max_attempts),
    CONSTRAINT notification_push_delivery_status_shape CHECK (
        (
            status = 'claimed'
            AND lease_owner IS NOT NULL
            AND claim_id IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND claimed_at IS NOT NULL
            AND acknowledged_at IS NULL
        )
        OR (
            status <> 'claimed'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
        )
    ),
    CONSTRAINT notification_push_delivery_claim_shape CHECK (
        (attempt_count = 0 AND claim_id IS NULL AND claimed_at IS NULL)
        OR (attempt_count > 0 AND claim_id IS NOT NULL AND claimed_at IS NOT NULL)
    ),
    CONSTRAINT notification_push_delivery_terminal_shape CHECK (
        (
            status = 'delivered'
            AND acknowledged_at IS NOT NULL
            AND last_error_code IS NULL
        )
        OR (status <> 'delivered' AND acknowledged_at IS NULL)
    ),
    CONSTRAINT notification_push_delivery_error_shape CHECK (
        (
            status IN ('pending','claimed','delivered','superseded')
            AND last_error_code IS NULL
        )
        OR (
            status IN ('retry','dead_letter')
            AND last_error_code IS NOT NULL
        )
    ),
    CONSTRAINT notification_push_delivery_time_order CHECK (
        updated_at >= created_at
        AND available_at >= created_at
        AND (claimed_at IS NULL OR claimed_at >= created_at)
        AND (
            lease_expires_at IS NULL
            OR (claimed_at IS NOT NULL AND lease_expires_at > claimed_at)
        )
        AND (
            acknowledged_at IS NULL
            OR (claimed_at IS NOT NULL AND acknowledged_at >= claimed_at)
        )
        AND (status <> 'delivered' OR acknowledged_at = updated_at)
        AND (
            status NOT IN ('retry','dead_letter')
            OR updated_at >= claimed_at
        )
    )
)
""".strip(),
    """
CREATE INDEX notification_push_delivery_owner_claimable
ON notification_push_delivery (owner_id, available_at, delivery_id)
WHERE status IN ('pending','retry','claimed')
""".strip(),
    """
CREATE INDEX notification_push_delivery_owner_destination
ON notification_push_delivery (owner_id, destination_id, status, delivery_id)
""".strip(),
    """
CREATE INDEX notification_push_delivery_owner_entry
ON notification_push_delivery (owner_id, entry_id, delivery_id)
""".strip(),
)