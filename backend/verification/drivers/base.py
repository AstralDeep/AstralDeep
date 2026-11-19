"""Driver protocol shared by the in-process and external verification drivers
(backend/verification/drivers/in_process.py, external.py): both expose fixture upload
and scope control so the same probes run against either.
"""

from __future__ import annotations

from typing import Any, Dict, Protocol, runtime_checkable

from verification.isolation import Principal


@runtime_checkable
class Driver(Protocol):
    mode: str
    auth_mode: str

    async def setup(self) -> None:
        pass

    async def teardown(self) -> None:
        pass

    async def upload_as(self, principal: Principal, fixture: Any) -> Dict[str, Any]:
        ...

    async def set_scope(
        self, principal: Principal, agent_id: str, scope: str, enabled: bool
    ) -> None:
        ...
