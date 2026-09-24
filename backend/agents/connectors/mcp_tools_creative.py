"""Creative/design connector tools: Adobe CC IMS token validation and Canva Connect API
calls when credentials are configured, plus no-network Artifacts/Graphs/Design spec
generators; Blender is a stub pending a public cloud API.
"""

import logging
from typing import Dict, Any

import requests

from astralprims import (
    Alert, Collapsible, Text, Container,
    create_ui_response,
)
from shared.external_http import request as http_request, ExternalHttpError, validate_egress_url

from agents.connectors._external import verdict_for_exception, user_facing_error

logger = logging.getLogger("Connectors.Creative")


_BLENDER_METADATA = {
    "name": "blender_tool",
    "description": "Blender 3D tooling connector. Requires a self-hosted Blender headless server (no public cloud API).",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["debug", "batch_transform", "export", "info"]},
            "target_objects": {"type": "array", "items": {"type": "string"}},
            "transform": {"type": "object"},
        },
        "required": ["action"],
    },
}


def handle_blender(args: Dict[str, Any]) -> Dict[str, Any]:
    action = args.get("action", "info")
    return create_ui_response([
        Alert(
            variant="info",
            title="Blender Connector",
            message=(
                "Blender has no public cloud API. To enable this tool, run a "
                "Blender headless instance with a Python scripting endpoint and "
                "set BLENDER_SERVER_URL in this agent's credentials. Integration "
                "with that endpoint is not implemented yet."
            ),
        ),
        Text(content=f"Requested action: {action}", variant="body"),
        Collapsible(
            title="Planned actions",
            content=[Container(children=[
                Text(content="• debug — scene / DAG inspection"),
                Text(content="• batch_transform — apply transforms to named objects"),
                Text(content="• export — export selected objects to FBX/GLTF"),
                Text(content="• info — scene statistics and object listing"),
            ])],
        ),
    ])


_ADOBE_IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"

_ADOBE_METADATA = {
    "name": "adobe_cc",
    "description": (
        "Adobe Creative Cloud connector. Validates ADOBE_CLIENT_ID/ADOBE_CLIENT_SECRET "
        "via the Adobe IMS token endpoint; full Firefly/CC API integration pending."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "app": {"type": "string", "enum": ["photoshop", "illustrator", "indesign", "firefly"]},
            "action": {"type": "string", "description": "Action to perform (informational only)"},
        },
        "required": ["app"],
    },
}


# Uses requests directly for IMS's form auth; SSRF check still applies
def _exchange_adobe_ims_token(client_id: str, client_secret: str) -> requests.Response:
    validate_egress_url(_ADOBE_IMS_TOKEN_URL)
    return requests.post(
        _ADOBE_IMS_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "openid,AdobeID,read_organizations,firefly_api,ff_apis",
        },
        timeout=30,
        allow_redirects=False,
    )


def handle_adobe(args: Dict[str, Any]) -> Dict[str, Any]:
    app = args.get("app", "photoshop")
    creds = args.get("_credentials") or {}
    client_id = creds.get("ADOBE_CLIENT_ID", "")
    client_secret = creds.get("ADOBE_CLIENT_SECRET", "")

    header = [Text(content=f"Adobe {app.capitalize()} Connector", variant="h2")]

    if not client_id or not client_secret:
        return create_ui_response(header + [
            Alert(
                variant="info",
                title="Credentials not configured",
                message=(
                    "Set both ADOBE_CLIENT_ID and ADOBE_CLIENT_SECRET in this agent's "
                    "settings (Adobe Developer Console → server-to-server credentials)."
                ),
            ),
            Collapsible(
                title="Supported apps (planned)",
                content=[Container(children=[
                    Text(content="• Photoshop: image editing, layer ops"),
                    Text(content="• Illustrator: vector creation, design automation"),
                    Text(content="• InDesign: layout, template fill"),
                    Text(content="• Firefly: text-to-image generation"),
                ])],
            ),
        ])

    try:
        resp = _exchange_adobe_ims_token(client_id, client_secret)
    except requests.RequestException as e:
        return create_ui_response(header + [
            Alert(
                variant="warning",
                title="Adobe IMS unreachable",
                message=f"Could not reach Adobe IMS: {e}",
            ),
        ])
    except ExternalHttpError as e:
        return create_ui_response(header + [
            Alert(variant="warning", title="Egress blocked", message=str(e)),
        ])

    if resp.status_code == 200 and "access_token" in (resp.text or ""):
        return create_ui_response(header + [
            Alert(
                variant="success",
                title="Credentials verified",
                message="Adobe IMS issued an access token. Full Firefly/CC actions pending implementation.",
            ),
            Collapsible(
                title="Token details",
                content=[Container(children=[
                    Text(content=f"Status: HTTP {resp.status_code}"),
                    Text(content="Scopes requested: openid, AdobeID, read_organizations, firefly_api, ff_apis"),
                ])],
            ),
        ])

    if resp.status_code in (400, 401, 403):
        snippet = (resp.text or "")[:300]
        return create_ui_response(header + [
            Alert(
                variant="warning",
                title="Credentials rejected",
                message=f"Adobe IMS returned HTTP {resp.status_code}: {snippet}",
            ),
        ])

    return create_ui_response(header + [
        Alert(
            variant="warning",
            title=f"Unexpected response (HTTP {resp.status_code})",
            message=(resp.text or "")[:300],
        ),
    ])


