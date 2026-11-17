"""Additive owner-scoped immutable Atlas page revisions and fenced edit command support,
executed only by database/migrations.py's guarded registry. Revision bytes stay opaque host
ciphertext, existing live heads are backfilled exactly, and tombstones remain terminal.
"""

ATLAS_REVISION_SCHEMA_STATEMENTS = (
    """
CREATE TABLE atlas_page_revision (
    owner_id TEXT NOT NULL CHECK(
        char_length(owner_id) BETWEEN 1 AND 512 AND octet_length(owner_id)<=2048
    ),
    page_id UUID NOT NULL,
    revision BIGINT NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991),
    sources JSONB NOT NULL CHECK(
        jsonb_typeof(sources)='array'
        AND jsonb_array_length(sources)<=8
        AND octet_length(sources::text)<=16416
    ),
    content BYTEA NOT NULL CHECK(octet_length(content) BETWEEN 1 AND 16384),
    content_sha256 TEXT NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at BIGINT NOT NULL CHECK(created_at BETWEEN 0 AND 9007199254740991),
    PRIMARY KEY(owner_id,page_id,revision),
    FOREIGN KEY(owner_id,page_id) REFERENCES atlas_page(owner_id,page_id)
        ON DELETE RESTRICT
)
""".strip(),
    """
CREATE INDEX atlas_page_revision_history
ON atlas_page_revision(owner_id,page_id,revision DESC)
""".strip(),
    """
CREATE FUNCTION reject_atlas_page_revision_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog AS $atlas_page_revision$
BEGIN
    RAISE EXCEPTION 'atlas page revisions are immutable' USING ERRCODE='23514';
END
$atlas_page_revision$
""".strip(),
    """
CREATE TRIGGER atlas_page_revision_immutable
BEFORE UPDATE OR DELETE ON atlas_page_revision
FOR EACH ROW EXECUTE FUNCTION reject_atlas_page_revision_mutation()
""".strip(),
    """
INSERT INTO atlas_page_revision(
    owner_id,page_id,revision,sources,content,content_sha256,created_at
)
SELECT
    owner_id,page_id,revision,sources,content,content_sha256,updated_at
FROM atlas_page
WHERE deleted_at IS NULL
""".strip(),
    """
ALTER TABLE atlas_page_command
DROP CONSTRAINT atlas_page_command_owner_id_page_id_command_key
""".strip(),
    """
ALTER TABLE atlas_page_command
DROP CONSTRAINT atlas_page_command_command_check
""".strip(),
    """
ALTER TABLE atlas_page_command
DROP CONSTRAINT atlas_page_command_check
""".strip(),
    """
ALTER TABLE atlas_page_command
ADD COLUMN expected_revision BIGINT
""".strip(),
    """
ALTER TABLE atlas_page_command
ADD CONSTRAINT atlas_page_command_expected_revision_check CHECK(
    expected_revision IS NULL OR expected_revision BETWEEN 1 AND 9007199254740991
)
""".strip(),
    """
ALTER TABLE atlas_page_command
ADD CONSTRAINT atlas_page_command_command_check CHECK(
    command IN ('create','edit','delete')
)
""".strip(),
    """
ALTER TABLE atlas_page_command
ADD CONSTRAINT atlas_page_command_check CHECK(
    (
        command='create'
        AND result_revision=1
        AND expected_revision IS NULL
    )
    OR (
        command='delete'
        AND result_revision>=2
        AND (
            expected_revision IS NULL
            OR result_revision=expected_revision+1
        )
    )
    OR (
        command='edit'
        AND result_revision>=2
        AND expected_revision=result_revision-1
    )
)
""".strip(),
    """
CREATE OR REPLACE FUNCTION enforce_atlas_page_lifecycle() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog AS $atlas_page_lifecycle$
DECLARE
    exact_revision BOOLEAN;
BEGIN
    IF OLD.deleted_at IS NOT NULL THEN
        RAISE EXCEPTION 'atlas page tombstone is terminal' USING ERRCODE='23514';
    END IF;
    IF NEW.owner_id IS DISTINCT FROM OLD.owner_id
       OR NEW.page_id IS DISTINCT FROM OLD.page_id
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.slug IS DISTINCT FROM OLD.slug
       OR NEW.revision<>OLD.revision+1
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'atlas page identity is immutable' USING ERRCODE='23514';
    END IF;
    IF NEW.updated_at<OLD.updated_at THEN
        RAISE EXCEPTION 'atlas page edit clock regressed' USING ERRCODE='23514';
    END IF;
    IF NEW.deleted_at IS NOT NULL THEN
        IF NEW.updated_at IS DISTINCT FROM NEW.deleted_at
           OR NEW.content IS NOT NULL
           OR NEW.content_sha256 IS NOT NULL
           OR NEW.sources<>'[]'::jsonb
        THEN
            RAISE EXCEPTION 'atlas page deletion is inconsistent' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;
    EXECUTE format(
        'SELECT EXISTS (SELECT 1 FROM %I.atlas_page_revision AS stored'
        ' WHERE stored.owner_id=$1 AND stored.page_id=$2 AND stored.revision=$3'
        ' AND stored.sources=$4 AND stored.content=$5 AND stored.content_sha256=$6'
        ' AND stored.created_at=$7)',
        TG_TABLE_SCHEMA
    ) INTO exact_revision
    USING NEW.owner_id,NEW.page_id,NEW.revision,NEW.sources,NEW.content,
        NEW.content_sha256,NEW.updated_at;
    IF NOT exact_revision THEN
        RAISE EXCEPTION 'atlas page head is not backed by an exact revision'
            USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END
$atlas_page_lifecycle$
""".strip(),
    """
CREATE FUNCTION verify_atlas_page_head_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog AS $atlas_page_head$
DECLARE
    exact_revision BOOLEAN;
BEGIN
    IF NEW.deleted_at IS NOT NULL THEN
        RETURN NULL;
    END IF;
    EXECUTE format(
        'SELECT EXISTS (SELECT 1 FROM %I.atlas_page_revision AS stored'
        ' WHERE stored.owner_id=$1 AND stored.page_id=$2 AND stored.revision=$3'
        ' AND stored.sources=$4 AND stored.content=$5 AND stored.content_sha256=$6'
        ' AND stored.created_at=$7)',
        TG_TABLE_SCHEMA
    ) INTO exact_revision
    USING NEW.owner_id,NEW.page_id,NEW.revision,NEW.sources,NEW.content,
        NEW.content_sha256,NEW.updated_at;
    IF NOT exact_revision THEN
        RAISE EXCEPTION 'atlas page head is not backed by an exact revision'
            USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END
$atlas_page_head$
""".strip(),
    """
CREATE CONSTRAINT TRIGGER atlas_page_head_revision
AFTER INSERT OR UPDATE ON atlas_page
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION verify_atlas_page_head_revision()
""".strip(),
)