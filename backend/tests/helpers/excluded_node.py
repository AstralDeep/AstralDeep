"""Builds a stand-in node mesh that answers as a node the owner excluded from the PHI group, so lifecycle tests reach the draft launch that orchestrator/phi_node_policy.py allows only there.
The excluded_node fixture in conftest.py installs it, and test_phi_node_gates.py proves the same answer on the real Kernel binding through tests/helpers/dev_mesh.py."""

from __future__ import annotations

from types import SimpleNamespace


class StandInKernelError(Exception):
    pass


def stand_in_mesh(*, enrolled: bool = True, member: bool = False, excluded: bool = True) -> SimpleNamespace:
    async def status():
        return {"enrolled": enrolled}

    async def phi_member():
        return member

    async def phi_excluded():
        return excluded

    return SimpleNamespace(
        started=True, status=status, phi_member=phi_member, phi_excluded=phi_excluded,
        binding=SimpleNamespace(KernelError=StandInKernelError),
    )
