"""Additive optional scheduled-job policy and occurrence-to-assignment binding tables; a
definition without a policy row keeps its pre-existing recurrence semantics, and
charges are never decremented so history survives.
"""

SCHEDULER_POLICY_SCHEMA_STATEMENTS = (
    """
CREATE TABLE scheduled_job_policy (
    job_id UUID PRIMARY KEY REFERENCES scheduled_job(id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    version BIGINT NOT NULL CHECK(version BETWEEN 1 AND 9007199254740991),
    max_runs INTEGER CHECK(max_runs IS NULL OR max_runs BETWEEN 1 AND 1000000),
    admitted_runs INTEGER NOT NULL DEFAULT 0 CHECK(admitted_runs >= 0),
    per_episode_limits JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK(jsonb_typeof(per_episode_limits)='object'
              AND octet_length(per_episode_limits::text)<=4096),
    max_outstanding_episodes INTEGER NOT NULL DEFAULT 1
        CHECK(max_outstanding_episodes BETWEEN 1 AND 64),
    monitor_changes BOOLEAN NOT NULL DEFAULT FALSE,
    definition_revision BIGINT NOT NULL DEFAULT 1
        CHECK(definition_revision BETWEEN 1 AND 9007199254740991),
    terminal_stop BOOLEAN NOT NULL DEFAULT FALSE,
    last_assignment_id UUID,
    updated_at BIGINT NOT NULL CHECK(updated_at >= 0),
    CONSTRAINT scheduled_job_policy_allowance
        CHECK(max_runs IS NULL OR admitted_runs <= max_runs)
)
""".strip(),
    "CREATE INDEX scheduled_job_policy_owner ON scheduled_job_policy (owner_id, job_id)",
    """
CREATE TABLE scheduled_occurrence_assignment (
    occurrence_id UUID NOT NULL
        REFERENCES scheduled_occurrence(occurrence_id) ON DELETE RESTRICT,
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    job_id UUID NOT NULL REFERENCES scheduled_job(id) ON DELETE RESTRICT,
    assignment_id UUID NOT NULL,
    spend INTEGER NOT NULL CHECK(spend BETWEEN 0 AND 1000000),
    admitted_at BIGINT NOT NULL CHECK(admitted_at >= 0),
    CONSTRAINT scheduled_occurrence_assignment_unique UNIQUE(occurrence_id, owner_id),
    FOREIGN KEY(assignment_id, owner_id)
        REFERENCES persistent_assignment(id, owner_user_id) ON DELETE CASCADE
)
""".strip(),
    "CREATE INDEX scheduled_occurrence_assignment_job ON scheduled_occurrence_assignment "
    "(job_id, owner_id, assignment_id)",
)
