"""Driver protocol shared by the in-process and external verification drivers
(backend/verification/drivers/in_process.py, external.py): both return the same
CapturedEvidence shape so the same checks run against either.
"""

from __future__ import annotations

from typing import Any, Dict, Protocol, runtime_checkable

from verification.evidence import CapturedEvidence
from verification.isolation import Principal
from verification.scenarios import Scenario


@runtime_checkable
class Driver(Protocol):
    mode: str
    auth_mode: str

    async def setup(self) -> None:
        pass

    async def teardown(self) -> None:
        pass

    async def run_scenario(self, scenario: Scenario) -> CapturedEvidence:
        ...

    async def upload_as(self, principal: Principal, fixture: Any) -> Dict[str, Any]:
        ...

    async def reference_attachment_as(
        self, principal: Principal, attachment_id: str, filename: str
    ) -> CapturedEvidence:
        ...

    async def set_scope(
        self, principal: Principal, agent_id: str, scope: str, enabled: bool
    ) -> None:
        ...
