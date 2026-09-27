"""Shared pytest fixtures for persistent_agents/tests: exposes the session-migrated Plane
template from tests/helpers/plane_template.py that test_engine_postgres.py's `plane`
fixture clones for every test.
"""

from tests.helpers.plane_template import plane_template  # noqa: F401
