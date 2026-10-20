"""Checks isolated completed-result clones against real PostgreSQL receipts and audit chains.
Exercises fresh reader authority, independent mutations and cleanup around the shared authenticated producer.
"""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.helpers.completed_result import completed_reader
from tests.helpers.plane_template import client_connections, engine_clone, populated_engine_template
from tests.helpers.session_plane_runtime import get_session_record
from tests.test_lets_gate_ordering import gate_orchestrator
from tests.test_request_session_authority_088 import session_fixture, signing_key as signing_key
from tests.test_work_result_postgres_088 import _mutate_action, _mutate_record, project, unavailable


@asynccontextmanager
async def reader(seed, signing_key, directory):
    with engine_clone(seed.template) as (runtime, database), pytest.MonkeyPatch.context() as patch:
        session = session_fixture(runtime, patch, signing_key, owner=seed.owner)
        async with completed_reader(runtime, session, gate_orchestrator.__wrapped__(patch)[0],
                                    patch, directory, seed) as op:
            yield SimpleNamespace(op=op, database=database, session=session)


@pytest.mark.asyncio
async def test_populated_result_clones_keep_receipts_sessions_and_mutations_isolated(
    completed_seed, signing_key, tmp_path,
):
    seed = completed_seed
    assert client_connections(seed.template.server_dsn, seed.template.name) == 0
    async with reader(seed, signing_key, tmp_path / "left") as left:
        async with reader(seed, signing_key, tmp_path / "right") as right:
            a, b = left.op, right.op
            assert left.database != right.database
            assert a.runtime is not b.runtime
            assert a.executor.store is not b.executor.store
            assert a.executor.service is not b.executor.service
            assert a.sessions is not b.sessions and a.sid != b.sid
            assert a.sid != a.original_sid and b.sid != b.original_sid
            assert a.ledger == b.ledger
            assert len(a.ledger) == 3
            assert a.model.state == a.source.state == "succeeded"
            assert len(a.model.attempts) == len(a.source.attempts) == 1
            assert a.completed.usage["spent"]["tokens"] == 120
            assert a.completed.usage["spent"]["tool_calls"] == 2
            value = await project(a)
            assert value == await project(b)
            assert value["content"]["passages"] == [{"id": "p001", "text": "Public release 088"}]
            assert len(a.seed_observations["model_calls"]) == 1
            assert len(a.seed_observations["physical"]) == 2
            with pytest.raises(TypeError):
                a.seed_observations["model_calls"][0][2]["api_key"] = "changed"
            a.model_calls.append("local-observer")
            assert b.model_calls == []
            a.model_calls.clear()
            await _mutate_action(a, a.model, lambda data: data["result"].update(result_digest="0" * 64))
            unavailable(await project(a))
            assert await project(b) == value
            await _mutate_record(a, lambda data: data["checkpoint"].update(private="local"))
            a.sessions.delete(a.sid)
            a.sessions.delete(a.original_sid)
            assert get_session_record(b.runtime, b.sid) is not None
            assert get_session_record(b.runtime, b.original_sid) is not None
            assert await project(b) == value
            assert b.audit._repo.verify_chain(b.owner) is None
        async with reader(seed, signing_key, tmp_path / "later") as later:
            assert later.database != left.database
            assert await project(later.op) == value
            assert later.op.ledger == b.ledger
            assert later.op.audit._repo.verify_chain(later.op.owner) is None
    assert client_connections(seed.template.server_dsn, seed.template.name) == 0


def test_populated_seed_closes_runtime_and_drops_database_when_producer_fails(plane_template):
    observed = {}

    def failed(runtime):
        observed["runtime"] = runtime
        with runtime.transaction() as tx:
            observed["database"] = tx.fetch_one("SELECT current_database() AS name")["name"]
        raise ValueError("synthetic producer failure")

    with pytest.raises(ValueError, match="producer failure"):
        with populated_engine_template(plane_template, failed):
            pytest.fail("failed producer exposed a clone")
    assert client_connections(plane_template.server_dsn, observed["database"]) == 0
    with pytest.raises(Exception):
        with observed["runtime"].transaction():
            pytest.fail("failed producer left an open runtime")
    from tests.helpers.plane_template import DatabaseCreationError
    with pytest.raises(DatabaseCreationError):
        with engine_clone(replace(plane_template, name=observed["database"])):
            pytest.fail("failed producer database was retained")
