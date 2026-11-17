from typing import Dict, Any, Optional
from .claude_native import ClaudeNativeAdapter

def get_provider(provider_type: str, credentials: Dict[str, Any]) -> Optional[Any]:
    """
    Factory method to resolve provider. 
    Added 'claude_native' support with mandatory credential validation.
    """
    if provider_type == "claude_native":
        if not credentials or "api_key" not in credentials:
            raise ValueError("Missing credentials for native Claude provider")
        return ClaudeNativeAdapter(api_key=credentials["api_key"])
    
    # Placeholder for existing logic
    return None
