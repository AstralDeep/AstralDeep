from typing import List, Dict, Any
from backend.agents.search_controller import SourceState

def render_search_controls(sources: List[Dict[str, Any]]) -> str:
    """Generates server-owned UI components for search navigation."""
    controls: List[str] = []
    for source in sources:
        state = source.get("state")
        # Handle both Enum and string representations for robustness
        state_val = state.value if isinstance(state, SourceState) else state
        
        if state_val == "revoked":
            controls.append(f"<!-- Source {source.get('id', 'unknown')} access revoked -->")
        elif state_val == "offline":
            controls.append(f"<!-- Source {source.get('id', 'unknown')} unavailable -->")
        else:
            name = source.get('name', 'Unknown')
            source_id = source.get('id', 'unknown')
            controls.append(f"<button data-source='{source_id}'>Open {name}</button>")
    return "\n".join(controls)
