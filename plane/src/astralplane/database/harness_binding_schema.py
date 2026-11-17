"""Additive immutable operation-to-harness-profile bindings, executed only by
database/migrations.py's guarded registry. Runner selection, provider policy, and profile
availability remain with the host.
"""

HARNESS_BINDING_SCHEMA_STATEMENTS = (
    """
CREATE TABLE operation_harness_binding (
    operation_id UUID PRIMARY KEY
        REFERENCES operation_record(operation_id) ON DELETE CASCADE,
    profile TEXT NOT NULL
        CHECK(char_length(profile) BETWEEN 1 AND 64
              AND profile ~ '^[a-z][a-z0-9_]{0,63}$'),
    profile_version TEXT NOT NULL
        CHECK(char_length(profile_version) BETWEEN 5 AND 64
              AND profile_version ~ '^[0-9]+\\.[0-9]+\\.[0-9]+$'),
    artifact_digest TEXT NOT NULL
        CHECK(artifact_digest ~ '^[0-9a-f]{64}$'),
    execution_generation BIGINT NOT NULL CHECK(execution_generation > 0),
    bound_at TIMESTAMPTZ NOT NULL CHECK(isfinite(bound_at))
)
""".strip(),
    """
CREATE INDEX operation_harness_binding_profile
ON operation_harness_binding(profile, profile_version, artifact_digest, operation_id)
""".strip(),
    """
CREATE FUNCTION reject_operation_harness_binding_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog, pg_temp
AS $operation_harness_binding$
DECLARE
    operation_exists BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        EXECUTE format(
            'SELECT EXISTS (SELECT 1 FROM %I.operation_record WHERE operation_id=$1)',
            TG_TABLE_SCHEMA
        ) INTO operation_exists USING OLD.operation_id;
        IF NOT operation_exists THEN
            RETURN OLD;
        END IF;
    END IF;
    RAISE EXCEPTION 'operation harness bindings are immutable'
        USING ERRCODE='23514';
END
$operation_harness_binding$
""".strip(),
    """
CREATE TRIGGER operation_harness_binding_immutable
BEFORE UPDATE OR DELETE ON operation_harness_binding
FOR EACH ROW EXECUTE FUNCTION reject_operation_harness_binding_mutation()
""".strip(),
)