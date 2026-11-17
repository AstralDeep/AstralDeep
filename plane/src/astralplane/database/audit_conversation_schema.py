"""Owner-and-conversation audit index that lets repositories/audit.py page one chat's events
without scanning the owner's whole history.
The guarded migration registry in database/migrations.py installs it at schema 089.015.
"""

AUDIT_CONVERSATION_SCHEMA_STATEMENTS = (
    "CREATE INDEX idx_audit_user_conversation_recorded ON audit_events "
    "(actor_user_id, conversation_id, recorded_at DESC, event_id DESC) "
    "WHERE conversation_id IS NOT NULL",
)
