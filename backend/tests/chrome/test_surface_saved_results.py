"""Owner-scoped saved-results host adapter over an actual committed receipt.

Only external IAM/JWKS, source and model replies are synthetic. The Save
(propose+save) that produces the receipt this surface reads runs the real,
registered ``/api/work/v1`` router against real PostgreSQL, exactly as
``tests/test_work_save_postgres_088.py`` verifies independently.
"""
import json
from uuid import uuid4

import pytest

from shared.feature_flags import flags

from orchestrator.projection_surfaces import saved_results
from tests.test_work_save_postgres_088 import (
    approval_for,
    completed as completed,
    fixture as fixture,
    gate_orchestrator as gate_orchestrator,
    operation as operation,
    path,
    plane as plane,
    post,
    proposal_body,
    research as research,
    runtime as runtime,
    save_path,
    saved as saved,
    signing_key as signing_key,
)

pytestmark = [pytest.mark.asyncio,
              pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)]


async def _committed(saved_value, monkeypatch):
    """Propose then Save through the real HTTP routes; return the publication id."""
    monkeypatch.setitem(flags._flags, "persistent_agents", True)
    # The saved-result detail offers the result's own existing authenticated
    # canvas download, which _export_link gates on the artifact_export flag
    # (feature 028); enable it so the detail exposes /api/export/canvas/.
    monkeypatch.setitem(flags._flags, "artifact_export", True)
    command = proposal_body(saved_value)
    proposed = await post(saved_value, path(saved_value), command)
    assert proposed.status_code == 200, proposed.text
    review = proposed.json()
    accepted = await post(saved_value, save_path(saved_value, command), approval_for(review))
    assert accepted.status_code == 200, accepted.text
    return command["publication_id"]


async def test_empty_owner_sees_no_receipts_before_any_save(saved, monkeypatch):
    monkeypatch.setitem(flags._flags, "persistent_agents", True)
    orch = saved.op.executor.orch
    listing = await saved_results.render(orch, saved.op.owner, [], {"mode": "list"})
    assert "No saved results yet" in listing
    native = await saved_results.components(orch, saved.op.owner, [], {"mode": "list"})
    assert "No saved results yet" in json.dumps(native)


async def test_disabled_feature_denies_the_surface_before_any_save(saved, monkeypatch):
    monkeypatch.setitem(flags._flags, "persistent_agents", False)
    orch = saved.op.executor.orch
    listing = await saved_results.render(orch, saved.op.owner, [], {"mode": "list"})
    assert "denied" in listing.lower()


async def test_owner_sees_the_committed_result_in_list_and_detail(saved, monkeypatch):
    publication_id = await _committed(saved, monkeypatch)
    orch = saved.op.executor.orch

    listing = await saved_results.render(orch, saved.op.owner, [], {"mode": "list"})
    assert "Reviewed result destination" in listing
    assert publication_id in listing

    detail = await saved_results.render(
        orch, saved.op.owner, [], {"mode": "detail", "publication_id": publication_id})
    assert publication_id in detail
    assert "Reviewed result destination" in detail
    # Provenance re-derived from the actual public research result.
    assert "93.184.216.34" in detail
    # The result's own existing authenticated download, never a new route.
    assert "/api/export/canvas/" in detail
    # Never the private excerpt/system material the save-flow tests already pin.
    assert "synthetic-provider-key" not in detail and "binding_key" not in detail


async def test_owner_sees_the_committed_result_natively(saved, monkeypatch):
    publication_id = await _committed(saved, monkeypatch)
    orch = saved.op.executor.orch
    components = await saved_results.components(orch, saved.op.owner, [], {"mode": "list"})
    encoded = json.dumps(components)
    assert publication_id in encoded
    assert "synthetic-provider-key" not in encoded


async def test_foreign_owner_sees_no_receipts(saved, monkeypatch):
    await _committed(saved, monkeypatch)
    orch = saved.op.executor.orch
    listing = await saved_results.render(orch, "someone-else", [], {"mode": "list"})
    assert "No saved results yet" in listing
    assert "Reviewed result destination" not in listing


async def test_unknown_publication_id_is_a_safe_empty_detail(saved, monkeypatch):
    await _committed(saved, monkeypatch)
    orch = saved.op.executor.orch
    detail = await saved_results.render(
        orch, saved.op.owner, [], {"mode": "detail", "publication_id": str(uuid4())})
    assert "not available" in detail
    assert "Reviewed result destination" not in detail


async def test_feature_off_denies_the_surface(saved, monkeypatch):
    await _committed(saved, monkeypatch)
    monkeypatch.setitem(flags._flags, "persistent_agents", False)
    orch = saved.op.executor.orch
    listing = await saved_results.render(orch, saved.op.owner, [], {"mode": "list"})
    assert "denied" in listing.lower()
    assert "Reviewed result destination" not in listing
