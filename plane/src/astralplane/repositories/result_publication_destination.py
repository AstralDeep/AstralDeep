"""Reads a bounded, complete current canvas destination under owner/publication/chat
lock order without allocating unbounded JSON. Consistency only, not publish
authority; used by repositories/assignments.py and result_publications.py.
"""

from __future__ import annotations

from astralplane.database.dialect import dialect_of
from astralplane.repositories import RepositoryValidationError, _freeze
from astralplane.repositories import assignments as a
from astralplane.repositories.result_publication_models import ResultPublicationContent
from astralplane.repositories.workspaces import (
    CanvasRepository,
    LayoutRepository,
    PublicationRebaseComponent,
    PublicationRebaseLayout,
    PublicationRepository,
)

MAX_ENTRIES = 1000
MAX_BYTES = 8 * 1024 * 1024
_SQLITE_ROW_FIELDS = {
    "saved_components": (
        "id",
        "chat_id",
        "user_id",
        "component_data",
        "component_type",
        "title",
        "created_at",
        "component_id",
        "position",
        "updated_at",
        "conversation_commit_id",
        "committed_render_revision",
    ),
    "workspace_layout": (
        "id",
        "chat_id",
        "user_id",
        "layout_key",
        "position",
        "layout",
        "created_at",
        "updated_at",
        "conversation_commit_id",
        "committed_render_revision",
    ),
}


def _require(value):
    if not value:
        a._conflict("assignment_publication_conflict")


def read(
    repo,
    tx,
    *,
    owner_id,
    conversation_id,
    expected_render_revision,
    expected_publication_id,
    maximum_bytes=1048576,
):
    if type(owner_id) is not str or type(conversation_id) is not str:
        raise RepositoryValidationError("invalid publication destination")
    a._text(owner_id)
    a._text(conversation_id, 512)
    a._integer(expected_render_revision, 0)
    a._integer(maximum_bytes, 1, MAX_BYTES)
    if expected_publication_id is not None:
        a._uuid(expected_publication_id)
    _require((expected_render_revision == 0) == (expected_publication_id is None))
    revision = None if expected_render_revision == 0 else expected_render_revision
    args = (owner_id, conversation_id, expected_publication_id, revision)
    predicate = (
        "user_id=%s AND chat_id=%s AND conversation_commit_id IS NOT DISTINCT FROM %s "
        "AND committed_render_revision IS NOT DISTINCT FROM %s"
    )
    previous_timeout = None
    if dialect_of(tx).is_sqlite:
        previous_timeout = tx.fetch_one("SELECT current_setting('lock_timeout') AS value")["value"]
        tx.fetch_one("SELECT set_config('lock_timeout','1ms',true)")
    try:
        with tx.savepoint("result_publication_destination"):
            _require(repo._lock_operation_owner(tx, owner_id))
            if expected_publication_id is not None:
                row = tx.fetch_one(
                    "SELECT commit_id FROM conversation_commit "
                    "WHERE commit_id=%s AND owner_user_id=%s FOR UPDATE NOWAIT",
                    (expected_publication_id, owner_id),
                )
                _require(row is not None)
                pub = PublicationRepository().get_for_owner(
                    tx, owner_id=owner_id, publication_id=expected_publication_id
                )
                _require(
                    pub is not None
                    and pub.state == "committed"
                    and pub.conversation_id == conversation_id
                    and type(pub.committed_render_revision) is int
                    and pub.committed_render_revision == expected_render_revision
                )
            head = tx.fetch_one(
                "SELECT render_revision,conversation_commit_id FROM chats "
                "WHERE user_id=%s AND id=%s FOR UPDATE NOWAIT",
                (owner_id, conversation_id),
            )
            _require(
                head is not None
                and type(head["render_revision"]) is int
                and head["render_revision"] == expected_render_revision
                and head["conversation_commit_id"] == expected_publication_id
            )
            identifiers, total, byte_count = {}, 0, 0
            for table in ("saved_components", "workspace_layout"):
                rows = tx.fetch_all(
                    "SELECT id FROM "
                    + table
                    + " WHERE "
                    + predicate
                    + " ORDER BY id LIMIT %s FOR UPDATE NOWAIT",
                    (*args, MAX_ENTRIES + 1),
                )
                total += len(rows)
                _require(total <= MAX_ENTRIES)
                identifiers[table] = {row["id"] for row in rows}
                projection = "row_to_json(item)::text"
                if dialect_of(tx).is_sqlite:
                    projection = (
                        "json_object("
                        + ",".join(
                            f"'{column}',item.{column}" for column in _SQLITE_ROW_FIELDS[table]
                        )
                        + ")"
                    )
                sizes = tx.fetch_one(
                    "SELECT count(*) AS n,COALESCE(sum(octet_length("
                    + projection + ")),0) AS bytes FROM "
                    + table
                    + " item WHERE "
                    + predicate,
                    args,
                )
                _require(sizes["n"] == len(rows))
                byte_count += sizes["bytes"]
                _require(byte_count <= maximum_bytes)
            components = CanvasRepository().list_current(
                tx, owner_id=owner_id, conversation_id=conversation_id
            )
            layouts = LayoutRepository().list_current(
                tx, owner_id=owner_id, conversation_id=conversation_id
            )
            _require(
                {row.row_id for row in components} == identifiers["saved_components"]
                and {row.layout_id for row in layouts} == identifiers["workspace_layout"]
            )
            for row in components:
                a._text(row.row_id, 512)
                a._text(row.component_id, 512)
                a._integer(row.position, 0)
            return ResultPublicationContent(
                tuple(
                    PublicationRebaseComponent(
                        row.row_id,
                        row.component_id,
                        _freeze(row.payload),
                        row.component_type,
                        row.title,
                        row.position,
                    )
                    for row in components
                ),
                tuple(
                    PublicationRebaseLayout(row.layout_key, row.position, _freeze(row.tree))
                    for row in layouts
                ),
            )
    except Exception as exc:
        if getattr(exc, "pgcode", None) == "55P03":
            a._conflict("assignment_publication_busy")
        raise
    finally:
        if previous_timeout is not None:
            tx.fetch_one("SELECT set_config('lock_timeout',%s,true)", (previous_timeout,))
