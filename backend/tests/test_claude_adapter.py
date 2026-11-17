import pytest
import httpx
from backend.agents.connectors.claude_native import ClaudeNativeAdapter

def test_claude_adapter_isolation():
    """Verify that the adapter does not pick up ambient environment variables."""
    adapter = ClaudeNativeAdapter(api_key="sk-test-123")
    with adapter._get_client() as client:
        # Ensure no custom headers from environment are leaked
        assert client.headers["x-api-key"] == "sk-test-123"
        # Verify trust_env is disabled
        assert client.trust_env is False
        # Verify proxies are explicitly disabled for egress compliance
        assert client.proxies is None

def test_claude_adapter_egress_compliance():
    """Verify adapter configuration meets security gate requirements."""
    adapter = ClaudeNativeAdapter(api_key="sk-test-123")
    client = adapter._get_client()
    assert client.verify is True
    assert client.trust_env is False
    assert client.proxies is None
    client.close()