_ADOBE_CHECK_METADATA = {
    "name": "adobe_credentials_check",
    "description": "Probe ADOBE_CLIENT_ID/SECRET by exchanging them for an IMS access token.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": True},
}


def handle_adobe_credentials_check(args: Dict[str, Any]) -> Dict[str, Any]:
    creds = args.get("_credentials") or {}
    client_id = creds.get("ADOBE_CLIENT_ID", "")
    client_secret = creds.get("ADOBE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return {
            "credential_test": "unconfigured",
            "detail": "ADOBE_CLIENT_ID and/or ADOBE_CLIENT_SECRET is not set.",
        }
    try:
        resp = _exchange_adobe_ims_token(client_id, client_secret)
    except requests.RequestException as e:
        return {"credential_test": "unreachable", "detail": str(e)}
    except ExternalHttpError as e:
        return verdict_for_exception(e)
    if resp.status_code == 200 and "access_token" in (resp.text or ""):
        return {"credential_test": "ok"}
    if resp.status_code in (400, 401, 403):
        return {"credential_test": "auth_failed", "detail": f"HTTP {resp.status_code}"}
    return {"credential_test": "unexpected", "detail": f"HTTP {resp.status_code}"}


_CANVA_BASE = "https://api.canva.com/rest/v1"

_CANVA_METADATA = {
    "name": "canva_design",
    "description": (
        "Canva design connector. When CANVA_API_KEY is configured, creates a "
        "design in your Canva workspace via the Canva Connect API; otherwise "
        "returns a stub describing the required credential."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "design_type": {"type": "string", "description": "Canva design type (e.g. 'presentation', 'doc', 'whiteboard')"},
            "title": {"type": "string", "description": "Optional title for the new design"},
        },
        "required": ["design_type"],
    },
}


def handle_canva(args: Dict[str, Any]) -> Dict[str, Any]:
    design_type = args.get("design_type", "presentation")
    title = args.get("title") or f"New {design_type}"

    creds = args.get("_credentials") or {}
    token = creds.get("CANVA_API_KEY", "")

    header = [Text(content=f"Canva — {design_type}", variant="h2")]

    if not token:
        return create_ui_response(header + [
            Alert(
                variant="info",
                title="Credentials not configured",
                message=(
                    "Set CANVA_API_KEY in this agent's settings (Canva Connect API "
                    "bearer token) to actually create designs."
                ),
            ),
        ])

    try:
        resp = http_request(
            "POST",
            f"{_CANVA_BASE}/designs",
            api_key=token,
            json_body={
                "design_type": {"type": "preset", "name": design_type},
                "title": title,
            },
        )
    except ExternalHttpError as e:
        return create_ui_response(header + [
            Alert(
                variant="warning",
                title="Canva call failed",
                message=user_facing_error(e, "Canva"),
            ),
        ])

    if resp.status_code in (200, 201):
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        design = (payload or {}).get("design") or {}
        urls = design.get("urls") or {}
        edit_url = urls.get("edit_url") or urls.get("view_url")
        design_id = design.get("id", "(unknown id)")
        components = header + [
            Alert(
                variant="success",
                title="Design created",
                message=f"Canva design {design_id} created in your workspace.",
            ),
        ]
        if edit_url:
            components.append(Text(content=f"Open in Canva: {edit_url}", variant="body"))
        return create_ui_response(components)

    return create_ui_response(header + [
        Alert(
            variant="warning",
            title=f"Unexpected response (HTTP {resp.status_code})",
            message=(resp.text or "")[:300],
        ),
    ])


_CANVA_CHECK_METADATA = {
    "name": "canva_credentials_check",
    "description": "Probe the saved Canva API key with a cheap GET /users/me.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": True},
}


