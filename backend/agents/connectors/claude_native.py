import httpx
from typing import Any, Dict

class ClaudeNativeAdapter:
    """Native adapter for Anthropic Messages API."""
    def __init__(self, api_key: str, timeout: float = 30.0):
        self.api_key = api_key
        self.timeout = timeout
        self.base_url = "https://api.anthropic.com/v1/messages"

    def _get_client(self) -> httpx.Client:
        # Explicitly disable environment-based auth/headers
        return httpx.Client(
            timeout=self.timeout,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            }
        )

    def send_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._get_client() as client:
            response = client.post(self.base_url, json=payload)
            response.raise_for_status()
            return response.json()
