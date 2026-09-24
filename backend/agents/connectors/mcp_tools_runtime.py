"""Adaptive Runtime Intelligence tool: inspects an incoming request and recommends which
bundled agent should handle it, exposed as RUNTIME_TOOL_REGISTRY to the Connectors
MCP server.
"""

import logging
from typing import Dict, Any

from astralprims import (
    Table, Text, Alert, create_ui_response,
)

logger = logging.getLogger("Connectors.Runtime")

_AGENT_ROUTING = [
    {"agent_id": "general-1", "name": "General Agent", "tags": ["general", "search", "data", "system"]},
    {"agent_id": "medical-1", "name": "Medical Agent", "tags": ["medical", "patient", "clinical", "health"]},
    {"agent_id": "weather-1", "name": "Weather Agent", "tags": ["weather", "forecast", "temperature"]},
    {"agent_id": "forecaster-1", "name": "Forecaster Agent", "tags": ["forecast", "prediction", "analysis"]},
    {"agent_id": "journal-review-1", "name": "Journal Review Agent", "tags": ["journal", "review", "manuscript", "publication"]},
    {"agent_id": "connectors-1", "name": "Connectors Agent", "tags": ["office", "excel", "ppt", "document", "email", "code", "review", "design"]},
]

_RUNTIME_METADATA = {
    "name": "adaptive_routing",
    "description": "Analyze a request and recommend the best agent(s) to handle it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The user's request to route"},
            "available_agents": {"type": "array", "items": {"type": "string"}, "description": "List of available agent IDs"},
            "top_n": {"type": "integer", "description": "Number of recommendations to return"},
        },
        "required": ["query"],
    },
}


def handle_adaptive_routing(args: Dict[str, Any]) -> Dict[str, Any]:
    query = args.get("query", "").lower()
    available = args.get("available_agents")
    top_n = args.get("top_n", 3)

    agents = _AGENT_ROUTING
    if available:
        agents = [a for a in _AGENT_ROUTING if a["agent_id"] in available]

    scored = []
    for agent in agents:
        score = 0
        for tag in agent["tags"]:
            if tag in query:
                score += len(tag)
        if agent["name"].lower() in query:
            score += 10
        scored.append((score, agent))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_n]

    if not top or top[0][0] == 0:
        return create_ui_response([
            Alert(variant="info", title="No strong matches",
                  message="No agent clearly matches this query. Try the General Agent first."),
        ])

    headers = ["Agent", "Confidence", "Match Reason"]
    rows = []
    for score, agent in top:
        confidence = "High" if score > 10 else "Medium" if score > 5 else "Low"
        matching_tags = [t for t in agent["tags"] if t in query]
        reason = ", ".join(matching_tags) if matching_tags else "keyword match"
        rows.append([agent["name"], confidence, reason])

    components = [
        Text(content="Adaptive Routing Recommendation", variant="h2"),
        Text(content=f"Query: {query[:100]}", variant="caption"),
        Table(headers=headers, rows=rows),
    ]

    return create_ui_response(components)


RUNTIME_TOOL_REGISTRY = {
    "adaptive_routing": {"function": handle_adaptive_routing, **_RUNTIME_METADATA},
}