"""Additive assignment tables, executed only by database/migrations.py's guarded
registry.
"""

ASSIGNMENT_SCHEMA_STATEMENTS = (
    """CREATE TABLE persistent_assignment (
        id UUID PRIMARY KEY, owner_user_id TEXT NOT NULL,
        submission_id UUID NOT NULL, submission_digest TEXT NOT NULL,
        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','paused','stopped','completed')),
        next_wake_at TIMESTAMPTZ, lease_expires_at TIMESTAMPTZ,
        state_version BIGINT NOT NULL CHECK(state_version > 0),
        data JSONB NOT NULL CHECK(jsonb_typeof(data)='object'
            AND octet_length(data::text)<=524288),
        UNIQUE(id,owner_user_id), UNIQUE(owner_user_id,submission_id),
        CHECK(length(owner_user_id) BETWEEN 1 AND 512),
        CHECK(submission_digest ~ '^[0-9a-f]{64}$'),
        CHECK(data->>'lifecycle'=lifecycle),
        CHECK((data->>'state_version')::bigint=state_version)
    )""",
    "CREATE INDEX persistent_assignment_due ON persistent_assignment(next_wake_at,id) "
    "WHERE lifecycle='active'",
    "CREATE INDEX persistent_assignment_owner ON persistent_assignment(owner_user_id,id)",
    """CREATE TABLE persistent_assignment_event (
        id UUID PRIMARY KEY, assignment_id UUID NOT NULL, owner_user_id TEXT NOT NULL,
        source_key TEXT NOT NULL, item_key TEXT NOT NULL, source_revision TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN
            ('pending','processing','completed','irrelevant','failed','reconciliation','superseded')),
        data JSONB NOT NULL CHECK(jsonb_typeof(data)='object'
            AND octet_length(data::text)<=16384),
        FOREIGN KEY(assignment_id,owner_user_id)
            REFERENCES persistent_assignment(id,owner_user_id) ON DELETE CASCADE,
        UNIQUE(assignment_id,source_key,item_key,source_revision),
        CHECK(length(source_key) BETWEEN 1 AND 512),
        CHECK(length(item_key) BETWEEN 1 AND 512),
        CHECK(length(source_revision) BETWEEN 1 AND 512)
    )""",
    "CREATE INDEX persistent_assignment_event_pending ON "
    "persistent_assignment_event(assignment_id,state,id)",
    """CREATE TABLE persistent_assignment_action (
        id UUID PRIMARY KEY, assignment_id UUID NOT NULL, owner_user_id TEXT NOT NULL,
        action_key TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
            ('ready','proposed','approved','reserved','started','succeeded',
             'failed_not_started','failed','declined','invalidated','uncertain')),
        data JSONB NOT NULL CHECK(jsonb_typeof(data)='object'
            AND octet_length(data::text)<=262144),
        FOREIGN KEY(assignment_id,owner_user_id)
            REFERENCES persistent_assignment(id,owner_user_id) ON DELETE CASCADE,
        UNIQUE(assignment_id,action_key), CHECK(length(action_key) BETWEEN 1 AND 512),
        CHECK(data->>'state'=state)
    )""",
    "CREATE INDEX persistent_assignment_action_state ON "
    "persistent_assignment_action(assignment_id,state,id)",
    "CREATE UNIQUE INDEX persistent_assignment_interactive_proposal ON "
    "persistent_assignment_action(owner_user_id,(data->>'interactive_proposal_id')) "
    "WHERE data->>'interactive_proposal_id' IS NOT NULL",
    """CREATE TABLE persistent_assignment_activity (
        id UUID PRIMARY KEY, assignment_id UUID NOT NULL, owner_user_id TEXT NOT NULL,
        activity_key TEXT NOT NULL, sequence BIGINT NOT NULL CHECK(sequence>0),
        notification_state TEXT NOT NULL CHECK(notification_state IN ('none','pending','notified')),
        data JSONB NOT NULL CHECK(jsonb_typeof(data)='object'
            AND octet_length(data::text)<=16384),
        FOREIGN KEY(assignment_id,owner_user_id)
            REFERENCES persistent_assignment(id,owner_user_id) ON DELETE CASCADE,
        UNIQUE(assignment_id,activity_key), UNIQUE(assignment_id,sequence),
        CHECK(length(activity_key) BETWEEN 1 AND 512)
    )""",
)
