-- Reference-only SQL for a per-tool, per-permission-kind schema delta and its agent_scopes
-- backfill; AstralDeep never executes it at startup or elsewhere — the live schema and any recovery
-- path are owned and applied by AstralPlane.

ALTER TABLE chats ADD COLUMN IF NOT EXISTS agent_id TEXT NULL;

ALTER TABLE tool_overrides ADD COLUMN IF NOT EXISTS permission_kind TEXT NULL;
ALTER TABLE tool_overrides DROP CONSTRAINT IF EXISTS tool_overrides_user_id_agent_id_tool_name_key;
CREATE UNIQUE INDEX IF NOT EXISTS tool_overrides_user_agent_tool_kind_uniq
    ON tool_overrides (user_id, agent_id, tool_name, COALESCE(permission_kind, ''));
