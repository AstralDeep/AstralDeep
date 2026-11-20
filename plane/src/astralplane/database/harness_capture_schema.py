"""Immutable admission captures share operation retention while assignment JSON preserves
its initial capture.
The guarded migration registry installs these objects before selected work can be admitted.
"""

HARNESS_CAPTURE_SCHEMA_STATEMENTS = (
    """
CREATE TABLE operation_harness_capture (
    operation_id UUID PRIMARY KEY REFERENCES operation_record(operation_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 1 AND 512),
    capture JSONB NOT NULL CHECK(jsonb_typeof(capture)='object'
        AND octet_length(capture::text)<=4096 AND capture->>'owner_id'=owner_id),
    created_at TIMESTAMPTZ NOT NULL CHECK(isfinite(created_at))
)
""".strip(),
    "CREATE INDEX operation_harness_capture_owner "
    "ON operation_harness_capture(owner_id,operation_id)",
    """
CREATE TRIGGER operation_harness_capture_immutable
BEFORE UPDATE OR DELETE ON operation_harness_capture
FOR EACH ROW EXECUTE FUNCTION reject_operation_harness_binding_mutation()
""".strip(),
    """
CREATE FUNCTION reject_assignment_harness_capture_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog, pg_temp
AS $assignment_harness_capture$
BEGIN
    IF (OLD.data->'harness_capture') IS DISTINCT FROM (NEW.data->'harness_capture') THEN
        RAISE EXCEPTION 'assignment harness captures are immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END
$assignment_harness_capture$
""".strip(),
    """
CREATE TRIGGER assignment_harness_capture_immutable
BEFORE UPDATE ON persistent_assignment
FOR EACH ROW EXECUTE FUNCTION reject_assignment_harness_capture_mutation()
""".strip(),
)
