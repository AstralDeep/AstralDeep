"""The actual connection admission pump preserves only closed trusted harness refusals.
Real registered owners and durable admissions retain existing error and scheduling frames.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from astralplane.repositories.harness_bindings import HarnessProfileIdentity
from orchestrator.harness.production_profiles import HarnessSelectionError
from tests.test_harness_admission_postgres import host
from persistent_agents.tests.test_engine_postgres import plane as plane


@pytest.mark.asyncio
@pytest.mark.parametrize('posture,code,retryable', [
    ('flag_off', 'harness_selection_disabled', False),
    ('stale', 'harness_owner_invalid', False),
    ('unavailable', 'harness_unavailable', True),
    ('unrelated', 'operation_failed', True),
    ('unknown_code', 'operation_failed', False),
    ('wrong_status', 'operation_failed', False),
])
async def test_actual_admission_pump_keeps_bounded_trusted_refusals(plane, monkeypatch, posture, code, retryable):
    orch, context, frame = host(plane)
    sent = []
    async def send(socket, raw):
        assert socket is context.websocket
        sent.append(json.loads(raw))
        return True
    orch._safe_send = send
    if posture == 'flag_off':
        with plane.transaction() as tx:
            plane.repositories.preferences.harness.compare_and_set(tx, owner_id='owner-a',
                expected_revision=0, identity=HarnessProfileIdentity('standard_local', '1.0.0', 'a' * 64),
                qualification_digest='b' * 64)
    elif posture == 'stale':
        orch.ui_sessions[context.websocket] = {'sub': 'owner-a'}
    else:
        errors = {'unavailable': HarnessSelectionError(),
                  'unrelated': ValueError('private configuration must not leave server'),
                  'unknown_code': HarnessSelectionError('private configuration must not leave server', 409),
                  'wrong_status': HarnessSelectionError('harness_unavailable', 409)}
        def failed(_owner):
            raise errors[posture]
        monkeypatch.setattr(orch.harness_selection, 'prepare_sync', failed)
    context.ingress.append(frame)
    await asyncio.wait_for(orch._connection_admission_pump(context), 10)
    assert len(sent) == 1 and sent[0]['type'] == 'error' and sent[0]['accepted'] is False
    assert sent[0]['code'] == code and sent[0]['retryable'] is retryable
    assert 'private configuration' not in repr(sent)
    assert frame.ingress_registration is None and not context.operation_tasks
    with plane.transaction() as tx:
        assert tx.fetch_one('SELECT count(*) AS n FROM operation_record')['n'] == 0


@pytest.mark.asyncio
async def test_actual_admission_pump_schedules_unchanged_registered_default_once(plane):
    orch, context, frame = host(plane)
    orch._send_operation_accepted = AsyncMock()
    orch._run_connection_operation = AsyncMock()
    orch._replay_voice_unavailable_if_tombstoned = AsyncMock(return_value=False)
    context.ingress.append(frame)
    await asyncio.wait_for(orch._connection_admission_pump(context), 10)
    await asyncio.gather(*context.operation_tasks)
    orch._send_operation_accepted.assert_awaited_once()
    orch._run_connection_operation.assert_awaited_once()
    assert frame.ingress_registration is None
    with plane.transaction() as tx:
        assert tx.fetch_one('SELECT count(*) AS n FROM operation_record')['n'] == 1
