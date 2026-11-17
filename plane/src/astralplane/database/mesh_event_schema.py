"""One owner's mesh event store: meshes, per-device feeds with chain heads and fork
evidence, signed events, sealed bodies addressed by hash, roster entries, and
countersigned checkpoints. It also links derived chat and message rows back to the event
that produced them; the guarded migration registry installs all of it.
"""

_HASH = "'^sha256:[0-9a-f]{64}$'"
_BOUND = "9007199254740991"

MESH_EVENT_SCHEMA_STATEMENTS = (
    f"""
CREATE TABLE event_mesh (
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    mesh_id TEXT NOT NULL CHECK(mesh_id ~ {_HASH}),
    recorded_at BIGINT NOT NULL CHECK(recorded_at BETWEEN 0 AND {_BOUND}),
    CONSTRAINT event_mesh_identity PRIMARY KEY(owner_id, mesh_id),
    CONSTRAINT event_mesh_id_unique UNIQUE(mesh_id)
)
""".strip(),
    f"""
CREATE TABLE event_feed (
    owner_id TEXT NOT NULL,
    mesh_id TEXT NOT NULL,
    feed_id TEXT NOT NULL CHECK(char_length(feed_id) BETWEEN 1 AND 128),
    state TEXT NOT NULL CHECK(state IN ('accepted', 'frozen')),
    head_seq BIGINT NOT NULL CHECK(head_seq BETWEEN 0 AND {_BOUND}),
    head_hash TEXT CHECK(head_hash ~ {_HASH}),
    recorded_at BIGINT NOT NULL CHECK(recorded_at BETWEEN 0 AND {_BOUND}),
    frozen_at BIGINT CHECK(frozen_at BETWEEN 0 AND {_BOUND}),
    CONSTRAINT event_feed_identity PRIMARY KEY(owner_id, mesh_id, feed_id),
    CONSTRAINT event_feed_mesh FOREIGN KEY(owner_id, mesh_id)
        REFERENCES event_mesh(owner_id, mesh_id) ON DELETE CASCADE,
    CONSTRAINT event_feed_head_shape CHECK((head_seq = 0) = (head_hash IS NULL)),
    CONSTRAINT event_feed_state_shape CHECK((state = 'frozen') = (frozen_at IS NOT NULL))
)
""".strip(),
    f"""
CREATE TABLE event_entry (
    owner_id TEXT NOT NULL,
    mesh_id TEXT NOT NULL,
    feed_id TEXT NOT NULL,
    seq BIGINT NOT NULL CHECK(seq BETWEEN 1 AND {_BOUND}),
    event_id TEXT NOT NULL CHECK(event_id ~ {_HASH}),
    prev_hash TEXT CHECK(prev_hash ~ {_HASH}),
    clock_ms BIGINT NOT NULL CHECK(clock_ms BETWEEN 0 AND {_BOUND}),
    clock_n BIGINT NOT NULL CHECK(clock_n BETWEEN 0 AND {_BOUND}),
    scope TEXT NOT NULL CHECK(char_length(scope) BETWEEN 1 AND 512),
    event_type TEXT NOT NULL CHECK(char_length(event_type) BETWEEN 1 AND 128),
    labels JSONB NOT NULL,
    key_id TEXT NOT NULL CHECK(char_length(key_id) BETWEEN 1 AND 256),
    body_hash TEXT NOT NULL CHECK(body_hash ~ {_HASH}),
    header_bytes BYTEA NOT NULL CHECK(octet_length(header_bytes) BETWEEN 1 AND 65536),
    header_sealed BYTEA NOT NULL CHECK(octet_length(header_sealed) BETWEEN 1 AND 131072),
    recorded_at BIGINT NOT NULL CHECK(recorded_at BETWEEN 0 AND {_BOUND}),
    CONSTRAINT event_entry_position PRIMARY KEY(owner_id, mesh_id, feed_id, seq),
    CONSTRAINT event_entry_id_unique UNIQUE(owner_id, mesh_id, event_id),
    CONSTRAINT event_entry_feed FOREIGN KEY(owner_id, mesh_id, feed_id)
        REFERENCES event_feed(owner_id, mesh_id, feed_id) ON DELETE CASCADE,
    CONSTRAINT event_entry_chain_shape CHECK((seq = 1) = (prev_hash IS NULL))
)
""".strip(),
    """
CREATE INDEX idx_event_entry_scope_clock
ON event_entry(owner_id, mesh_id, scope, clock_ms, clock_n, feed_id, seq)
""".strip(),
    """
CREATE INDEX idx_event_entry_body
ON event_entry(owner_id, mesh_id, body_hash)
""".strip(),
    f"""
CREATE TABLE event_fork_evidence (
    owner_id TEXT NOT NULL,
    mesh_id TEXT NOT NULL,
    feed_id TEXT NOT NULL,
    seq BIGINT NOT NULL CHECK(seq BETWEEN 1 AND {_BOUND}),
    event_id TEXT NOT NULL CHECK(event_id ~ {_HASH}),
    header_bytes BYTEA NOT NULL CHECK(octet_length(header_bytes) BETWEEN 1 AND 65536),
    header_sealed BYTEA NOT NULL CHECK(octet_length(header_sealed) BETWEEN 1 AND 131072),
    recorded_at BIGINT NOT NULL CHECK(recorded_at BETWEEN 0 AND {_BOUND}),
    CONSTRAINT event_fork_evidence_identity
        PRIMARY KEY(owner_id, mesh_id, feed_id, seq, event_id),
    CONSTRAINT event_fork_evidence_feed FOREIGN KEY(owner_id, mesh_id, feed_id)
        REFERENCES event_feed(owner_id, mesh_id, feed_id) ON DELETE CASCADE
)
""".strip(),
    f"""
CREATE TABLE event_body (
    owner_id TEXT NOT NULL,
    mesh_id TEXT NOT NULL,
    body_hash TEXT NOT NULL CHECK(body_hash ~ {_HASH}),
    sealed BYTEA NOT NULL CHECK(octet_length(sealed) BETWEEN 1 AND 16777216),
    recorded_at BIGINT NOT NULL CHECK(recorded_at BETWEEN 0 AND {_BOUND}),
    CONSTRAINT event_body_identity PRIMARY KEY(owner_id, mesh_id, body_hash),
    CONSTRAINT event_body_mesh FOREIGN KEY(owner_id, mesh_id)
        REFERENCES event_mesh(owner_id, mesh_id) ON DELETE CASCADE
)
""".strip(),
    f"""
CREATE TABLE event_roster_entry (
    owner_id TEXT NOT NULL,
    mesh_id TEXT NOT NULL,
    entry_id TEXT NOT NULL CHECK(entry_id ~ {_HASH}),
    kind TEXT NOT NULL CHECK(char_length(kind) BETWEEN 1 AND 64),
    follows JSONB NOT NULL CHECK(jsonb_typeof(follows) = 'array'),
    signed_bytes BYTEA NOT NULL CHECK(octet_length(signed_bytes) BETWEEN 1 AND 65536),
    recorded_at BIGINT NOT NULL CHECK(recorded_at BETWEEN 0 AND {_BOUND}),
    CONSTRAINT event_roster_entry_identity PRIMARY KEY(owner_id, mesh_id, entry_id),
    CONSTRAINT event_roster_entry_mesh FOREIGN KEY(owner_id, mesh_id)
        REFERENCES event_mesh(owner_id, mesh_id) ON DELETE CASCADE
)
""".strip(),
    f"""
CREATE TABLE event_checkpoint (
    owner_id TEXT NOT NULL,
    mesh_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL CHECK(checkpoint_id ~ {_HASH}),
    feed_id TEXT NOT NULL CHECK(char_length(feed_id) BETWEEN 1 AND 128),
    seq BIGINT NOT NULL CHECK(seq BETWEEN 1 AND {_BOUND}),
    head_hash TEXT NOT NULL CHECK(head_hash ~ {_HASH}),
    signed_bytes BYTEA NOT NULL CHECK(octet_length(signed_bytes) BETWEEN 1 AND 65536),
    recorded_at BIGINT NOT NULL CHECK(recorded_at BETWEEN 0 AND {_BOUND}),
    CONSTRAINT event_checkpoint_identity PRIMARY KEY(owner_id, mesh_id, checkpoint_id),
    CONSTRAINT event_checkpoint_mesh FOREIGN KEY(owner_id, mesh_id)
        REFERENCES event_mesh(owner_id, mesh_id) ON DELETE CASCADE
)
""".strip(),
    """
CREATE INDEX idx_event_checkpoint_feed
ON event_checkpoint(owner_id, mesh_id, feed_id, seq)
""".strip(),
    f"""
CREATE TABLE event_checkpoint_countersignature (
    owner_id TEXT NOT NULL,
    mesh_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    signer_id TEXT NOT NULL CHECK(char_length(signer_id) BETWEEN 1 AND 128),
    signature BYTEA NOT NULL CHECK(octet_length(signature) BETWEEN 1 AND 1024),
    recorded_at BIGINT NOT NULL CHECK(recorded_at BETWEEN 0 AND {_BOUND}),
    CONSTRAINT event_checkpoint_countersignature_identity
        PRIMARY KEY(owner_id, mesh_id, checkpoint_id, signer_id),
    CONSTRAINT event_checkpoint_countersignature_checkpoint
        FOREIGN KEY(owner_id, mesh_id, checkpoint_id)
        REFERENCES event_checkpoint(owner_id, mesh_id, checkpoint_id) ON DELETE CASCADE
)
""".strip(),
    f"""
ALTER TABLE chats ADD COLUMN event_id TEXT
    CONSTRAINT chats_event_id_shape CHECK(event_id ~ {_HASH})
    CONSTRAINT chats_event_id_unique UNIQUE
""".strip(),
    f"""
ALTER TABLE messages ADD COLUMN event_id TEXT
    CONSTRAINT messages_event_id_shape CHECK(event_id ~ {_HASH})
    CONSTRAINT messages_event_id_unique UNIQUE
""".strip(),
)
