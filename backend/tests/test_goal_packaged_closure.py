"""Checks that qualified harness artifacts include the goal controller and completion publication code."""

import pytest

from orchestrator.harness import production_profiles as profiles
from tests.test_harness_packaged_artifacts import manifest_fact


@pytest.mark.parametrize("name", [
    "orchestrator/async_tasks.py",
    "orchestrator/subtasks.py",
    "orchestrator/goal_progress.py",
    "orchestrator/goal_project_context.py",
    "orchestrator/goal_requirements.py",
    "orchestrator/goal_verification.py",
    "orchestrator/conversation_publication.py",
    "orchestrator/history.py",
    "shared/feature_flags.py",
])
def test_modified_goal_implementation_invalidates_packaged_qualification(monkeypatch, tmp_path, name):
    fact = manifest_fact()
    original = profiles._implementation_path
    changed = tmp_path / "changed.py"
    changed.write_text("changed implementation bytes\n")
    monkeypatch.setattr(profiles, "_implementation_path", lambda candidate: changed if candidate == name else original(candidate))
    with pytest.raises(profiles.HarnessSelectionError, match="harness_artifact_unavailable"):
        profiles._verify_fact(fact)
