"""Verifies fail-closed composition and purpose checks for human-request authority."""

import importlib
from types import SimpleNamespace

import pytest

from persistent_agents.models import AssignmentError


pytestmark = pytest.mark.asyncio


async def test_boundary_requires_real_composition_and_existing_services():
    module = importlib.import_module("orchestrator.human_request_authority")
    with pytest.raises(AssignmentError, match="human_request_unavailable"):
        module.HumanRequestBoundary(SimpleNamespace())


@pytest.mark.parametrize("purpose", ["remote_effect", "unsupported", None])
async def test_retired_and_unknown_socket_purposes_refuse_before_registration(purpose):
    module = importlib.import_module("orchestrator.human_request_authority")
    boundary = object.__new__(module.HumanRequestBoundary)
    with pytest.raises(AssignmentError, match="human_authentication_required"):
        module._HumanSocketRequest(
            boundary, websocket=None, context=None, message={}, method="WS_WRITE", purpose=purpose)
    with pytest.raises(AssignmentError, match="human_authentication_required"):
        module.capture_human_socket_request(boundary, websocket=None, context=None, message={}, purpose=purpose)