def handle_canva_credentials_check(args: Dict[str, Any]) -> Dict[str, Any]:
    creds = args.get("_credentials") or {}
    token = creds.get("CANVA_API_KEY", "")
    if not token:
        return {"credential_test": "unconfigured", "detail": "CANVA_API_KEY is not set."}
    try:
        resp = http_request("GET", f"{_CANVA_BASE}/users/me", api_key=token)
    except ExternalHttpError as e:
        return verdict_for_exception(e)
    if resp.status_code == 200:
        return {"credential_test": "ok"}
    return {"credential_test": "unexpected", "detail": f"HTTP {resp.status_code}"}


def _ui(components) -> Dict[str, Any]:
    return {
        "_ui_components": [c if isinstance(c, dict) else c.to_dict() for c in components],
        "_data": None,
    }


_SAMPLE_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _sample_series(seed: int, n: int = 6) -> list:
    return [((seed * 7 + k * 5) % 17) + 4 for k in range(n)]


def _artifact_widget(section: Dict[str, Any], i: int) -> Dict[str, Any]:
    wtype = section.get("widget_type", "text")
    title = section.get("title", f"Widget {i + 1}")
    source = section.get("data_source")
    labels = section.get("labels") or _SAMPLE_LABELS
    values = section.get("values") or _sample_series(i, len(labels))
    subtitle = f"Source: {source}" if source else "Sample data"

    if wtype == "metric":
        return {"type": "metric", "title": title,
                "value": str(section.get("value") or max(values)),
                "subtitle": subtitle,
                "variant": section.get("variant", "default")}

    if wtype == "chart":
        kind = section.get("chart_kind", "bar")
        if kind == "pie":
            return {"type": "pie_chart", "title": title,
                    "labels": list(labels), "data": list(values)}
        chart_type = "line_chart" if kind == "line" else "bar_chart"
        return {"type": chart_type, "title": title, "labels": list(labels),
                "datasets": [{"label": title, "data": list(values)}]}

    if wtype == "table":
        headers = section.get("headers") or ["Item", "Value"]
        rows = section.get("rows") or [[label, value] for label, value in zip(labels, values)]
        return {"type": "table", "title": title, "headers": list(headers),
                "rows": [list(r) for r in rows]}

    if wtype == "timeline":
        events = [e for e in (section.get("events") or []) if isinstance(e, dict)]
        if not events:
            events = [{"time": f"{9 + k}:00", "title": f"{label} — sample entry"}
                      for k, label in enumerate(labels[:4])]
        return {"type": "timeline", "title": title, "items": events}

    if wtype == "map":
        items = [{"label": label, "value": str(value)} for label, value in zip(labels, values)]
        return {"type": "keyvalue", "title": f"{title} (map preview)",
                "items": items, "columns": 2}

    return {"type": "card", "title": title, "content": [
        {"type": "text", "variant": "markdown",
         "content": section.get("description") or subtitle},
    ]}


_ARTIFACTS_METADATA = {
    "name": "interactive_artifacts",
    "description": (
        "Build a live interactive dashboard preview from real UI widgets "
        "(metrics, charts, tables, timelines, key-value sheets). Provide "
        "realistic sample labels/values/rows per section — they render "
        "immediately; omitted data falls back to placeholder series."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Dashboard title"},
            "subtitle": {"type": "string", "description": "One-line dashboard subtitle"},
            "sections": {"type": "array", "items": {"type": "object", "properties": {
                "widget_type": {"type": "string",
                                "enum": ["chart", "metric", "table", "timeline", "map", "text"]},
                "title": {"type": "string"},
                "data_source": {"type": "string"},
                "description": {"type": "string", "description": "Body text for text widgets"},
                "chart_kind": {"type": "string", "enum": ["bar", "line", "pie"]},
                "labels": {"type": "array", "items": {"type": "string"},
                           "description": "Category/axis labels (realistic sample data encouraged)"},
                "values": {"type": "array", "items": {"type": "number"},
                           "description": "Series values matching labels"},
                "value": {"type": "string", "description": "Headline value for metric widgets"},
                "variant": {"type": "string", "enum": ["default", "success", "warning", "error"]},
                "headers": {"type": "array", "items": {"type": "string"}},
                "rows": {"type": "array", "items": {"type": "array"},
                         "description": "Table rows matching headers"},
                "events": {"type": "array", "description": "Timeline entries",
                           "items": {"type": "object", "properties": {
                               "time": {"type": "string"},
                               "title": {"type": "string"},
                               "description": {"type": "string"},
                               "variant": {"type": "string",
                                           "enum": ["default", "success", "warning", "error", "info"]},
                           }}},
            }}},
        },
        "required": ["title", "sections"],
    },
}


