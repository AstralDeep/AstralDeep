from .claude_native import ClaudeNativeAdapter

def get_provider(provider_type: str, credentials: Dict[str, str]):
    """
    Factory method to resolve provider. 
    Added 'claude_native' support.
    """
    if provider_type == "claude_native":
        if "api_key" not in credentials:
            raise ValueError("Missing credentials for native Claude provider")
        return ClaudeNativeAdapter(api_key=credentials["api_key"])
    
    # ... existing logic for openai/other providers ...
