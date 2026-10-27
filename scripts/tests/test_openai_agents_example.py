"""Offline, deterministic verification of scripts/openai_agents_example: the fake server
enforces the MCP 2026-07-28 metadata/header contract on every request, models scope- and
owner-aware tool projection, the lifecycle, denial, failure, and recovery paths run over
an in-process transport, redaction keeps credentials and payloads out of logs and
receipts, and the SDK binding disables cloud trace export before any request. No
third-party SDK and no network are required here; the real-SDK qualification suite lives
in scripts/openai_agents_example/tests/.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import sys
import types

import httpx
import pytest

from scripts.openai_agents_example import contract
from scripts.openai_agents_example.agent import (
    AGENT_INSTRUCTIONS, AGENT_TOOL_NAMES, EXCLUDED_TOOL_NAMES, AstralAgentSession,
)
from scripts.openai_agents_example.client import (
    AstralWorkClient, CredentialRefused, MCPProtocolError, OperationPendingTimeout, ToolCallError,
)
from scripts.openai_agents_example.qualify import (
    SYNTHETIC_PHI_INSTRUCTIONS, QualificationConfig, main, run_qualification,
)
from scripts.openai_agents_example.redaction import (
    RedactingFilter, sanitize_line, sanitize_mapping, scrub_text,
)

from scripts.openai_agents_example.tests.mcp_fakes import (
    BEARER, FULL_TOOLS, OPERATION_ID, OTHER_BEARER, READONLY_BEARER, READONLY_TOOLS,
    REVOKED_BEARER, FakeAstral, operation as _operation,
)

def _client(fake, **kwargs):
    defaults = dict(base_url="http://testserver", bearer=BEARER, transport=fake.transport)
    defaults.update(kwargs)
    return AstralWorkClient(**defaults)


def _run(coroutine):
    return asyncio.run(coroutine)


def _install_agents_stub(monkeypatch):
    calls = []

    class FakeTool:
        def __init__(self, name, function):
            self.name = name
            self.function = function

    def function_tool(func=None, *, name_override=None, description_override=None,
                      use_docstring_info=None, strict_mode=None, **_kwargs):
        def decorate(fn):
            calls.append(("function_tool", name_override or fn.__name__))
            return FakeTool(name_override or fn.__name__, fn)

        return decorate(func) if func is not None else decorate

    class FakeAgent:
        def __init__(self, *, name, instructions, tools, model=None):
            calls.append(("agent", name, tuple(tool.name for tool in tools)))
            self.name = name
            self.instructions = instructions
            self.tools = tools
            self.model = model

    module = types.ModuleType("agents")
    module.set_tracing_disabled = lambda flag: calls.append(("set_tracing_disabled", flag))
    module.set_trace_processors = lambda processors: calls.append(("set_trace_processors", list(processors)))
    module.function_tool = function_tool
    module.Agent = FakeAgent
    monkeypatch.setitem(sys.modules, "agents", module)
    return calls


def _qualifying_config(fake, **overrides):
    defaults = dict(
        bearer=BEARER, readonly_bearer=READONLY_BEARER, other_owner_bearer=OTHER_BEARER,
        revoked_bearer=REVOKED_BEARER, transport=fake.transport,
        max_polls=3, interval_seconds=0.0, timeout_seconds=30.0,
    )
    defaults.update(overrides)
    return QualificationConfig(**defaults)


def test_envelope_carries_required_metadata():
    envelope = contract.request_envelope("server/discover", None, request_id=7)
    assert envelope["jsonrpc"] == "2.0" and envelope["id"] == 7
    meta = envelope["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert "io.modelcontextprotocol/clientCapabilities" in meta


def test_headers_carry_mirror_fields():
    headers = contract.request_headers(
        bearer=BEARER, method="tools/call", tool_name="astral_get_operation",
    )
    assert headers["MCP-Protocol-Version"] == "2026-07-28"
    assert headers["Mcp-Method"] == "tools/call"
    assert headers["Mcp-Name"] == "astral_get_operation"
    assert headers["Authorization"] == f"Bearer {BEARER}"


def test_mirror_name_sentinel_for_unsafe_names():
    assert contract.encode_mirror_name("astral_get_operation") == "astral_get_operation"
    unsafe = contract.encode_mirror_name("tool with space/ü")
    assert unsafe.startswith("=?base64?") and unsafe.endswith("?=")
    decoded = base64.b64decode(unsafe[len("=?base64?"):-len("?=")]).decode("utf-8")
    assert decoded == "tool with space/ü"
    assert contract.encode_mirror_name("=?base64?YQ==?=").startswith("=?base64?")


def test_terminal_dispositions_match_server_contract():
    assert contract.TERMINAL_DISPOSITIONS == {"completed", "failed", "cancelled"}
    assert contract.TERMINAL_DISPOSITIONS < contract.DISPOSITIONS


def test_discover_and_tools_listing_over_contract_fakes():
    fake = FakeAstral()

    async def flow():
        async with _client(fake) as client:
            return await client.discover(), await client.list_tools()

    discovery, tools = _run(flow())
    assert discovery["supportedVersions"] == ["2026-07-28"]
    assert tools == FULL_TOOLS


def test_full_lifecycle_paths_over_fake():
    fake = FakeAstral()

    async def flow():
        async with _client(fake) as client:
            submitted = await client.submit(
                idempotency_key="retry-key-1", name="qualification",
                instructions="synthetic instructions",
            )
            replay = await client.submit(
                idempotency_key="retry-key-1", name="qualification",
                instructions="synthetic instructions",
            )
            current = await client.get(submitted["id"])
            listed = await client.list(limit=10)
            terminal = await client.wait_terminal(
                submitted["id"], after_revision=1, interval_seconds=0.0,
                sleep=lambda *_: asyncio.sleep(0),
            )
            artifact = await client.artifact(submitted["id"])
            cancelled = await client.cancel(submitted["id"], expected_revision=terminal["revision"])
            replayed = await client.cancel(
                submitted["id"], submission_id="kept-submission-id",
                expected_revision=terminal["revision"],
            )
            paused = await client.pause(submitted["id"], expected_revision=terminal["revision"])
            return submitted, replay, current, listed, terminal, artifact, cancelled, replayed, paused

    (submitted, replay, current, listed, terminal, artifact,
     cancelled, _replayed, paused) = _run(flow())
    assert submitted["id"] == OPERATION_ID and submitted["created"] is True
    assert replay["id"] == OPERATION_ID and replay["created"] is False
    assert current["id"] == OPERATION_ID
    assert listed["operations"] == []
    assert terminal["disposition"] == "completed"
    assert artifact["id"] == OPERATION_ID
    assert cancelled["applied"] is True
    assert paused["operation"]["disposition"] == "paused"


def test_tool_error_raises_typed_error():
    fake = FakeAstral(behaviors={"astral_get_operation": ("tool_error", "work_not_found")})

    async def flow():
        async with _client(fake) as client:
            await client.get(OPERATION_ID)

    with pytest.raises(ToolCallError, match="work_not_found"):
        _run(flow())


def test_revoked_credential_refused_with_challenge():
    fake = FakeAstral()

    async def flow():
        async with _client(fake, bearer=REVOKED_BEARER) as client:
            await client.discover()

    with pytest.raises(CredentialRefused) as excinfo:
        _run(flow())
    assert excinfo.value.status == 401
    assert excinfo.value.challenge


def test_http_and_rpc_errors_are_typed():
    def unavailable_handler(request):
        return httpx.Response(503, text="unavailable")

    async def flow_http():
        client = AstralWorkClient(
            base_url="http://testserver", bearer=BEARER,
            transport=httpx.MockTransport(unavailable_handler),
        )
        async with client:
            await client.discover()

    with pytest.raises(MCPProtocolError) as excinfo:
        _run(flow_http())
    assert excinfo.value.status == 503

    def rpc_error_handler(request):
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no"}},
        )

    async def flow_rpc():
        client = AstralWorkClient(
            base_url="http://testserver", bearer=BEARER,
            transport=httpx.MockTransport(rpc_error_handler),
        )
        async with client:
            await client.discover()

    with pytest.raises(MCPProtocolError, match="rpc_error"):
        _run(flow_rpc())


def test_malformed_responses_fail_closed():
    def plain_handler(request):
        return httpx.Response(200, text="not-json")

    def wrong_envelope_handler(request):
        return httpx.Response(200, json={"unexpected": True})

    def no_result_handler(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1})

    for handler, code in (
        (plain_handler, "invalid_response_body"),
        (wrong_envelope_handler, "invalid_response_envelope"),
        (no_result_handler, "invalid_response_result"),
    ):
        async def flow(handler=handler):
            client = AstralWorkClient(
                base_url="http://testserver", bearer=BEARER,
                transport=httpx.MockTransport(handler),
            )
            async with client:
                await client.discover()

        with pytest.raises(MCPProtocolError, match=code):
            _run(flow())


def test_transport_failure_is_typed():
    def failing_handler(request):
        raise httpx.ConnectError("down")

    async def flow():
        client = AstralWorkClient(
            base_url="http://testserver", bearer=BEARER,
            transport=httpx.MockTransport(failing_handler),
        )
        async with client:
            await client.discover()

    with pytest.raises(MCPProtocolError, match="transport_unavailable"):
        _run(flow())


def test_wait_terminal_truthful_success_paths():
    for final in ("completed", "failed", "cancelled"):
        fake = FakeAstral(behaviors={"astral_get_operation_events": [
            {"revision": 2, "changed": True, "resync_required": False,
             "operation": _operation("active", 2)},
            {"revision": 3, "changed": True, "resync_required": False,
             "operation": _operation(final, 3)},
        ]})

        async def flow():
            async with _client(fake) as client:
                return await client.wait_terminal(
                    OPERATION_ID, after_revision=1, interval_seconds=0.0, max_polls=5,
                    sleep=lambda *_: asyncio.sleep(0),
                )

        terminal = _run(flow())
        assert terminal["disposition"] == final
        assert terminal["revision"] == 3


def test_wait_terminal_bounded_polls_never_fabricate_state():
    fake = FakeAstral(behaviors={"astral_get_operation_events": {
        "revision": 2, "changed": True, "resync_required": False,
        "operation": _operation("active", 2),
    }})

    async def flow():
        async with _client(fake) as client:
            await client.wait_terminal(
                OPERATION_ID, after_revision=1, interval_seconds=0.0, max_polls=2,
                timeout_seconds=60.0, sleep=lambda *_: asyncio.sleep(0),
            )

    with pytest.raises(OperationPendingTimeout) as excinfo:
        _run(flow())
    assert "polls=2" in str(excinfo.value.detail)
    polls = [
        request for request in fake.requests
        if json.loads(request.content)["method"] == "tools/call"
    ]
    assert len(polls) == 2


def test_wait_terminal_wall_clock_bound():
    fake = FakeAstral()
    ticks = itertools.count(0, 2)

    async def flow():
        client = _client(fake, clock=lambda: float(next(ticks)))
        async with client:
            await client.wait_terminal(
                OPERATION_ID, after_revision=1, interval_seconds=0.0, max_polls=100,
                timeout_seconds=5.0, sleep=lambda *_: asyncio.sleep(0),
            )

    with pytest.raises(OperationPendingTimeout):
        _run(flow())


def test_wait_terminal_resync_drops_cursor():
    fake = FakeAstral(behaviors={"astral_get_operation_events": [
        {"revision": 2, "changed": False, "resync_required": True, "operation": None},
        {"revision": 3, "changed": True, "resync_required": False,
         "operation": _operation("completed", 3)},
    ]})

    async def flow():
        async with _client(fake) as client:
            return await client.wait_terminal(
                OPERATION_ID, after_revision=9, interval_seconds=0.0, max_polls=5,
                sleep=lambda *_: asyncio.sleep(0),
            )

    assert _run(flow())["disposition"] == "completed"


def test_wait_terminal_refuses_out_of_contract_disposition():
    fake = FakeAstral(behaviors={"astral_get_operation_events": [
        {"revision": 2, "changed": True, "resync_required": False,
         "operation": _operation("mysterious", 2)},
    ]})

    async def flow():
        async with _client(fake) as client:
            await client.wait_terminal(
                OPERATION_ID, after_revision=1, interval_seconds=0.0, max_polls=3,
                sleep=lambda *_: asyncio.sleep(0),
            )

    with pytest.raises(MCPProtocolError, match="disposition_out_of_contract"):
        _run(flow())


def test_wait_terminal_rejects_invalid_bounds():
    fake = FakeAstral()

    async def flow():
        async with _client(fake) as client:
            await client.wait_terminal(OPERATION_ID, max_polls=0)

    with pytest.raises(ValueError):
        _run(flow())


def test_redaction_scrubs_credentials_and_payloads():
    scrubbed = scrub_text("bearer afk_alpha.beta.gamma and more afk_short_text")
    assert "afk_" not in scrubbed
    assert "[redacted]" in scrubbed
    assert sanitize_line("token afk_x.y.z trailing") == "token [redacted] trailing"


def test_sanitize_mapping_allowlist():
    sanitized = sanitize_mapping({
        "event": "request_completed", "method": "tools/call", "tool": "astral_get_operation",
        "status": 200, "elapsed_ms": 4, "operation_id": OPERATION_ID,
        "instructions": "secret payload", "authorization": "Bearer afk_x.y.z",
        "unknown_field": "drop-me",
    })
    assert set(sanitized) == {
        "event", "method", "tool", "status", "elapsed_ms", "operation_id",
    }
    assert sanitized["operation_id"] == OPERATION_ID


def test_redacting_filter_cleans_records():
    logger = logging.getLogger("redaction-probe")
    logger.setLevel(logging.INFO)
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    try:
        logger.info("submit failed for afk_alpha.beta.delta with %s", "instructions text")
    finally:
        logger.removeHandler(handler)
    assert "afk_alpha.beta.delta" not in records[0]
    assert "instructions text" in records[0]


def test_session_requires_sdk_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "agents", None)
    fake = FakeAstral()
    with pytest.raises(ImportError, match="requirements.lock.txt"):
        AstralAgentSession(client=_client(fake))


def test_session_disables_tracing_before_any_request(monkeypatch):
    calls = _install_agents_stub(monkeypatch)
    fake = FakeAstral()
    client = _client(fake)
    session = AstralAgentSession(client=client)
    assert calls[0] == ("set_tracing_disabled", True)
    assert calls[1] == ("set_trace_processors", [])
    assert session.tracing_before_first_request is True

    async def flow():
        return await client.get(OPERATION_ID)

    _run(flow())
    assert session.tracing_before_first_request is True
    assert fake.requests


def test_session_detects_requests_before_tracing_disable(monkeypatch):
    calls = _install_agents_stub(monkeypatch)
    fake = FakeAstral()

    async def first_request():
        async with _client(fake) as client:
            await client.get(OPERATION_ID)
            return client

    client = _run(first_request())
    session = AstralAgentSession(client=client)
    assert calls[0] == ("set_tracing_disabled", True)
    assert session.tracing_before_first_request is False


def test_agent_tools_bind_client_and_respect_boundaries(monkeypatch):
    _install_agents_stub(monkeypatch)
    fake = FakeAstral()
    session = AstralAgentSession(client=_client(fake))
    agent = session.build_agent()
    tool_names = [tool.name for tool in agent.tools]
    assert tuple(tool_names) == AGENT_TOOL_NAMES
    assert not EXCLUDED_TOOL_NAMES & set(tool_names)
    assert agent.instructions == AGENT_INSTRUCTIONS
    submit_tool = next(tool for tool in agent.tools if tool.name == "astral_submit_operation")

    async def invoke():
        return await submit_tool.function(
            idempotency_key="bound-key", name="qualification", instructions="bound instructions",
        )

    bound = _run(invoke())
    assert bound["id"] == OPERATION_ID
    assert fake.requests[-1].headers["Mcp-Name"] == "astral_submit_operation"


def test_excluded_tools_cover_subscriptions_emergency_and_pause():
    assert EXCLUDED_TOOL_NAMES == {
        "astral_pause_operation",
        "astral_emergency_status", "astral_emergency_stop", "astral_emergency_resume",
        "astral_subscribe_operation_completion",
        "astral_list_operation_completion_subscriptions",
        "astral_cancel_operation_completion_subscription",
    }


def test_qualification_config_from_environment():
    config = QualificationConfig.from_environment({
        "ASTRAL_BASE_URL": "http://example.test:9999",
        "ASTRAL_WORK_BEARER": BEARER,
        "ASTRAL_WORK_BEARER_READONLY": READONLY_BEARER,
        "ASTRAL_WORK_BEARER_OTHER_OWNER": OTHER_BEARER,
        "ASTRAL_WORK_BEARER_REVOKED": REVOKED_BEARER,
        "ASTRAL_QUALIFICATION_MAX_POLLS": "7",
        "ASTRAL_QUALIFICATION_INTERVAL_SECONDS": "0.5",
        "ASTRAL_QUALIFICATION_TIMEOUT_SECONDS": "45",
    })
    assert config.base_url == "http://example.test:9999"
    assert config.bearer == BEARER
    assert config.max_polls == 7 and config.interval_seconds == 0.5 and config.timeout_seconds == 45.0


def test_run_qualification_requires_bearer():
    with pytest.raises(SystemExit, match="ASTRAL_WORK_BEARER"):
        _run(run_qualification(QualificationConfig(bearer="")))


def test_run_qualification_full_receipt_and_sanitization(monkeypatch):
    _install_agents_stub(monkeypatch)
    fake = FakeAstral(behaviors={"astral_get_artifact": ("tool_error", "work_not_found")})
    captured = []

    async def flow():
        receipt = await run_qualification(_qualifying_config(fake))
        captured.append(json.dumps(receipt))
        return receipt

    receipt = _run(flow())
    outcomes = {item["name"]: item["outcome"] for item in receipt["checks"]}
    assert outcomes["tracing_disabled_before_first_request"] == "passed"
    assert outcomes["mcp_protected_resource_metadata"] == "passed"
    assert outcomes["mcp_tools_listing_scoped"] == "passed"
    assert outcomes["work_submit_idempotent_retry"] == "passed"
    assert outcomes["work_poll_events_bounded"] == "passed"
    assert outcomes["work_artifact_access"] == "passed"
    assert outcomes["work_cancel"] == "passed"
    assert outcomes["phi_submission_refused"] == "passed"
    assert outcomes["permission_denied_out_of_scope"] == "passed"
    assert outcomes["cross_owner_isolated"] == "passed"
    assert outcomes["revoked_credential_refused"] == "passed"
    assert outcomes["human_only_actions_denied"] == "passed"
    assert receipt["receipt_kind"] == "synthetic_staging_qualification"
    assert receipt["failures"] == []
    assert BEARER not in captured[0]
    assert "qc-probe@example.com" not in captured[0]


def test_run_qualification_records_failures_and_skips(monkeypatch):
    _install_agents_stub(monkeypatch)
    fake = FakeAstral()

    async def flow():
        return await run_qualification(_qualifying_config(
            fake, readonly_bearer=None, other_owner_bearer=None, revoked_bearer=None,
        ))

    receipt = _run(flow())
    outcomes = {item["name"]: item["outcome"] for item in receipt["checks"]}
    assert outcomes["permission_denied_out_of_scope"] == "skipped"
    assert outcomes["cross_owner_isolated"] == "skipped"
    assert outcomes["revoked_credential_refused"] == "skipped"
    assert receipt["failures"] == []


def test_main_writes_receipt(monkeypatch, tmp_path):
    _install_agents_stub(monkeypatch)
    fake = FakeAstral()
    output = tmp_path / "receipt.json"
    original = QualificationConfig.from_environment

    def with_transport(environ=None):
        config = original(environ)
        config.transport = fake.transport
        return config

    monkeypatch.setattr(QualificationConfig, "from_environment", staticmethod(with_transport))
    monkeypatch.setenv("ASTRAL_WORK_BEARER", BEARER)
    exit_code = main(["--output", str(output), "--base-url", "http://testserver"])
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["receipt_kind"] == "synthetic_staging_qualification"
    assert document["checks"]
    assert exit_code == 0


def test_phi_instructions_are_synthetic():
    assert "@example.com" in SYNTHETIC_PHI_INSTRUCTIONS
    assert "follow-up schedule" in SYNTHETIC_PHI_INSTRUCTIONS


def _outcome(receipt, name):
    return next(item["outcome"] for item in receipt["checks"] if item["name"] == name)


def test_source_revision_handles_git_failures(monkeypatch):
    from scripts.openai_agents_example import qualify

    def raising_run(*args, **kwargs):
        raise OSError("no git")

    monkeypatch.setattr(qualify.subprocess, "run", raising_run)
    assert qualify._source_revision() is None

    def garbage_run(*args, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout="not-a-revision\n")

    monkeypatch.setattr(qualify.subprocess, "run", garbage_run)
    assert qualify._source_revision() is None


def test_metadata_contract_failures(monkeypatch):
    _install_agents_stub(monkeypatch)

    async def collect(fake):
        receipt = await run_qualification(_qualifying_config(fake))
        return {item["name"]: item["outcome"] for item in receipt["checks"]}

    cases = [
        (FakeAstral(metadata_status=500), "mcp_protected_resource_metadata", "failed"),
        (FakeAstral(metadata={"resource": "x"}), "mcp_protected_resource_metadata", "failed"),
        (FakeAstral(metadata={
            "resource": "x", "authorization_servers": ["iam"],
            "bearer_methods_supported": ["header"],
        }), "mcp_protected_resource_metadata", "failed"),
        (FakeAstral(metadata={
            "resource": "x", "authorization_servers": ["iam"],
            "scopes_supported": ["mcp:tools:invoke"], "bearer_methods_supported": ["body"],
        }), "mcp_protected_resource_metadata", "failed"),
        (FakeAstral(discovery={"supportedVersions": ["1999-01-01"]}),
         "mcp_discover_contract", "failed"),
    ]
    for fake, name, expected in cases:
        outcomes = _run(collect(fake))
        assert outcomes[name] == expected, name


def test_tools_listing_and_submit_branches(monkeypatch):
    _install_agents_stub(monkeypatch)

    async def collect(fake, **overrides):
        receipt = await run_qualification(_qualifying_config(fake, **overrides))
        return {item["name"]: item["outcome"] for item in receipt["checks"]}

    outcomes = _run(collect(FakeAstral(tools_by_bearer={BEARER: READONLY_TOOLS})))
    assert outcomes["mcp_tools_listing_scoped"] == "failed"

    outcomes = _run(collect(
        FakeAstral(behaviors={"astral_submit_operation": [
            {**_operation("queued", 1), "created": True, "id": OPERATION_ID},
            {**_operation("queued", 1), "created": False, "id": "another-op"},
        ]}),
    ))
    assert outcomes["work_submit_idempotent_retry"] == "failed"

    outcomes = _run(collect(
        FakeAstral(behaviors={"astral_submit_operation": [
            {**_operation("queued", 1), "created": False},
        ]}),
    ))
    assert outcomes["work_submit_idempotent_retry"] == "failed"

    outcomes = _run(collect(
        FakeAstral(behaviors={"astral_get_operation": ("tool_error", "work_unavailable")}),
    ))
    assert outcomes["work_cancel"] == "failed"

    outcomes = _run(collect(FakeAstral(behaviors={
        "astral_submit_operation": ("tool_error", "work_repository_unavailable"),
    })))
    assert outcomes["work_poll_events_bounded"] == "skipped"
    assert outcomes["work_artifact_access"] == "skipped"


def test_denial_branches(monkeypatch):
    _install_agents_stub(monkeypatch)

    async def collect(fake, **overrides):
        receipt = await run_qualification(_qualifying_config(fake, **overrides))
        return {item["name"]: item["outcome"] for item in receipt["checks"]}

    outcomes = _run(collect(
        FakeAstral(), phi_instructions="Plain synthetic text without trigger patterns",
    ))
    assert outcomes["phi_submission_refused"] == "failed"

    outcomes = _run(collect(
        FakeAstral(tools_by_bearer={READONLY_BEARER: FULL_TOOLS}),
    ))
    assert outcomes["permission_denied_out_of_scope"] == "failed"

    outcomes = _run(collect(
        FakeAstral(readonly_submit_error="work_not_found"),
    ))
    assert outcomes["permission_denied_out_of_scope"] == "failed"

    outcomes = _run(collect(
        FakeAstral(tools_by_bearer={OTHER_BEARER: [{"name": "astral_list_operations"}]}),
    ))
    assert outcomes["cross_owner_isolated"] == "skipped"

    outcomes = _run(collect(
        FakeAstral(other_owner_get_succeeds=True),
    ))
    assert outcomes["cross_owner_isolated"] == "failed"

    outcomes = _run(collect(FakeAstral(revoked_challenge=False)))
    assert outcomes["revoked_credential_refused"] == "failed"

    outcomes = _run(collect(FakeAstral(revoked_status=503)))
    assert outcomes["revoked_credential_refused"] == "failed"

    outcomes = _run(collect(FakeAstral(revoked_bearers=set())))
    assert outcomes["revoked_credential_refused"] == "failed"

    outcomes = _run(collect(FakeAstral(behaviors={
        "astral_decide_operation": {"approved": True},
    })))
    assert outcomes["human_only_actions_denied"] == "failed"


def test_run_qualification_records_check_exceptions(monkeypatch):
    _install_agents_stub(monkeypatch)
    fake = FakeAstral(behaviors={
        "astral_get_operation_events": MCPProtocolError("invalid_poll_revision"),
    })

    async def flow():
        return await run_qualification(_qualifying_config(fake))

    receipt = _run(flow())
    outcomes = {item["name"]: item["outcome"] for item in receipt["checks"]}
    assert outcomes["work_poll_events_bounded"] == "failed"


def test_main_returns_failure_exit_code(monkeypatch, tmp_path):
    _install_agents_stub(monkeypatch)
    fake = FakeAstral()
    output = tmp_path / "receipt.json"
    original = QualificationConfig.from_environment

    def with_transport(environ=None):
        config = original(environ)
        config.transport = fake.transport
        config.phi_instructions = "plain synthetic text"
        return config

    monkeypatch.setattr(QualificationConfig, "from_environment", staticmethod(with_transport))
    monkeypatch.setenv("ASTRAL_WORK_BEARER", BEARER)
    exit_code = main(["--output", str(output), "--base-url", "http://testserver"])
    assert exit_code == 1
    document = json.loads(output.read_text(encoding="utf-8"))
    assert "phi_submission_refused" in document["failures"]


def test_agent_tool_wrappers_bind_every_operation(monkeypatch):
    _install_agents_stub(monkeypatch)
    fake = FakeAstral()
    session = AstralAgentSession(client=_client(fake))

    async def flow():
        get_tool, list_tool, events_tool, artifact_tool, cancel_tool = (
            session.build_agent().tools[1:]
        )
        await get_tool.function(operation_id=OPERATION_ID)
        await list_tool.function(limit=5, after_id=None)
        await events_tool.function(operation_id=OPERATION_ID, after_revision=2)
        await artifact_tool.function(operation_id=OPERATION_ID)
        await cancel_tool.function(
            operation_id=OPERATION_ID, submission_id="wrapper-submission", expected_revision=1,
        )

    _run(flow())
    tools_called = [
        json.loads(request.content).get("params", {}).get("name")
        for request in fake.requests
        if json.loads(request.content)["method"] == "tools/call"
    ]
    assert tools_called == [
        "astral_get_operation", "astral_list_operations", "astral_get_operation_events",
        "astral_get_artifact", "astral_cancel_operation",
    ]
