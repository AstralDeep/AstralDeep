"""Exercises publication races and journal/audit rollback using isolated real Plane databases and independent pools."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event
import time
from uuid import uuid4

from psycopg2.extensions import make_dsn

import pytest

from audit.repository import AuditRepository
from orchestrator.a2a_publication_store import PlaneA2APublicationStore
from shared.a2a_publication import (
    A2APublicationError, A2APublicationRegistry, ConfirmationMismatchError,
    PublicationForbiddenError, PublicationStoreUnavailable,
)
from tests.helpers.voice_plane_runtime import isolated_plane_runtime, VoicePlaneTestRuntime, build_test_database_url


@pytest.fixture
def publication_workers(monkeypatch):
    monkeypatch.setenv('AUDIT_HMAC_SECRET', 'publication-regression-key-with-32-characters')
    monkeypatch.setenv('AUDIT_HMAC_KEY_ID', 'publication_regression')
    with isolated_plane_runtime('ad_test_a2a') as first:
        second = VoicePlaneTestRuntime.__new__(VoicePlaneTestRuntime)
        connection = first._driver_pool.getconn()
        try:
            dsn = make_dsn(build_test_database_url(), dbname=connection.info.dbname)
        finally:
            first._driver_pool.putconn(connection)
        second._connect(dsn)
        try:
            actor = 'publication-' + uuid4().hex
            workers = []
            for runtime in (first, second):
                audit = AuditRepository(plane_runtime=runtime, plane_repositories=runtime.repositories)
                store = PlaneA2APublicationStore(runtime=runtime, repositories=runtime.repositories, audit=audit)
                registry = A2APublicationRegistry(catalog=lambda: ('weather-1',),
                    audiences=lambda _: ('tools:read', 'tools:write'), store=store)
                workers.append((registry, store, audit, runtime))
            yield actor, workers
        finally:
            second.close()


def proposal(registry, actor):
    return registry.create_intent(agent_id='weather-1', audiences=['tools:read'],
                                  lifetime='open', actor=actor)


def confirm(registry, intent, actor):
    return registry.confirm(intent_id=intent.intent_id,
                            confirmation_token=intent.confirmation_token, actor=actor)


@pytest.mark.parametrize('inactive', ['withdrawn', 'expired'])
def test_bounded_history_keeps_other_owner_consent_across_workers(publication_workers, inactive):
    from tests.test_a2a_publication import _Clock
    actor, workers = publication_workers
    clock = _Clock()
    for registry, _, _, _ in workers:
        registry._catalog = lambda: ('weather-1', 'web-research-1')
        registry._max_inventory = 2
        registry._time_fn = clock
    first, _, audit, _ = workers[0]
    second, store, _, _ = workers[1]
    alice, bob = actor + '-alice', actor + '-bob'
    original = confirm(first, proposal(first, alice), alice)
    def publish_bob(lifetime):
        intent = second.create_intent(agent_id='web-research-1', audiences=['tools:read'],
                                      lifetime=lifetime, actor=bob, owner=bob)
        return confirm(second, intent, bob)
    historical = publish_bob('1h')
    if inactive == 'withdrawn':
        second.withdraw(publication_id=historical.publication_id, actor=bob)
    else:
        clock.advance(3601)
    replacement = publish_bob('open')
    restarted = A2APublicationRegistry(catalog=first._catalog, audiences=first._audiences,
        max_inventory=2, time_fn=clock, store=store)
    assert first.active() == restarted.active() == (original, replacement)
    assert restarted.get(historical.publication_id) is None
    assert restarted.inventory(actor=alice)[0]['publication_id'] == original.publication_id
    assert len(store.load()['publications']) == 2
    for owner in (alice, bob):
        assert audit.verify_chain(owner) is None


def test_capacity_denial_is_durable_atomic_and_recoverable(publication_workers, monkeypatch):
    actor, workers = publication_workers
    for registry, _, _, _ in workers:
        registry._catalog = lambda: ('weather-1', 'web-research-1', 'third')
        registry._max_inventory = 2
    first, store, audit, _ = workers[0]
    second = workers[1][0]
    original = confirm(first, proposal(first, actor), actor)
    def intent_for(agent_id):
        return second.create_intent(agent_id=agent_id, audiences=['tools:read'],
                                    lifetime='open', actor=actor)
    other = confirm(second, intent_for('web-research-1'), actor)
    intent = intent_for('third')
    before = store.load()
    with monkeypatch.context() as patch:
        patch.setattr(audit, 'insert_in_transaction', lambda *a, **k: None)
        with pytest.raises(PublicationStoreUnavailable):
            confirm(first, intent, actor)
    assert store.load() == before
    assert second.get_intent(intent.intent_id) == intent
    with pytest.raises(A2APublicationError, match='capacity'):
        confirm(first, intent, actor)
    assert second.get_intent(intent.intent_id) is None
    assert second.active() == (original, other)
    with pytest.raises(A2APublicationError):
        confirm(second, intent, actor)
    rows, _ = audit.list_for_user(actor)
    assert [row.action_type for row in rows].count('a2a_publication.published') == 2
    assert [row.action_type for row in rows].count('a2a_publication.confirm_denied') == 1
    first.withdraw(publication_id=other.publication_id, actor=actor)
    replacement = confirm(second, intent_for('third'), actor)
    assert first.active() == (original, replacement)
    assert len(store.load()['publications']) == 2
    assert audit.verify_chain(actor) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['rest', 'card', 'a2a'])
async def test_requests_progress_while_real_plane_journal_is_locked(publication_workers, monkeypatch, route):
    import httpx
    from fastapi import FastAPI
    from orchestrator.a2a_orchestrator_executor import setup_orchestrator_a2a
    from orchestrator.a2a_publication_api import publication_router
    from tests.test_a2a_publication import _auth, _orchestrator
    monkeypatch.setenv('USE_MOCK_AUTH', 'true')
    monkeypatch.setenv('KEYCLOAK_AUTHORITY', 'https://iam.example/realms/astral')
    monkeypatch.setenv('KEYCLOAK_CLIENT_ID', 'astral-frontend')
    actor, workers = publication_workers
    registry = workers[0][0]
    confirm(registry, proposal(registry, actor), actor)
    orch = _orchestrator()
    orch.a2a_publications = registry
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(publication_router)
    setup_orchestrator_a2a(app, orch)
    _, blocker_store, _, blocker_runtime = workers[1]
    held, release = Event(), Event()
    guarded_release = []
    def hold_journal():
        with blocker_runtime.transaction() as transaction:
            blocker_store._record(transaction)
            held.set()
            if not release.wait(timeout=10):
                guarded_release.append(True)
    def observe_database_wait():
        deadline = time.monotonic() + 10
        while not release.is_set():
            blocked = blocker_runtime.fetch_one(
                'SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() '
                'AND cardinality(pg_blocking_pids(pid)) > 0) AS blocked')['blocked']
            if blocked:
                return True
            if time.monotonic() >= deadline:
                return False
            Event().wait(0.01)
        return False
    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_journal)
        assert await asyncio.to_thread(held.wait, 10)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            if route == 'rest':
                request = client.get('/api/a2a/publications', headers=_auth())
            elif route == 'card':
                request = client.get('/a2a/.well-known/agent-card.json')
            else:
                request = client.post('/a2a', headers={**_auth(), 'A2A-Version': '1.0'}, json={
                    'jsonrpc': '2.0', 'id': '1', 'method': 'SendMessage', 'params': {'message': {
                        'message_id': 'm-1', 'role': 'ROLE_USER', 'parts': [{'data': {'method': 'tools/list'}}]}}})
            task = asyncio.create_task(request)
            try:
                assert await asyncio.to_thread(observe_database_wait)
                await asyncio.sleep(0)
                assert not task.done()
                assert not guarded_release
            finally:
                release.set()
            response = await task
        holder.result(timeout=10)
    assert response.status_code == 200


def test_independent_workers_race_single_use_and_observe_withdrawal(publication_workers):
    actor, workers = publication_workers
    first, _, audit, _ = workers[0]
    second = workers[1][0]
    intent = proposal(first, actor)
    barrier = Barrier(2)
    def race(registry):
        barrier.wait(timeout=5)
        try:
            return confirm(registry, intent, actor)
        except A2APublicationError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, (first, second)))
    winners = [result for result in results if not isinstance(result, Exception)]
    assert len(winners) == 1
    assert first.get_intent(intent.intent_id) is None
    assert len(second.active()) == 1
    first.withdraw(publication_id=winners[0].publication_id, actor=actor)
    assert second.active() == ()
    assert audit.verify_chain(actor) is None
    rows, _ = audit.list_for_user(actor)
    assert sorted(row.action_type for row in rows) == [
        'a2a_publication.intent_created', 'a2a_publication.published', 'a2a_publication.withdrawn']


@pytest.mark.parametrize('failure', ['append', 'signer', 'dto', 'receipt', 'commit'])
def test_audit_and_commit_failure_rollback_then_retry(publication_workers, monkeypatch, failure):
    from audit import repository as module
    actor, workers = publication_workers
    registry, store, audit, runtime = workers[0]
    intent = proposal(registry, actor)
    before = store.load()
    def fail(*args, **kwargs):
        raise RuntimeError('injected durable failure')
    with monkeypatch.context() as patch:
        if failure == 'append':
            patch.setattr(audit, 'insert_in_transaction', fail)
        elif failure == 'signer':
            patch.setattr(module, '_authenticate', fail)
        elif failure == 'dto':
            patch.setattr(module, '_record_to_dto', fail)
        elif failure == 'receipt':
            original = audit.insert_in_transaction
            def no_receipt(*args, **kwargs):
                original(*args, **kwargs)
                return None
            patch.setattr(audit, 'insert_in_transaction', no_receipt)
        else:
            original = runtime.transaction
            @contextmanager
            def abort_commit(**kwargs):
                with original(**kwargs) as transaction:
                    yield transaction
                    connection = transaction._connection
                    class CommitFailure:
                        def __getattr__(self, name):
                            return getattr(connection, name)
                        @property
                        def autocommit(self):
                            return connection.autocommit
                        @autocommit.setter
                        def autocommit(self, value):
                            connection.autocommit = value
                        def commit(self):
                            raise RuntimeError('commit refused')
                    transaction._connection = CommitFailure()
            original_save = store.save
            def aborting_save(*args, **kwargs):
                with monkeypatch.context() as local:
                    local.setattr(runtime, 'transaction', abort_commit)
                    return original_save(*args, **kwargs)
            patch.setattr(store, 'save', aborting_save)
        with pytest.raises(PublicationStoreUnavailable):
            confirm(registry, intent, actor)
    assert store.load() == before
    assert registry.get_intent(intent.intent_id) == intent
    rows, _ = audit.list_for_user(actor)
    assert [row.action_type for row in rows] == ['a2a_publication.intent_created']
    record = confirm(registry, intent, actor)
    assert workers[1][0].get(record.publication_id) == record
    assert audit.verify_chain(actor) is None


def test_denial_consumption_is_audited_and_rollback_preserves_retry(publication_workers, monkeypatch):
    actor, workers = publication_workers
    registry, _, audit, _ = workers[0]
    intent = proposal(registry, actor)
    original = audit.insert_in_transaction
    with monkeypatch.context() as patch:
        patch.setattr(audit, 'insert_in_transaction', lambda *a, **k: None)
        with pytest.raises(PublicationStoreUnavailable):
            registry.confirm(intent_id=intent.intent_id, confirmation_token='wrong', actor=actor)
    assert registry.get_intent(intent.intent_id) == intent
    assert audit.insert_in_transaction == original
    with pytest.raises(ConfirmationMismatchError):
        registry.confirm(intent_id=intent.intent_id, confirmation_token='wrong', actor=actor)
    assert workers[1][0].get_intent(intent.intent_id) is None
    assert audit.verify_chain(actor) is None
    assert {row.action_type for row in audit.list_for_user(actor)[0]} == {
        'a2a_publication.intent_created', 'a2a_publication.confirm_denied'}


def test_owner_recheck_inside_transaction_refuses_changed_authority(publication_workers):
    actor, workers = publication_workers
    registry, store, audit, _ = workers[0]
    intent = proposal(registry, actor)
    before = store.load()
    with pytest.raises(PublicationForbiddenError):
        registry.confirm(intent_id=intent.intent_id, confirmation_token=intent.confirmation_token,
                         actor=actor, authorize=lambda agent: False)
    assert store.load() == before
    assert workers[1][0].active() == ()
    assert len(audit.list_for_user(actor)[0]) == 1


def test_withdrawal_audit_failure_preserves_durable_publication(publication_workers, monkeypatch):
    actor, workers = publication_workers
    registry, store, audit, _ = workers[0]
    record = confirm(registry, proposal(registry, actor), actor)
    before = store.load()
    with monkeypatch.context() as patch:
        patch.setattr(audit, 'insert_in_transaction', lambda *a, **k: None)
        with pytest.raises(PublicationStoreUnavailable):
            registry.withdraw(publication_id=record.publication_id, actor=actor)
    assert store.load() == before
    assert workers[1][0].is_published('weather-1')
    assert len(audit.list_for_user(actor)[0]) == 2


def test_two_distinct_intents_race_leaves_one_active_and_audits_denial(publication_workers):
    actor, workers = publication_workers
    first, _, audit, _ = workers[0]
    second = workers[1][0]
    intents = [proposal(first, actor), proposal(second, actor)]
    barrier = Barrier(2)
    def race(pair):
        registry, intent = pair
        barrier.wait(timeout=5)
        try:
            return confirm(registry, intent, actor)
        except A2APublicationError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, zip((first, second), intents)))
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert len(first.active()) == 1
    assert first.pending_intents() == ()
    assert audit.verify_chain(actor) is None
    actions = [row.action_type for row in audit.list_for_user(actor)[0]]
    assert actions.count('a2a_publication.published') == 1
    assert actions.count('a2a_publication.confirm_denied') == 1


def test_unavailable_signing_key_denies_both_worker_reads(publication_workers, monkeypatch):
    actor, workers = publication_workers
    first = workers[0][0]
    confirm(first, proposal(first, actor), actor)
    second = workers[1][0]
    assert second.is_published('weather-1')
    monkeypatch.delenv('AUDIT_HMAC_SECRET')
    assert first.active() == ()
    assert second.active() == ()
    assert not second.available


def test_durable_invocation_denial_uses_normal_chain(publication_workers):
    actor, workers = publication_workers
    registry, _, audit, _ = workers[0]
    registry.record_event({'action': 'invocation_denied', 'actor': actor, 'agent_id': 'weather-1'})
    assert audit.verify_chain(actor) is None
    assert audit.list_for_user(actor)[0][0].action_type == 'a2a_publication.invocation_denied'


def test_commit_acknowledgement_loss_cannot_duplicate_publication(publication_workers, monkeypatch):
    actor, workers = publication_workers
    registry, store, audit, runtime = workers[0]
    intent = proposal(registry, actor)
    original = runtime.transaction
    @contextmanager
    def lost_ack(**kwargs):
        with original(**kwargs) as transaction:
            yield transaction
            connection = transaction._connection
            class LostAcknowledgement:
                def __getattr__(self, name):
                    return getattr(connection, name)
                @property
                def autocommit(self):
                    return connection.autocommit
                @autocommit.setter
                def autocommit(self, value):
                    connection.autocommit = value
                def commit(self):
                    connection.commit()
                    raise RuntimeError('commit acknowledgement lost')
            transaction._connection = LostAcknowledgement()
    original_save = store.save
    def uncertain_save(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(runtime, 'transaction', lost_ack)
            return original_save(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(store, 'save', uncertain_save)
        with pytest.raises(PublicationStoreUnavailable):
            confirm(registry, intent, actor)
    with pytest.raises(A2APublicationError):
        confirm(registry, intent, actor)
    assert len(workers[1][0].active()) == 1
    assert audit.verify_chain(actor) is None
    assert [row.action_type for row in audit.list_for_user(actor)[0]].count('a2a_publication.published') == 1


def test_production_wiring_uses_bound_durable_audit(publication_workers):
    from orchestrator.a2a_publication_wiring import attach_publication_registry
    from tests.test_a2a_publication import _orchestrator
    actor, workers = publication_workers
    _, _, audit, runtime = workers[0]
    orch = _orchestrator()
    del orch.a2a_publications
    orch.plane_repository_source = runtime
    orch.audit_repo = audit
    registry = attach_publication_registry(orch)
    confirm(registry, proposal(registry, actor), actor)
    assert workers[1][0].is_published('weather-1')
    assert audit.verify_chain(actor) is None


def test_store_rejects_missing_binding_and_invalid_row(publication_workers, monkeypatch):
    _, workers = publication_workers
    _, store, _, runtime = workers[0]
    with monkeypatch.context() as patch:
        patch.setattr(store, '_audit', None)
        with pytest.raises(PublicationStoreUnavailable):
            store.load()
    with monkeypatch.context() as patch:
        patch.setattr(runtime.repositories.workspaces.canvas, 'get_current_by_row_id', lambda *a, **k: None)
        with pytest.raises(PublicationStoreUnavailable):
            store.load()


def test_store_version_and_repository_conflicts_are_explicit(publication_workers, monkeypatch):
    from astralplane.errors import RepositoryConflictError
    from shared.a2a_publication import PublicationStoreConflict
    actor, workers = publication_workers
    _, store, _, runtime = workers[0]
    state = dict(store.load())
    event = {'actor': actor, 'action': 'intent_created', 'agent_id': 'weather-1'}
    for version in (True, '1', 2):
        with pytest.raises(PublicationStoreConflict):
            store.save({**state, 'version': version}, 0, event)
    def conflict(*args, **kwargs):
        raise RepositoryConflictError('changed')
    monkeypatch.setattr(runtime.repositories.workspaces.canvas, 'replace', conflict)
    with pytest.raises(PublicationStoreConflict):
        store.save({**state, 'version': 1}, 0, event)


def test_expiry_while_waiting_to_commit_is_rechecked_and_consumed(publication_workers, monkeypatch):
    from shared.a2a_publication import StaleIntentError
    actor, workers = publication_workers
    registry, store, audit, _ = workers[0]
    clock = [1000.0]
    registry._time_fn = lambda: clock[0]
    intent = proposal(registry, actor)
    original_save = store.save
    def delayed_save(*args, **kwargs):
        clock[0] = intent.expires_at
        return original_save(*args, **kwargs)
    monkeypatch.setattr(store, 'save', delayed_save)
    with pytest.raises(StaleIntentError):
        confirm(registry, intent, actor)
    assert registry.get_intent(intent.intent_id) is None
    assert registry.active() == ()
    assert audit.list_for_user(actor)[0][0].action_type == 'a2a_publication.confirm_stale'


def test_catalog_loss_at_commit_retries_and_audits_consumption(publication_workers, monkeypatch):
    from shared.a2a_publication import NotEligibleError
    actor, workers = publication_workers
    registry, store, audit, _ = workers[0]
    intent = proposal(registry, actor)
    original_save = store.save
    def changed_catalog(*args, **kwargs):
        registry._catalog = lambda: ()
        return original_save(*args, **kwargs)
    monkeypatch.setattr(store, 'save', changed_catalog)
    with pytest.raises(NotEligibleError):
        confirm(registry, intent, actor)
    assert registry.get_intent(intent.intent_id) is None
    assert registry.active() == ()
    assert audit.list_for_user(actor)[0][0].action_type == 'a2a_publication.confirm_denied'
