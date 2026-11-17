"""Additive immutable selected-input metadata and declarative reverse-reference schema,
executed only by database/migrations.py's guarded registry.
"""

SELECTED_INPUT_SCHEMA_STATEMENTS = (
    """
CREATE FUNCTION valid_assignment_selected_input(value JSONB) RETURNS BOOLEAN
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path=pg_catalog AS $$
DECLARE item JSONB; agent JSONB; count_skill INTEGER=0; count_note INTEGER=0;
BEGIN
    IF (jsonb_typeof(value)='object' AND octet_length(value::text)<=16384
        AND value ?& ARRAY['version','expansion_version','references','agent',
                         'binding_key_id','combined_binding']
        AND value-ARRAY['version','expansion_version','references','agent',
                       'binding_key_id','combined_binding']='{}'::jsonb
        AND value->'version'='1'::jsonb AND value->>'version'='1'
        AND value->'expansion_version'='1'::jsonb AND value->>'expansion_version'='1'
        AND jsonb_typeof(value->'binding_key_id')='string'
        AND value->>'binding_key_id' ~ '^[a-z][a-z0-9_]{0,31}$'
        AND jsonb_typeof(value->'combined_binding')='string'
        AND value->>'combined_binding' ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(value->'references')='array') IS NOT TRUE THEN RETURN FALSE; END IF;
    IF jsonb_array_length(value->'references')>28 THEN RETURN FALSE; END IF;
    FOR item IN SELECT * FROM jsonb_array_elements(value->'references') LOOP
        IF (jsonb_typeof(item)='object' AND item ?& ARRAY['kind','resource_id','revision']
            AND item-ARRAY['kind','resource_id','revision']='{}'::jsonb
            AND jsonb_typeof(item->'kind')='string' AND item->>'kind' IN ('skill','note')
            AND jsonb_typeof(item->'resource_id')='string'
            AND item->>'resource_id' ~
              '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
            AND jsonb_typeof(item->'revision')='number'
            AND item->>'revision' ~ '^[1-9][0-9]{0,15}$') IS NOT TRUE THEN
            RETURN FALSE;
        END IF;
        IF (item->>'revision')::numeric>9007199254740991 THEN RETURN FALSE; END IF;
        IF item->>'kind'='skill' THEN count_skill=count_skill+1;
        ELSE count_note=count_note+1; END IF;
    END LOOP;
    IF count_skill>20 OR count_note>8 OR EXISTS (
        SELECT 1 FROM jsonb_array_elements(value->'references') r
        GROUP BY r->>'kind',r->>'resource_id' HAVING count(*)>1
    ) THEN RETURN FALSE; END IF;
    agent=value->'agent';
    IF agent='null'::jsonb THEN RETURN count_skill+count_note>0; END IF;
    RETURN (jsonb_typeof(agent)='object'
        AND agent ?& ARRAY['agent_id','revision_id','definition_digest','kind']
        AND agent-ARRAY['agent_id','revision_id','definition_digest','kind']='{}'::jsonb
        AND agent->'kind'='"declarative"'::jsonb
        AND jsonb_typeof(agent->'agent_id')='string'
        AND char_length(agent->>'agent_id') BETWEEN 1 AND 255
        AND btrim(agent->>'agent_id')=agent->>'agent_id'
        AND agent->>'agent_id' !~ '[[:cntrl:]]'
        AND jsonb_typeof(agent->'revision_id')='string'
        AND agent->>'revision_id' ~
          '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
        AND jsonb_typeof(agent->'definition_digest')='string'
        AND agent->>'definition_digest' ~ '^[0-9a-f]{64}$') IS TRUE;
END $$
""".strip(),
    "ALTER TABLE assignment_guidance_selection ADD COLUMN selected_input JSONB "
    "CHECK(selected_input IS NULL OR valid_assignment_selected_input(selected_input))",
    "ALTER TABLE assignment_guidance_selection ADD CONSTRAINT assignment_selection_revision "
    "UNIQUE(owner_id,assignment_id,instruction_revision)",
    "ALTER TABLE user_agent_revision ADD CONSTRAINT user_agent_definition_reference "
    "UNIQUE(revision_id,agent_id,owner_user_id,revision_kind,definition_digest)",
    """
CREATE TABLE assignment_selected_agent (
    owner_id TEXT NOT NULL, assignment_id UUID NOT NULL,
    instruction_revision BIGINT NOT NULL CHECK(instruction_revision BETWEEN 1 AND 9007199254740991),
    agent_id TEXT NOT NULL CHECK(char_length(agent_id) BETWEEN 1 AND 255),
    revision_id UUID NOT NULL,
    definition_digest TEXT NOT NULL CHECK(definition_digest ~ '^[0-9a-f]{64}$'),
    kind TEXT NOT NULL DEFAULT 'declarative' CHECK(kind='declarative'),
    active BOOLEAN NOT NULL DEFAULT TRUE, invalidated_at BIGINT CHECK(invalidated_at>=0),
    PRIMARY KEY(owner_id,assignment_id),
    FOREIGN KEY(owner_id,assignment_id,instruction_revision)
        REFERENCES assignment_guidance_selection(owner_id,assignment_id,instruction_revision)
        ON DELETE CASCADE,
    FOREIGN KEY(revision_id,agent_id,owner_id,kind,definition_digest)
        REFERENCES user_agent_revision(revision_id,agent_id,owner_user_id,
                                       revision_kind,definition_digest)
)
""".strip(),
    "CREATE INDEX assignment_selected_agent_active ON assignment_selected_agent "
    "(owner_id,agent_id,assignment_id) WHERE active AND invalidated_at IS NULL",
    """
CREATE FUNCTION reject_selected_agent_identity_update() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
    IF (to_jsonb(NEW)-ARRAY['active','invalidated_at']) IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['active','invalidated_at']) THEN
        RAISE EXCEPTION 'selected agent identity is immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$
""".strip(),
    "CREATE TRIGGER assignment_selected_agent_immutable BEFORE UPDATE ON assignment_selected_agent "
    "FOR EACH ROW EXECUTE FUNCTION reject_selected_agent_identity_update()",
)
