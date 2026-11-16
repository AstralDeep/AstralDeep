import pytest
from backend.agents.connectors.claude_native import ClaudeNativeAdapter

def test_claude_adapter_isolation():
    """Verify that the adapter does not pick up ambient environment variables."""
    adapter = ClaudeNativeAdapter(api_key="sk-test-123")
    client = adapter._get_client()
    
    # Ensure no custom headers from environment are leaked
    assert client.headers["x-api-key"] == "sk-test-123"
    assert "ANTHROPIC_CUSTOM_HEADERS" not in client.headers
