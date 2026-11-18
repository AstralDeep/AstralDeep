
import importlib
from types import SimpleNamespace

import pytest

from persistent_agents.models import AssignmentError


pytestmark = pytest.mark.asyncio


async def test_boundary_requires_real_composition_and_existing_services():
    module = importlib.import_module("orchestrator.human_request_authority")
    with pytest.raises(AssignmentError, match="human_request_unavailable"):
        module.HumanRequestBoundary(SimpleNamespace())


