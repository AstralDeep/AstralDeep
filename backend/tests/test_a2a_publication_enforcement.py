"""Regression tests for scoped A2A admission and atomic publication lifecycle decisions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError
from types import SimpleNamespace

import pytest

from tests.test_a2a_publication import _Store, _orchestrator, _publish, _registry
from shared.a2a_publication import A2APublicationError, PublicationStoreUnavailable
from orchestrator.a2a_publication_policy import A2APublicationPolicy


def test_selected_scope_filters_each_skill():
    orch = _orchestrator()
    registry = _registry()
    _publish(registry)
    policy = A2APublicationPolicy(orch, registry, catalog=lambda: ('weather-1',))
    read = SimpleNamespace(agent_id='weather-1', skill_id='get_weather')
    write = SimpleNamespace(agent_id='weather-1', skill_id='set_weather')
    assert policy.filter_projected_tools([read, write]) == (read,)


def test_audit_failure_cannot_publish_or_consume_confirmation():
    registry = _registry()
    intent = registry.create_intent(agent_id='weather-1', audiences=['tools:read'],
                                    lifetime='open', actor='owner')
    def fail(event):
        raise RuntimeError('audit unavailable')
    registry._audit_sink = fail
    with pytest.raises(PublicationStoreUnavailable):
        registry.confirm(intent_id=intent.intent_id,
                         confirmation_token=intent.confirmation_token, actor='owner')
    assert registry.active() == ()
    assert registry.get_intent(intent.intent_id) == intent


def test_worker_reads_withdrawal_from_shared_store():
    store = _Store()
    first = _registry(store=store)
    record = _publish(first)
    second = _registry(store=store)
    assert second.is_published('weather-1')
    first.withdraw(publication_id=record.publication_id, actor=record.created_by)
    assert not second.is_published('weather-1')


def test_two_pending_intents_cannot_publish_one_agent_twice():
    registry = _registry()
    intents = [registry.create_intent(agent_id='weather-1', audiences=['tools:read'],
                                     lifetime='open', actor='owner') for _ in range(2)]
    registry.confirm(intent_id=intents[0].intent_id,
                     confirmation_token=intents[0].confirmation_token, actor='owner')
    with pytest.raises(A2APublicationError):
        registry.confirm(intent_id=intents[1].intent_id,
                         confirmation_token=intents[1].confirmation_token, actor='owner')
    assert len(registry.active()) == 1


def test_same_registry_confirmation_is_single_use():
    registry = _registry()
    intent = registry.create_intent(agent_id='weather-1', audiences=['tools:read'],
                                    lifetime='open', actor='owner')
    barrier = Barrier(2)
    original = registry._catalog
    def catalog():
        try:
            barrier.wait(timeout=0.1)
        except BrokenBarrierError:
            pass
        return original()
    registry._catalog = catalog
    def confirm():
        try:
            return registry.confirm(intent_id=intent.intent_id,
                                    confirmation_token=intent.confirmation_token, actor='owner')
        except Exception as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(), range(2)))
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert len(registry.active()) == 1


@pytest.mark.parametrize('scope', ['', 'tools:write', None])
def test_current_skill_scope_never_expands_publication(scope):
    orch = _orchestrator()
    registry = _registry()
    _publish(registry)
    policy = A2APublicationPolicy(orch, registry, catalog=lambda: ('weather-1',))
    orch.agent_cards['weather-1'].skills[0].scope = scope
    assert policy.admit('weather-1', 'get_weather') is None
    assert policy.filter_projected_tools([SimpleNamespace(agent_id='weather-1', skill_id='get_weather')]) == ()


def test_read_only_publication_denies_direct_write_and_preserves_normal_dispatch(monkeypatch):
    from tests.test_a2a_publication import (
        _auth, _client, _publish_for_client, _send, _skill, _task, _tool_names,
    )
    monkeypatch.setenv('USE_MOCK_AUTH', 'true')
    monkeypatch.setenv('KEYCLOAK_AUTHORITY', 'https://iam.example/realms/astral')
    monkeypatch.setenv('KEYCLOAK_CLIENT_ID', 'astral-frontend')
    orch = _orchestrator()
    orch.agent_cards['weather-1'].skills.append(_skill('set_weather', 'tools:write'))
    client = _client(orch)
    _publish_for_client(client)
    listed = _send(client, _auth(), [{'data': {'method': 'tools/list'}}])
    assert _tool_names(listed.json()) == {'get_weather'}
    denied = _send(client, _auth(), [{'data': {'method': 'tools/call', 'name': 'set_weather', 'arguments': {}}}])
    assert _task(denied.json())['status']['state'] == 'TASK_STATE_FAILED'
    orch.execute_authorized_tool.assert_not_awaited()
    allowed = _send(client, _auth(), [{'data': {'method': 'tools/call', 'name': 'get_weather', 'arguments': {}}}])
    assert _task(allowed.json())['status']['state'] == 'TASK_STATE_COMPLETED'
    assert orch.execute_authorized_tool.await_args.kwargs['channel'] == 'a2a'
    assert orch.execute_authorized_tool.await_args.kwargs['delegation_subject_token']


def test_owner_change_revokes_admission_and_card():
    orch = _orchestrator()
    registry = _registry()
    intent = registry.create_intent(agent_id='weather-1', audiences=['tools:read'],
                                    lifetime='open', actor='owner', owner='old-owner')
    registry.confirm(intent_id=intent.intent_id, confirmation_token=intent.confirmation_token, actor='owner')
    policy = A2APublicationPolicy(orch, registry, catalog=lambda: ('weather-1',),
                                  owner_lookup=lambda _: 'new-owner')
    assert policy.admit('weather-1', 'get_weather') is None
    assert policy.published_records() == ()


def test_conflicts_refresh_and_retry_bounded():
    from shared.a2a_publication import PublicationStoreConflict
    class Conflicts(_Store):
        def save(self, *args, **kwargs):
            raise PublicationStoreConflict('contended')
    registry = _registry(store=Conflicts())
    with pytest.raises(PublicationStoreConflict):
        registry.create_intent(agent_id='weather-1', audiences=['tools:read'], lifetime='open', actor='owner')
    assert registry.pending_intents() == ()


def test_memory_authority_failure_rolls_back():
    from shared.a2a_publication import PublicationForbiddenError
    registry = _registry()
    with pytest.raises(PublicationForbiddenError):
        registry.create_intent(agent_id='weather-1', audiences=['tools:read'], lifetime='open', actor='owner',
                               authorize=lambda _: False)
    assert registry.pending_intents() == ()


def test_owner_can_withdraw_after_capability_loses_safe_catalog_membership(monkeypatch):
    from tests.test_a2a_publication import ALICE_EMAIL, _api_client, _auth
    monkeypatch.setenv('USE_MOCK_AUTH', 'true')
    monkeypatch.setenv('KEYCLOAK_AUTHORITY', 'https://iam.example/realms/astral')
    monkeypatch.setenv('KEYCLOAK_CLIENT_ID', 'astral-frontend')
    orch = _orchestrator(owners={'weather-1': ALICE_EMAIL})
    client = _api_client(orch)
    intent = client.post('/api/a2a/publications/intents', headers=_auth(),
                         json={'agent_id': 'weather-1', 'audiences': ['tools:read'], 'lifetime': 'open'}).json()
    record = client.post(f"/api/a2a/publications/intents/{intent['intent_id']}/confirm", headers=_auth(),
                         json={'confirmation_token': intent['confirmation_token']}).json()
    orch.tool_permissions._safe.clear()
    response = client.delete(f"/api/a2a/publications/{record['publication_id']}", headers=_auth())
    assert response.status_code == 200
    assert response.json()['status'] == 'withdrawn'
