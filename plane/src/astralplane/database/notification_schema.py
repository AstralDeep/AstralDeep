"""Defines owner-scoped notification preferences, opaque inbox records, and durable
delivery identities. Routing, provider selection, IAM, and content rendering stay in
the host, which supplies only bounded policy inputs and ciphertext.
"""

NOTIFICATION_SCHEMA_STATEMENTS = (
    """
CREATE TABLE notification_preference (
    owner_id TEXT PRIMARY KEY CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    in_app_completion BOOLEAN NOT NULL,
    in_app_failure BOOLEAN NOT NULL,
    in_app_required_action BOOLEAN NOT NULL,
    inbox_completion BOOLEAN NOT NULL,
    inbox_failure BOOLEAN NOT NULL,
    inbox_required_action BOOLEAN NOT NULL,
    push_completion BOOLEAN NOT NULL,
    push_failure BOOLEAN NOT NULL,
    push_required_action BOOLEAN NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1
        CHECK(revision BETWEEN 1 AND 9007199254740991),
    updated_at BIGINT NOT NULL CHECK(updated_at BETWEEN 0 AND 9007199254740991)
)
""".strip(),
    """
CREATE TABLE notification_inbox_entry (
    entry_id UUID PRIMARY KEY,
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    event_kind TEXT NOT NULL
        CHECK(event_kind IN ('completion','failure','required_action')),
    source_id TEXT NOT NULL CHECK(char_length(source_id) BETWEEN 1 AND 512),
    deduplication_key TEXT NOT NULL
        CHECK(char_length(deduplication_key) BETWEEN 1 AND 512),
    content_format TEXT NOT NULL CHECK(char_length(content_format) BETWEEN 1 AND 128),
    content_ciphertext BYTEA NOT NULL
        CHECK(octet_length(content_ciphertext) BETWEEN 1 AND 65536),
    content_sha256 CHAR(64) NOT NULL
        CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at BIGINT NOT NULL CHECK(created_at BETWEEN 0 AND 9007199254740991),
    retention_expires_at BIGINT NOT NULL
        CHECK(retention_expires_at BETWEEN 1 AND 9007199254740991),
    acknowledged_at BIGINT
        CHECK(acknowledged_at IS NULL OR acknowledged_at BETWEEN 0 AND 9007199254740991),
    deleted_at BIGINT
        CHECK(deleted_at IS NULL OR deleted_at BETWEEN 0 AND 9007199254740991),
    revision BIGINT NOT NULL DEFAULT 0
        CHECK(revision BETWEEN 0 AND 9007199254740991),
    CONSTRAINT notification_inbox_entry_retention_order
        CHECK(retention_expires_at > created_at),
    CONSTRAINT notification_inbox_entry_acknowledgment_order
        CHECK(acknowledged_at IS NULL OR acknowledged_at >= created_at),
    CONSTRAINT notification_inbox_entry_deletion_order
        CHECK(deleted_at IS NULL OR deleted_at >= created_at),
    CONSTRAINT notification_inbox_entry_deduplication
        UNIQUE(owner_id, deduplication_key),
    CONSTRAINT notification_inbox_entry_owner_identity
        UNIQUE(entry_id, owner_id),
    CONSTRAINT notification_inbox_entry_tombstone_revision
        CHECK(deleted_at IS NULL OR revision >= 1)
)
""".strip(),
    """
CREATE TABLE notification_delivery_identity (
    delivery_id UUID PRIMARY KEY,
    entry_id UUID NOT NULL,
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    delivery_key TEXT NOT NULL CHECK(char_length(delivery_key) BETWEEN 1 AND 512),
    created_at BIGINT NOT NULL CHECK(created_at BETWEEN 0 AND 9007199254740991),
    CONSTRAINT notification_delivery_identity_entry
        FOREIGN KEY (entry_id, owner_id)
        REFERENCES notification_inbox_entry(entry_id, owner_id) ON DELETE CASCADE,
    CONSTRAINT notification_delivery_identity_owner_key
        UNIQUE(owner_id, delivery_key)
)
""".strip(),
    """
CREATE INDEX notification_inbox_entry_owner_live
ON notification_inbox_entry (owner_id, created_at DESC, entry_id DESC)
WHERE deleted_at IS NULL
""".strip(),
    """
CREATE INDEX notification_inbox_entry_owner_retention
ON notification_inbox_entry (owner_id, retention_expires_at, entry_id)
WHERE deleted_at IS NULL
""".strip(),
    """
CREATE INDEX notification_delivery_identity_owner_entry
ON notification_delivery_identity (owner_id, entry_id, delivery_id)
""".strip(),
)