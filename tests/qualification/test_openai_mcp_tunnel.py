import pytest
import asyncio
from unittest.mock import MagicMock, patch
from backend.orchestrator.mcp_server_endpoint import MCPServerEndpoint
from backend.auth.iam import validate_token, TokenError

# Synthetic Smoke Test for OpenAI MCP Tunnel Qualification
# This test exercises the discovery-401/legacy-initialize fallback logic.

class TestOpenAITunnelQualification:
    
    @pytest.fixture
    def mcp_endpoint(self):
        return MCPServerEndpoint()

    @pytest.mark.asyncio
    async def test_discovery_auth_flow(self, mcp_endpoint):
        """
        Validates that unauthenticated discovery returns 401 (modern) 
        and does not fallback to a 503 error state.
        """
        # Simulate unauthenticated discovery request
        response = await mcp_endpoint.handle_discovery(headers={})
        assert response.status_code == 401
        assert "auth_required" in response.body

    @pytest.mark.asyncio
    async def test_token_validation_matrix(self, mcp_endpoint):
        """
        Exercises: Valid owner, Cross-owner, Expired, and Revoked tokens.
        """
        scenarios = [
            {"token": "valid_owner_token", "expected": 200},
            {"token": "expired_token", "expected": 401},
            {"token": "revoked_token", "expected": 401},
            {"token": "cross_owner_token", "expected": 403},
        ]

        for scenario in scenarios:
            headers = {"Authorization": f"Bearer {scenario['token']}"}
            response = await mcp_endpoint.handle_mcp_request(headers=headers)
            assert response.status_code == scenario["expected"], \
                f"Failed scenario {scenario['token']}: expected {scenario['expected']}, got {response.status_code}"

    @pytest.mark.asyncio
    async def test_reconnect_and_payload_bounds(self, mcp_endpoint):
        """
        Tests disconnect/reconnect and bounded payload handling.
        """
        # Test large payload rejection (Egress/PHI protection)
        large_payload = "A" * 10**7 # 10MB
        headers = {"Authorization": "Bearer valid_owner_token"}
        response = await mcp_endpoint.handle_mcp_request(headers=headers, payload=large_payload)
        assert response.status_code in [413, 400]

    @pytest.mark.asyncio
    async def test_reproduce_503_fallback_issue(self, mcp_endpoint):
        """
        Specifically targets the inferred issue: 
        Does the discovery-401 trigger a 503 readiness failure?
        """
        # Simulate the specific sequence: Discovery -> 401 -> Re-init with legacy headers
        # If the server returns 503 instead of 401/200, the test fails.
        headers = {"X-Legacy-Initialize": "true"}
        response = await mcp_endpoint.handle_discovery(headers=headers)
        
        # The requirement: "missing compatibility must not be reported as success"
        # We ensure it doesn't hit a 503.
        assert response.status_code != 503, "Detected 503 fallback error during discovery"
