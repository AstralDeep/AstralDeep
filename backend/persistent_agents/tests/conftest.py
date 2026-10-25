"""Shared pytest fixtures for persistent_agents/tests: exposes the session-migrated Plane
template from tests/helpers/plane_template.py that test_engine_postgres.py's `plane`
fixture clones for every test, and pins a deterministic PHI analyzer so runner
lifecycle tests do not depend on the host Presidio model being constructible.
"""

import pytest

from tests.helpers.plane_template import plane_template  # noqa: F401


@pytest.fixture(autouse=True)
def deterministic_phi_gate():
    from personalization import phi_gate
    from personalization.phi_gate import PHIGate

    live = phi_gate.get_phi_gate()
    phi_gate.set_phi_gate(PHIGate(analyzer=_StaticAnalyzer()))
    yield
    phi_gate.set_phi_gate(live)


class _StaticAnalyzer:
    def analyze(self, **_):
        return []
