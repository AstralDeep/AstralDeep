"""End-to-end framework-credential conformance (feature 088 T050 + T051).

Boots the REAL MCP endpoint (``orchestrator.mcp_server_endpoint.install_mcp_server``)
on a real ``uvicorn`` server bound to an OS-assigned localhost port, wires a
REAL ``FrameworkCredentialService``/``FrameworkWorkOperations`` pair against
an isolated Postgres/Plane schema (the shared ``plane`` fixture from
``persistent_agents/tests/test_engine_postgres.py``), issues one framework
credential the way the product does (``FrameworkCredentialService.issue``
under a real ``SessionConsentObservation``), and then drives it from a
SEPARATE OS process: ``python -m astral_sdk`` (the SDK's own CLI, exactly as
an external caller would invoke it) reaching this server over a real
loopback socket.

What this test does NOT do: it does not exercise the product's own boot
wiring (nothing in ``orchestrator.py`` sets ``orch.framework_credentials``/
``orch.framework_work_operations`` on the live Orchestrator singleton today —
see ``backend/tests/chrome/test_chrome_surface.py::test_connections_surface_enabled_but_unwired_shows_unavailable``,
which pins that gap as a known, tested state). Wiring those two attributes
onto a purpose-built orchestrator instance for this test mirrors exactly how
``backend/tests/test_mcp_endpoint_064.py`` and
``backend/tests/test_work_framework_compat_088.py`` already construct their
own minimal orchestrator doubles — it is the established pattern for
exercising this endpoint, not a bypass of anything this suite is meant to
prove.

Shared, checked-in fixtures under ``backend/tests/fixtures/framework_conformance/``
are the SAME files ``sdk/tests/test_tool_catalog_contract.py`` loads — one
recorded shape, read by both suites, never duplicated by hand.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from cryptography.fernet import Fernet
from fastapi import FastAPI

from audit.repository import AuditRepository
from orchestrator.framework_credentials import FrameworkCredentialService
from orchestrator.mcp_server_endpoint import install_mcp_server
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.session_store import WebSessionStore
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationRequest,
    OwnerScope,
    WorkAdmissionCoordinator,
)
from orchestrator.work_operations import FrameworkWorkOperations
from persistent_agents.models import AssignmentError
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore
from persistent_agents.tests.test_engine_postgres import plane as plane
from personalization.phi_gate import PHIGate
from shared.feature_flags import flags
from tests.helpers.session_consent_088 import consent_from_store

runtime = plane  # re-exported so pytest discovers the same fixture under this name

REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = REPO_ROOT / "sdk"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "framework_conformance"
_ALL_SCOPES = ("operations.submit", "operations.read", "operations.control", "artifacts.read")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-framework-conformance-audit-key")
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.test/realms/astral")


@pytest.fixture
def _framework_flag_on():
    prior = flags._flags.get("framework_credentials")
    flags._flags["framework_credentials"] = True
    yield
    flags._flags["framework_credentials"] = prior


class _ConformanceOrchestrator:
    """The minimal real dependency graph ``mcp_server_endpoint`` needs.

    Admission/observability are the exact self-contained doubles
    ``backend/tests/test_mcp_endpoint_064.py`` already uses (no DB — Work
    admission is a distinct, in-memory concern from Plane-backed Work
    operations). ``framework_credentials``/``framework_work_operations`` are
    REAL, Postgres-backed instances.
    """

    def __init__(self, *, credentials: FrameworkCredentialService, ops: FrameworkWorkOperations):
        self.history = SimpleNamespace(db=None)
        self.framework_credentials = credentials
        self.framework_work_operations = ops
        configs = (
            AdmissionClassConfig(AdmissionClass.GLOBAL, None, 8, 0, None, "test"),
            AdmissionClassConfig(AdmissionClass.MCP, AdmissionClass.GLOBAL, 4, 8, 1000, "test"),
        )
        self.work_admission = WorkAdmissionCoordinator(
            admission_classes=configs, repository=InMemoryWorkAdmissionRepository(),
            clock=lambda: datetime.now(UTC), slot_lease=timedelta(seconds=30),
        )
        self.runtime_observability = RuntimeObservability()

    async def _call_work_admission(self, method, *args, **kwargs):
        return method(*args, **kwargs)

    async def _claim_personal_agent_operation(self, *, owner_user_id, operation_kind,
                                              idempotency_namespace, idempotency_key,
                                              normalized_identity, admission_class,
                                              request_generation, wait_seconds, **kwargs):
        del normalized_identity, wait_seconds, kwargs
        owner = OperationOwner(OwnerScope.USER, owner_user_id, None)
        admitted = self.work_admission.submit(OperationRequest(
            operation_kind=operation_kind, admission_class=admission_class, owner=owner,
            submission_id=uuid.uuid4(), idempotency_namespace=idempotency_namespace,
            idempotency_key=idempotency_key, normalized_input_digest="a" * 64,
            chat_id=None, parent_operation_id=None, connection_generation=None,
            request_generation=request_generation,
        ))
        if not admitted.accepted:
            return None
        claim = self.work_admission.claim_operation(admission_class, admitted.operation_id)
        return (owner, claim) if claim is not None else None


class _UvicornThread(threading.Thread):
    """A real ``uvicorn`` server on a background thread with its own event loop."""

    def __init__(self, app: FastAPI) -> None:
        super().__init__(daemon=True)
        self.config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self._loop: asyncio.AbstractEventLoop | None = None

    def run(self) -> None:  # noqa: D102 - threading.Thread override
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self.server.serve())

    def wait_until_started(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while not getattr(self.server, "started", False):
            if time.monotonic() >= deadline:
                raise TimeoutError("uvicorn did not start in time")
            time.sleep(0.02)

    @property
    def base_url(self) -> str:
        sock = self.server.servers[0].sockets[0]
        host, port = sock.getsockname()[:2]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.join(timeout=15)


@pytest.fixture
def credentials(runtime):
    return FrameworkCredentialService(plane_runtime=runtime, audit=AuditRepository(plane_runtime=runtime))


@pytest.fixture
def assignments(runtime):
    orch = SimpleNamespace(history=SimpleNamespace(get_chat=lambda *args, **kwargs: None))
    return AssignmentService(orch, AssignmentStore(plane_runtime=runtime), enabled=True,
                             phi_gate=PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: [])))


@pytest.fixture
def ops(assignments, credentials):
    return FrameworkWorkOperations(assignments=assignments, credentials=credentials)


@pytest.fixture
def session(runtime):
    owner = str(uuid.uuid4())
    sid = uuid.uuid4().hex
    store = WebSessionStore(plane_runtime=runtime, plane_repositories=runtime.repositories)
    store.create(sid, user_id=owner, access_token="synthetic-access",
                refresh_token="synthetic-refresh", hard_max_seconds=3600)
    yield store, owner, sid


@pytest.fixture
def live_server(_framework_flag_on, credentials, ops):
    app = FastAPI()
    orchestrator = _ConformanceOrchestrator(credentials=credentials, ops=ops)
    install_mcp_server(app, orchestrator, public_base_url="http://mcp.conformance.test")
    thread = _UvicornThread(app)
    thread.start()
    thread.wait_until_started()
    try:
        yield thread.base_url
    finally:
        thread.stop()


def _resolve_with_retry(credentials: FrameworkCredentialService, token: str,
                        *, attempts: int = 60, delay: float = 0.1):
    """``resolve_bearer``, absorbing a known sub-second Plane freshness-window race.

    ``FrameworkCredentialRepository.assert_current_execution`` (Plane,
    ``astralplane.repositories.history``) refuses a bearer whose host-captured
    ``started_at`` is not, at the instant of its OWN ``clock_timestamp()``
    read, ``<= now`` — a strict, one-sided bound with no slop. In a
    virtualized dev environment the Postgres container's clock and this
    process's clock can each momentarily lead the other by anywhere from a
    few tens to a few hundred milliseconds, so ANY resolution attempt (not
    only the first) can transiently return ``None`` through NO fault of the
    credential, the SDK, or this test — confirmed by the SAME race
    reproducing directly against ``orchestrator.framework_credentials``
    outside any code this workstream touches, and independently in the
    ALREADY-SHIPPED ``backend/tests/test_work_framework_compat_088.py``
    (T046-T049, a different workstream) on this same host. Retrying a
    handful of times is the honest mitigation on the calling side; it never
    changes what a genuinely revoked/expired credential resolves to (that
    check short-circuits before the clock comparison and is retried here
    only to reach the SAME deterministic ``None`` again).
    """
    caller = None
    for _ in range(attempts):
        caller = credentials.resolve_bearer(token)
        if caller is not None:
            return caller
        time.sleep(delay)
    return caller


def _wait_until_resolvable(credentials: FrameworkCredentialService, token: str) -> None:
    if _resolve_with_retry(credentials, token) is None:
        raise AssertionError(
            "framework credential never became resolvable — this is NOT the known "
            "sub-second clock-skew race (which resolves within a few retries) and "
            "likely indicates a genuine regression"
        )


def _issue_token(credentials, store, owner, sid, **overrides) -> str:
    fields = {
        "owner_id": owner, "caller": consent_from_store(store, owner, sid).observation,
        "name": "conformance credential", "scopes": _ALL_SCOPES,
        "expires_in_seconds": 3600, "max_admissions": 1000,
    }
    fields.update(overrides)
    _view, token = credentials.issue(**fields)
    _wait_until_resolvable(credentials, token)
    return token


def _run_sdk_once(base_url: str, token: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SDK_ROOT), env.get("PYTHONPATH")]))
    return subprocess.run(
        [sys.executable, "-m", "astral_sdk", *args, "--base-url", base_url, "--token", token],
        capture_output=True, text=True, timeout=30, cwd=str(REPO_ROOT), env=env,
    )


def _run_sdk(base_url: str, token: str, *args: str,
            retries: int = 10, delay: float = 0.1) -> subprocess.CompletedProcess:
    """Run the SDK CLI, absorbing the SAME known transient clock-skew race
    :func:`_wait_until_resolvable` documents — every ``tools/call``/``tools/list``
    independently re-resolves the bearer server-side, so the race can recur on
    ANY call, not only the first. Retried ONLY for the exact ``invalid_token``
    signature on an otherwise-untouched credential; every other failure (a
    real scope/conflict/not-found refusal, or an intentionally revoked
    credential — which is unconditionally ``invalid_token`` regardless of any
    clock, so a retry there just re-confirms the same, correct outcome)
    returns immediately, unretried.
    """
    result = _run_sdk_once(base_url, token, *args)
    for _ in range(retries):
        if result.returncode == 0:
            return result
        try:
            code = json.loads(result.stderr).get("code")
        except (ValueError, AttributeError):
            return result
        if code != "invalid_token":
            return result
        time.sleep(delay)
        result = _run_sdk_once(base_url, token, *args)
    return result


def _stderr_json(result: subprocess.CompletedProcess) -> dict[str, Any]:
    return json.loads(result.stderr)


# -- fixture sanity: the recorded shapes actually match the live projection ---

def test_operation_field_set_matches_the_checked_in_contract():
    contract = json.loads((SDK_ROOT / "astral_sdk" / "work_contract.json").read_text(encoding="utf-8"))
    fixture = json.loads((FIXTURES_DIR / "submit_operation_result.json").read_text(encoding="utf-8"))
    assert set(fixture) - {"created"} == set(contract["operation"]["fields"])
    assert fixture["disposition"] in contract["operation"]["dispositions"]


def test_tools_list_fixture_matches_a_live_projection(credentials, session):
    from orchestrator.mcp_projection import project_tools

    store, owner, sid = session
    token = _issue_token(credentials, store, owner, sid)
    caller = _resolve_with_retry(credentials, token)
    live = project_tools(None, owner, {"sub": owner, "_framework_scopes": sorted(caller.scopes)})
    fixture = json.loads((FIXTURES_DIR / "tools_list_result.json").read_text(encoding="utf-8"))
    assert {t.name for t in live} == {t["name"] for t in fixture["tools"]}
    for tool in live:
        assert tool.descriptor["name"] in {t["name"] for t in fixture["tools"]}


# -- separate-process SDK CLI against the live in-process server -------------

def test_sdk_cli_submit_get_cancel_over_a_real_socket(live_server, credentials, session):
    store, owner, sid = session
    token = _issue_token(credentials, store, owner, sid)

    submitted = _run_sdk(live_server, token, "submit", "--idempotency-key", str(uuid.uuid4()),
                        "--name", "A note", "--instructions", "Write one short paragraph.")
    assert submitted.returncode == 0, submitted.stderr
    operation = json.loads(submitted.stdout)
    assert operation["created"] is True
    assert operation["title"] == "A note"

    fetched = _run_sdk(live_server, token, "get", operation["id"])
    assert fetched.returncode == 0, fetched.stderr
    assert json.loads(fetched.stdout)["id"] == operation["id"]

    cancelled = _run_sdk(live_server, token, "cancel", operation["id"],
                        "--expected-revision", str(operation["revision"]))
    assert cancelled.returncode == 0, cancelled.stderr
    assert json.loads(cancelled.stdout)["operation"]["disposition"] == "cancelled"


def test_sdk_cli_cancel_at_a_stale_revision_conflicts_with_no_further_mutation(live_server, credentials, session):
    store, owner, sid = session
    token = _issue_token(credentials, store, owner, sid)
    operation = json.loads(_run_sdk(live_server, token, "submit", "--idempotency-key", str(uuid.uuid4()),
                                    "--name", "A", "--instructions", "B").stdout)

    first = _run_sdk(live_server, token, "cancel", operation["id"],
                    "--expected-revision", str(operation["revision"]))
    assert first.returncode == 0, first.stderr

    # The revision has now moved; retrying with the ORIGINAL (stale) revision
    # must conflict, not silently re-apply.
    stale = _run_sdk(live_server, token, "cancel", operation["id"],
                    "--expected-revision", str(operation["revision"]))
    assert stale.returncode == 1
    assert _stderr_json(stale)["code"] == "assignment_revision_conflict"

    unchanged = json.loads(_run_sdk(live_server, token, "get", operation["id"]).stdout)
    assert unchanged["disposition"] == "cancelled"
    assert unchanged["revision"] == json.loads(first.stdout)["operation"]["revision"]


def test_sdk_cli_denies_a_missing_submit_scope_before_any_mutation(live_server, credentials, session):
    """A credential lacking ``operations.submit`` never even SEES the tool.

    ``orchestrator.mcp_projection._framework_tools`` filters the projected
    catalog by scope BEFORE ``tools/call`` is reachable at all — so calling
    the tool by name anyway hits the server's generic "unavailable" refusal
    (the same one an unknown tool name gets), not the facade's own
    ``framework_scope_required`` (that check is real, and pinned directly
    against the facade in ``backend/tests/test_work_framework_compat_088.py``,
    but it is defense-in-depth: unreachable over MCP once projection already
    filtered the tool out).
    """
    store, owner, sid = session
    token = _issue_token(credentials, store, owner, sid, scopes=("operations.read",))

    tools = json.loads(_run_sdk(live_server, token, "tools").stdout)
    assert "astral_submit_operation" not in {tool["name"] for tool in tools}

    denied = _run_sdk(live_server, token, "submit", "--idempotency-key", str(uuid.uuid4()),
                     "--name", "A", "--instructions", "B")
    assert denied.returncode == 1
    assert "unavailable" in _stderr_json(denied)["error"].lower()

    listed = json.loads(_run_sdk(live_server, token, "list").stdout)
    assert listed["operations"] == []


def test_sdk_cli_only_projects_tools_the_credentials_scopes_admit(live_server, credentials, session):
    store, owner, sid = session
    token = _issue_token(credentials, store, owner, sid, scopes=("operations.read",))
    tools = json.loads(_run_sdk(live_server, token, "tools").stdout)
    names = {tool["name"] for tool in tools}
    assert names == {"astral_get_operation", "astral_list_operations", "astral_get_operation_events"}


def test_sdk_cli_poll_ends_with_a_bounded_auth_error_after_revocation(live_server, credentials, session):
    store, owner, sid = session
    view, token = credentials.issue(
        owner_id=owner, caller=consent_from_store(store, owner, sid).observation,
        name="revocable", scopes=_ALL_SCOPES, expires_in_seconds=3600, max_admissions=1000,
    )
    _wait_until_resolvable(credentials, token)
    operation = json.loads(_run_sdk(live_server, token, "submit", "--idempotency-key", str(uuid.uuid4()),
                                    "--name", "A", "--instructions", "B").stdout)

    credentials.revoke(owner_id=owner, credential_id=view["credential_id"])

    polled = _run_sdk(live_server, token, "poll", operation["id"],
                     "--after-revision", str(operation["revision"]))
    assert polled.returncode == 1
    error = _stderr_json(polled)
    assert error["code"] == "invalid_token"
    assert error["status_code"] == 401


# -- human-only commands: refused in-process, never reachable over MCP -------

@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["decide", "reconcile", "delete"])
async def test_human_only_commands_refuse_before_any_mutation_and_are_never_projected(
    ops, credentials, session, command,
):
    fixture = json.loads((FIXTURES_DIR / "human_only_refusal.json").read_text(encoding="utf-8"))
    assert command in fixture["commands"]

    store, owner, sid = session
    token = _issue_token(credentials, store, owner, sid)
    caller = credentials.resolve_bearer(token)
    submitted = await ops.submit(caller, idempotency_key=str(uuid.uuid4()), name="A", instructions="B")

    with pytest.raises(AssignmentError, match=fixture["safe_error_code"]):
        await getattr(ops, command)(caller, submitted["id"])

    unchanged = await ops.get(caller, submitted["id"])
    assert unchanged["revision"] == submitted["revision"]
    assert unchanged["disposition"] == submitted["disposition"]


def test_human_only_commands_have_no_mcp_tool_name(live_server, credentials, session):
    store, owner, sid = session
    token = _issue_token(credentials, store, owner, sid)
    tools = json.loads(_run_sdk(live_server, token, "tools").stdout)
    names = {tool["name"] for tool in tools}
    for forbidden in ("astral_decide_operation", "astral_reconcile_operation", "astral_delete_operation"):
        assert forbidden not in names


# -- uncertain-outcome vocabulary is real and distinct from "not found" ------

def test_reconciliation_required_is_a_known_nonterminal_disposition():
    contract = json.loads((SDK_ROOT / "astral_sdk" / "work_contract.json").read_text(encoding="utf-8"))
    assert "reconciliation_required" in contract["operation"]["dispositions"]
    assert "reconciliation_required" not in ("completed", "failed", "cancelled")


# -- byte-identical JWT-only path when the flag is off (regression guard) ----

def test_flag_off_projects_no_framework_tool_even_with_a_framework_shaped_bearer(credentials, session, ops):
    """With the flag off, ``mcp_server_endpoint`` never even resolves the
    bearer as a framework credential — restoring the pre-088 JWT-only path
    byte-for-byte (``_framework_bearer_resolver`` returns ``None``)."""
    from orchestrator.mcp_server_endpoint import _framework_bearer_resolver

    prior = flags._flags.get("framework_credentials")
    flags._flags["framework_credentials"] = False
    try:
        orchestrator = _ConformanceOrchestrator(credentials=credentials, ops=ops)
        assert _framework_bearer_resolver(orchestrator) is None
    finally:
        flags._flags["framework_credentials"] = prior
