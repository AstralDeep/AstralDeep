"""Exercises the denial and success branches of framework operation authority checks with stand-in record types.
It covers orchestrator/framework_operation_authority.py and its SessionAuthorityUnavailable refusals."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import framework_operation_authority as far
from orchestrator.session_authority import SessionAuthorityUnavailable


class Record:
    owner_id = "owner-1"
    operation = {"authority": {"origin": "framework", "reference_id": "cred-1"}}


class Observation:
    def __init__(self, owner_id="owner-1", credential_id="cred-1", scopes=("operations.submit",)):
        self.credential = SimpleNamespace(owner_id=owner_id, credential_id=credential_id, scopes=scopes)


@pytest.fixture(autouse=True)
def stand_ins(monkeypatch):
    monkeypatch.setattr(far, "FrameworkCredentialObservation", Observation)
    monkeypatch.setattr(far, "framework_chat_record", lambda record: None)


def authority(runtime, observation=None):
    return far.FrameworkOperationExecutionAuthority(Record(), observation or Observation(), runtime)


def test_claims_carry_owner_and_credential():
    claims = authority(object()).claims
    assert claims == {"sub": "owner-1", "machine_class": "framework", "_framework_credential_id": "cred-1"}


def test_authority_repository_returns_credential_repository():
    runtime = SimpleNamespace(repositories=SimpleNamespace(framework_credentials="repo"))
    assert far.authority_repository(runtime, authority(runtime)) == "repo"


def test_authority_repository_refuses_foreign_type_and_runtime():
    runtime = object()
    with pytest.raises(SessionAuthorityUnavailable, match="operation_authority_unavailable"):
        far.authority_repository(runtime, SimpleNamespace(plane_runtime=runtime))
    with pytest.raises(SessionAuthorityUnavailable, match="operation_authority_unavailable"):
        far.authority_repository(runtime, authority(object()))


@pytest.mark.parametrize("observation", [
    Observation(owner_id="someone-else"),
    Observation(credential_id="cred-2"),
    Observation(scopes=("operations.submit", "extra")),
    object(),
])
def test_authority_repository_refuses_mismatched_observation(observation):
    runtime = object()
    with pytest.raises(SessionAuthorityUnavailable, match="framework_operation_authority_unavailable"):
        far.authority_repository(runtime, far.FrameworkOperationExecutionAuthority(
            Record(), observation, runtime))


def test_assert_operation_owner_checks_runtime_then_owner():
    runtime = SimpleNamespace(repositories=SimpleNamespace(framework_credentials="repo"))
    seen = []
    service = SimpleNamespace(store=SimpleNamespace(plane_runtime=runtime),
                              _owner=lambda owner, claims, human: seen.append((owner, claims, human)))
    held = authority(runtime)
    far.assert_operation_owner(service, held, Record())
    assert seen == [("owner-1", held.claims, False)]
    other = authority(object())
    with pytest.raises(SessionAuthorityUnavailable, match="operation_authority_unavailable"):
        far.assert_operation_owner(service, other, Record())


def test_invocation_principal_marks_framework_channel():
    principal = far.invocation_principal(authority(object()))
    assert principal["_invocation_channel"] == "framework"
    assert principal["sub"] == "owner-1"
    with pytest.raises(SessionAuthorityUnavailable, match="operation_authority_unavailable"):
        far.invocation_principal(SimpleNamespace(claims={}))


async def test_refresh_refuses_non_framework_origin():
    record = SimpleNamespace(operation={"authority": {"origin": "user"}})
    with pytest.raises(SessionAuthorityUnavailable, match="operation_authority_unavailable"):
        await far.refresh_assignment_execution_authority(record, sessions=None, plane_runtime=object())
    bare = SimpleNamespace(operation={})
    with pytest.raises(SessionAuthorityUnavailable, match="operation_authority_unavailable"):
        await far.refresh_assignment_execution_authority(bare, sessions=None, plane_runtime=object())
