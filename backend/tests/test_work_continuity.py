"""Unit boundaries for orchestrator/work_continuity.py: the recheck request vocabulary
stays closed, the continuation view resolves only durable event keys, and the module
never imports the full orchestrator runtime.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

import orchestrator.work_continuity as continuity
from orchestrator.work_controls import WorkControlRequest
from persistent_agents.models import AssignmentError


def test_recheck_request_keeps_the_closed_control_vocabulary():
    body = WorkControlRequest(expected_revision=1, submission_id=str(uuid4()))
    assert body.expected_revision == 1
    for field, value in [
        ("submission_id", "not-a-uuid"), ("submission_id", str(uuid4()).upper()),
        ("expected_revision", 0), ("expected_revision", True), ("expected_revision", "1"),
        ("unexpected_field", {"escalate": True}),
    ]:
        fields = {"expected_revision": 1, "submission_id": str(uuid4())}
        fields[field] = value
        with pytest.raises(ValidationError):
            WorkControlRequest.model_validate(fields)


def _read(wait=None, *, lifecycle="active", phase="awaiting_event"):
    operation = {"control": {"wait": dict(wait)}} if wait else {"control": {}}
    return SimpleNamespace(assignment=SimpleNamespace(
        operation=operation, lifecycle=lifecycle, phase=phase,
        assignment_id=str(uuid4())))


def _view(tx=None, repository=None, owner="owner", read=None):
    return continuity._continuation_view(tx, repository, owner, read)


def test_view_resolves_wake_completion_event_sources():
    source_id = str(uuid4())
    read = _read({"event_key": "completion:" + source_id, "source_revision": 4})
    source = SimpleNamespace(assignment=SimpleNamespace(operation={}),
                             disposition="queued")
    view = _view(read=read, repository=SimpleNamespace(
        get_operation=lambda tx, owner_id, assignment_id: source))
    assert view["source_operation_id"] == source_id
    assert view["source_disposition"] == "queued"
    assert view["source_missing"] is False
    assert view["awaiting_event"] is True


def test_view_reports_missing_sources_and_non_wake_keys():
    read = _read({"event_key": "completion:" + str(uuid4()), "source_revision": 1})
    missing = _view(repository=SimpleNamespace(
        get_operation=lambda tx, owner_id, assignment_id: None), read=read)
    assert missing["source_missing"] is True and missing["source_disposition"] is None
    for key in ("owner:" + str(uuid4()), "remote-job:" + "a" * 32,
                "operation:" + str(uuid4()) + ":completed"):
        assert _view(read=_read({"event_key": key, "source_revision": 0})) is None


def test_view_is_absent_without_a_wait():
    assert _view(read=_read()) is None


async def test_recheck_never_accepts_an_unauthenticated_caller():
    service = continuity.WorkContinuityService(SimpleNamespace(
        orch=None,
        store=SimpleNamespace(plane_runtime=None, repository=None, async_runtime=None)))

    with pytest.raises(AssignmentError) as refused:
        await service.recheck(str(uuid4()), object(), caller=object())
    assert refused.value.code == "work_authentication_required"


def test_module_never_pulls_the_full_orchestrator_runtime():
    import os
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); import orchestrator.work_continuity; "
         "assert 'orchestrator.orchestrator' not in sys.modules" % str(backend)],
        capture_output=True, text=True, cwd=str(backend),
        env={**os.environ, "PYTHONPATH": str(backend)})
    assert result.returncode == 0, result.stderr
