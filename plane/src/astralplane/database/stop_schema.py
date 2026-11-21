"""Neutral owner stop epochs and immutable peer acknowledgments persist through
caller-owned transactions. Stop enforcement, peer identity validation and audit
decisions remain with the embedding host."""

STOP_SCHEMA_STATEMENTS = (
    """
CREATE TABLE owner_stop_epoch (
    owner_id TEXT PRIMARY KEY CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    epoch BIGINT NOT NULL DEFAULT 0 CHECK(epoch >= 0),
    revision BIGINT NOT NULL DEFAULT 0 CHECK(revision >= 0),
    engaged BOOLEAN NOT NULL DEFAULT FALSE,
    engaged_at TIMESTAMPTZ,
    engaged_by TEXT CHECK(char_length(engaged_by) BETWEEN 1 AND 512),
    reason TEXT CHECK(char_length(reason) BETWEEN 0 AND 280),
    updated_at TIMESTAMPTZ,
    CONSTRAINT owner_stop_epoch_state CHECK (
        (epoch = 0 AND revision = 0 AND NOT engaged AND engaged_at IS NULL
            AND engaged_by IS NULL AND reason IS NULL AND updated_at IS NULL)
        OR (epoch >= 1 AND revision >= epoch AND engaged_at IS NOT NULL
            AND engaged_by IS NOT NULL AND reason IS NOT NULL AND updated_at IS NOT NULL
            AND isfinite(engaged_at) AND isfinite(updated_at) AND updated_at >= engaged_at)
    )
)
""".strip(),
    """
CREATE TABLE peer_stop_acknowledgment (
    owner_id TEXT NOT NULL REFERENCES owner_stop_epoch(owner_id),
    epoch BIGINT NOT NULL CHECK(epoch >= 1),
    mesh_id TEXT NOT NULL CHECK(char_length(mesh_id) BETWEEN 1 AND 64),
    peer_id TEXT NOT NULL CHECK(char_length(peer_id) BETWEEN 1 AND 64),
    receipt_digest TEXT NOT NULL CHECK(receipt_digest ~ '^[0-9a-f]{64}$'),
    acknowledged_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (owner_id, epoch, mesh_id, peer_id)
)
""".strip(),
    """
CREATE TABLE owner_stop_operation_epoch (
    owner_id TEXT NOT NULL REFERENCES owner_stop_epoch(owner_id),
    operation_id UUID NOT NULL,
    epoch BIGINT NOT NULL CHECK(epoch >= 0),
    PRIMARY KEY (owner_id, operation_id)
)
""".strip(),
)
