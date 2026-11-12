"""Checks verification owner propagation against the real fixture JWT verifier.
The in-process driver must use the same issuer-scoped owner for state and socket authority.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from verification.drivers.fixture_identity import FixtureIdentity
from verification.drivers.in_process import InProcessDriver
from verification.isolation import Principal


@pytest.fixture
def driver(tmp_path):
    value = InProcessDriver(SimpleNamespace(run_dir=str(tmp_path), run_id="__verif__owners"))
    value._fixture_identity = FixtureIdentity(value.config.run_id)
    value.orch = SimpleNamespace(
        _llm_store=None, ui_sessions={}, ui_clients=[], _ws_active_chat={},
        _new_connection_context=lambda ws: SimpleNamespace(websocket=ws),
    )
    try:
        yield value
    finally:
        value._fixture_identity.close()


def test_state_owner_matches_authenticated_socket_owner(driver):
    from orchestrator.auth import verify_production_token, verify_user
    from shared.node_identity import canonical_owner_id

    logical = Principal("__verif__owners_primary")
    principal = driver._execution_principal(logical)

    async def exercise():
        ws = await driver._register_session(principal)
        session = driver.orch.ui_sessions[ws]
        verified = await verify_user(await verify_production_token(session["_raw_token"]))
        assert session == verified | {"_raw_token": session["_raw_token"]}
        assert principal.user_id == verified["sub"]
        assert principal.user_id == canonical_owner_id(
            driver._fixture_identity.authority, verified["astral_idp_sub"],
        )
        assert verified["astral_idp_sub"].startswith("__verif__owners_primary_exec_")
        assert driver._execution_principal(principal) is principal

    asyncio.run(exercise())


def test_foreign_or_changed_principals_do_not_gain_owner_mapping(driver):
    driver._execution_principal(Principal("__verif__owners_primary"))
    with pytest.raises(ValueError, match="namespace"):
        driver._execution_principal(Principal("oidc.foreign.__verif__owners_primary"))
    with pytest.raises(RuntimeError, match="roles changed"):
        driver._execution_principal(Principal("__verif__owners_primary", ["admin"]))


def test_long_subject_preserves_canonical_blob_owner_limit(driver):
    principal = driver._execution_principal(Principal("__verif__owners_" + "x" * 300))
    assert principal.user_id.startswith("oidc.")
    assert len(principal.user_id) == 255


def test_owner_mapping_requires_active_fixture_issuer(tmp_path):
    value = InProcessDriver(SimpleNamespace(run_dir=str(tmp_path), run_id="__verif__owners"))
    with pytest.raises(RuntimeError, match="fixture identity"):
        value._execution_principal(Principal("__verif__owners_primary"))


def test_closed_fixture_cannot_reuse_owner_mapping(driver):
    logical = Principal("__verif__owners_primary")
    driver._execution_principal(logical)
    driver._fixture_identity.close()
    with pytest.raises(RuntimeError, match="active fixture identity"):
        driver._execution_principal(logical)


def test_unstorable_namespace_is_refused_before_owner_mapping(driver):
    driver.config.run_id = "__verif__" + "x" * 230
    with pytest.raises(ValueError, match="namespace is too long"):
        driver._execution_principal(Principal(driver.config.run_id + "_primary"))
    assert driver._execution_principals == {}


def test_session_rejects_subject_mapping_that_disagrees_with_owner(driver):
    principal = driver._execution_principal(Principal("__verif__owners_primary"))
    driver._execution_subjects[principal.user_id] = "__verif__owners_other"
    with pytest.raises(RuntimeError, match="authenticated owner differs"):
        asyncio.run(driver._register_session(principal))
    assert driver.orch.ui_sessions == {}


class _Runtime:
    def __init__(self):
        self.transaction_calls = 0

    def transaction(self):
        from contextlib import nullcontext

        self.transaction_calls += 1
        return nullcontext(object())


def test_cleanup_refuses_plane_without_issuer_scoping_before_transaction():
    from verification.isolation import teardown

    class LegacyCleanup:
        def purge_run(self, transaction, *, profile, run_id):
            raise AssertionError("legacy cleanup must not receive canonical owners")

    runtime = _Runtime()
    with pytest.raises(RuntimeError, match="issuer-scoped verification cleanup"):
        teardown(
            plane_runtime=runtime,
            plane_repositories=SimpleNamespace(harness_cleanup=LegacyCleanup()),
            run_id="__verif__owners", owner_issuer_hash="0123456789abcdef",
        )
    assert runtime.transaction_calls == 0


def test_cleanup_forwards_exact_fixture_issuer_to_plane():
    from astralplane.repositories.harness_cleanup import HarnessCleanupProfile
    from verification.isolation import teardown

    observed = []

    class ScopedCleanup:
        def purge_run(self, transaction, *, profile, run_id, owner_issuer_hash=None):
            observed.append((profile, run_id, owner_issuer_hash))
            return SimpleNamespace(total_deleted=7)

    runtime = _Runtime()
    assert teardown(
        plane_runtime=runtime,
        plane_repositories=SimpleNamespace(harness_cleanup=ScopedCleanup()),
        run_id="__verif__owners", owner_issuer_hash="0123456789abcdef",
    ) == 7
    assert observed == [(HarnessCleanupProfile.VERIFICATION, "__verif__owners", "0123456789abcdef")]
    assert runtime.transaction_calls == 1


def test_setup_refuses_legacy_cleanup_before_fixture_writes(tmp_path, monkeypatch):
    from orchestrator import orchestrator as orchestrator_module

    class LegacyCleanup:
        def purge_run(self, transaction, *, profile, run_id):
            raise AssertionError("cleanup is unavailable")

    events = []

    async def close():
        events.append("closed")

    runtime = _Runtime()
    graph = SimpleNamespace(
        _close_started_services=close,
        runtime_composition=SimpleNamespace(plane=SimpleNamespace(
            runtime=runtime,
            repositories=SimpleNamespace(harness_cleanup=LegacyCleanup()), blobs=object(),
        )),
    )
    monkeypatch.setattr(orchestrator_module, "Orchestrator", lambda: graph)
    value = InProcessDriver(SimpleNamespace(
        run_dir=str(tmp_path), run_id="__verif__owners", mode="in_process",
    ))
    value._register_general_agent = lambda: events.append("registered")
    with pytest.raises(RuntimeError, match="issuer-scoped verification cleanup"):
        asyncio.run(value.setup())
    assert events == ["closed"]
    assert not (tmp_path / "fixtures").exists()
    assert runtime.transaction_calls == 0
    assert value.orch is None
    assert value._fixture_identity.closed
