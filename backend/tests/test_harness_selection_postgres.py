"""Current signed owner requests control durable harness selection with CAS and atomic audit.
Unqualified deployment, retired session, outage and flag changes preserve the exact stored choice.
"""

import pytest

from astralplane.repositories.harness_bindings import HarnessProfileIdentity
from orchestrator.harness.production_profiles import HarnessSelectionError
from orchestrator.harness.selection_service import HarnessSelectionService
from orchestrator.human_request_authority import authenticate_current_human_request
from persistent_agents.models import AssignmentError
from tests.test_human_request_authority_088 import human as human
from tests.test_work_control_authority_088 import (
    bound as bound, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key, incoming,
)
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record

pytestmark = pytest.mark.asyncio


@pytest.fixture
def harness(human):
    service = HarnessSelectionService(human[2])
    human[2].harness_selection = service
    return service


async def caller(human, bound, fixture, **kwargs):
    return await authenticate_current_human_request(
        incoming(bound, fixture, **kwargs), boundary=human[1])


async def test_default_selection_reset_is_strict_owner_cas_and_restart_safe(
        harness, human, bound, fixture, runtime):
    current = await caller(human, bound, fixture)
    value = await harness.status(current)
    assert value["selection"] == {"harness_id": None, "revision": 0}
    assert value["profiles"][0]["available"] is False
    reset = await harness.select(current, harness_id=None, expected_revision=0)
    assert reset["selection"] == {"harness_id": None, "revision": 1}
    restarted = HarnessSelectionService(human[2])
    human[2].harness_selection = restarted
    assert (await restarted.status(current))["selection"] == reset["selection"]
    with pytest.raises(HarnessSelectionError, match="harness_selection_conflict"):
        await restarted.select(current, harness_id=None, expected_revision=0)
    with runtime.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM audit_events WHERE actor_user_id=%s "
            "AND action_type='harness.selection'", (fixture[1],))["n"] == 1


async def test_unqualified_local_selection_never_writes_or_calls_provider(
        harness, human, bound, fixture, runtime, monkeypatch):
    current = await caller(human, bound, fixture)
    monkeypatch.setattr(harness, "enabled", lambda: True)
    with pytest.raises(HarnessSelectionError, match="harness_qualification_missing"):
        await harness.select(current, harness_id="standard_local", expected_revision=0)
    assert (await harness.status(current))["selection"]["revision"] == 0
    with runtime.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM audit_events WHERE actor_user_id=%s "
            "AND action_type='harness.selection'", (fixture[1],))["n"] == 0


async def test_retired_session_cannot_reset_a_stored_selection(harness, human, bound, fixture, runtime):
    current = await caller(human, bound, fixture)
    old = get_session_record(runtime, fixture[2])
    replace_session_record(runtime, old)
    with pytest.raises(AssignmentError, match="human_authentication_required"):
        await harness.select(current, harness_id=None, expected_revision=0)
    with runtime.transaction() as tx:
        assert runtime.repositories.preferences.harness.get(tx, owner_id=fixture[1]).revision == 0


async def test_flag_off_denies_new_selected_work_but_default_reset_remains_possible(
        harness, human, bound, fixture, runtime):
    with runtime.transaction() as tx:
        selected = runtime.repositories.preferences.harness.compare_and_set(tx, owner_id=fixture[1],
            expected_revision=0, identity=HarnessProfileIdentity("standard_local", "1.0.0", "a" * 64),
            qualification_digest="b" * 64)
    with pytest.raises(HarnessSelectionError, match="harness_selection_disabled"):
        harness.prepare_sync(fixture[1])
    current = await caller(human, bound, fixture)
    reset = await harness.select(current, harness_id=None, expected_revision=selected.revision)
    assert reset["selection"]["harness_id"] is None
    prepared = harness.prepare_sync(fixture[1])
    assert prepared.capture is None and prepared.selection.revision == reset["selection"]["revision"]
