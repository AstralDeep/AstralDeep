
from types import SimpleNamespace

import pytest

from orchestrator import context_authority as module
from persistent_agents.models import AssignmentError

pytestmark = pytest.mark.asyncio


async def acquire(turn):
    host, socket, chat, _ = turn
    return await module.capture_context_authority(orchestrator=host, websocket=socket, chat_id=chat)


async def verify(lease, turn):
    host, socket, chat, _ = turn
    return await lease.verify(orchestrator=host, websocket=socket, chat_id=chat)


async def test_missing_and_forged_authority_never_authorize():
    for value in (None, "owner", SimpleNamespace(owner_id="owner")):
        with pytest.raises(AssignmentError):
            with module.use_context_authority(value):
                pytest.fail("forged authority entered")
        with pytest.raises(AssignmentError):
            module.ContextAuthorityLease(value, orchestrator=object())
    with pytest.raises(AssignmentError):
        module.current_context_authority(orchestrator=object(), websocket=None, chat_id="chat")
    with pytest.raises(AssignmentError):
        await module.capture_context_authority(orchestrator=object(), websocket=None, chat_id="chat")


