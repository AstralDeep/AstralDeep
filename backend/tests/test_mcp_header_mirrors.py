"""Tests for the bounded x-mcp-header mirror contract: projection withholding of
unsupported annotation forms in mcp_projection.py and pre-dispatch Mcp-Param
mirror-header matching plus CORS preflight reflection in mcp_server_endpoint.py.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from orchestrator.mcp_projection import (
    MirrorParam,
    has_unsupported_mirrors,
    project_tools,
)
from orchestrator.mcp_server_endpoint import install_mcp_server
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationRequest,
    OwnerScope,
    WorkAdmissionCoordinator,
)
from shared import node_identity
from shared.protocol import AgentCard, AgentSkill, MCPResponse
from tests.helpers.dev_mesh import MCP_BASE, MCP_META, SignedClient, door_for, shared_node


BASE = MCP_BASE
META = MCP_META


class _Permissions:
    def __init__(self):
        self.allowed = True

    def is_tool_allowed(self, user_id, agent_id, tool_name):
        return self.allowed

    def get_enabled_scope_names(self, user_id, agent_id):
        return ["tools:read"] if self.allowed else []

    def get_tool_scope(self, agent_id, tool_name):
        return "tools:read"

    def list_disabled_agents(self, _user_id):
        return ()


def _skill(skill_id, input_schema, *, scope="tools:read", output_schema=None):
    return AgentSkill(
        id=skill_id,
        name=skill_id,
        description=f"Skill {skill_id}",
        scope=scope,
        input_schema=input_schema,
        output_schema=output_schema,
    )


class _Orchestrator:
    def __init__(self, skills_by_agent):
        self.history = SimpleNamespace(db=SimpleNamespace())
        self.tool_permissions = _Permissions()
        self.security_flags = {}
        self.local_agents = {agent_id: object() for agent_id in skills_by_agent}
        self.agents = {agent_id: object() for agent_id in skills_by_agent}
        self.agent_cards = {
            agent_id: AgentCard(
                name=agent_id,
                description="Test agent",
                agent_id=agent_id,
                skills=list(skills),
            )
            for agent_id, skills in skills_by_agent.items()
        }
        configs = (
            AdmissionClassConfig(AdmissionClass.GLOBAL, None, 2, 0, None, "test"),
            AdmissionClassConfig(
                AdmissionClass.MCP,
                AdmissionClass.GLOBAL,
                1,
                2,
                1000,
                "test",
            ),
        )
        self.work_admission = WorkAdmissionCoordinator(
            admission_classes=configs,
            repository=InMemoryWorkAdmissionRepository(),
            clock=lambda: datetime.now(UTC),
            slot_lease=timedelta(seconds=30),
        )
        self.runtime_observability = RuntimeObservability()
        self.calls = []

    def _is_draft_agent(self, agent_id):
        return False

    async def _call_work_admission(self, method, *args, **kwargs):
        return method(*args, **kwargs)

    async def _claim_personal_agent_operation(
        self,
        *,
        owner_user_id,
        operation_kind,
        idempotency_namespace,
        idempotency_key,
        normalized_identity,
        admission_class,
        request_generation,
        wait_seconds,
        **kwargs,
    ):
        del normalized_identity, wait_seconds, kwargs
        owner = OperationOwner(OwnerScope.USER, owner_user_id, None)
        admitted = self.work_admission.submit(
            OperationRequest(
                operation_kind=operation_kind,
                admission_class=admission_class,
                owner=owner,
                submission_id=uuid.uuid4(),
                idempotency_namespace=idempotency_namespace,
                idempotency_key=idempotency_key,
                normalized_input_digest="a" * 64,
                chat_id=None,
                parent_operation_id=None,
                connection_generation=None,
                request_generation=request_generation,
            )
        )
        if not admitted.accepted:
            return None
        claim = self.work_admission.claim_operation(admission_class, admitted.operation_id)
        return (owner, claim) if claim is not None else None

    async def execute_mcp_tool(self, **kwargs):
        self.calls.append(kwargs)
        return MCPResponse(
            result={"echo": kwargs["arguments"]},
            ui_components=[{"type": "text", "content": "Rendered result"}],
            responder_info={"name": "test", "version": "1"},
        )


def _annotated_schema(
    annotation="Mcp-Param-Region",
    prop_type="string",
    *,
    prop_name="region",
    required=False,
):
    prop = {"type": prop_type}
    if annotation is not None:
        prop["x-mcp-header"] = annotation
    schema = {"type": "object", "properties": {prop_name: prop}}
    if required:
        schema["required"] = [prop_name]
    return schema


def _endpoint_skills():
    return {
            "weather-1": [
                _skill("geo", _annotated_schema()),
                _skill("metrics", {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer", "x-mcp-header": "Mcp-Param-Count"},
                        "enabled": {"type": "boolean", "x-mcp-header": "Mcp-Param-Enabled"},
                        "ratio": {"type": "number", "x-mcp-header": "Mcp-Param-Ratio"},
                    },
                }),
                _skill("optional_geo", {
                    "type": "object",
                    "properties": {
                        "region": {
                            "type": ["string", "null"],
                            "x-mcp-header": "Mcp-Param-Region",
                        },
                    },
                }),
                _skill("census", {
                    "type": "object",
                    "properties": {
                        "a": {"type": "string", "x-mcp-header": "Mcp-Param-Region"},
                        "b": {"type": "string", "x-mcp-header": "Mcp-Param-Region"},
                    },
                }),
                _skill("surveyor", {
                           "type": "object",
                           "properties": {
                               "outer": {
                                   "type": "object",
                                   "properties": {
                                       "inner": {
                                           "type": "string",
                                           "x-mcp-header": "Mcp-Param-Inner",
                                       },
                                   },
                               },
                           },
                       }),
            ]
        }


module_loop = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def mesh(tmp_path_factory):
    async with shared_node(tmp_path_factory, _Orchestrator(_endpoint_skills())) as served:
        yield served


@pytest_asyncio.fixture(loop_scope="module")
async def endpoint(mesh):
    orchestrator = _Orchestrator(_endpoint_skills())
    async with door_for(mesh, orchestrator, lambda app, host: install_mcp_server(app, host, public_base_url=BASE)) as door:
        yield SignedClient(door, door.mint(["geo", "metrics", "optional_geo"])), orchestrator


def _request(method, params=None, *, headers=None, request_id=1):
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {**(params or {}), "_meta": dict(META)},
    }
    merged = {
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    }
    merged.update(headers or {})
    return body, merged


async def _call(client, name, arguments, *, headers=None):
    body, request_headers = _request(
        "tools/call",
        {"name": name, "arguments": arguments},
        headers={"Mcp-Name": name, **(headers or {})},
    )
    return await client.post("/mcp", json=body, headers=request_headers)


def test_supported_scalar_annotation_is_advertised_with_mirror_params():
    orchestrator = _Orchestrator({"weather-1": [_skill("geo", _annotated_schema())]})
    tools = project_tools(orchestrator, "u1")
    assert [tool.name for tool in tools] == ["geo"]
    assert tools[0].mirror_params == (
        MirrorParam(argument="region", header="Mcp-Param-Region"),
    )
    assert tools[0].mirrors_supported is True
    assert has_unsupported_mirrors(orchestrator, "u1", "geo") is False
    assert (
        tools[0].descriptor["inputSchema"]["properties"]["region"]["x-mcp-header"]
        == "Mcp-Param-Region"
    )


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "x-mcp-header": "Mcp-Param-Region", "properties": {}},
        {
            "type": "object",
            "properties": {
                "outer": {
                    "type": "object",
                    "properties": {
                        "inner": {"type": "string", "x-mcp-header": "Mcp-Param-Inner"},
                    },
                },
            },
        },
        {
            "type": "object",
            "$defs": {"s": {"type": "string", "x-mcp-header": "Mcp-Param-S"}},
            "properties": {},
        },
        {
            "type": "object",
            "properties": {
                "list": {
                    "type": "array",
                    "items": {"type": "string", "x-mcp-header": "Mcp-Param-L"},
                },
            },
        },
        {
            "type": "object",
            "properties": {},
            "additionalProperties": {
                "type": "string",
                "x-mcp-header": "Mcp-Param-A",
            },
        },
        {
            "type": "object",
            "allOf": [{
                "type": "object",
                "properties": {
                    "a": {"type": "string", "x-mcp-header": "Mcp-Param-A"},
                },
            }],
            "properties": {},
        },
        {"type": "object", "properties": {"x-mcp-header": {"type": "string"}}},
    ],
)
def test_prohibited_annotation_locations_withhold_the_tool(schema):
    orchestrator = _Orchestrator({"weather-1": [_skill("geo", schema)]})
    assert project_tools(orchestrator, "u1") == ()
    assert has_unsupported_mirrors(orchestrator, "u1", "geo") is True


def test_output_schema_annotation_withholds_the_tool():
    orchestrator = _Orchestrator({
        "weather-1": [
            _skill(
                "geo",
                _annotated_schema(),
                output_schema={
                    "type": "object",
                    "properties": {
                        "out": {"type": "string", "x-mcp-header": "Mcp-Param-Out"},
                    },
                },
            ),
        ],
    })
    assert project_tools(orchestrator, "u1") == ()
    assert has_unsupported_mirrors(orchestrator, "u1", "geo") is True


@pytest.mark.parametrize(
    "header",
    [
        "X-Custom",
        "Mcp-Param-",
        "",
        "Mcp Param Region",
        "Mcp-Param-Region\n",
        "Mcp-Param-Région",
        True,
        42,
    ],
)
def test_invalid_annotation_values_withhold_the_tool(header):
    orchestrator = _Orchestrator(
        {"weather-1": [_skill("geo", _annotated_schema(annotation=header))]}
    )
    assert project_tools(orchestrator, "u1") == ()
    assert has_unsupported_mirrors(orchestrator, "u1", "geo") is True


@pytest.mark.parametrize(
    "prop_type",
    ["object", "array", "null", ["object", "null"], ["array", "string"], {"a": 1}],
)
def test_non_scalar_annotated_properties_withhold_the_tool(prop_type):
    orchestrator = _Orchestrator(
        {"weather-1": [_skill("geo", _annotated_schema(prop_type=prop_type))]}
    )
    assert project_tools(orchestrator, "u1") == ()
    assert has_unsupported_mirrors(orchestrator, "u1", "geo") is True


def test_untyped_annotated_property_withholds_the_tool():
    schema = {
        "type": "object",
        "properties": {"region": {"x-mcp-header": "Mcp-Param-Region"}},
    }
    orchestrator = _Orchestrator({"weather-1": [_skill("geo", schema)]})
    assert project_tools(orchestrator, "u1") == ()


def test_nullable_scalar_annotation_is_supported():
    schema = _annotated_schema(prop_type=["string", "null"])
    orchestrator = _Orchestrator({"weather-1": [_skill("geo", schema)]})
    tools = project_tools(orchestrator, "u1")
    assert tools[0].mirror_params == (
        MirrorParam(argument="region", header="Mcp-Param-Region"),
    )


def test_duplicate_header_names_withhold_the_tool():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "x-mcp-header": "Mcp-Param-Region"},
            "b": {"type": "string", "x-mcp-header": "Mcp-Param-Region"},
        },
    }
    orchestrator = _Orchestrator({"weather-1": [_skill("census", schema)]})
    assert project_tools(orchestrator, "u1") == ()
    assert has_unsupported_mirrors(orchestrator, "u1", "census") is True


def test_case_variant_duplicate_header_names_withhold_the_tool():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "x-mcp-header": "Mcp-Param-Region"},
            "b": {"type": "string", "x-mcp-header": "mcp-param-region"},
        },
    }
    orchestrator = _Orchestrator({"weather-1": [_skill("census", schema)]})
    assert project_tools(orchestrator, "u1") == ()
    assert has_unsupported_mirrors(orchestrator, "u1", "census") is True


def test_multiple_distinct_mirror_params_are_supported():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "integer", "x-mcp-header": "Mcp-Param-One"},
            "b": {"type": "string", "x-mcp-header": "Mcp-Param-Two"},
        },
    }
    orchestrator = _Orchestrator({"weather-1": [_skill("pair", schema)]})
    tools = project_tools(orchestrator, "u1")
    assert tools[0].mirror_params == (
        MirrorParam(argument="a", header="Mcp-Param-One"),
        MirrorParam(argument="b", header="Mcp-Param-Two"),
    )
    assert tools[0].mirrors_supported is True


def test_unannotated_tool_keeps_empty_mirror_spec():
    schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    orchestrator = _Orchestrator({"weather-1": [_skill("plain", schema)]})
    tools = project_tools(orchestrator, "u1")
    assert tools[0].mirror_params == ()
    assert tools[0].mirrors_supported is True
    assert has_unsupported_mirrors(orchestrator, "u1", "plain") is False


@module_loop
async def test_matching_mirror_header_dispatches(endpoint):
    client, orchestrator = endpoint
    response = await _call(client, "geo", {"region": "eu-west"}, headers={"Mcp-Param-Region": "eu-west"})
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"echo": {"region": "eu-west"}}
    assert orchestrator.calls[0]["arguments"] == {"region": "eu-west"}
    claims = orchestrator.calls[0]["claims"]
    assert claims["sub"] == node_identity.bound_mesh_owner()
    assert claims["_caller_id"] == client.ticket.id


@pytest.mark.parametrize("other", ["eu-west", "us-east"])
@module_loop
async def test_duplicate_mirror_headers_are_refused_before_dispatch(endpoint, other):
    client, orchestrator = endpoint
    body, headers = _request("tools/call", {"name": "geo", "arguments": {"region": "eu-west"}})
    response = await client.post(
        "/mcp", json=body,
        headers=[*headers.items(), ("Mcp-Name", "geo"),
                 ("Mcp-Param-Region", "eu-west"), ("mcp-param-region", other)],
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    assert orchestrator.calls == []


@pytest.mark.parametrize("count,advertised", [(16, True), (17, False)])
def test_projection_rejects_mirror_counts_beyond_browser_preflight_capacity(count, advertised):
    schema = {"type": "object", "properties": {
        f"arg{index}": {"type": "string", "x-mcp-header": f"Mcp-Param-{index}"}
        for index in range(count)
    }}
    orchestrator = _Orchestrator({"weather-1": [_skill("geo", schema)]})
    assert bool(project_tools(orchestrator, "u1")) is advertised


@pytest.mark.parametrize("length,advertised", [(128, True), (129, False)])
def test_projection_rejects_header_names_beyond_browser_preflight_capacity(length, advertised):
    schema = _annotated_schema(annotation="Mcp-Param-" + "a" * (length - 10))
    orchestrator = _Orchestrator({"weather-1": [_skill("geo", schema)]})
    assert bool(project_tools(orchestrator, "u1")) is advertised


@module_loop
async def test_missing_mirror_header_is_refused_before_dispatch(endpoint):
    client, orchestrator = endpoint
    response = await _call(client, "geo", {"region": "eu-west"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    assert orchestrator.calls == []
    assert (
        orchestrator.work_admission.inspect_admission_class(AdmissionClass.MCP).active_count
        == 0
    )


@module_loop
async def test_mismatched_mirror_value_is_refused_before_dispatch(endpoint):
    client, orchestrator = endpoint
    response = await _call(
        client,
        "geo",
        {"region": "eu-west"},
        headers={"Mcp-Param-Region": "us-east"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    assert orchestrator.calls == []


@pytest.mark.parametrize(
    "value",
    [
        "eu west",
        "=?base64?QdW?=",
        "=?base64??=",
        "=?base64?ZXUtd2VzdA==",
        "=?base64?ZXUtd2VzdA==?==",
    ],
)
@module_loop
async def test_malformed_mirror_values_are_refused_before_dispatch(endpoint, value):
    client, orchestrator = endpoint
    response = await _call(
        client,
        "geo",
        {"region": "eu-west"},
        headers={"Mcp-Param-Region": value},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    assert orchestrator.calls == []


@module_loop
async def test_base64_sentinel_mirror_value_matches_utf8_argument(endpoint):
    client, orchestrator = endpoint
    encoded = base64.b64encode("café".encode()).decode()
    response = await _call(
        client,
        "geo",
        {"region": "café"},
        headers={"Mcp-Param-Region": f"=?base64?{encoded}?="},
    )
    assert response.status_code == 200
    assert orchestrator.calls[0]["arguments"] == {"region": "café"}


@module_loop
async def test_base64_sentinel_mirror_value_matches_numeric_argument(endpoint):
    client, orchestrator = endpoint
    encoded = base64.b64encode(b"42").decode()
    response = await _call(
        client,
        "metrics",
        {"count": 42},
        headers={"Mcp-Param-Count": f"=?base64?{encoded}?="},
    )
    assert response.status_code == 200
    assert orchestrator.calls[0]["arguments"] == {"count": 42}


@pytest.mark.parametrize("header_name", ["MCP-PARAM-REGION", "mcp-param-region", "Mcp-Param-Region"])
@module_loop
async def test_mirror_header_name_matching_is_case_insensitive(endpoint, header_name):
    client, orchestrator = endpoint
    response = await _call(
        client,
        "geo",
        {"region": "eu-west"},
        headers={header_name: "eu-west"},
    )
    assert response.status_code == 200
    assert orchestrator.calls[0]["arguments"] == {"region": "eu-west"}


@module_loop
async def test_absent_optional_argument_requires_header_absence(endpoint):
    client, orchestrator = endpoint
    response = await _call(client, "optional_geo", {})
    assert response.status_code == 200
    assert orchestrator.calls[0]["arguments"] == {}


@module_loop
async def test_null_argument_requires_header_absence(endpoint):
    client, orchestrator = endpoint
    response = await _call(client, "optional_geo", {"region": None})
    assert response.status_code == 200
    assert orchestrator.calls[0]["arguments"] == {"region": None}


@module_loop
async def test_header_for_absent_argument_is_refused(endpoint):
    client, orchestrator = endpoint
    response = await _call(client, "optional_geo", {}, headers={"Mcp-Param-Region": "eu-west"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    assert orchestrator.calls == []


@module_loop
async def test_header_for_null_argument_is_refused(endpoint):
    client, orchestrator = endpoint
    response = await _call(
        client,
        "optional_geo",
        {"region": None},
        headers={"Mcp-Param-Region": "eu-west"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    assert orchestrator.calls == []


@module_loop
async def test_integer_boolean_and_number_mirrors(endpoint):
    client, orchestrator = endpoint
    ok = await _call(
        client,
        "metrics",
        {"count": 42, "enabled": True, "ratio": 3.5},
        headers={
            "Mcp-Param-Count": "42",
            "Mcp-Param-Enabled": "true",
            "Mcp-Param-Ratio": "3.5",
        },
    )
    assert ok.status_code == 200
    assert orchestrator.calls[0]["arguments"] == {"count": 42, "enabled": True, "ratio": 3.5}

    for wrong in (
        {"Mcp-Param-Count": "43", "Mcp-Param-Enabled": "true", "Mcp-Param-Ratio": "3.5"},
        {"Mcp-Param-Count": "42", "Mcp-Param-Enabled": "1", "Mcp-Param-Ratio": "3.5"},
        {"Mcp-Param-Count": "42", "Mcp-Param-Enabled": "true", "Mcp-Param-Ratio": "3.50"},
    ):
        refused = await _call(client, "metrics", {"count": 42, "enabled": True, "ratio": 3.5}, headers=wrong)
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == -32020
    assert len(orchestrator.calls) == 1


@module_loop
async def test_non_scalar_argument_on_annotated_property_is_refused(endpoint):
    client, orchestrator = endpoint
    response = await _call(
        client,
        "geo",
        {"region": {"nested": "eu-west"}},
        headers={"Mcp-Param-Region": "eu-west"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    assert orchestrator.calls == []


@module_loop
async def test_tools_list_omits_unsupported_annotated_tools(endpoint):
    client, _ = endpoint
    body, headers = _request("tools/list")
    response = await client.post("/mcp", json=body, headers=headers)
    names = [tool["name"] for tool in response.json()["result"]["tools"]]
    assert names == ["geo", "metrics", "optional_geo"]


def _challenge_error(response):
    return response.headers["www-authenticate"].rpartition('error="')[2].rstrip('"')


@module_loop
async def test_unsupported_annotation_call_is_refused_at_the_ticket_door(endpoint):
    client, orchestrator = endpoint
    covering = SignedClient(client.door, client.door.mint(["census", "surveyor"]))
    for name, arguments, headers in (
        ("census", {"a": "x"}, {"Mcp-Param-Region": "x"}),
        ("surveyor", {"outer": {"inner": "x"}}, {}),
    ):
        response = await _call(covering, name, arguments, headers=headers)
        assert (response.status_code, _challenge_error(response)) == (403, "caller.tool_unavailable")
    assert orchestrator.calls == []
    assert (
        orchestrator.work_admission.inspect_admission_class(AdmissionClass.MCP).active_count
        == 0
    )


@module_loop
async def test_unknown_tool_is_not_covered_by_the_ticket(endpoint):
    client, orchestrator = endpoint
    response = await _call(client, "nonexistent", {})
    assert (response.status_code, _challenge_error(response)) == (403, "caller.tool_not_covered")
    assert orchestrator.calls == []


@module_loop
async def test_preflight_reflects_mirror_header_namespace(endpoint):
    client, _ = endpoint
    preflight = await client.options(
        "/mcp",
        headers={
            "Origin": BASE,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "mcp-param-region, X-Other, MCP-PARAM-COUNT",
        },
    )
    assert preflight.status_code == 204
    allowed = preflight.headers["access-control-allow-headers"]
    for name in ("mcp-param-region", "MCP-PARAM-COUNT"):
        assert name in allowed
    assert "X-Other" not in allowed
    for fixed in ("Authorization", "Content-Type", "MCP-Protocol-Version", "Mcp-Method", "Mcp-Name"):
        assert fixed in allowed


@module_loop
async def test_preflight_without_requested_headers_lists_fixed_set(endpoint):
    client, _ = endpoint
    preflight = await client.options(
        "/mcp",
        headers={
            "Origin": BASE,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.headers["access-control-allow-headers"] == (
        "Authorization, Content-Type, MCP-Protocol-Version, Mcp-Method, Mcp-Name"
    )


@pytest.mark.parametrize(
    "requested",
    ["mcp-param evil", "mcp-paramx", "mcp-param-", "authorization", "x" * 129],
)
@module_loop
async def test_preflight_ignores_non_mirror_lookalikes(endpoint, requested):
    client, _ = endpoint
    preflight = await client.options(
        "/mcp",
        headers={
            "Origin": BASE,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": requested,
        },
    )
    assert preflight.headers["access-control-allow-headers"] == (
        "Authorization, Content-Type, MCP-Protocol-Version, Mcp-Method, Mcp-Name"
    )


@module_loop
async def test_browser_preflight_then_mirrored_call_from_same_origin(endpoint):
    client, orchestrator = endpoint
    preflight = await client.options(
        "/mcp",
        headers={
            "Origin": BASE,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "mcp-param-region",
        },
    )
    assert preflight.status_code == 204
    body, headers = _request(
        "tools/call",
        {"name": "geo", "arguments": {"region": "eu-west"}},
        headers={
            "Mcp-Name": "geo",
            "Mcp-Param-Region": "eu-west",
            "Origin": BASE,
        },
    )
    response = await client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    assert orchestrator.calls[0]["arguments"] == {"region": "eu-west"}
