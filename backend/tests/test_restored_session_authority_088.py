"""Real private PostgreSQL/public Plane recovery with Deep's encrypted grants.

No application process is restarted here and no institutional OAuth is performed.
The isolated database is the only retirement target; operational closure and
paired production backup verification remain separate qualification.
"""

import asyncio
from dataclasses import replace

import psycopg2.extensions
import pytest
from astralplane import retire_restored_sessions
from astralplane.database.migrations import CURRENT_DATA_PLANE_REVISION

from orchestrator import offline_grant as grants_module
from tests.helpers.session_consent_088 import consent_from_store
from tests.helpers.session_plane_runtime import (
    get_session_record,
    isolated_plane_runtime,
)
from tests.helpers.voice_plane_runtime import build_test_database_url
from tests.test_session_incarnation_grants_088 import fixture as fixture


@pytest.fixture
def runtime():
    with isolated_plane_runtime("restore_grants088") as value:
        yield value


def retire(runtime):
    identity = runtime.fetch_one(
        "SELECT current_database() AS database, current_schema() AS schema"
    )
    parameters = psycopg2.extensions.parse_dsn(build_test_database_url())
    parameters["dbname"] = identity["database"]
    return retire_restored_sessions(
        database_url=psycopg2.extensions.make_dsn(**parameters),
        expected_database=identity["database"],
        expected_schema=identity["schema"],
        expected_schema_revision=CURRENT_DATA_PLANE_REVISION.schema_revision,
        expected_migration_digest=CURRENT_DATA_PLANE_REVISION.migration_digest,
    )


@pytest.mark.parametrize("recreate", [False, True])
def test_retired_restore_cannot_reuse_original_consent_or_encrypted_grant(
    fixture, runtime, recreate
):
    sessions, grants, owner, sid = fixture
    selected = consent_from_store(sessions, owner, sid)
    grant_id = grants.capture(owner, selected)
    original_grant = grants._grant(owner, grant_id)
    old = get_session_record(runtime, sid)
    result = retire(runtime)
    assert result.retired_sessions == 1
    assert get_session_record(runtime, sid) is None
    # This old process deliberately remains present in the test. Its private
    # cache cannot revive durable grant authority; operators must still discard
    # every process before reopening ordinary cookie authentication.
    if recreate:
        with runtime.transaction() as transaction:
            replacement = runtime.repositories.history.sessions.put(
                transaction, replace(old, incarnation_id=None)
            )
        assert replacement.incarnation_id != old.incarnation_id
        assert replace(old, incarnation_id=replacement.incarnation_id) == replacement
    with pytest.raises(grants_module.OfflineGrantError, match="re-consent"):
        grants.capture(owner, selected)
    with pytest.raises(grants_module.OfflineGrantError, match="unavailable"):
        asyncio.run(grants.mint_access_token(grant_id, user_id=owner))
    assert grants._grant(owner, grant_id) == original_grant
    if recreate:
        assert get_session_record(runtime, sid) == replacement
        fresh_consent = consent_from_store(sessions, owner, sid)
        fresh_grant = grants.capture(owner, fresh_consent)
        assert fresh_grant != grant_id
        assert (
            grants._resolve_reference(grants._grant(owner, fresh_grant))[
                "incarnation_id"
            ]
            == replacement.incarnation_id
        )


def test_each_restored_snapshot_requires_a_new_retirement(fixture, runtime):
    sessions, grants, owner, sid = fixture
    selected = consent_from_store(sessions, owner, sid)
    grant_id = grants.capture(owner, selected)
    original_grant = grants._grant(owner, grant_id)
    original_session = get_session_record(runtime, sid)
    assert retire(runtime).retired_sessions == 1
    assert retire(runtime).retired_sessions == 0
    # A qualification-only snapshot replay preserves original UUID/ciphertext,
    # unlike ordinary session issuance. Never expose this SQL as an operator tool.
    runtime.execute(
        """INSERT INTO web_session
           (sid,user_id,access_token_enc,refresh_token_enc,interactive_anchor,
            hard_expires_at,last_refresh_at,resumed,created_at,incarnation_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::uuid)""",
        (
            original_session.session_id,
            original_session.owner_id,
            original_session.access_token_ciphertext,
            original_session.refresh_token_ciphertext,
            original_session.interactive_anchor,
            original_session.hard_expires_at,
            original_session.last_refresh_at,
            original_session.resumed,
            original_session.created_at,
            original_session.incarnation_id,
        ),
    )
    assert get_session_record(runtime, sid) == original_session
    assert retire(runtime).retired_sessions == 1
    assert grants._grant(owner, grant_id) == original_grant
    with pytest.raises(grants_module.OfflineGrantError):
        asyncio.run(grants.mint_access_token(grant_id, user_id=owner))
