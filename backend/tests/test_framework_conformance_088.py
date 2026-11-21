"""Tests for end-to-end framework-credential conformance
(orchestrator/framework_credentials.py, mcp_server_endpoint.py): the SDK's own CLI
driving a real MCP server over loopback, covering scope-gated tool projection and
revocation.
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
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from orchestrator.work_admission import (
    OperationOwner,
    OperationRequest,
    OwnerScope,
)
from shared.feature_flags import flags


REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = REPO_ROOT / "sdk"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "framework_conformance"
_ALL_SCOPES = ("operations.submit", "operations.read", "operations.control", "artifacts.read")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-framework-conformance-audit-key")
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.test/realms/astral")


@pytest.fixture
def _framework_flag_on():
    prior = flags._flags.get("framework_credentials")
    flags._flags["framework_credentials"] = True
    yield
    flags._flags["framework_credentials"] = prior


class _ConformanceOrchestrator:

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

    def run(self) -> None:  # noqa: D102
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


def _run_sdk_once(base_url: str, token: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SDK_ROOT), env.get("PYTHONPATH")]))
    return subprocess.run(
        [sys.executable, "-m", "astral_sdk", *args, "--base-url", base_url, "--token", token],
        capture_output=True, text=True, timeout=30, cwd=str(REPO_ROOT), env=env,
    )


def _run_sdk(base_url: str, token: str, *args: str,
            retries: int = 10, delay: float = 0.1) -> subprocess.CompletedProcess:
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


def test_operation_field_set_matches_the_checked_in_contract():
    contract = json.loads((SDK_ROOT / "astral_sdk" / "work_contract.json").read_text(encoding="utf-8"))
    fixture = json.loads((FIXTURES_DIR / "submit_operation_result.json").read_text(encoding="utf-8"))
    assert set(fixture) - {"created"} == set(contract["operation"]["fields"])
    assert fixture["disposition"] in contract["operation"]["dispositions"]


def test_reconciliation_required_is_a_known_nonterminal_disposition():
    contract = json.loads((SDK_ROOT / "astral_sdk" / "work_contract.json").read_text(encoding="utf-8"))
    assert "reconciliation_required" in contract["operation"]["dispositions"]
    assert "reconciliation_required" not in ("completed", "failed", "cancelled")


