"""Event-derived run and learning records for one owner's mesh: an append-only history of
executor leases and handoffs, an append-only history of goal state transitions, and an
append-only learning ledger of proposals, adoptions and reversals with the event ids that
justify each. The guarded migration registry installs all of it.
"""

_HASH = "'^sha256:[0-9a-f]{64}$'"
_BOUND = "9007199254740991"

RUN_LEDGER_SCHEMA_STATEMENTS = (
    f"""
CREATE TABLE run_lease_entry (
    owner_id TEXT NOT NULL,
    mesh_id TEXT NOT NULL,
    run_id TEXT NOT NULL CHECK(char_length(run_id) BETWEEN 1 AND 256),
    epoch BIGINT NOT NULL CHECK(epoch BETWEEN 1 AND {_BOUND}),
    kind TEXT NOT NULL CHECK(kind IN ('take', 'transfer', 'takeover', 'state')),
    executor_feed_id TEXT NOT NULL CHECK(char_length(executor_feed_id) BETWEEN 1 AND 128),
    state TEXT NOT NULL CHECK(state IN ('running', 'paused', 'completed', 'failed', 'cancelled')),
    event_id TEXT NOT NULL CHECK(event_id ~ {_HASH}),
    recorded_at BIGINT NOT NULL CHECK(recorded_at BETWEEN 0 AND {_BOUND}),
    CONSTRAINT run_lease_entry_identity PRIMARY KEY(owner_id, mesh_id, run_id, epoch),
    CONSTRAINT run_lease_entry_event_unique UNIQUE(owner_id, mesh_id, event_id),
    CONSTRAINT run_lease_entry_mesh FOREIGN KEY(owner_id, mesh_id)
        REFERENCES event_mesh(owner_id, mesh_id) ON DELETE CASCADE,
    CONSTRAINT run_lease_entry_first_take CHECK((epoch = 1) = (kind = 'take'))
)
""".strip(),
    f"""
CREATE TABLE run_goal_transition (
    owner_id TEXT NOT NULL,
    mesh_id TEXT NOT NULL,
    run_id TEXT NOT NULL CHECK(char_length(run_id) BETWEEN 1 AND 256),
    revision BIGINT NOT NULL CHECK(revision BETWEEN 1 AND {_BOUND}),
    goal TEXT NOT NULL CHECK(char_length(goal) BETWEEN 1 AND 8192),
    state TEXT NOT NULL CHECK(state IN ('active', 'paused', 'stalled', 'achieved', 'abandoned')),
    rounds BIGINT NOT NULL CHECK(rounds BETWEEN 0 AND {_BOUND}),
    stalls BIGINT NOT NULL CHECK(stalls BETWEEN 0 AND {_BOUND}),
    event_id TEXT NOT NULL CHECK(event_id ~ {_HASH}),
    recorded_at BIGINT NOT NULL CHECK(recorded_at BETWEEN 0 AND {_BOUND}),
    CONSTRAINT run_goal_transition_identity PRIMARY KEY(owner_id, mesh_id, run_id, revision),
    CONSTRAINT run_goal_transition_event_unique UNIQUE(owner_id, mesh_id, event_id),
    CONSTRAINT run_goal_transition_mesh FOREIGN KEY(owner_id, mesh_id)
        REFERENCES event_mesh(owner_id, mesh_id) ON DELETE CASCADE,
    CONSTRAINT run_goal_transition_first_state CHECK(revision > 1 OR state = 'active')
)
""".strip(),
    f"""
CREATE TABLE learning_ledger_entry (
    owner_id TEXT NOT NULL,
    mesh_id TEXT NOT NULL,
    event_id TEXT NOT NULL CHECK(event_id ~ {_HASH}),
    kind TEXT NOT NULL CHECK(kind IN ('proposal', 'adoption', 'reversal')),
    proposal_id TEXT NOT NULL CHECK(proposal_id ~ {_HASH}),
    item_kind TEXT NOT NULL CHECK(item_kind IN ('memory', 'skill', 'instruction')),
    content_hash TEXT NOT NULL CHECK(content_hash ~ {_HASH}),
    scope TEXT NOT NULL CHECK(char_length(scope) BETWEEN 1 AND 512),
    review TEXT CHECK(review IN ('automatic', 'owner')),
    evidence JSONB NOT NULL CHECK(
        jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) >= 1
    ),
    recorded_at BIGINT NOT NULL CHECK(recorded_at BETWEEN 0 AND {_BOUND}),
    CONSTRAINT learning_ledger_entry_identity PRIMARY KEY(owner_id, mesh_id, event_id),
    CONSTRAINT learning_ledger_entry_mesh FOREIGN KEY(owner_id, mesh_id)
        REFERENCES event_mesh(owner_id, mesh_id) ON DELETE CASCADE,
    CONSTRAINT learning_ledger_entry_proposal_identity
        CHECK((kind = 'proposal') = (proposal_id = event_id)),
    CONSTRAINT learning_ledger_entry_review_shape
        CHECK((kind = 'adoption') = (review IS NOT NULL))
)
""".strip(),
    """
CREATE UNIQUE INDEX uq_learning_ledger_entry_adoption
ON learning_ledger_entry(owner_id, mesh_id, proposal_id) WHERE kind = 'adoption'
""".strip(),
    """
CREATE UNIQUE INDEX uq_learning_ledger_entry_reversal
ON learning_ledger_entry(owner_id, mesh_id, proposal_id) WHERE kind = 'reversal'
""".strip(),
    """
CREATE INDEX idx_learning_ledger_entry_scope
ON learning_ledger_entry(owner_id, mesh_id, scope, recorded_at, event_id)
""".strip(),
)
