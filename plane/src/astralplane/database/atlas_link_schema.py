"""Additive owner-scoped Atlas page links, per-page link revisions, and immutable link
commands, executed only by database/migrations.py's guarded registry. Both endpoints must be
live pages of one owner; rendering, authorization, and link policy remain with the host.
"""

ATLAS_LINK_SCHEMA_STATEMENTS = (
    """
CREATE TABLE atlas_page_link (
    owner_id TEXT NOT NULL CHECK(
        char_length(owner_id) BETWEEN 1 AND 512 AND octet_length(owner_id)<=2048
    ),
    source_page_id UUID NOT NULL,
    target_page_id UUID NOT NULL,
    link_revision BIGINT NOT NULL CHECK(
        link_revision BETWEEN 1 AND 9007199254740991
    ),
    linked_at BIGINT NOT NULL CHECK(linked_at BETWEEN 0 AND 9007199254740991),
    PRIMARY KEY(owner_id,source_page_id,target_page_id),
    FOREIGN KEY(owner_id,source_page_id) REFERENCES atlas_page(owner_id,page_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(owner_id,target_page_id) REFERENCES atlas_page(owner_id,page_id)
        ON DELETE RESTRICT,
    CONSTRAINT atlas_page_link_not_reflexive CHECK(source_page_id<>target_page_id)
)
""".strip(),
    """
CREATE INDEX atlas_page_link_outbound
ON atlas_page_link(owner_id,source_page_id,linked_at DESC,target_page_id DESC)
""".strip(),
    """
CREATE INDEX atlas_page_link_backlinks
ON atlas_page_link(owner_id,target_page_id,linked_at DESC,source_page_id DESC)
""".strip(),
    """
CREATE TABLE atlas_page_link_head (
    owner_id TEXT NOT NULL CHECK(
        char_length(owner_id) BETWEEN 1 AND 512 AND octet_length(owner_id)<=2048
    ),
    page_id UUID NOT NULL,
    link_revision BIGINT NOT NULL CHECK(
        link_revision BETWEEN 1 AND 9007199254740991
    ),
    updated_at BIGINT NOT NULL CHECK(updated_at BETWEEN 0 AND 9007199254740991),
    PRIMARY KEY(owner_id,page_id),
    FOREIGN KEY(owner_id,page_id) REFERENCES atlas_page(owner_id,page_id)
        ON DELETE RESTRICT
)
""".strip(),
    """
CREATE TABLE atlas_page_link_command (
    owner_id TEXT NOT NULL CHECK(
        char_length(owner_id) BETWEEN 1 AND 512 AND octet_length(owner_id)<=2048
    ),
    command_id UUID NOT NULL,
    page_id UUID NOT NULL,
    request_digest TEXT NOT NULL CHECK(request_digest ~ '^[0-9a-f]{64}$'),
    expected_page_revision BIGINT NOT NULL CHECK(
        expected_page_revision BETWEEN 1 AND 9007199254740991
    ),
    expected_link_revision BIGINT NOT NULL CHECK(
        expected_link_revision BETWEEN 0 AND 9007199254740991
    ),
    result_link_revision BIGINT NOT NULL CHECK(
        result_link_revision BETWEEN 1 AND 9007199254740991
    ),
    linked_at BIGINT NOT NULL CHECK(linked_at BETWEEN 0 AND 9007199254740991),
    PRIMARY KEY(owner_id,command_id),
    FOREIGN KEY(owner_id,page_id) REFERENCES atlas_page(owner_id,page_id)
        ON DELETE RESTRICT,
    CHECK(result_link_revision=expected_link_revision+1)
)
""".strip(),
    """
CREATE INDEX atlas_page_link_command_page
ON atlas_page_link_command(owner_id,page_id,linked_at,command_id)
""".strip(),
    """
CREATE FUNCTION reject_atlas_page_link_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog AS $atlas_page_link$
BEGIN
    RAISE EXCEPTION 'atlas page links are replaced, not mutated' USING ERRCODE='23514';
END
$atlas_page_link$
""".strip(),
    """
CREATE TRIGGER atlas_page_link_immutable
BEFORE UPDATE ON atlas_page_link
FOR EACH ROW EXECUTE FUNCTION reject_atlas_page_link_mutation()
""".strip(),
    """
CREATE FUNCTION enforce_atlas_page_link_head_advance() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog AS $atlas_page_link_head$
BEGIN
    IF NEW.owner_id IS DISTINCT FROM OLD.owner_id
       OR NEW.page_id IS DISTINCT FROM OLD.page_id
       OR NEW.link_revision<>OLD.link_revision+1
       OR NEW.updated_at<OLD.updated_at
    THEN
        RAISE EXCEPTION 'atlas page link revision must advance exactly once' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END
$atlas_page_link_head$
""".strip(),
    """
CREATE TRIGGER atlas_page_link_head_advance
BEFORE UPDATE ON atlas_page_link_head
FOR EACH ROW EXECUTE FUNCTION enforce_atlas_page_link_head_advance()
""".strip(),
    """
CREATE FUNCTION reject_atlas_page_link_command_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog AS $atlas_page_link_command$
BEGIN
    RAISE EXCEPTION 'atlas page link commands are immutable' USING ERRCODE='23514';
END
$atlas_page_link_command$
""".strip(),
    """
CREATE TRIGGER atlas_page_link_command_immutable
BEFORE UPDATE OR DELETE ON atlas_page_link_command
FOR EACH ROW EXECUTE FUNCTION reject_atlas_page_link_command_mutation()
""".strip(),
)