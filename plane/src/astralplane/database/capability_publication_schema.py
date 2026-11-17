"""Neutral owner capability-publication rows persist publication state, opaque
host-encrypted metadata, and exact capability revisions. The host owns discovery,
audience policy, IAM, and every A2A transport behavior."""

CAPABILITY_PUBLICATION_SCHEMA_STATEMENTS = (
    """
CREATE TABLE capability_publication (
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    capability_id TEXT NOT NULL CHECK(char_length(capability_id) BETWEEN 1 AND 512),
    audience_id TEXT NOT NULL CHECK(char_length(audience_id) BETWEEN 1 AND 512),
    capability_revision TEXT NOT NULL
        CHECK(char_length(capability_revision) BETWEEN 1 AND 512),
    metadata_ciphertext TEXT NOT NULL
        CHECK(char_length(metadata_ciphertext) BETWEEN 1 AND 65536),
    metadata_digest TEXT NOT NULL CHECK(metadata_digest ~ '^[0-9a-f]{64}$'),
    publication_state TEXT NOT NULL DEFAULT 'published'
        CHECK(publication_state IN ('published', 'withdrawn', 'expired')),
    state_revision BIGINT NOT NULL DEFAULT 1 CHECK(state_revision >= 1),
    published_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    end_reason TEXT CHECK(end_reason IS NULL OR char_length(end_reason) BETWEEN 0 AND 280),
    PRIMARY KEY (owner_id, capability_id, audience_id),
    CONSTRAINT capability_publication_lifetime CHECK (
        isfinite(published_at) AND isfinite(expires_at) AND expires_at > published_at
    ),
    CONSTRAINT capability_publication_state_shape CHECK (
        (publication_state = 'published' AND ended_at IS NULL AND end_reason IS NULL)
        OR (publication_state = 'withdrawn' AND ended_at IS NOT NULL
            AND isfinite(ended_at) AND ended_at >= published_at)
        OR (publication_state = 'expired' AND ended_at IS NOT NULL
            AND isfinite(ended_at) AND ended_at >= expires_at AND end_reason IS NULL)
    )
)
""".strip(),
    """
CREATE INDEX capability_publication_owner_live
ON capability_publication (owner_id, capability_id, audience_id, expires_at)
WHERE publication_state = 'published'
""".strip(),
)