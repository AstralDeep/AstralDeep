"""Owner-scoped Mac computer-use bookkeeping: session records, task grants, host effect
intents, and approval-continuation fences store only digests and bounded identifiers.
The guarded migration registry installs these objects before any Deep computer use.
"""

COMPUTER_USE_SCHEMA_STATEMENTS = (
    """
CREATE TABLE computer_session (
    session_id UUID PRIMARY KEY,
    owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 1 AND 512),
    host_id TEXT NOT NULL CHECK(length(host_id) BETWEEN 1 AND 256),
    host_incarnation TEXT NOT NULL CHECK(length(host_incarnation) BETWEEN 1 AND 128),
    state TEXT NOT NULL CHECK(state IN ('active', 'paused', 'ended')),
    pause_reason TEXT CHECK(pause_reason IS NULL OR length(pause_reason) BETWEEN 1 AND 64),
    revision BIGINT NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991),
    created_at BIGINT NOT NULL CHECK(created_at >= 0),
    updated_at BIGINT NOT NULL CHECK(updated_at >= 0),
    ended_at BIGINT CHECK(ended_at IS NULL OR ended_at >= 0),
    CONSTRAINT computer_session_lifecycle CHECK(
        (state = 'ended') = (ended_at IS NOT NULL)
        AND (state = 'paused') = (pause_reason IS NOT NULL)
        AND created_at <= updated_at
        AND (ended_at IS NULL OR ended_at >= created_at)
    )
)
""".strip(),
    """
CREATE INDEX computer_session_owner
ON computer_session(owner_id, state, session_id)
""".strip(),
    """
CREATE TABLE computer_task_grant (
    grant_id UUID PRIMARY KEY,
    owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 1 AND 512),
    binding_digest TEXT NOT NULL CHECK(binding_digest ~ '^[0-9a-f]{64}$'),
    expires_at BIGINT NOT NULL CHECK(expires_at >= 0),
    hard_cap BIGINT NOT NULL CHECK(hard_cap BETWEEN 1 AND 9007199254740991),
    use_count BIGINT NOT NULL DEFAULT 0 CHECK(use_count BETWEEN 0 AND 9007199254740991),
    revision BIGINT NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991),
    created_at BIGINT NOT NULL CHECK(created_at >= 0),
    revoked_at BIGINT CHECK(revoked_at IS NULL OR revoked_at >= 0),
    CONSTRAINT computer_task_grant_lifecycle CHECK(
        created_at <= expires_at
        AND use_count <= hard_cap
        AND (revoked_at IS NULL OR revoked_at >= created_at)
    )
)
""".strip(),
    """
CREATE INDEX computer_task_grant_owner
ON computer_task_grant(owner_id, binding_digest, grant_id)
""".strip(),
    """
CREATE TABLE computer_host_effect_intent (
    operation_id UUID PRIMARY KEY,
    owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 1 AND 512),
    verb TEXT NOT NULL CHECK(verb ~ '^[a-z][a-z0-9_]{0,63}$'),
    argument_digest TEXT NOT NULL CHECK(argument_digest ~ '^[0-9a-f]{64}$'),
    state TEXT NOT NULL CHECK(state IN ('pending', 'committed', 'unknown', 'acknowledged')),
    created_at BIGINT NOT NULL CHECK(created_at >= 0),
    updated_at BIGINT NOT NULL CHECK(updated_at >= 0),
    resolved_at BIGINT CHECK(resolved_at IS NULL OR resolved_at >= 0),
    acknowledged_at BIGINT CHECK(acknowledged_at IS NULL OR acknowledged_at >= 0),
    CONSTRAINT computer_host_effect_intent_lifecycle CHECK(
        (state = 'pending') = (resolved_at IS NULL)
        AND (state = 'acknowledged') = (acknowledged_at IS NOT NULL)
        AND created_at <= updated_at
        AND (resolved_at IS NULL OR resolved_at >= created_at)
        AND (acknowledged_at IS NULL
             OR (resolved_at IS NOT NULL AND acknowledged_at >= resolved_at))
    )
)
""".strip(),
    """
CREATE INDEX computer_host_effect_intent_owner
ON computer_host_effect_intent(owner_id, state, operation_id)
""".strip(),
    """
CREATE TABLE computer_approval_fence (
    fence_id UUID PRIMARY KEY,
    owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 1 AND 512),
    continuation_digest TEXT NOT NULL CHECK(continuation_digest ~ '^[0-9a-f]{64}$'),
    state TEXT NOT NULL CHECK(state IN ('open', 'consumed', 'released')),
    created_at BIGINT NOT NULL CHECK(created_at >= 0),
    updated_at BIGINT NOT NULL CHECK(updated_at >= 0),
    closed_at BIGINT CHECK(closed_at IS NULL OR closed_at >= 0),
    CONSTRAINT computer_approval_fence_lifecycle CHECK(
        (state = 'open') = (closed_at IS NULL)
        AND created_at <= updated_at
        AND (closed_at IS NULL OR closed_at >= created_at)
    )
)
""".strip(),
    """
CREATE INDEX computer_approval_fence_owner
ON computer_approval_fence(owner_id, state, fence_id)
""".strip(),
)
