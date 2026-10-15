"""
Tests for MCP resources endpoint (Bounty #322)
Tests resources/list and resources/read methods.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock


class TestResourceList:
    """Test cases for resources/list method"""

    @pytest.mark.asyncio
    async def test_list_returns_empty_for_no_works(self):
        """User without works gets empty list"""
        from orchestrator.mcp_server_endpoint import list_resources

        # Mock orchestrator with empty work_ops
        orchestrator = MagicMock()
        ops = MagicMock()
        ops.list = AsyncMock(return_value={"works": []})
        orchestrator.work_ops = ops

        resources = await list_resources(orchestrator, "user123", {"sub": "user123"})
        assert resources == []
        ops.list.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_list_returns_resources_for_works(self):
        """User with works gets resource URIs"""
        from orchestrator.mcp_server_endpoint import list_resources

        # Mock orchestrator with sample works
        orchestrator = MagicMock()
        ops = MagicMock()
        works = [
            {"id": "work-1", "title": "Task A"},
            {"id": "work-2", "title": "Task B"},
        ]
        ops.list = AsyncMock(return_value={"works": works})
        orchestrator.work_ops = ops

        resources = await list_resources(orchestrator, "user123", {"sub": "user123"})

        assert len(resources) == 4  # Each work has /status and /artifacts
        assert any("astral://work/work-1/status" in r["uri"] for r in resources)
        assert any("astral://work/work-1/artifacts" in r["uri"] for r in resources)
        assert any("astral://work/work-2/status" in r["uri"] for r in resources)
        assert any("astral://work/work-2/artifacts" in r["uri"] for r in resources)

    @pytest.mark.asyncio
    async def test_list_handles_missing_work_ops(self):
        """When work_ops is not found, returns empty list with warning"""
        from orchestrator.mcp_server_endpoint import list_resources

        # Mock orchestrator without work_ops
        orchestrator = MagicMock(spec=[])
        orchestrator.work_ops = None
        orchestrator.runtime = None

        resources = await list_resources(orchestrator, "user123", {"sub": "user123"})
        assert resources == []

    @pytest.mark.asyncio
    async def test_list_handles_error_gracefully(self):
        """Errors during list should be caught and return empty list"""
        from orchestrator.mcp_server_endpoint import list_resources

        orchestrator = MagicMock()
        ops = MagicMock()
        ops.list = AsyncMock(side_effect=Exception("Database error"))
        orchestrator.work_ops = ops

        resources = await list_resources(orchestrator, "user123", {"sub": "user123"})
        assert resources == []


class TestResourceRead:
    """Test cases for resources/read method"""

    @pytest.mark.asyncio
    async def test_read_valid_status_resource(self):
        """Reading valid status resource returns bounded view model"""
        from orchestrator.mcp_server_endpoint import read_resource

        orchestrator = MagicMock()
        ops = MagicMock()
        ops.get = AsyncMock(return_value={
            "id": "work-1",
            "title": "Test Task",
            "status": "running",
            "progress": 50,
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T12:00:00Z",
            "owner_id": "user123",
        })
        orchestrator.work_ops = ops

        result = await read_resource(
            orchestrator, "user123", "astral://work/work-1/status", {"sub": "user123"}
        )

        assert result["uri"] == "astral://work/work-1/status"
        assert result["mimeType"] == "application/json"
        assert result["boundedViewModel"]["id"] == "work-1"
        assert result["boundedViewModel"]["title"] == "Test Task"
        assert result["boundedViewModel"]["status"] == "running"
        assert result["boundedViewModel"]["progress"] == 50
        # owner_id should NOT be exposed
        assert "owner_id" not in result["boundedViewModel"]
        assert "credentials" not in result["boundedViewModel"]

    @pytest.mark.asyncio
    async def test_read_valid_artifact_resource(self):
        """Reading valid artifact resource returns bounded model"""
        from orchestrator.mcp_server_endpoint import read_resource

        orchestrator = MagicMock()
        ops = MagicMock()
        ops.get = AsyncMock(return_value={
            "id": "work-1",
            "title": "Test Task",
            "status": "completed",
            "artifact_count": 3,
            "owner_id": "user123",
        })
        orchestrator.work_ops = ops

        result = await read_resource(
            orchestrator, "user123", "astral://artifact/work-1", {"sub": "user123"}
        )

        assert result["boundedViewModel"]["workId"] == "work-1"
        assert result["boundedViewModel"]["artifactCount"] == 3
        assert "owner_id" not in result["boundedViewModel"]

    @pytest.mark.asyncio
    async def test_read_cross_owner_denied(self):
        """Reading another user's work should return 403"""
        from orchestrator.mcp_server_endpoint import read_resource, MCPAuthError

        orchestrator = MagicMock()
        ops = MagicMock()
        ops.get = AsyncMock(return_value={
            "id": "work-1",
            "title": "Test Task",
            "owner_id": "other_user",  # Different owner
        })
        orchestrator.work_ops = ops

        with pytest.raises(MCPAuthError) as exc_info:
            await read_resource(
                orchestrator, "user123", "astral://work/work-1/status", {"sub": "user123"}
            )

        assert exc_info.value.status_code == 403
        assert exc_info.value.error == "forbidden"

    @pytest.mark.asyncio
    async def test_read_invalid_uri_format(self):
        """Invalid URI format should return 400"""
        from orchestrator.mcp_server_endpoint import read_resource, MCPAuthError

        orchestrator = MagicMock()

        with pytest.raises(MCPAuthError) as exc_info:
            await read_resource(
                orchestrator, "user123", "invalid-uri", {"sub": "user123"}
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.error == "invalid_uri"

    @pytest.mark.asyncio
    async def test_read_invalid_artifact_uri(self):
        """Invalid artifact URI should return 400"""
        from orchestrator.mcp_server_endpoint import read_resource, MCPAuthError

        orchestrator = MagicMock()

        with pytest.raises(MCPAuthError) as exc_info:
            await read_resource(
                orchestrator, "user123", "astral://work/INVALID!", {"sub": "user123"}
            )

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_read_missing_work_ops(self):
        """When work_ops is not available, returns 503"""
        from orchestrator.mcp_server_endpoint import read_resource, MCPAuthError

        orchestrator = MagicMock(spec=[])
        orchestrator.work_ops = None
        orchestrator.runtime = None

        with pytest.raises(MCPAuthError) as exc_info:
            await read_resource(
                orchestrator, "user123", "astral://work/work-1/status", {"sub": "user123"}
            )

        assert exc_info.value.status_code == 503
        assert exc_info.value.error == "service_unavailable"

    @pytest.mark.asyncio
    async def test_read_returns_sandboxed_model(self):
        """Response should only contain safe fields, no sensitive data"""
        from orchestrator.mcp_server_endpoint import read_resource

        orchestrator = MagicMock()
        ops = MagicMock()
        ops.get = AsyncMock(return_value={
            "id": "work-1",
            "title": "Test Task",
            "status": "completed",
            "progress": 100,
            "owner_id": "user123",
            "credentials": "secret_token",
            "notes": "internal note",
            "created_at": "2024-01-01",
            "updated_at": "2024-01-02",
        })
        orchestrator.work_ops = ops

        result = await read_resource(
            orchestrator, "user123", "astral://work/work-1/status", {"sub": "user123"}
        )

        bounded = result["boundedViewModel"]
        # Safe fields should be present
        assert "id" in bounded
        assert "title" in bounded
        assert "status" in bounded
        assert "progress" in bounded
        # Sensitive fields should be excluded
        assert "owner_id" not in bounded
        assert "credentials" not in bounded
        assert "notes" not in bounded

    @pytest.mark.asyncio
    async def test_read_handles_internal_error(self):
        """Internal errors should be caught and return 500"""
        from orchestrator.mcp_server_endpoint import read_resource, MCPAuthError

        orchestrator = MagicMock()
        ops = MagicMock()
        ops.get = AsyncMock(side_effect=Exception("Database connection lost"))
        orchestrator.work_ops = ops

        with pytest.raises(MCPAuthError) as exc_info:
            await read_resource(
                orchestrator, "user123", "astral://work/work-1/status", {"sub": "user123"}
            )

        assert exc_info.value.status_code == 500
        assert exc_info.value.error == "internal_error"


class TestFeatureNegotiation:
    """Test feature negotiation fallback"""

    @pytest.mark.asyncio
    async def test_resources_in_method_phase(self):
        """resources/list and resources/read should be in _METHOD_PHASE"""
        from orchestrator.mcp_server_endpoint import _METHOD_PHASE

        assert "resources/list" in _METHOD_PHASE
        assert "resources/read" in _METHOD_PHASE
        assert _METHOD_PHASE["resources/list"] == "resources_list"
        assert _METHOD_PHASE["resources/read"] == "resources_read"

    @pytest.mark.asyncio
    async def test_resources_in_method_scope(self):
        """resources methods should require mcp:resources:read scope"""
        from orchestrator.mcp_server_endpoint import _METHOD_SCOPE

        assert _METHOD_SCOPE["resources/list"] == ("mcp:resources:read",)
        assert _METHOD_SCOPE["resources/read"] == ("mcp:resources:read",)

    @pytest.mark.asyncio
    async def test_resource_uri_patterns_exist(self):
        """URI patterns should be defined and match expected formats"""
        from orchestrator.mcp_server_endpoint import (
            _RESOURCE_URI_PATTERN,
            _ARTIFACT_URI_PATTERN,
        )

        # Valid status URI
        assert _RESOURCE_URI_PATTERN.match("astral://work/abc123/status")
        # Valid artifact URI
        assert _ARTIFACT_URI_PATTERN.match("astral://artifact/abc123")
        # Invalid URI
        assert not _RESOURCE_URI_PATTERN.match("astral://work/INVALID!")
        assert not _ARTIFACT_URI_PATTERN.match("astral://artifact/INVALID!")


class TestSecurityChecks:
    """Security-related test cases"""

    @pytest.mark.asyncio
    async def test_uri_injection_blocked(self):
        """URI injection attempts should be blocked"""
        from orchestrator.mcp_server_endpoint import read_resource, MCPAuthError

        orchestrator = MagicMock()

        # Attempt SQL injection in URI
        with pytest.raises(MCPAuthError):
            await read_resource(
                orchestrator, "user123",
                "astral://work/'; DROP TABLE users;--/status",
                {"sub": "user123"}
            )

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self):
        """Path traversal attempts should be blocked"""
        from orchestrator.mcp_server_endpoint import read_resource, MCPAuthError

        orchestrator = MagicMock()

        with pytest.raises(MCPAuthError):
            await read_resource(
                orchestrator, "user123",
                "astral://work/../../etc/passwd/status",
                {"sub": "user123"}
            )

    @pytest.mark.asyncio
    async def test_empty_uri_rejected(self):
        """Empty or null URI should be rejected"""
        from orchestrator.mcp_server_endpoint import read_resource, MCPAuthError

        orchestrator = MagicMock()

        with pytest.raises(MCPAuthError):
            await read_resource(
                orchestrator, "user123", "",
                {"sub": "user123"}
            )
