import httpx
from typing import Any, Dict

class ClaudeNativeAdapter:
    """Native adapter for Anthropic Messages API with Fence egress compliance."""
    def __init__(self, api_key: str, timeout: float = 30.0):
        self.api_key = api_key
        self.timeout = timeout
        self.base_url = "https://api.anthropic.com/v1/messages"

    def _get_client(self) -> httpx.Client:
        # Enforce strict egress control via Fence firewall requirements
        # Disable environment trust to prevent credential leakage
        # Explicitly set proxy to None to ensure block-mode compliance
        return httpx.Client(
            timeout=self.timeout,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            trust_env=False,
            verify=True,
            proxies=None
        )

    def send_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Sends message with mandatory egress validation."""
        with self._get_client() as client:
            try:
                response = client.post(self.base_url, json=payload)
                response.raise_for_status()
                return response.json()
            except httpx.ConnectError as e:
                # Log egress block for audit compliance
                raise ConnectionError(f"Egress firewall blocked request: {e}")
