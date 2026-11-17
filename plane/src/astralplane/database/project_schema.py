"""Normalized owner-scoped projects and chat membership extend the guarded registry.
Project tombstones retain create identities while composite keys prevent cross-owner links.
"""

from typing import Final

PROJECT_SCHEMA_STATEMENTS: Final = (
    "ALTER TABLE chats ADD CONSTRAINT chats_owner_identity UNIQUE(user_id,id)",
    """
    CREATE TABLE chat_project (
        owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 1 AND 512),
        project_id UUID NOT NULL,
        name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 200 AND name=btrim(name)),
        position BIGINT NOT NULL CHECK(position BETWEEN 0 AND 9007199254740991),
        revision BIGINT NOT NULL DEFAULT 1 CHECK(revision BETWEEN 1 AND 9007199254740991),
        created_at BIGINT NOT NULL CHECK(created_at BETWEEN 0 AND 9007199254740991),
        updated_at BIGINT NOT NULL CHECK(updated_at BETWEEN created_at AND 9007199254740991),
        create_digest TEXT NOT NULL CHECK(create_digest ~ '^[0-9a-f]{64}$'),
        deleted BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY(owner_id,project_id)
    )
    """,
    "CREATE INDEX chat_project_order ON chat_project(owner_id,position,project_id)"
    " WHERE NOT deleted",
    """
    CREATE TABLE chat_project_membership (
        owner_id TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        project_id UUID NOT NULL,
        PRIMARY KEY(owner_id,chat_id),
        CONSTRAINT chat_project_membership_project FOREIGN KEY(owner_id,project_id)
            REFERENCES chat_project(owner_id,project_id) ON DELETE RESTRICT,
        CONSTRAINT chat_project_membership_chat FOREIGN KEY(owner_id,chat_id)
            REFERENCES chats(user_id,id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX chat_project_membership_order"
    " ON chat_project_membership(owner_id,project_id,chat_id)",
)
