"""Verify Safety HTML delivery through chrome dispatch and current signed socket authority.
Correlated browser results retain owner/request custody across navigation and delivery waits.
"""

from uuid import uuid4

import pytest

from orchestrator import chrome_events
from shared.protocol import ChromeRender, ProtocolValidationError

pytestmark = pytest.mark.asyncio


async def open_safety(state, generation):
    return await chrome_events._handle_chrome_event(state.orch, state.socket, "chrome_open",
        {"surface": "safety", "params": {}}, "owner", request_generation=generation)


@pytest.mark.parametrize("changes", [
    {"request_generation": None}, {"request_generation": "stale"}, {"request_generation": True},
    {"region": "topbar"},
])
async def test_safety_html_rejects_missing_generation_and_foreign_regions(changes):
    values = {"surface_key": "safety", "request_generation": str(uuid4()), **changes}
    with pytest.raises(ProtocolValidationError):
        ChromeRender(**values).to_json()


