"""Exercises late publication authority changes across the real audit lock and unresolved owner reads."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
import time
from uuid import uuid4

import pytest

from audit.schemas import AuditEventCreate
from shared.a2a_publication import NotEligibleError, PublicationForbiddenError, StaleIntentError
from tests.test_a2a_publication import (
    ADMIN, ALICE_EMAIL, _api_client, _auth, _client, _orchestrator, _send, _task, _tool_names,
)
from tests import test_a2a_publication_postgres

publication_workers = test_a2a_publication_postgres.publication_workers


@pytest.fixture
def mock_publication_auth(monkeypatch):
    monkeypatch.setenv('USE_MOCK_AUTH', 'true')
    monkeypatch.setenv('KEYCLOAK_AUTHORITY', 'https://iam.example/realms/astral')
    monkeypatch.setenv('KEYCLOAK_CLIENT_ID', 'astral-frontend')


def admin_auth():
    return _auth(ADMIN, 'admin@example.test', roles=('admin',))


def admin_proposal(client):
    return client.post('/api/a2a/publications/intents', headers=admin_auth(),
                       json={'agent_id': 'weather-1', 'audiences': ['tools:read'], 'lifetime': 'open'})


def admin_confirm(client, intent):
    return client.post(f"/api/a2a/publications/intents/{intent['intent_id']}/confirm", headers=admin_auth(),
                       json={'confirmation_token': intent['confirmation_token']})


@pytest.mark.parametrize('change', ['expiry', 'catalog', 'scope', 'authority', 'owner'])
def test_audit_lock_wait_revalidates_current_publication_decision(publication_workers, monkeypatch, change):
    actor, workers = publication_workers
    registry, store, audit, _ = workers[0]
    _, _, blocker_audit, blocker_runtime = workers[1]
    clock = [1000.0]
    registry._time_fn = lambda: clock[0]
    with blocker_runtime.transaction() as transaction:
        blocker_runtime.repositories.agents.upsert_ownership(transaction,
            agent_id='weather-1', owner_email=ALICE_EMAIL, is_public=False, observed_at=1000)
    def current_owner(agent_id):
        with blocker_runtime.transaction() as transaction:
            row = blocker_runtime.repositories.agents.get_ownership(transaction, agent_id=agent_id)
            return None if row is None else row.owner_email
    from orchestrator.a2a_publication_policy import A2APublicationPolicy
    policy = A2APublicationPolicy(_orchestrator(), registry,
        catalog=lambda: ('weather-1',), owner_lookup=current_owner)
    intent = registry.create_intent(agent_id='weather-1', audiences=['tools:read'],
        lifetime='open', actor=actor, owner=ALICE_EMAIL)
    before = store.load()
    authority = [True]
    entered_append = Event()
    original_append = audit.insert_in_transaction
    def observing_append(*args, **kwargs):
        entered_append.set()
        return original_append(*args, **kwargs)
    monkeypatch.setattr(audit, 'insert_in_transaction', observing_append)
    blocker_event = AuditEventCreate(actor_user_id=actor, auth_principal=actor,
        event_class='settings', action_type='publication_test.blocker', description='Audit lock barrier',
        correlation_id=str(uuid4()), started_at=datetime.now(timezone.utc), outcome='success')
    with ThreadPoolExecutor(max_workers=1) as pool:
        with blocker_runtime.transaction() as blocking:
            blocker_audit.insert_in_transaction(blocker_event, transaction=blocking, plane_runtime=blocker_runtime)
            future = pool.submit(registry.confirm, intent_id=intent.intent_id,
                confirmation_token=intent.confirmation_token, actor=actor, authorize=lambda agent: authority[0] and policy.owner_matches(agent, ALICE_EMAIL))
            assert entered_append.wait(timeout=10)
            deadline = time.monotonic() + 10
            while not blocking.fetch_one(
                'SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() '
                'AND pg_backend_pid()=ANY(pg_blocking_pids(pid))) AS blocked'
            )['blocked']:
                if time.monotonic() >= deadline:
                    pytest.fail('confirmation did not reach the held audit-chain lock')
                Event().wait(0.01)
            if change == 'expiry':
                clock[0] = intent.expires_at
            elif change == 'catalog':
                registry._catalog = lambda: ()
            elif change == 'scope':
                registry._audiences = lambda _: ('tools:write',)
            elif change == 'owner':
                assert blocker_runtime.repositories.agents.remove_ownership(blocking,
                    agent_id='weather-1', owner_email=ALICE_EMAIL)
            else:
                authority[0] = False
        expected = {'expiry': StaleIntentError, 'authority': PublicationForbiddenError, 'owner': PublicationForbiddenError}.get(change, NotEligibleError)
        with pytest.raises(expected):
            future.result(timeout=10)
    assert registry.active() == ()
    actions = [row.action_type for row in audit.list_for_user(actor)[0]]
    assert 'a2a_publication.published' not in actions
    if change in {'authority', 'owner'}:
        assert store.load() == before
        assert registry.get_intent(intent.intent_id) == intent
    else:
        assert registry.get_intent(intent.intent_id) is None
        assert actions.count('a2a_publication.confirm_stale' if change == 'expiry' else 'a2a_publication.confirm_denied') == 1
    assert audit.verify_chain(actor) is None


def test_admin_cannot_create_unresolved_owner_intent_then_recover_it(mock_publication_auth):
    orch = _orchestrator()
    resolved = [False]
    def lookup(agent):
        if not resolved[0]:
            raise RuntimeError('owner read unavailable')
        return ALICE_EMAIL
    orch.a2a_agent_owners = lookup
    client = _api_client(orch)
    response = admin_proposal(client)
    assert response.status_code == 503
    assert orch.a2a_publications.pending_intents() == ()
    resolved[0] = True
    fresh = admin_proposal(client)
    assert fresh.status_code == 200
    assert admin_confirm(client, fresh.json()).status_code == 200
    assert orch.a2a_publications.active()[0].owner == ALICE_EMAIL


def test_confirmed_absence_must_still_match_at_admin_confirmation(mock_publication_auth):
    orch = _orchestrator()
    client = _api_client(orch)
    intent = admin_proposal(client).json()
    assert orch.a2a_publications.get_intent(intent['intent_id']).owner is None
    orch.a2a_agent_owners['weather-1'] = ALICE_EMAIL
    response = admin_confirm(client, intent)
    assert response.status_code == 403
    assert orch.a2a_publications.active() == ()


@pytest.mark.parametrize('original_owner,change', [
    (None, 'owned'), (None, 'failure'), (None, 'missing_lookup'),
    (ALICE_EMAIL, 'absent'), (ALICE_EMAIL, 'failure'), (ALICE_EMAIL, 'owned'),
])
def test_current_owner_state_gates_card_listing_and_direct_invocation(mock_publication_auth, original_owner, change):
    owners = {} if original_owner is None else {'weather-1': original_owner}
    orch = _orchestrator(owners=owners)
    api = _api_client(orch)
    intent = admin_proposal(api).json()
    assert admin_confirm(api, intent).status_code == 200
    client = _client(orch)
    assert 'get_weather' in _tool_names(_send(client, admin_auth(), [{'data': {'method': 'tools/list'}}]).json())
    if change == 'failure':
        def failed(agent):
            raise RuntimeError('owner read unavailable')
        orch.a2a_publication_policy._owner_lookup = failed
    elif change == 'missing_lookup':
        orch.a2a_publication_policy._owner_lookup = None
    elif change == 'owned':
        orch.a2a_agent_owners['weather-1'] = 'new-owner@example.test'
    else:
        orch.a2a_agent_owners.clear()
    assert 'weather-1' not in {skill['id'] for skill in client.get('/a2a/.well-known/agent-card.json').json()['skills']}
    assert _tool_names(_send(client, admin_auth(), [{'data': {'method': 'tools/list'}}]).json()) == set()
    denied = _send(client, admin_auth(), [{'data': {'method': 'tools/call', 'name': 'get_weather', 'arguments': {}}}])
    assert _task(denied.json())['status']['state'] == 'TASK_STATE_FAILED'
    orch.execute_authorized_tool.assert_not_awaited()
