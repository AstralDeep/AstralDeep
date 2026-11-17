"""Conversation branches extend messages with parent links, give every chat one
compare-and-set branch head, and record fork lineage on the chat row.
The guarded migration registry installs these objects before branch reads are served.
"""

CONVERSATION_BRANCH_SCHEMA_STATEMENTS = (
    "ALTER TABLE messages ADD COLUMN parent_message_id BIGINT",
    """
WITH visible_markers AS (
    SELECT message.id,
           message.chat_id,
           message.user_id,
           message.conversation_commit_id,
           message.committed_render_revision,
           message.timestamp,
           message.commit_position,
           CASE
               WHEN message.conversation_commit_id IS NULL THEN TRUE
               WHEN publication.state = 'committed'
                    AND publication.committed_render_revision =
                        message.committed_render_revision THEN TRUE
               ELSE FALSE
           END AS is_visible
    FROM messages AS message
    LEFT JOIN conversation_commit AS publication
      ON publication.commit_id = message.conversation_commit_id
),
ordered AS (
    SELECT marked.*,
           ROW_NUMBER() OVER branch_window AS position
    FROM visible_markers AS marked
    WINDOW branch_window AS (
        PARTITION BY marked.chat_id, marked.user_id
        ORDER BY COALESCE(marked.committed_render_revision, 0) ASC,
                 CASE
                     WHEN marked.conversation_commit_id IS NULL
                         THEN marked.timestamp
                     ELSE marked.commit_position::BIGINT
                 END ASC,
                 marked.id ASC
    )
),
sequenced AS (
    SELECT ordered.*,
           LAG(ordered.id) OVER step_window AS previous_id,
           LAG(ordered.conversation_commit_id) OVER step_window AS previous_commit_id,
           MAX(CASE WHEN ordered.is_visible THEN ordered.position END)
               OVER step_frame AS previous_visible_position
    FROM ordered AS ordered
    WINDOW step_window AS (
        PARTITION BY ordered.chat_id, ordered.user_id
        ORDER BY ordered.position
    ),
           step_frame AS (
        PARTITION BY ordered.chat_id, ordered.user_id
        ORDER BY ordered.position
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    )
),
annotated AS (
    SELECT sequenced.id,
           CASE
               WHEN sequenced.previous_id IS NULL THEN NULL
               WHEN sequenced.previous_commit_id
                        IS NOT DISTINCT FROM sequenced.conversation_commit_id
                   THEN sequenced.previous_id
               ELSE visible_previous.id
           END AS parent_id
    FROM sequenced AS sequenced
    LEFT JOIN ordered AS visible_previous
      ON visible_previous.chat_id = sequenced.chat_id
     AND visible_previous.user_id IS NOT DISTINCT FROM sequenced.user_id
     AND visible_previous.position = sequenced.previous_visible_position
)
UPDATE messages AS message
SET parent_message_id = annotated.parent_id
FROM annotated
WHERE message.id = annotated.id
""".strip(),
    """
ALTER TABLE messages ADD CONSTRAINT messages_parent_branch_self CHECK (
    parent_message_id IS NULL OR parent_message_id <> id
)
""".strip(),
    """
ALTER TABLE messages ADD CONSTRAINT messages_parent_branch FOREIGN KEY (
    parent_message_id
) REFERENCES messages(id) ON DELETE SET NULL
""".strip(),
    """
CREATE INDEX messages_parent_branch_children
ON messages(parent_message_id) WHERE parent_message_id IS NOT NULL
""".strip(),
    "ALTER TABLE chats ADD COLUMN fork_source_chat_id TEXT",
    "ALTER TABLE chats ADD COLUMN fork_source_message_id BIGINT",
    """
ALTER TABLE chats ADD CONSTRAINT chats_fork_source_chat FOREIGN KEY (
    fork_source_chat_id
) REFERENCES chats(id) ON DELETE SET NULL
""".strip(),
    """
ALTER TABLE chats ADD CONSTRAINT chats_fork_source_message FOREIGN KEY (
    fork_source_message_id
) REFERENCES messages(id) ON DELETE SET NULL
""".strip(),
    """
CREATE TABLE chat_branch_head (
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    head_message_id BIGINT,
    branch_snapshot_id BIGINT,
    version BIGINT NOT NULL,
    CONSTRAINT chat_branch_head_pkey PRIMARY KEY (chat_id),
    CONSTRAINT chat_branch_head_owner UNIQUE (user_id, chat_id),
    CONSTRAINT chat_branch_head_chat FOREIGN KEY (user_id, chat_id)
        REFERENCES chats(user_id, id) ON DELETE CASCADE,
    CONSTRAINT chat_branch_head_message FOREIGN KEY (head_message_id)
        REFERENCES messages(id) ON DELETE SET NULL,
    CONSTRAINT chat_branch_head_snapshot FOREIGN KEY (branch_snapshot_id)
        REFERENCES workspace_snapshot(id) ON DELETE SET NULL,
    CONSTRAINT chat_branch_head_version_positive CHECK (version >= 0)
)
""".strip(),
    """
INSERT INTO chat_branch_head (chat_id, user_id, head_message_id, branch_snapshot_id, version)
SELECT chat.id, chat.user_id, latest.id, NULL, 0
FROM chats AS chat
LEFT JOIN LATERAL (
    SELECT message.id
    FROM messages AS message
    LEFT JOIN conversation_commit AS publication
      ON publication.commit_id = message.conversation_commit_id
    WHERE message.chat_id = chat.id
      AND message.user_id = chat.user_id
      AND (
        message.conversation_commit_id IS NULL
        OR (
          publication.state = 'committed'
          AND publication.committed_render_revision = message.committed_render_revision
        )
      )
    ORDER BY
        COALESCE(message.committed_render_revision, 0) DESC,
        CASE
            WHEN message.conversation_commit_id IS NULL
                THEN message.timestamp
            ELSE message.commit_position::BIGINT
        END DESC,
        message.id DESC
    LIMIT 1
) AS latest ON TRUE
WHERE chat.user_id IS NOT NULL
""".strip(),
)
