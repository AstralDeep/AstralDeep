"""Additive owner guidance storage schema, executed only by database/migrations.py's
guarded registry.
"""

GUIDANCE_SCHEMA_STATEMENTS = (
    (
        'CREATE TABLE owner_skill_head (\n'
        '        owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 A'
        'ND 256\n'
        '            AND octet_length(owner_id)<=1024),\n'
        "        skill_id UUID NOT NULL, slug TEXT NOT NULL CHECK(slug ~ '^[a-z"
        "0-9][a-z0-9-]{0,47}$'),\n"
        '        revision BIGINT NOT NULL CHECK(revision BETWEEN 1 AND 90071992'
        '54740991),\n'
        '        name TEXT NOT NULL CHECK(char_length(name) BETWEEN 2 AND 60),\n'
        "        alias TEXT NOT NULL CHECK(alias='' OR alias ~ '^[a-z][a-z0-9_-"
        "]{0,23}$'),\n"
        "        applies_to JSONB NOT NULL CHECK(jsonb_typeof(applies_to)='arra"
        "y' AND jsonb_array_length(applies_to)<=8),\n"
        '        enabled BOOLEAN NOT NULL, definition_digest TEXT NOT NULL CHEC'
        "K(definition_digest ~ '^[0-9a-f]{64}$'),\n"
        '        created_at BIGINT NOT NULL CHECK(created_at>=0),\n'
        '        updated_at BIGINT NOT NULL CHECK(updated_at>=created_at),\n'
        '        deleted_at BIGINT CHECK(deleted_at IS NULL OR (deleted_at=upda'
        'ted_at AND NOT enabled)),\n'
        '        PRIMARY KEY(owner_id,skill_id), UNIQUE(skill_id)\n'
        '    )'
    ),
    (
        'CREATE UNIQUE INDEX owner_skill_live_slug ON owner_skill_head(owner_id'
        ',slug) WHERE deleted_at IS NULL'
    ),
    (
        'CREATE UNIQUE INDEX owner_skill_live_alias ON owner_skill_head(owner_i'
        "d,alias) WHERE deleted_at IS NULL AND alias<>''"
    ),
    (
        'CREATE TABLE owner_skill_revision (\n'
        '        owner_id TEXT NOT NULL, skill_id UUID NOT NULL,\n'
        '        revision BIGINT NOT NULL CHECK(revision BETWEEN 1 AND 90071992'
        '54740991),\n'
        "        definition JSONB NOT NULL CHECK(jsonb_typeof(definition)='obje"
        "ct' AND octet_length(definition::text)<=32768),\n"
        "        definition_digest TEXT NOT NULL CHECK(definition_digest ~ '^[0"
        "-9a-f]{64}$'),\n"
        '        created_at BIGINT NOT NULL CHECK(created_at>=0), deleted BOOLE'
        'AN NOT NULL,\n'
        "        command_id UUID, command TEXT CHECK(command IN ('create','repl"
        "ace','delete')),\n"
        "        request_digest TEXT CHECK(request_digest ~ '^[0-9a-f]{64}$'),\n"
        '        legacy_markdown BYTEA, legacy_digest TEXT, legacy_updated_at B'
        'IGINT,\n'
        '        PRIMARY KEY(owner_id,skill_id,revision), UNIQUE(owner_id,comma'
        'nd_id),\n'
        '        FOREIGN KEY(owner_id,skill_id) REFERENCES owner_skill_head(own'
        'er_id,skill_id)\n'
        '            ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,\n'
        '        CHECK((command_id IS NULL AND command IS NULL AND request_dige'
        'st IS NULL)\n'
        '           OR (command_id IS NOT NULL AND command IS NOT NULL AND requ'
        'est_digest IS NOT NULL)),\n'
        '        CHECK((legacy_markdown IS NULL AND legacy_digest IS NULL AND l'
        'egacy_updated_at IS NULL)\n'
        '           OR (revision=1 AND NOT deleted AND command_id IS NULL AND l'
        'egacy_markdown IS NOT NULL\n'
        '               AND octet_length(legacy_markdown) BETWEEN 1 AND 32768\n'
        '               AND legacy_digest IS NOT NULL AND legacy_updated_at IS NOT NULL\n'
        "               AND legacy_digest ~ '^[0-9a-f]{64}$' AND legacy_updated"
        '_at>=0))\n'
        '    )'
    ),
    (
        'ALTER TABLE owner_skill_head ADD CONSTRAINT owner_skill_current_revisi'
        'on FOREIGN KEY(owner_id,skill_id,revision) REFERENCES owner_skill_revi'
        'sion(owner_id,skill_id,revision) DEFERRABLE INITIALLY DEFERRED'
    ),
    (
        'CREATE FUNCTION reject_guidance_snapshot_update() RETURNS trigger LANG'
        'UAGE plpgsql SET search_path TO pg_catalog AS $fn$\n'
        "    BEGIN RAISE EXCEPTION 'guidance snapshots are immutable' USING ERR"
        "CODE='23514'; END\n"
        '    $fn$'
    ),
    (
        'CREATE TRIGGER owner_skill_revision_immutable BEFORE UPDATE ON owner_s'
        'kill_revision FOR EACH ROW EXECUTE FUNCTION reject_guidance_snapshot_u'
        'pdate()'
    ),
    (
        'CREATE TABLE owner_skill_catalog (\n'
        '        owner_id TEXT PRIMARY KEY CHECK(char_length(owner_id) BETWEEN '
        '1 AND 256 AND octet_length(owner_id)<=1024),\n'
        "        manifest_digest TEXT NOT NULL CHECK(manifest_digest ~ '^[0-9a-"
        "f]{64}$'),\n"
        "        entry_digest TEXT NOT NULL CHECK(entry_digest ~ '^[0-9a-f]{64}"
        "$'),\n"
        "        mappings JSONB NOT NULL CHECK(jsonb_typeof(mappings)='array' A"
        'ND jsonb_array_length(mappings)<=20\n'
        '            AND octet_length(mappings::text)<=16384),\n'
        '        created_at BIGINT NOT NULL CHECK(created_at>=0)\n'
        '    )'
    ),
    (
        'CREATE TRIGGER owner_skill_catalog_immutable BEFORE UPDATE ON owner_sk'
        'ill_catalog FOR EACH ROW EXECUTE FUNCTION reject_guidance_snapshot_upd'
        'ate()'
    ),
    (
        'CREATE TABLE explicit_note_current (\n'
        '        owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 A'
        'ND 256 AND octet_length(owner_id)<=1024),\n'
        '        note_id UUID NOT NULL, revision BIGINT NOT NULL CHECK(revision'
        ' BETWEEN 1 AND 9007199254740991),\n'
        '        format_version INTEGER, category TEXT, enabled BOOLEAN,\n'
        '        created_at BIGINT, updated_at BIGINT, expires_at BIGINT, ciphe'
        'rtext BYTEA,\n'
        '        deleted_at BIGINT, deleted_reason TEXT,\n'
        '        PRIMARY KEY(owner_id,note_id), UNIQUE(note_id),\n'
        '        CHECK((deleted_at IS NULL AND deleted_reason IS NULL AND revis'
        'ion<9007199254740991\n'
        "               AND format_version=1 AND category IN ('profession','goa"
        "l','preference','workflow_tag','context')\n"
        '               AND category IS NOT NULL AND enabled IS NOT NULL AND fo'
        'rmat_version IS NOT NULL\n'
        '               AND created_at IS NOT NULL AND created_at BETWEEN 0 AND'
        ' 9007199254740991 AND updated_at IS NOT NULL AND updated_at>=created_a'
        't AND updated_at<=9007199254740991\n'
        '               AND (expires_at IS NULL OR expires_at>updated_at AND ex'
        'pires_at<=9007199254740991)\n'
        '               AND ciphertext IS NOT NULL AND octet_length(ciphertext)'
        ' BETWEEN 1 AND 16384)\n'
        '           OR (deleted_at IS NOT NULL AND deleted_at BETWEEN 0 AND 900'
        '7199254740991 AND deleted_reason IS NOT NULL\n'
        "               AND deleted_reason IN ('forgotten','expired') AND forma"
        't_version IS NULL AND category IS NULL\n'
        '               AND enabled IS NULL AND created_at IS NULL AND updated_'
        'at IS NULL AND expires_at IS NULL AND ciphertext IS NULL))\n'
        '    )'
    ),
    (
        'CREATE INDEX explicit_note_expiry ON explicit_note_current(expires_at,'
        'owner_id,note_id) WHERE deleted_at IS NULL AND expires_at IS NOT NULL'
    ),
    (
        'CREATE TABLE assignment_guidance_selection (\n'
        '        owner_id TEXT NOT NULL, assignment_id UUID NOT NULL,\n'
        '        instruction_revision BIGINT NOT NULL CHECK(instruction_revisio'
        'n>0),\n'
        "        reference_digest TEXT NOT NULL CHECK(reference_digest ~ '^[0-9"
        "a-f]{64}$'),\n"
        '        created_at BIGINT NOT NULL CHECK(created_at>=0),\n'
        '        PRIMARY KEY(owner_id,assignment_id),\n'
        '        FOREIGN KEY(assignment_id,owner_id) REFERENCES persistent_assi'
        'gnment(id,owner_user_id)\n'
        '            ON DELETE CASCADE\n'
        '    )'
    ),
    (
        'CREATE TRIGGER assignment_guidance_selection_immutable BEFORE UPDATE O'
        'N assignment_guidance_selection FOR EACH ROW EXECUTE FUNCTION reject_g'
        'uidance_snapshot_update()'
    ),
    (
        'CREATE TABLE assignment_guidance_reference (\n'
        '        owner_id TEXT NOT NULL, assignment_id UUID NOT NULL,\n'
        '        instruction_revision BIGINT NOT NULL CHECK(instruction_revisio'
        'n>0),\n'
        "        kind TEXT NOT NULL CHECK(kind IN ('skill','note')), resource_i"
        'd UUID NOT NULL,\n'
        '        revision BIGINT NOT NULL CHECK(revision BETWEEN 1 AND 90071992'
        '54740991),\n'
        '        active BOOLEAN NOT NULL DEFAULT TRUE, invalidated_at BIGINT CH'
        'ECK(invalidated_at>=0),\n'
        '        PRIMARY KEY(owner_id,assignment_id,instruction_revision,kind,r'
        'esource_id),\n'
        '        FOREIGN KEY(assignment_id,owner_id) REFERENCES persistent_assi'
        'gnment(id,owner_user_id) ON DELETE CASCADE\n'
        '    )'
    ),
    (
        'CREATE INDEX assignment_guidance_resource ON assignment_guidance_refer'
        'ence(owner_id,kind,resource_id,assignment_id) WHERE active AND invalid'
        'ated_at IS NULL'
    ),
    (
        'CREATE INDEX assignment_guidance_assignment ON assignment_guidance_ref'
        'erence(owner_id,assignment_id) WHERE active'
    ),
)
