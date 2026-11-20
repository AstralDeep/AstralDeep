"""Defines owner-scoped completion subscriptions and idempotency-keyed wake receipt tables.
The guarded migration registry installs these independent of operation state and wait-state JSON.
"""

COMPLETION_WAKE_SCHEMA_STATEMENTS = (
    """
CREATE TABLE completion_subscription (
    subscription_id UUID PRIMARY KEY,
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    waiter_operation_id UUID NOT NULL,
    waiter_owner_id TEXT NOT NULL CHECK(char_length(waiter_owner_id) BETWEEN 1 AND 512),
    source_operation_id UUID NOT NULL,
    source_owner_id TEXT NOT NULL CHECK(char_length(source_owner_id) BETWEEN 1 AND 512),
    terminal_condition TEXT NOT NULL CHECK(
        terminal_condition IN ('completed', 'failed', 'cancelled', 'any_terminal')),
    source_revision BIGINT NOT NULL CHECK(source_revision BETWEEN 1 AND 9007199254740991),
    current_revision_fence BIGINT NOT NULL
        CHECK(current_revision_fence BETWEEN 1 AND 9007199254740991),
    created_at BIGINT NOT NULL CHECK(created_at >= 0),
    revoked_at BIGINT CHECK(revoked_at IS NULL OR revoked_at >= created_at),
    CONSTRAINT completion_subscription_revision_fence
        CHECK(source_revision <= current_revision_fence),
    CONSTRAINT completion_subscription_no_self_wake CHECK(
        waiter_operation_id <> source_operation_id OR waiter_owner_id <> source_owner_id
    )
)
""".strip(),
    "CREATE INDEX completion_subscription_source ON completion_subscription "
    "(source_owner_id, source_operation_id, subscription_id)",
    "CREATE INDEX completion_subscription_waiter ON completion_subscription "
    "(waiter_owner_id, waiter_operation_id, subscription_id)",
    """
CREATE TABLE wake_receipt (
    receipt_id UUID PRIMARY KEY,
    subscription_id UUID NOT NULL
        REFERENCES completion_subscription(subscription_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    idempotency_key TEXT NOT NULL CHECK(char_length(idempotency_key) BETWEEN 1 AND 512),
    observed_terminal TEXT NOT NULL
        CHECK(observed_terminal IN ('completed', 'failed', 'cancelled')),
    observed_revision BIGINT NOT NULL
        CHECK(observed_revision BETWEEN 1 AND 9007199254740991),
    accepted_at BIGINT NOT NULL CHECK(accepted_at >= 0),
    replay_of UUID REFERENCES wake_receipt(receipt_id) ON DELETE SET NULL,
    CONSTRAINT wake_receipt_idempotent UNIQUE(subscription_id, idempotency_key)
)
""".strip(),
    "CREATE INDEX wake_receipt_subscription ON wake_receipt (subscription_id, receipt_id)",
)
