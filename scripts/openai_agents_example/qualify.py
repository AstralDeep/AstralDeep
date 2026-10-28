"""Checks an external SDK consumer against Astral's authenticated Work endpoint.
Records client source and directly observed server image identities separately without storing credentials or payloads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from scripts.openai_agents_example.agent import AGENT_TOOL_NAMES, AstralAgentSession
from scripts.openai_agents_example.client import (
    AstralWorkClient,
    CredentialRefused,
    MCPProtocolError,
    OperationPendingTimeout,
    ToolCallError,
)
from scripts.openai_agents_example.redaction import scrub_text
from scripts.openai_agents_example.server_identity import observe_server

DEFAULT_BASE_URL = "http://localhost:8001"
DEFAULT_MAX_POLLS = 30
DEFAULT_INTERVAL_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 60.0
SYNTHETIC_OPERATION_NAME = "OpenAI Agents SDK qualification"
SYNTHETIC_INSTRUCTIONS = (
    "Qualification probe: reply with the single word acknowledged. No tools, no data access."
)
SYNTHETIC_PHI_INSTRUCTIONS = "Contact qc-probe@example.com about the follow-up schedule."
_COMPLETION_SUBSCRIPTION_TOOLS = (
    "astral_subscribe_operation_completion",
    "astral_list_operation_completion_subscriptions",
    "astral_cancel_operation_completion_subscription",
)
_HUMAN_ONLY_TOOLS = (
    "astral_decide_operation", "astral_reconcile_operation", "astral_delete_operation",
)
_METADATA_FIELDS = {"resource", "authorization_servers", "scopes_supported", "bearer_methods_supported"}
_SCOPE_REFUSALS = frozenset({"Tool is unavailable or not authorized",
    "Tool is unavailable or no longer authorized", "Work read is no longer authorized", "framework_scope_required"})


@dataclass
class QualificationConfig:
    base_url: str = DEFAULT_BASE_URL
    bearer: str = ""
    readonly_bearer: str | None = None
    other_owner_bearer: str | None = None
    revoked_bearer: str | None = None
    max_polls: int = DEFAULT_MAX_POLLS
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    operation_instructions: str = SYNTHETIC_INSTRUCTIONS
    phi_instructions: str = SYNTHETIC_PHI_INSTRUCTIONS
    transport: Any = None
    server_container: str | None = None

    def _client(self, bearer: str) -> AstralWorkClient:
        return AstralWorkClient(
            base_url=self.base_url, bearer=bearer, transport=self.transport,
        )

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> QualificationConfig:
        env = os.environ if environ is None else environ
        return cls(
            base_url=env.get("ASTRAL_BASE_URL", DEFAULT_BASE_URL),
            bearer=env.get("ASTRAL_WORK_BEARER", ""),
            readonly_bearer=env.get("ASTRAL_WORK_BEARER_READONLY") or None,
            other_owner_bearer=env.get("ASTRAL_WORK_BEARER_OTHER_OWNER") or None,
            revoked_bearer=env.get("ASTRAL_WORK_BEARER_REVOKED") or None,
            server_container=env.get("ASTRAL_QUALIFICATION_SERVER_CONTAINER") or None,
            max_polls=int(env.get("ASTRAL_QUALIFICATION_MAX_POLLS", DEFAULT_MAX_POLLS)),
            interval_seconds=float(
                env.get("ASTRAL_QUALIFICATION_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
            ),
            timeout_seconds=float(
                env.get("ASTRAL_QUALIFICATION_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
            ),
        )


@dataclass
class _Context:
    config: QualificationConfig
    client: AstralWorkClient
    session: AstralAgentSession
    tool_names: list[str]
    submitted: dict[str, Any] = field(default_factory=dict)


def _source_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[a-f0-9]{40}", revision):
        return None
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return revision if status.returncode == 0 and not status.stdout.strip() else None


def _check(name: str, outcome: str, detail: str | None = None) -> dict[str, Any]:
    return {"name": name, "outcome": outcome, "detail": scrub_text(detail) if detail else None}


async def _refusal_detail(call: Any) -> str:
    try:
        await call()
    except ToolCallError as exc:
        return str(exc)
    return "refusal-expected"


async def check_metadata_contract(context: _Context) -> dict[str, Any]:
    metadata_url = f"{context.config.base_url}/.well-known/oauth-protected-resource/mcp"
    async with httpx.AsyncClient(trust_env=False, timeout=15.0, transport=context.config.transport) as probe:
        response = await probe.get(metadata_url)
    if response.status_code != 200:
        return _check("mcp_protected_resource_metadata", "failed", f"status={response.status_code}")
    document = response.json()
    if not _METADATA_FIELDS.issubset(document):
        return _check("mcp_protected_resource_metadata", "failed", "metadata_fields_missing")
    if document.get("bearer_methods_supported") != ["header"]:
        return _check("mcp_protected_resource_metadata", "failed", "bearer_methods_unexpected")
    if "mcp:tools:invoke" not in document.get("scopes_supported", []):
        return _check("mcp_protected_resource_metadata", "failed", "scopes_unexpected")
    discovery = await context.client.discover()
    versions = discovery.get("supportedVersions")
    if versions != ["2026-07-28"]:
        return _check("mcp_discover_contract", "failed", "versions_unexpected")
    return _check("mcp_protected_resource_metadata", "passed")


async def check_tools_listing(context: _Context) -> dict[str, Any]:
    if "astral_submit_operation" not in context.tool_names:
        return _check("mcp_tools_listing_scoped", "failed", "submit_tool_absent")
    session_tools = {tool.name for tool in context.session.build_agent().tools}
    if session_tools != set(AGENT_TOOL_NAMES):
        return _check("mcp_tools_listing_scoped", "failed", "session_tool_set_drifted")
    advertised = session_tools & set(_COMPLETION_SUBSCRIPTION_TOOLS)
    if advertised:
        return _check("mcp_tools_listing_scoped", "failed", "subscription_tools_advertised")
    return _check("mcp_tools_listing_scoped", "passed")


async def check_submit_idempotent_retry(context: _Context) -> dict[str, Any]:
    idempotency_key = str(uuid.uuid4())
    deadline = max(300, int(context.config.timeout_seconds) * 2)
    first = await context.client.submit(
        idempotency_key=idempotency_key, name=SYNTHETIC_OPERATION_NAME,
        instructions=context.config.operation_instructions, deadline_in_seconds=deadline,
    )
    second = await context.client.submit(
        idempotency_key=idempotency_key, name=SYNTHETIC_OPERATION_NAME,
        instructions=context.config.operation_instructions, deadline_in_seconds=deadline,
    )
    if first.get("id") != second.get("id"):
        return _check("work_submit_idempotent_retry", "failed", "operation_id_diverged")
    if first.get("created") is not True or second.get("created") is not False:
        return _check("work_submit_idempotent_retry", "failed", "created_flags_unexpected")
    try:
        if str(uuid.UUID(first.get("id"))) != first["id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        return _check("work_submit_idempotent_retry", "failed", "operation_id_invalid")
    context.submitted["operation"] = first
    return _check("work_submit_idempotent_retry", "passed", f"operation_id={first.get('id')}")


async def check_poll_events_bounded(context: _Context) -> dict[str, Any]:
    operation = context.submitted.get("operation")
    if operation is None:
        return _check("work_poll_events_bounded", "skipped", "no_submitted_operation")
    try:
        first = await context.client.events(operation["id"], after_revision=operation.get("revision"))
    except ToolCallError:
        return _check("work_poll_events_bounded", "failed", "events_request_refused")
    if not isinstance(first.get("revision"), int):
        return _check("work_poll_events_bounded", "failed", "first_poll_revision_missing")
    if first.get("changed") is not True and first.get("operation") is not None:
        return _check("work_poll_events_bounded", "failed", "unchanged_poll_carried_operation")
    revision = first["revision"]
    submission_id = str(uuid.uuid4())
    try:
        applied = await context.client.cancel(
            operation["id"], submission_id=submission_id, expected_revision=revision,
        )
    except ToolCallError:
        return _check("work_poll_events_bounded", "failed", "cancel_request_refused")
    if applied.get("applied") is not True:
        return _check("work_poll_events_bounded", "failed", "cancel_not_applied")
    try:
        terminal = await context.client.wait_terminal(
            operation["id"], after_revision=None,
            max_polls=context.config.max_polls,
            interval_seconds=context.config.interval_seconds,
            timeout_seconds=context.config.timeout_seconds,
        )
    except OperationPendingTimeout:
        return _check("work_poll_events_bounded", "failed", "terminal_poll_timeout")
    if terminal.get("disposition") != "cancelled":
        return _check("work_poll_events_bounded", "failed", "terminal_disposition_unexpected")
    context.submitted["terminal"] = terminal
    return _check("work_poll_events_bounded", "passed", "disposition=cancelled")


async def check_artifact_access(context: _Context) -> dict[str, Any]:
    operation = context.submitted.get("operation")
    if operation is None:
        return _check("work_artifact_access", "skipped", "no_submitted_operation")
    try:
        artifact = await context.client.artifact(operation["id"])
    except ToolCallError:
        return _check("work_artifact_access", "failed", "artifact_request_refused")
    if artifact.get("id") != operation["id"]:
        return _check("work_artifact_access", "failed", "artifact_id_mismatch")
    result = artifact.get("result")
    if (type(artifact.get("revision")) is not int or artifact["revision"] < 1
            or type(result) is not dict or set(result) != {"version", "available", "reason", "content"}
            or type(result["version"]) is not int or result["version"] != 1
            or type(result["available"]) is not bool):
        return _check("work_artifact_access", "failed", "artifact_envelope_invalid")
    if result["available"]:
        if result["reason"] is not None or type(result["content"]) is not dict or not result["content"]:
            return _check("work_artifact_access", "failed", "artifact_content_invalid")
        return _check("work_artifact_access", "passed", "artifact_available=true")
    if result["reason"] not in ("unsupported", "not_completed", "not_retained") or result["content"] is not None:
        return _check("work_artifact_access", "failed", "artifact_unavailable")
    return _check("work_artifact_access", "passed", f"artifact_available=false reason={result['reason']}")


async def check_cancel(context: _Context) -> dict[str, Any]:
    operation = await context.client.submit(
        idempotency_key=str(uuid.uuid4()), name=SYNTHETIC_OPERATION_NAME,
        instructions=context.config.operation_instructions, deadline_in_seconds=3600,
    )
    current = await context.client.get(operation["id"])
    revision = current.get("revision")
    if not isinstance(revision, int):
        return _check("work_cancel", "failed", "revision_unavailable")
    submission_id = str(uuid.uuid4())
    first = await context.client.cancel(
        operation["id"], submission_id=submission_id, expected_revision=revision,
    )
    second = await context.client.cancel(
        operation["id"], submission_id=submission_id, expected_revision=revision,
    )
    cancelled = first.get("operation") or {}
    replayed = second.get("operation") or {}
    if (first.get("applied") is not True or second.get("applied") is not False
            or type(cancelled.get("revision")) is not int or cancelled["revision"] <= revision
            or cancelled.get("id") != operation["id"] or replayed.get("id") != operation["id"]
            or cancelled.get("disposition") != "cancelled" or replayed.get("disposition") != "cancelled"
            or replayed.get("revision") != cancelled["revision"]):
        return _check("work_cancel", "failed", "cancel_or_replay_not_applied_as_expected")
    return _check(
        "work_cancel",
        "passed",
        f"applied={first.get('applied')} replay_disposition={replayed.get('disposition')}",
    )


async def check_phi_refusal(context: _Context) -> dict[str, Any]:
    detail = await _refusal_detail(lambda: context.client.submit(
        idempotency_key=str(uuid.uuid4()), name=SYNTHETIC_OPERATION_NAME,
        instructions=context.config.phi_instructions,
    ))
    if detail != "assignment_sensitive_content_refused":
        return _check("phi_submission_refused", "failed", "expected_phi_refusal_missing")
    return _check("phi_submission_refused", "passed", "sensitive_content_refused")


async def check_readonly_permission_denial(context: _Context) -> dict[str, Any]:
    if not context.config.readonly_bearer:
        return _check("permission_denied_out_of_scope", "skipped", "readonly_bearer_unconfigured")
    async with context.config._client(context.config.readonly_bearer) as restricted:
        tools = [tool.get("name") for tool in await restricted.list_tools()]
        if "astral_submit_operation" in tools:
            return _check("permission_denied_out_of_scope", "failed", "submit_projected_to_readonly")
        detail = await _refusal_detail(lambda: restricted.submit(
            idempotency_key=str(uuid.uuid4()), name=SYNTHETIC_OPERATION_NAME,
            instructions=context.config.operation_instructions,
        ))
        if detail not in _SCOPE_REFUSALS:
            return _check("permission_denied_out_of_scope", "failed", "expected_scope_refusal_missing")
    return _check("permission_denied_out_of_scope", "passed", "scope_refused")


async def check_cross_owner_isolation(context: _Context) -> dict[str, Any]:
    if not context.config.other_owner_bearer:
        return _check("cross_owner_isolated", "skipped", "other_owner_bearer_unconfigured")
    operation = context.submitted.get("operation")
    if operation is None:
        return _check("cross_owner_isolated", "skipped", "no_submitted_operation")
    async with context.config._client(context.config.other_owner_bearer) as other:
        tools = [tool.get("name") for tool in await other.list_tools()]
        if "astral_get_operation" not in tools:
            return _check("cross_owner_isolated", "skipped", "other_owner_lacks_read_scope")
        detail = await _refusal_detail(lambda: other.get(operation["id"]))
    if detail != "work_not_found":
        return _check("cross_owner_isolated", "failed", "expected_owner_refusal_missing")
    return _check("cross_owner_isolated", "passed", "work_not_found")


async def check_revoked_credential_refused(context: _Context) -> dict[str, Any]:
    if not context.config.revoked_bearer:
        return _check("revoked_credential_refused", "skipped", "revoked_bearer_unconfigured")
    async with context.config._client(context.config.revoked_bearer) as revoked:
        try:
            await revoked.discover()
        except CredentialRefused as exc:
            if (exc.status != 401 or not exc.challenge or not exc.challenge.lower().startswith("bearer ")
                    or "resource_metadata=" not in exc.challenge.lower()):
                return _check("revoked_credential_refused", "failed", "challenge_header_missing")
            return _check("revoked_credential_refused", "passed", f"status={exc.status}")
        except MCPProtocolError:
            return _check("revoked_credential_refused", "failed", "unexpected_credential_failure")
    return _check("revoked_credential_refused", "failed", "revoked_bearer_accepted")


async def check_human_only_denials(context: _Context) -> dict[str, Any]:
    denied = []
    for tool in _HUMAN_ONLY_TOOLS:
        detail = await _refusal_detail(
            lambda tool=tool: context.client.call_tool(tool, {"operation_id": str(uuid.uuid4())})
        )
        denied.append(detail in _SCOPE_REFUSALS)
    if not all(denied):
        return _check("human_only_actions_denied", "failed", "expected_human_only_refusal_missing")
    return _check("human_only_actions_denied", "passed", "all_human_only_actions_refused")


def _named(function, name):
    function.receipt_name = name
    return function


CHECKS = (
    _named(check_metadata_contract, "mcp_protected_resource_metadata"),
    _named(check_tools_listing, "mcp_tools_listing_scoped"),
    _named(check_submit_idempotent_retry, "work_submit_idempotent_retry"),
    _named(check_poll_events_bounded, "work_poll_events_bounded"),
    _named(check_artifact_access, "work_artifact_access"),
    _named(check_cancel, "work_cancel"),
    _named(check_phi_refusal, "phi_submission_refused"),
    _named(check_readonly_permission_denial, "permission_denied_out_of_scope"),
    _named(check_cross_owner_isolation, "cross_owner_isolated"),
    _named(check_revoked_credential_refused, "revoked_credential_refused"),
    _named(check_human_only_denials, "human_only_actions_denied"),
)


async def run_qualification(config: QualificationConfig) -> dict[str, Any]:
    if not config.bearer:
        raise SystemExit("ASTRAL_WORK_BEARER is required; issue it in the Connections surface")
    client_before = _source_revision()
    server_before = observe_server(config.base_url, config.server_container)
    async with config._client(config.bearer) as client:
        session = AstralAgentSession(client=client)
        tools = await client.list_tools()
        context = _Context(
            config=config, client=client, session=session,
            tool_names=[tool.get("name") for tool in tools],
        )
        checks = [_check(
            "tracing_disabled_before_first_request",
            "passed" if session.tracing_before_first_request else "failed",
        )]
        for check in CHECKS:
            try:
                checks.append(await check(context))
            except (MCPProtocolError, ToolCallError):
                checks.append(_check(
                    check.receipt_name, "failed", "request_failed",
                ))
    server_after = observe_server(config.base_url, config.server_container)
    checks.append(_check("client_source_observed",
        "passed" if client_before is not None and client_before == _source_revision() else "failed",
        "stable_clean_checkout" if client_before is not None else "client_source_unavailable"))
    checks.append(_check("server_artifact_observed",
        "passed" if server_before is not None and server_before == server_after else "failed",
        "stable_direct_container" if server_before is not None and server_before == server_after
        else "missing_or_changed_server_observation"))
    failures = [item["name"] for item in checks if item["outcome"] == "failed"]
    return {
        "receipt_kind": "synthetic_staging_qualification",
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": config.base_url,
        "client": {"source_revision": client_before},
        "server": server_before,
        "credential_source": "operator_connections_surface",
        "checks": checks,
        "boundaries": {
            "max_polls": config.max_polls,
            "interval_seconds": config.interval_seconds,
            "timeout_seconds": config.timeout_seconds,
        },
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Qualify the OpenAI Agents SDK example against a real Astral deployment.",
    )
    parser.add_argument("--output", default="staging-receipt.json")
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args(argv)
    config = QualificationConfig.from_environment()
    if args.base_url:
        config.base_url = args.base_url
    receipt = asyncio.run(run_qualification(config))
    Path(args.output).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failed = len(receipt["failures"])
    skipped = sum(1 for item in receipt["checks"] if item["outcome"] == "skipped")
    print(
        f"qualification receipt written to {args.output}: "
        f"{len(receipt['checks']) - failed - skipped} passed, {failed} failed, {skipped} skipped"
    )
    return 1 if failed or skipped else 0


if __name__ == "__main__":
    sys.exit(main())
