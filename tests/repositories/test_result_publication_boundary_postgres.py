"""Real-PostgreSQL test for astralplane.repositories.history and workspaces: a legacy
parent-stage lock does not freeze bytes already reviewed onto the current canvas.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import independent_database, uid

from astralplane.repositories.history import ConversationRepository
from astralplane.repositories.workspaces import (
    CanvasComponentRecord,
    CanvasRepository,
    PublicationRepository,
)


@pytest.mark.postgresql_lock_catalog
def test_legacy_parent_stage_lock_does_not_freeze_reviewed_canvas_bytes(database):
    owner, chat, publication = uid(), uid(), uid()
    canvas, publications = CanvasRepository(), PublicationRepository()
    with database.transaction() as tx:
        schema = tx.fetch_one("SELECT current_schema() AS s")["s"]
        ConversationRepository().create(
            tx,
            conversation_id=chat,
            owner_id=owner,
            title="Synthetic review",
            agent_id=None,
            created_at=1,
        )
        publications.stage(
            tx,
            publication_id=publication,
            owner_id=owner,
            conversation_id=chat,
            request_generation=uid(),
            base_render_revision=0,
            started_at=datetime.now(UTC),
        )
        canvas.create(
            tx,
            CanvasComponentRecord(
                uid(),
                chat,
                owner,
                "result",
                {"type": "text", "text": "reviewed"},
                "text",
                None,
                0,
                1,
                1,
                publication,
                1,
            ),
        )

    def edit():
        with independent_database(schema) as db, db.transaction() as tx:
            return canvas.replace(
                tx,
                owner_id=owner,
                conversation_id=chat,
                component_id="result",
                publication_id=publication,
                committed_render_revision=1,
                expected_updated_at=1,
                updated_at=2,
                payload={"type": "text", "text": "changed after review"},
                component_type="text",
                title=None,
            )

    with ThreadPoolExecutor(max_workers=1) as pool, database.transaction() as tx:
        publications.validate_stage(
            tx, owner_id=owner, conversation_id=chat, publication_id=publication
        )
        assert pool.submit(edit).result(5).payload["text"] == "changed after review"
        assert canvas.list_scoped(
            tx,
            owner_id=owner,
            conversation_id=chat,
            publication_id=publication,
            committed_render_revision=1,
            expected_base_render_revision=0,
        )[0].payload["text"] == ("changed after review")
