"""Feature 074 T171: exact flag-off behavior and evidence parity."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from orchestrator.governed_dispatch import GovernedFinalDispatch
from orchestrator.lets_config import LetsHostConfig, load_lets_config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"FF_LETS_EXTERNAL_WARDEN": "false"},
        {"FF_LETS_EXTERNAL_WARDEN": "false", "LETS_MODE": "off"},
    ],
)
async def test_flag_off_is_byte_and_behavior_identical_without_lets_evidence(
    environment: dict[str, str],
) -> None:
    loaded = load_lets_config(environment)
    assert loaded.config is not None
    assert loaded.config.mode == "off"
    assert loaded.readiness.status == "disabled"
    assert loaded.readiness.lets_configured is False

    gateway = MagicMock()
    plane = MagicMock()
    repository = MagicMock()
    resolver = MagicMock()
    dispatch = GovernedFinalDispatch(
        mode="off",
        gateway=gateway,
        plane=plane,
        authority_repository=repository,
        runtime_resolver=resolver,
    )
    arguments = {
        "query": "exact user payload",
        "nested": {"count": 2, "enabled": True},
    }
    wire_before = json.dumps(
        arguments,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    calls: list[dict[str, object]] = []

    async def invoke(capabilities: dict[str, object]) -> dict[str, object]:
        calls.append(capabilities)
        return {"arguments": arguments, "status": "existing-success"}

    result = await dispatch.execute(
        owner_id="owner-a",
        agent_id="agent-a",
        tool_id="clinical.search_v2",
        scope="tools:read",
        channel="rest",
        audit_correlation_id="audit-a",
        final_arguments=arguments,
        invoke=invoke,
    )

    wire_after = json.dumps(
        result["arguments"],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert result["status"] == "existing-success"
    assert wire_after == wire_before
    assert calls == [{}]
    assert "astraldeep.lets/v1" not in arguments
    resolver.assert_not_called()
    gateway.authorize.assert_not_called()
    plane.transaction.assert_not_called()
    repository.get_active_binding.assert_not_called()


def test_off_configuration_ignores_dormant_invalid_lets_settings() -> None:
    config = LetsHostConfig.from_environ(
        {
            "FF_LETS_EXTERNAL_WARDEN": "false",
            "LETS_MODE": "off",
            "LETS_WARDEN_URL": "not a URL",
            "LETS_SERVICE_TOKEN_FILE": "missing-relative-secret",
            "LETS_POLICY_DIGEST": "not-a-digest",
        }
    )

    assert config.mode == "off"
    assert config.warden_origin is None
    assert config.service_token_file is None
    assert config.identity is None
    assert config.service_identity_mode is None
    assert config.trust_manifest is None
    assert config.redacted()["service_token_file"] == "<unset>"
    assert config.redacted()["service_identity_mode"] is None
    assert config.redacted()["identity"] is None
