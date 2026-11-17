"""The single additive declarative-agent metadata migration, executed only by
database/migrations.py.
"""

from typing import Final

DECLARATIVE_AGENT_SCHEMA_STATEMENTS: Final = (
    "ALTER TABLE user_agent ADD COLUMN agent_kind TEXT NOT NULL DEFAULT 'executable' "
    "CHECK (agent_kind IN ('executable','declarative'))",
    "ALTER TABLE user_agent ADD COLUMN selected_definition_revision_id UUID",
    "ALTER TABLE user_agent ADD CONSTRAINT user_agent_kind_owner_unique "
    "UNIQUE (agent_id,owner_user_id,agent_kind)",
    "ALTER TABLE user_agent_revision ADD COLUMN revision_kind TEXT NOT NULL DEFAULT 'executable' "
    "CHECK (revision_kind IN ('executable','declarative'))",
    "ALTER TABLE user_agent_revision ADD COLUMN definition_version INTEGER",
    "ALTER TABLE user_agent_revision ADD COLUMN definition_json JSONB",
    "ALTER TABLE user_agent_revision ADD COLUMN definition_digest CHAR(64)",
    "ALTER TABLE user_agent_revision ADD CONSTRAINT user_agent_revision_kind_owner_unique "
    "UNIQUE (revision_id,agent_id,owner_user_id,revision_kind)",
    "ALTER TABLE user_agent_revision ADD CONSTRAINT user_agent_revision_kind_owner_fk "
    "FOREIGN KEY (agent_id,owner_user_id,revision_kind) "
    "REFERENCES user_agent(agent_id,owner_user_id,agent_kind) ON DELETE RESTRICT",
    "ALTER TABLE user_agent ADD CONSTRAINT user_agent_definition_selection_fk "
    "FOREIGN KEY (selected_definition_revision_id,agent_id,owner_user_id,agent_kind) "
    "REFERENCES user_agent_revision(revision_id,agent_id,owner_user_id,revision_kind) "
    "ON DELETE RESTRICT",
    """ALTER TABLE user_agent ADD CONSTRAINT user_agent_kind_state_check CHECK (
        (agent_kind='executable' AND selected_definition_revision_id IS NULL)
        OR (agent_kind='declarative'
            AND status IN ('draft','active','archived')
            AND ((status='active' AND selected_definition_revision_id IS NOT NULL
                  AND deleted_at IS NULL)
                 OR (status<>'active' AND selected_definition_revision_id IS NULL))
            AND (deleted_at IS NULL OR status='archived')
            AND active_revision_id IS NULL AND last_known_good_revision_id IS NULL
            AND host_client_id IS NULL AND host_session_id IS NULL
            AND host_last_seen_at IS NULL AND selected_host_session_id IS NULL
            AND authoritative_instance_id IS NULL AND draft_id IS NULL
            AND lifecycle_generation=0 AND generation_counter=0
            AND constitution_version IS NULL AND validated_at IS NULL
            AND validated_policy_revision IS NULL AND NOT revalidation_required
            AND declared_tools='[]' AND declared_scopes='[]' AND declared_egress IS NULL
            AND NOT is_public))""",
    "ALTER TABLE user_agent_revision DROP CONSTRAINT user_agent_revision_compatibility_state_check",
    "ALTER TABLE user_agent_revision DROP CONSTRAINT user_agent_revision_state_check",
    "ALTER TABLE user_agent_revision DROP CONSTRAINT user_agent_revision_artifact_check",
    """ALTER TABLE user_agent_revision ADD CONSTRAINT user_agent_revision_artifact_check CHECK (
        (revision_kind='executable'
         AND compatibility_state IN ('compatible','incompatible','legacy_pending')
         AND state IN ('legacy_pending','prepared','starting','ready','active','retired','failed')
         AND definition_version IS NULL AND definition_json IS NULL AND definition_digest IS NULL
         AND ((compatibility_state='legacy_pending' AND state='legacy_pending')
              OR (compatibility_state<>'legacy_pending' AND artifact_digest IS NOT NULL
                  AND manifest_json IS NOT NULL AND artifact_relative_path IS NOT NULL
                  AND runtime_contract_version IS NOT NULL AND release_lock_digest IS NOT NULL
                  AND promotion_token IS NOT NULL)))
        OR (revision_kind='declarative' AND compatibility_state='declarative' AND state='definition'
            AND definition_version IS NOT NULL AND definition_version=1
            AND definition_json IS NOT NULL AND jsonb_typeof(definition_json)='object'
            AND definition_json ? 'version' AND definition_json->'version'='1'::jsonb
            AND octet_length(definition_json::text)<=131072
            AND definition_digest IS NOT NULL AND definition_digest ~ '^[0-9a-f]{64}$'
            AND artifact_digest IS NULL AND manifest_json IS NULL AND artifact_relative_path IS NULL
            AND runtime_contract_version IS NULL AND release_lock_digest IS NULL
            AND promotion_token IS NULL AND previous_good_revision_id IS NULL
            AND state_revision=0 AND confirmed_at IS NULL AND promoted_at IS NULL
            AND failed_at IS NULL AND failure_code IS NULL))""",
    "ALTER TABLE agent_runtime_instance ADD COLUMN revision_kind TEXT NOT NULL "
    "DEFAULT 'executable' CHECK (revision_kind='executable')",
    "ALTER TABLE agent_runtime_instance ADD CONSTRAINT agent_runtime_executable_revision_fk "
    "FOREIGN KEY (revision_id,agent_id,owner_user_id,revision_kind) "
    "REFERENCES user_agent_revision(revision_id,agent_id,owner_user_id,revision_kind) "
    "ON DELETE RESTRICT",
    "ALTER TABLE draft_artifact_publication ADD COLUMN revision_kind TEXT NOT NULL "
    "DEFAULT 'executable' CHECK (revision_kind='executable')",
    "ALTER TABLE draft_artifact_publication ADD CONSTRAINT "
    "draft_publication_executable_revision_fk "
    "FOREIGN KEY (target_revision_id,target_agent_id,owner_user_id,revision_kind) "
    "REFERENCES user_agent_revision(revision_id,agent_id,owner_user_id,revision_kind) "
    "ON DELETE RESTRICT",
    "ALTER TABLE draft_agents ADD COLUMN published_revision_kind TEXT NOT NULL "
    "DEFAULT 'executable' CHECK (published_revision_kind='executable')",
    "ALTER TABLE draft_agents ADD CONSTRAINT draft_agents_executable_revision_fk "
    "FOREIGN KEY (published_revision_id,target_agent_id,user_id,published_revision_kind) "
    "REFERENCES user_agent_revision(revision_id,agent_id,owner_user_id,revision_kind) "
    "ON DELETE RESTRICT",
    """CREATE TABLE user_agent_command_receipt (
        owner_user_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        agent_kind TEXT NOT NULL DEFAULT 'declarative' CHECK (agent_kind='declarative'),
        command_id UUID NOT NULL CHECK (
            command_id::text ~
            '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
        command_version INTEGER NOT NULL CHECK (command_version=1),
        command TEXT NOT NULL CHECK (
            command IN ('create','revise','activate','archive','clone','delete')),
        request_digest CHAR(64) NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
        result_state_revision BIGINT NOT NULL CHECK (result_state_revision>=0),
        result_definition_revision_id UUID,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (owner_user_id,command_id),
        FOREIGN KEY (agent_id,owner_user_id,agent_kind)
            REFERENCES user_agent(agent_id,owner_user_id,agent_kind) ON DELETE RESTRICT,
        FOREIGN KEY (result_definition_revision_id,agent_id,owner_user_id,agent_kind)
            REFERENCES user_agent_revision(revision_id,agent_id,owner_user_id,revision_kind)
            ON DELETE RESTRICT
    )""",
    "CREATE INDEX user_agent_command_receipt_target ON "
    "user_agent_command_receipt(agent_id,owner_user_id)",
)
