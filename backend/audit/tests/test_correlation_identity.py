"""Checks correlation UUID canonicalization before audit signing through models and real Plane persistence. Remote-machine callers retain their compact operational IDs and metadata while audit chains remain verifiable."""

import asyncio
from uuid import UUID

import pytest
from pydantic import ValidationError

from audit.recorder import Recorder
from orchestrator import remote_machines

IDENTITY = UUID('aabbccdd-1122-4333-8444-5566778899aa')
FORMS = [str(IDENTITY), IDENTITY.hex, str(IDENTITY).upper(), '{' + str(IDENTITY) + '}', IDENTITY.urn]


@pytest.mark.parametrize('value', FORMS)
def test_uuid_correlation_is_canonical_before_signing(make_event, value):
    event = make_event(correlation_id=value, inputs_meta={'machine_id': IDENTITY.hex})
    assert event.correlation_id == str(IDENTITY)
    assert event.inputs_meta == {'machine_id': IDENTITY.hex}


@pytest.mark.parametrize('value', ['opaque-request', 'corr-1', 'not-a-uuid', ' c '])
def test_opaque_correlation_contract_is_unchanged(make_event, value):
    assert make_event(correlation_id=value).correlation_id == value


@pytest.mark.parametrize('value', ['', None, 1, True, {}, []])
def test_invalid_correlation_shape_still_refuses(make_event, value):
    with pytest.raises(ValidationError):
        make_event(correlation_id=value)


@pytest.mark.parametrize('value', FORMS)
def test_real_plane_appends_and_transactional_appends_preserve_chain(repo, database, make_event, unique_user, value):
    event = make_event(actor_user_id=unique_user, auth_principal=unique_user, correlation_id=value)
    first = repo.insert(event)
    with database.transaction() as transaction:
        second = repo.insert_in_transaction(event, transaction=transaction, plane_runtime=database)
    assert first.event_id != second.event_id
    assert first.correlation_id == second.correlation_id == str(IDENTITY)
    assert repo.verify_chain(unique_user) is None
    assert repo.get_for_user(unique_user + '-foreign', first.event_id) is None


def test_remote_machine_registered_connection_and_fallback_keep_real_chain(repo, database, unique_user, monkeypatch, tmp_path):
    queue = tmp_path / 'retry.jsonl'
    recorder = Recorder(repo, retry_queue=queue)
    monkeypatch.setattr('audit.recorder.get_recorder', lambda: recorder)
    try:
        machine_id = remote_machines.create_machine(database, unique_user, 'fixture', 'ssh.invalid', 22, 'runner', 'linux', 'plain')
        assert UUID(machine_id).hex == machine_id
        remote_machines.audit_machine_event(unique_user, 'remote_machine.registered', 'Registered synthetic machine',
            machine_id=machine_id, label='fixture', cred_type='password')
        remote_machines.record_probe(database, unique_user, machine_id, 'timeout')
        remote_machines.audit_machine_event(unique_user, 'remote_machine.cleanup', 'Synthetic fallback event')
        events, _ = repo.list_for_user(unique_user)
        assert {event.action_type for event in events} == {
            'remote_machine.registered', 'remote_machine.connection', 'remote_machine.cleanup',
        }
        assert len(events) == 3
        for event in events:
            assert str(UUID(event.correlation_id)) == event.correlation_id
            if event.action_type != 'remote_machine.cleanup':
                assert event.correlation_id == str(UUID(machine_id))
                assert event.inputs_meta['machine_id'] == machine_id
            with database.transaction() as transaction:
                stored = database.repositories.audit.get(transaction, chain_id=unique_user, event_id=event.event_id)
                assert stored.event.chain_id == stored.event.auth_principal == unique_user
            assert repo.get_for_user(unique_user + '-foreign', event.event_id) is None
        assert remote_machines.get_machine(database, unique_user, machine_id)['machine_id'] == machine_id
        assert repo.verify_chain(unique_user) is None
        assert not queue.exists()
    finally:
        asyncio.run(recorder.close())


def test_invalid_remote_machine_id_never_creates_durable_audit(repo, unique_user, monkeypatch, tmp_path):
    queue = tmp_path / 'retry.jsonl'
    recorder = Recorder(repo, retry_queue=queue)
    monkeypatch.setattr('audit.recorder.get_recorder', lambda: recorder)
    try:
        remote_machines.audit_machine_event(unique_user, 'remote_machine.connection', 'Synthetic invalid identity',
            machine_id='not-a-uuid')
        assert repo.list_for_user(unique_user) == ([], None)
        assert repo.verify_chain(unique_user) is None
        assert queue.exists()
    finally:
        asyncio.run(recorder.close())
