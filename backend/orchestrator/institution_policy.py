"""What an institution node demands of the device behind a mesh request before its own gates run: the device must be a member whose identity attestation is current, and PHI-labeled work is accepted only from a member of that mesh's PHI group.
node_mesh_chat.py calls it for every request it serves and again once a ticket's labels are known, and it reads standing from the kernel's roster through node_mesh.py.
"""

from __future__ import annotations

from typing import Any

from orchestrator.node_mesh import NodeMesh
from orchestrator.node_mesh_chat import Labels, Refusal

NOT_A_MEMBER = "institution.not_a_member"
ATTESTATION_STALE = "institution.attestation_stale"
PEER_NOT_PHI = "institution.peer_not_phi"


class InstitutionPolicy:
    def __init__(self, mesh: NodeMesh):
        self._mesh = mesh

    async def check(self, request: Any, floor: Labels) -> None:
        standing = await self._mesh.peer_standing(request.from_device)
        if standing is None:
            raise Refusal(NOT_A_MEMBER)
        if standing.needs_attestation:
            raise Refusal(ATTESTATION_STALE)
        if floor[0] == "phi" and not standing.phi:
            raise Refusal(PEER_NOT_PHI)
