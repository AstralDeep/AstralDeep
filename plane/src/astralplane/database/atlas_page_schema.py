"""Additive owner- and project-scoped Atlas page heads and immutable command receipts,
executed only by database/migrations.py's guarded registry. Page content is host ciphertext,
tombstones are terminal, and editing and links remain outside this schema slice.
"""

ATLAS_PAGE_SCHEMA_STATEMENTS = (
    """
CREATE TABLE atlas_page (
    owner_id TEXT NOT NULL CHECK(
        char_length(owner_id) BETWEEN 1 AND 512 AND octet_length(owner_id)<=2048
    ),
    page_id UUID NOT NULL,
    project_id TEXT CHECK(
        project_id IS NULL OR (
            char_length(project_id) BETWEEN 1 AND 128
            AND octet_length(project_id)<=512
        )
    ),
    slug TEXT NOT NULL CHECK(
        char_length(slug) BETWEEN 1 AND 128
        AND slug ~ '^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$'
    ),
    revision BIGINT NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991),
    sources JSONB NOT NULL CHECK(
        jsonb_typeof(sources)='array'
        AND jsonb_array_length(sources)<=8
        AND octet_length(sources::text)<=16416
    ),
    content BYTEA,
    content_sha256 TEXT CHECK(
        content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    created_at BIGINT NOT NULL CHECK(created_at BETWEEN 0 AND 9007199254740991),
    updated_at BIGINT NOT NULL CHECK(
        updated_at BETWEEN created_at AND 9007199254740991
    ),
    deleted_at BIGINT CHECK(
        deleted_at IS NULL OR (
            deleted_at=updated_at
            AND deleted_at>=created_at
            AND deleted_at<=9007199254740991
        )
    ),
    PRIMARY KEY(owner_id,page_id),
    CONSTRAINT atlas_page_lifecycle_shape CHECK(
        (
            deleted_at IS NULL
            AND content IS NOT NULL
            AND octet_length(content) BETWEEN 1 AND 16384
            AND content_sha256 IS NOT NULL
        )
        OR (
            deleted_at IS NOT NULL
            AND content IS NULL
            AND content_sha256 IS NULL
            AND sources='[]'::jsonb
        )
    )
)
""".strip(),
    """
CREATE UNIQUE INDEX atlas_page_live_slug
ON atlas_page(owner_id,COALESCE(project_id,''),slug)
WHERE deleted_at IS NULL
""".strip(),
    """
CREATE INDEX atlas_page_live_listing
ON atlas_page(owner_id,COALESCE(project_id,''),created_at DESC,page_id DESC)
WHERE deleted_at IS NULL
""".strip(),
    """
CREATE FUNCTION enforce_atlas_page_lifecycle() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog AS $atlas_page_lifecycle$
BEGIN
    IF OLD.deleted_at IS NOT NULL THEN
        RAISE EXCEPTION 'atlas page tombstone is terminal' USING ERRCODE='23514';
    END IF;
    IF NEW.deleted_at IS NULL
       OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
       OR NEW.page_id IS DISTINCT FROM OLD.page_id
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.slug IS DISTINCT FROM OLD.slug
       OR NEW.revision<>OLD.revision+1
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.updated_at IS DISTINCT FROM NEW.deleted_at
       OR NEW.content IS NOT NULL
       OR NEW.content_sha256 IS NOT NULL
       OR NEW.sources<>'[]'::jsonb
    THEN
        RAISE EXCEPTION 'atlas page content is immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END
$atlas_page_lifecycle$
""".strip(),
    """
CREATE TRIGGER atlas_page_lifecycle
BEFORE UPDATE ON atlas_page
FOR EACH ROW EXECUTE FUNCTION enforce_atlas_page_lifecycle()
""".strip(),
    """
CREATE TABLE atlas_page_command (
    owner_id TEXT NOT NULL CHECK(
        char_length(owner_id) BETWEEN 1 AND 512 AND octet_length(owner_id)<=2048
    ),
    command_id UUID NOT NULL,
    page_id UUID NOT NULL,
    command TEXT NOT NULL CHECK(command IN ('create','delete')),
    request_digest TEXT NOT NULL CHECK(request_digest ~ '^[0-9a-f]{64}$'),
    result_revision BIGINT NOT NULL CHECK(
        result_revision BETWEEN 1 AND 9007199254740991
    ),
    created_at BIGINT NOT NULL CHECK(created_at BETWEEN 0 AND 9007199254740991),
    PRIMARY KEY(owner_id,command_id),
    UNIQUE(owner_id,page_id,command),
    FOREIGN KEY(owner_id,page_id) REFERENCES atlas_page(owner_id,page_id)
        ON DELETE RESTRICT,
    CHECK(
        (command='create' AND result_revision=1)
        OR (command='delete' AND result_revision>=2)
    )
)
""".strip(),
    """
CREATE INDEX atlas_page_command_page
ON atlas_page_command(owner_id,page_id,created_at,command_id)
""".strip(),
    """
CREATE FUNCTION reject_atlas_page_command_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog AS $atlas_page_command$
BEGIN
    RAISE EXCEPTION 'atlas page command receipts are immutable' USING ERRCODE='23514';
END
$atlas_page_command$
""".strip(),
    """
CREATE TRIGGER atlas_page_command_immutable
BEFORE UPDATE OR DELETE ON atlas_page_command
FOR EACH ROW EXECUTE FUNCTION reject_atlas_page_command_mutation()
""".strip(),
)