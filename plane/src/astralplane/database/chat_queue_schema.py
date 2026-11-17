"""Owner- and chat-scoped queued follow-up rows with per-item revision fences, chat
cascade retention, and deterministic chat-local ordering. The guarded migration registry
installs this table; dispatch, rendering, audit, and PHI policy stay with the host.
"""

CHAT_QUEUE_SCHEMA_STATEMENTS = (
    """
CREATE TABLE chat_queue_item (
    item_id UUID PRIMARY KEY,
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    text TEXT NOT NULL CHECK(char_length(text) BETWEEN 1 AND 16384),
    position BIGINT NOT NULL CHECK(position BETWEEN 0 AND 9007199254740991),
    revision BIGINT NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991),
    created_at BIGINT NOT NULL CHECK(created_at BETWEEN 0 AND 9007199254740991),
    updated_at BIGINT NOT NULL CHECK(updated_at BETWEEN 0 AND 9007199254740991),
    CONSTRAINT chat_queue_item_chat_order UNIQUE(chat_id, position)
        DEFERRABLE INITIALLY IMMEDIATE,
    CONSTRAINT chat_queue_item_retention CHECK(updated_at >= created_at)
)
""".strip(),
)
