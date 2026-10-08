import pytest
import asyncio
from unittest.mock import AsyncMock, patch
from astral_deep.core.health_check import SystemHealthChecker, ServiceStatus

@pytest.mark.asyncio
async def test_health_checker_success():
    # Mocking httpx to simulate a successful response
    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.return_value = AsyncMock(status_code=200)
        
        checker = SystemHealthChecker("http://localhost:8000", {"test": "/test"})
        results = await checker.run_all_checks()
        
        assert len(results) == 1
        assert results[0].is_up is True
        assert results[0].name == "test"

@pytest.mark.asyncio
async def test_health_checker_connection_failure():
    # Mocking httpx to simulate a connection error
    with patch("httpx.AsyncClient.get", side_effect=Exception("Connection refused")):
        checker = SystemHealthChecker("http://localhost:8000", {"test": "/test"})
        results = await checker.run_all_checks()
        
        assert results[0].is_up is False
        assert "Connection refused" in results[0].details or "Connection refused" in results[0].actionable_error
