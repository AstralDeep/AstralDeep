from typing import List, Dict

def render_search_controls(sources: List[Dict]) -> str:
    """Generates server-owned UI components for search navigation."""
    controls = []
    for source in sources:
        state = source.get("state", "available")
        if state == "revoked":
            controls.append(f"<!-- Source {source['id']} access revoked -->")
        elif state == "offline":
            controls.append(f"<!-- Source {source['id']} unavailable -->")
        else:
            controls.append(f"<button data-source='{source['id']}'>Open {source['name']}</button>")
    return "\n".join(controls)
