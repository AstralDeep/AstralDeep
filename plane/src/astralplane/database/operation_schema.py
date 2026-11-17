"""Additive one-shot profile and durable original-key receipt columns, executed only by
database/migrations.py's guarded registry.
"""

OPERATION_SCHEMA_STATEMENTS = (
    "ALTER TABLE persistent_assignment ADD COLUMN execution_profile TEXT NOT NULL "
    "DEFAULT 'persistent' CHECK(execution_profile IN ('persistent','one_shot'))",
    "ALTER TABLE persistent_assignment ADD CONSTRAINT persistent_assignment_profile_data "
    "CHECK(execution_profile=COALESCE(data->>'execution_profile','persistent'))",
    "CREATE INDEX persistent_assignment_profile_owner ON "
    "persistent_assignment(owner_user_id,execution_profile,id)",
    "CREATE INDEX persistent_assignment_profile_due ON "
    "persistent_assignment(execution_profile,next_wake_at,id) WHERE lifecycle='active'",
    """CREATE TABLE assignment_operation_receipt (
        owner_id TEXT NOT NULL CHECK(octet_length(owner_id) BETWEEN 1 AND 512),
        origin_namespace TEXT NOT NULL CHECK(octet_length(origin_namespace) BETWEEN 1 AND 64),
        caller_key TEXT NOT NULL CHECK(octet_length(caller_key) BETWEEN 1 AND 256),
        command_digest TEXT NOT NULL CHECK(command_digest ~ '^[0-9a-f]{64}$'),
        credential_id TEXT CHECK(octet_length(credential_id) BETWEEN 1 AND 256),
        assignment_id UUID NOT NULL,
        live_assignment_id UUID,
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY(owner_id,origin_namespace,caller_key),
        CHECK(live_assignment_id IS NULL OR live_assignment_id=assignment_id),
        FOREIGN KEY(live_assignment_id,owner_id)
            REFERENCES persistent_assignment(id,owner_user_id)
            ON DELETE SET NULL(live_assignment_id)
    )""",
    "CREATE INDEX assignment_operation_receipt_live ON "
    "assignment_operation_receipt(live_assignment_id,owner_id) "
    "WHERE live_assignment_id IS NOT NULL",
)