def handle_artifacts(args: Dict[str, Any]) -> Dict[str, Any]:
    title = args.get("title", "Dashboard")
    sections = [s for s in args.get("sections", []) if isinstance(s, dict)]

    components: list = [{
        "type": "hero",
        "title": title,
        "eyebrow": "Interactive dashboard",
        "subtitle": args.get("subtitle")
        or "Live preview with sample data — wire up data sources to go live.",
        "variant": "gradient",
        "badges": [f"{len(sections)} widgets"],
    }]
    components.extend(_artifact_widget(section, i) for i, section in enumerate(sections))
    components.append({"type": "text", "variant": "caption",
                       "content": "Values shown are sample data unless a section supplied real ones."})
    return _ui(components)


_GRAPHS_METADATA = {
    "name": "visual_graphs",
    "description": "Generate Obsidian-style visual graph network data from entities and relationships.",
    "input_schema": {
        "type": "object",
        "properties": {
            "nodes": {"type": "array", "items": {"type": "string"}},
            "edges": {"type": "array", "items": {"type": "object", "properties": {
                "source": {"type": "string"},
                "target": {"type": "string"},
                "label": {"type": "string"},
            }}},
        },
        "required": ["nodes", "edges"],
    },
}


def handle_graphs(args: Dict[str, Any]) -> Dict[str, Any]:
    nodes = args.get("nodes", [])
    edges = args.get("edges", [])

    node_text = "\n".join(f"• {n}" for n in nodes)
    edge_text = "\n".join(
        f"• {e['source']} → {e['target']}" + (f" ({e['label']})" if e.get("label") else "")
        for e in edges
    )
    components = [
        Text(content="Visual Graph Network", variant="h2"),
        Collapsible(title=f"Nodes ({len(nodes)})", content=[Text(content=node_text)]),
        Collapsible(title=f"Edges ({len(edges)})", content=[Text(content=edge_text)]),
        Text(
            content="To visualize: import into Obsidian, Cytoscape, or a graph visualization library.",
            variant="caption",
        ),
    ]
    return create_ui_response(components)


_DESIGN_METADATA = {
    "name": "claude_design",
    "description": "UI/UX design suggestions. Get design recommendations for layout, color, typography.",
    "input_schema": {
        "type": "object",
        "properties": {
            "context": {"type": "string", "description": "Design context: web, mobile, dashboard, landing page, etc."},
            "style_preferences": {"type": "string", "description": "Style preferences"},
        },
        "required": ["context"],
    },
}

_COLOR_PALETTES = {
    "minimal": ["#FFFFFF", "#F5F5F5", "#333333", "#666666", "#999999"],
    "bold": ["#FF6B6B", "#4ECDC4", "#45B7D1", "#F9ED69", "#FF8E72"],
    "corporate": ["#1A365D", "#2B6CB0", "#EDF2F7", "#4A5568", "#63B3ED"],
    "playful": ["#FF6B6B", "#FFE66D", "#4ECDC4", "#FF8E72", "#A8E6CF"],
}


def handle_design(args: Dict[str, Any]) -> Dict[str, Any]:
    context = args.get("context", "web")
    style = args.get("style_preferences", "minimal")

    style_key = style if style in _COLOR_PALETTES else "minimal"
    palette = _COLOR_PALETTES[style_key]

    components = [
        {"type": "hero", "title": "Design recommendations",
         "eyebrow": context, "subtitle": f"Aesthetic direction: {style_key}",
         "variant": "subtle", "badges": [style_key, context]},
        {"type": "pie_chart", "title": f"Color palette — {style_key}",
         "labels": list(palette), "data": [1] * len(palette), "colors": list(palette)},
        {"type": "keyvalue", "title": "Foundations", "columns": 3, "items": [
            {"label": "Headings", "value": "Inter / system-ui", "hint": "semibold, tight tracking"},
            {"label": "Body", "value": "16px / 1.6", "hint": "max 70ch line length"},
            {"label": "Spacing", "value": "8px grid", "hint": "8, 16, 24, 32, 48, 64"},
        ]},
        Alert(
            variant="info", title="Accessibility",
            message="Ensure WCAG 2.1 AA contrast ratios. Test with keyboard and screen reader.",
        ),
    ]
    return _ui(components)


CREATIVE_TOOL_REGISTRY = {
    "blender_tool": {"function": handle_blender, **_BLENDER_METADATA},
    "adobe_cc": {"function": handle_adobe, **_ADOBE_METADATA},
    "adobe_credentials_check": {"function": handle_adobe_credentials_check, **_ADOBE_CHECK_METADATA},
    "canva_design": {"function": handle_canva, **_CANVA_METADATA},
    "canva_credentials_check": {"function": handle_canva_credentials_check, **_CANVA_CHECK_METADATA},
    "interactive_artifacts": {"function": handle_artifacts, **_ARTIFACTS_METADATA},
    "visual_graphs": {"function": handle_graphs, **_GRAPHS_METADATA},
    "claude_design": {"function": handle_design, **_DESIGN_METADATA},
}
