from typing import List, Dict, Any
from backend.agents.search_controller import SourceState

def render_search_controls(sources: List[Dict[str, Any]]) -> str:
    """Generates server-owned UI components for search navigation."""
    controls = []
    for source in sources:
        state = source.get("state")
        # Handle both Enum and string representations for robustness
        state_val = state.value if isinstance(state, SourceState) else state
        
        if state_val == "revoked":
            controls.append(f"<!-- Source {source.get('id')} access revoked -->")
        elif state_val == "offline":
            controls.append(f"<!-- Source {source.get('id')} unavailable -->")
        else:
            controls.append(f"<button data-source='{source.get('id')}'>Open {source.get('name', 'Unknown')}</button>")
    return "\n".join(controls)
