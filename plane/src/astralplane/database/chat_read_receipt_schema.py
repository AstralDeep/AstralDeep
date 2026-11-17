"""Owner- and chat-scoped read receipts holding each chat's last-seen commit
position and a monotonic revision. The guarded migration registry installs this
table; unread policy, rendering, and audit stay with the host.
"""

CHAT_READ_RECEIPT_SCHEMA_STATEMENTS = (
    """
CREATE TABLE chat_read_receipt (
    owner_id TEXT NOT NULL CHECK(char_length(owner_id) BETWEEN 1 AND 512),
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    last_seen_position BIGINT NOT NULL
        CHECK(last_seen_position BETWEEN 0 AND 9007199254740991),
    revision BIGINT NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991),
    CONSTRAINT chat_read_receipt_chat_identity PRIMARY KEY(owner_id, chat_id)
)
""".strip(),
)
