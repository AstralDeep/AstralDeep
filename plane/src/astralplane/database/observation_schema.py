"""Additive owner- and conversation-scoped tool observation heads and bounded opaque
content pages, executed only by database/migrations.py's guarded registry. Observation
content is host ciphertext, expiry is absolute, and recall authorization, packing, and
provider policy remain outside this schema slice.
"""

OBSERVATION_SCHEMA_STATEMENTS = (
    """
CREATE TABLE tool_observation (
    owner_id TEXT NOT NULL CHECK(
        char_length(owner_id) BETWEEN 1 AND 512 AND octet_length(owner_id)<=2048
    ),
    conversation_id TEXT NOT NULL CHECK(
        char_length(conversation_id) BETWEEN 1 AND 512
        AND octet_length(conversation_id)<=2048
    ),
    observation_id UUID NOT NULL,
    capture_seq BIGINT NOT NULL CHECK(capture_seq BETWEEN 1 AND 9007199254740991),
    operation_id TEXT NOT NULL CHECK(
        char_length(operation_id) BETWEEN 1 AND 512
        AND octet_length(operation_id)<=2048
    ),
    seal TEXT CHECK(
        seal IS NULL OR (
            char_length(seal) BETWEEN 1 AND 512 AND octet_length(seal)<=2048
        )
    ),
    source_refs JSONB NOT NULL CHECK(
        jsonb_typeof(source_refs)='array'
        AND jsonb_array_length(source_refs)<=8
        AND octet_length(source_refs::text)<=16416
    ),
    page_count BIGINT NOT NULL CHECK(page_count BETWEEN 0 AND 512),
    observation_octets BIGINT NOT NULL CHECK(
        observation_octets BETWEEN 0 AND 8388608
    ),
    content_sha256 TEXT CHECK(
        content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    capture_digest TEXT NOT NULL CHECK(capture_digest ~ '^[0-9a-f]{64}$'),
    created_at BIGINT NOT NULL CHECK(created_at BETWEEN 0 AND 9007199254740991),
    expires_at BIGINT NOT NULL CHECK(expires_at BETWEEN 1 AND 9007199254740991),
    purged_at BIGINT CHECK(
        purged_at IS NULL OR (
            purged_at>=created_at AND purged_at<=9007199254740991
        )
    ),
    purged_reason TEXT CHECK(
        purged_reason IS NULL OR purged_reason IN ('deleted','revoked','expired')
    ),
    PRIMARY KEY(owner_id,conversation_id,observation_id),
    UNIQUE(owner_id,conversation_id,capture_seq),
    CONSTRAINT tool_observation_retention_window CHECK(
        purged_at IS NOT NULL OR (
            expires_at>created_at
            AND expires_at<=created_at+86400
        )
    ),
    CONSTRAINT tool_observation_lifecycle_shape CHECK(
        (
            purged_at IS NULL
            AND page_count BETWEEN 1 AND 512
            AND observation_octets BETWEEN 1 AND 8388608
            AND content_sha256 IS NOT NULL
        )
        OR (
            purged_at IS NOT NULL
            AND purged_reason IS NOT NULL
            AND page_count=0
            AND observation_octets=0
            AND content_sha256 IS NULL
            AND seal IS NULL
            AND source_refs='[]'::jsonb
        )
    )
)
""".strip(),
    """
CREATE UNIQUE INDEX tool_observation_recall
ON tool_observation(owner_id,conversation_id,capture_seq)
WHERE purged_at IS NULL
""".strip(),
    """
CREATE INDEX tool_observation_expiry
ON tool_observation(owner_id,conversation_id,expires_at)
WHERE purged_at IS NULL
""".strip(),
    """
CREATE FUNCTION reject_tool_observation_delete() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog AS $tool_observation_delete$
BEGIN
    RAISE EXCEPTION 'tool observation tombstones are durable' USING ERRCODE='23514';
END
$tool_observation_delete$
""".strip(),
    """
CREATE TRIGGER tool_observation_delete
BEFORE DELETE ON tool_observation
FOR EACH ROW EXECUTE FUNCTION reject_tool_observation_delete()
""".strip(),
    """
CREATE TABLE tool_observation_page (
    owner_id TEXT NOT NULL CHECK(
        char_length(owner_id) BETWEEN 1 AND 512 AND octet_length(owner_id)<=2048
    ),
    conversation_id TEXT NOT NULL CHECK(
        char_length(conversation_id) BETWEEN 1 AND 512
        AND octet_length(conversation_id)<=2048
    ),
    observation_id UUID NOT NULL,
    page_no BIGINT NOT NULL CHECK(page_no BETWEEN 1 AND 512),
    content BYTEA NOT NULL CHECK(octet_length(content) BETWEEN 1 AND 16384),
    content_sha256 TEXT NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY(owner_id,conversation_id,observation_id,page_no),
    FOREIGN KEY(owner_id,conversation_id,observation_id)
        REFERENCES tool_observation(owner_id,conversation_id,observation_id)
        ON DELETE RESTRICT
)
""".strip(),
    """
CREATE FUNCTION enforce_tool_observation_lifecycle() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog AS $tool_observation_lifecycle$
BEGIN
    IF OLD.purged_at IS NOT NULL THEN
        RAISE EXCEPTION 'tool observation tombstone is terminal' USING ERRCODE='23514';
    END IF;
    IF NEW.owner_id IS DISTINCT FROM OLD.owner_id
       OR NEW.conversation_id IS DISTINCT FROM OLD.conversation_id
       OR NEW.observation_id IS DISTINCT FROM OLD.observation_id
       OR NEW.capture_seq IS DISTINCT FROM OLD.capture_seq
       OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
       OR NEW.capture_digest IS DISTINCT FROM OLD.capture_digest
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
    THEN
        RAISE EXCEPTION 'tool observation identity is immutable' USING ERRCODE='23514';
    END IF;
    IF NEW.purged_at IS NULL
       OR NEW.purged_reason IS NULL
       OR NEW.page_count<>0
       OR NEW.observation_octets<>0
       OR NEW.content_sha256 IS NOT NULL
       OR NEW.seal IS NOT NULL
       OR NEW.source_refs<>'[]'::jsonb
    THEN
        RAISE EXCEPTION 'tool observation content is immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END
$tool_observation_lifecycle$
""".strip(),
    """
CREATE TRIGGER tool_observation_lifecycle
BEFORE UPDATE ON tool_observation
FOR EACH ROW EXECUTE FUNCTION enforce_tool_observation_lifecycle()
""".strip(),
    """
CREATE FUNCTION reject_tool_observation_page_update() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog AS $tool_observation_page$
BEGIN
    RAISE EXCEPTION 'tool observation pages are immutable' USING ERRCODE='23514';
END
$tool_observation_page$
""".strip(),
    """
CREATE TRIGGER tool_observation_page_immutable
BEFORE UPDATE ON tool_observation_page
FOR EACH ROW EXECUTE FUNCTION reject_tool_observation_page_update()
""".strip(),
    """
CREATE FUNCTION enforce_tool_observation_page_purge() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog AS $tool_observation_page_purge$
DECLARE
    purged boolean;
BEGIN
    EXECUTE 'SELECT EXISTS (SELECT 1 FROM '
        || quote_ident(TG_TABLE_SCHEMA)
        || '.tool_observation WHERE owner_id = $1 AND conversation_id = $2'
        || ' AND observation_id = $3 AND purged_at IS NOT NULL)'
        INTO purged
        USING OLD.owner_id, OLD.conversation_id, OLD.observation_id;
    IF NOT purged THEN
        RAISE EXCEPTION 'tool observation pages are purge-only' USING ERRCODE='23514';
    END IF;
    RETURN OLD;
END
$tool_observation_page_purge$
""".strip(),
    """
CREATE TRIGGER tool_observation_page_purge
BEFORE DELETE ON tool_observation_page
FOR EACH ROW EXECUTE FUNCTION enforce_tool_observation_page_purge()
""".strip(),
)
