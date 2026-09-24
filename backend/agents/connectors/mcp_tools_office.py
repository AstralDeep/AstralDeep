"""Office connector tools generating Excel/CSV, PowerPoint outlines, Word documents,
pitch decks, and Outlook email (Microsoft Graph when credentialed, preview otherwise)
as astralprims SDUI components.
"""

import csv
import io
import logging
import os
import re
from typing import Dict, Any

from astralprims import (
    Table, Alert, Collapsible, FileDownload, Text, Container, Divider,
    create_ui_response,
)
from shared.external_http import request as http_request, ExternalHttpError

from agents.connectors._external import verdict_for_exception, user_facing_error

logger = logging.getLogger("Connectors.Office")


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str, default: str = "file") -> str:
    cleaned = _SAFE_NAME_RE.sub("_", (name or "").strip()) or default
    return cleaned[:120]


def _write_download_file(args: Dict[str, Any], filename: str, contents: bytes) -> str:
    user_id = args.get("user_id") or "legacy"
    session_id = args.get("session_id") or "default"

    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    download_dir = os.path.join(backend_dir, "tmp", user_id, session_id)
    os.makedirs(download_dir, exist_ok=True)
    file_path = os.path.join(download_dir, filename)
    with open(file_path, "wb") as f:
        f.write(contents)

    return f"/api/download/{session_id}/{filename}"


_EXCEL_METADATA = {
    "name": "excel_generate",
    "description": "Generate a table with downloadable CSV export.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Title for the spreadsheet/table"},
            "columns": {"type": "array", "items": {"type": "string"}, "description": "Column headers"},
            "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}, "description": "Data rows"},
            "description": {"type": "string", "description": "Optional description/caption"},
        },
        "required": ["title", "columns", "rows"],
    },
}


def handle_excel_generate(args: Dict[str, Any]) -> Dict[str, Any]:
    title = args.get("title", "Spreadsheet")
    columns = args.get("columns", [])
    rows = args.get("rows", [])
    description = args.get("description", "")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(row)

    filename = f"{_safe_filename(title, 'spreadsheet')}.csv"
    download_url = _write_download_file(args, filename, buf.getvalue().encode("utf-8"))

    return create_ui_response([
        Text(content=description or title, variant="h3"),
        Table(headers=columns, rows=rows),
        FileDownload(label=f"Download {filename}", url=download_url, filename=filename),
    ])


_PPT_METADATA = {
    "name": "powerpoint_outline",
    "description": "Generate a structured presentation outline with slides and bullet points.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Presentation title"},
            "slides": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
            }, "description": "List of slides with titles and bullet points"},
            "description": {"type": "string", "description": "Optional subtitle/description"},
        },
        "required": ["title", "slides"],
    },
}


def handle_ppt_outline(args: Dict[str, Any]) -> Dict[str, Any]:
    title = args.get("title", "Presentation")
    slides = args.get("slides", [])
    description = args.get("description", "")

    components = [Text(content=title, variant="h2")]
    if description:
        components.append(Text(content=description, variant="body"))

    for i, slide in enumerate(slides):
        slide_title = slide.get("title", f"Slide {i + 1}")
        bullets = slide.get("bullets", [])
        bullet_text = "\n".join(f"• {b}" for b in bullets)
        components.append(Collapsible(
            title=f"Slide {i + 1}: {slide_title}",
            content=[Text(content=bullet_text)],
        ))

    components.append(Text(content=f"Total: {len(slides)} slides", variant="caption"))
    return create_ui_response(components)


_WORD_METADATA = {
    "name": "word_document",
    "description": "Generate a formatted document with sections, paragraphs, and downloadable markdown export.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Document title"},
            "sections": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "content": {"type": "string"},
                },
            }, "description": "Document sections"},
            "include_download": {"type": "boolean", "description": "Include a download button"},
        },
        "required": ["title", "sections"],
    },
}


def handle_word_document(args: Dict[str, Any]) -> Dict[str, Any]:
    title = args.get("title", "Document")
    sections = args.get("sections", [])
    include_download = args.get("include_download", True)

    components = [Text(content=title, variant="h2")]

    md_parts = [f"# {title}\n"]
    for section in sections:
        heading = section.get("heading", "")
        content = section.get("content", "")
        components.append(Collapsible(title=heading, content=[Text(content=content)]))
        md_parts.append(f"## {heading}\n\n{content}\n")

    if include_download:
        filename = f"{_safe_filename(title, 'document')}.md"
        download_url = _write_download_file(args, filename, "\n".join(md_parts).encode("utf-8"))
        components.append(FileDownload(
            label=f"Download {filename}",
            url=download_url,
            filename=filename,
        ))

    return create_ui_response(components)


_OUTLOOK_METADATA = {
    "name": "outlook_email",
    "description": (
        "Compose an email. If MS_GRAPH_ACCESS_TOKEN is configured (Mail.Send scope), "
        "send it via Microsoft Graph; otherwise return a preview only."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient email address (or comma-separated list)"},
            "subject": {"type": "string", "description": "Email subject line"},
            "body": {"type": "string", "description": "Email body content"},
            "cc": {"type": "string", "description": "CC recipients (comma-separated)"},
            "priority": {"type": "string", "enum": ["normal", "high", "low"]},
            "send": {"type": "boolean", "description": "If true and credentials present, actually send; default false (preview only)"},
        },
        "required": ["to", "subject", "body"],
    },
}

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _split_recipients(raw: str) -> list:
    if not raw:
        return []
    return [{"emailAddress": {"address": a.strip()}} for a in re.split(r"[,;]\s*", raw) if a.strip()]


def _email_preview(to: str, subject: str, body: str, cc: str, priority: str) -> list:
    return [Container(children=[
        Alert(variant="info", title=f"To: {to}", message=f"Subject: {subject}"),
        Text(
            content=f"Priority: {priority}" + (f"  |  CC: {cc}" if cc else ""),
            variant="body",
        ),
        Divider(),
        Text(content=body, variant="body"),
    ])]


def handle_outlook_email(args: Dict[str, Any]) -> Dict[str, Any]:
    to = args.get("to", "")
    subject = args.get("subject", "")
    body = args.get("body", "")
    cc = args.get("cc", "")
    priority = args.get("priority", "normal")
    send = bool(args.get("send", False))

    creds = args.get("_credentials") or {}
    token = creds.get("MS_GRAPH_ACCESS_TOKEN", "")

    preview = _email_preview(to, subject, body, cc, priority)

    if not send:
        if not token:
            preview.append(Alert(
                variant="info",
                title="Preview only",
                message=(
                    "Add a Microsoft Graph access token (Mail.Send scope) in the agent's "
                    "settings to enable sending. Re-run with send=true after configuring."
                ),
            ))
        else:
            preview.append(Alert(
                variant="info",
                title="Preview only",
                message="Credentials configured. Re-run with send=true to actually send via Microsoft Graph.",
            ))
        return create_ui_response(preview)

    if not token:
        preview.append(Alert(
            variant="warning",
            title="Cannot send",
            message="MS_GRAPH_ACCESS_TOKEN is not configured. Save it in the agent's settings.",
        ))
        return create_ui_response(preview)

    importance = {"high": "high", "low": "low"}.get(priority, "normal")
    message = {
        "message": {
            "subject": subject,
            "importance": importance,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": _split_recipients(to),
        },
        "saveToSentItems": True,
    }
    if cc:
        message["message"]["ccRecipients"] = _split_recipients(cc)

    try:
        resp = http_request(
            "POST",
            f"{_GRAPH_BASE}/me/sendMail",
            api_key=token,
            json_body=message,
        )
    except ExternalHttpError as e:
        preview.append(Alert(
            variant="warning",
            title="Send failed",
            message=user_facing_error(e, "Microsoft Graph"),
        ))
        return create_ui_response(preview)

    if resp.status_code in (200, 202):
        preview.append(Alert(
            variant="success",
            title="Sent",
            message=f"Email sent via Microsoft Graph (HTTP {resp.status_code}).",
        ))
    else:
        preview.append(Alert(
            variant="warning",
            title=f"Unexpected response (HTTP {resp.status_code})",
            message=(resp.text or "")[:300],
        ))
    return create_ui_response(preview)


_OUTLOOK_CHECK_METADATA = {
    "name": "outlook_credentials_check",
    "description": "Probe the saved Microsoft Graph access token with a cheap GET /me.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": True},
}


def handle_outlook_credentials_check(args: Dict[str, Any]) -> Dict[str, Any]:
    creds = args.get("_credentials") or {}
    token = creds.get("MS_GRAPH_ACCESS_TOKEN", "")
    if not token:
        return {"credential_test": "unconfigured", "detail": "MS_GRAPH_ACCESS_TOKEN is not set."}
    try:
        resp = http_request("GET", f"{_GRAPH_BASE}/me", api_key=token)
    except ExternalHttpError as e:
        return verdict_for_exception(e)
    if resp.status_code == 200:
        return {"credential_test": "ok"}
    return {"credential_test": "unexpected", "detail": f"HTTP {resp.status_code}"}


_PITCH_TEMPLATES = {
    "startup": {
        "title": "Startup Pitch Deck",
        "slides": [
            {"title": "Problem", "bullets": ["The problem your customers face", "Why it matters", "Current solutions fall short"]},
            {"title": "Solution", "bullets": ["Your product/service", "How it solves the problem", "Key differentiators"]},
            {"title": "Market", "bullets": ["TAM, SAM, SOM", "Target customers", "Market trends"]},
            {"title": "Business Model", "bullets": ["Revenue streams", "Pricing strategy", "Unit economics"]},
            {"title": "Traction", "bullets": ["Key metrics", "Customer testimonials", "Growth trajectory"]},
            {"title": "Team", "bullets": ["Founders & key hires", "Advisors", "Why this team"]},
            {"title": "Ask", "bullets": ["Funding amount", "Use of funds", "Milestones"]},
        ],
    },
    "sales": {
        "title": "Sales Proposal Deck",
        "slides": [
            {"title": "Executive Summary", "bullets": ["Client challenge", "Proposed solution", "Expected ROI"]},
            {"title": "Current Situation", "bullets": ["Client's current state", "Pain points", "Opportunity cost"]},
            {"title": "Proposed Solution", "bullets": ["Detailed approach", "Timeline", "Deliverables"]},
            {"title": "Pricing", "bullets": ["Package options", "Payment terms", "Value comparison"]},
            {"title": "Case Studies", "bullets": ["Similar clients", "Results achieved", "Testimonials"]},
            {"title": "Next Steps", "bullets": ["Action items", "Timeline", "Contact info"]},
        ],
    },
    "investor": {
        "title": "Investor Update Deck",
        "slides": [
            {"title": "Highlights", "bullets": ["Top wins this period", "Key metrics snapshot", "Headline news"]},
            {"title": "KPIs", "bullets": ["Revenue / ARR", "Customer growth", "Burn rate & runway"]},
            {"title": "Product Updates", "bullets": ["New features shipped", "Roadmap progress", "Customer feedback"]},
            {"title": "Team Updates", "bullets": ["Key hires", "Org changes", "Culture highlights"]},
            {"title": "Challenges & Risks", "bullets": ["What's not working", "Mitigation plans", "Help needed"]},
            {"title": "Looking Ahead", "bullets": ["Next quarter goals", "Key initiatives", "Fundraising plans"]},
        ],
    },
    "product": {
        "title": "Product Launch Deck",
        "slides": [
            {"title": "Vision", "bullets": ["Product vision statement", "Why now", "Market gap"]},
            {"title": "Product Overview", "bullets": ["Core features", "User experience", "Technical highlights"]},
            {"title": "Target Audience", "bullets": ["Primary personas", "Use cases", "Customer journey"]},
            {"title": "Competitive Landscape", "bullets": ["Competitor comparison", "Differentiation matrix", "Moats"]},
            {"title": "Go-to-Market", "bullets": ["Launch strategy", "Channels", "Success metrics"]},
            {"title": "Roadmap", "bullets": ["Phase 1 (MVP)", "Phase 2", "Long-term vision"]},
        ],
    },
    "project": {
        "title": "Project Proposal Deck",
        "slides": [
            {"title": "Project Overview", "bullets": ["Objective", "Scope", "Success criteria"]},
            {"title": "Approach", "bullets": ["Methodology", "Phases", "Dependencies"]},
            {"title": "Timeline", "bullets": ["Milestones", "Critical path", "Buffer"]},
            {"title": "Resources", "bullets": ["Team & roles", "Budget", "Tools & infrastructure"]},
            {"title": "Risks", "bullets": ["Key risks", "Probability/impact", "Mitigation"]},
            {"title": "Governance", "bullets": ["Reporting cadence", "Decision rights", "Escalation path"]},
        ],
    },
    "strategy": {
        "title": "Strategy Deck",
        "slides": [
            {"title": "Where We Are", "bullets": ["Current state assessment", "SWOT analysis", "Key metrics"]},
            {"title": "Where We're Going", "bullets": ["Vision 2026", "Strategic pillars", "BHAG"]},
            {"title": "How We Get There", "bullets": ["Initiative 1", "Initiative 2", "Initiative 3"]},
            {"title": "Resource Allocation", "bullets": ["Investment priorities", "Trade-offs", "Sequencing"]},
            {"title": "Success Metrics", "bullets": ["Leading indicators", "Lagging indicators", "Review cadence"]},
        ],
    },
}

_PITCH_METADATA = {
    "name": "pitch_template",
    "description": "Generate a pre-structured pitch/presentation template. Available types: startup, sales, investor, product, project, strategy.",
    "input_schema": {
        "type": "object",
        "properties": {
            "template_type": {"type": "string", "enum": list(_PITCH_TEMPLATES.keys())},
            "custom_title": {"type": "string", "description": "Override the default title"},
        },
        "required": ["template_type"],
    },
}


def handle_pitch_template(args: Dict[str, Any]) -> Dict[str, Any]:
    template_type = args.get("template_type", "startup")
    custom_title = args.get("custom_title")

    template = _PITCH_TEMPLATES.get(template_type, _PITCH_TEMPLATES["startup"])
    title = custom_title or template["title"]

    components = [
        Text(content=title, variant="h2"),
        Text(content=f"Template type: {template_type}", variant="caption"),
    ]
    for slide in template["slides"]:
        bullets = "\n".join(f"• {b}" for b in slide["bullets"])
        components.append(Collapsible(
            title=slide["title"],
            content=[Text(content=bullets)],
        ))
    components.append(Text(content="Edit each slide to add your specifics.", variant="caption"))
    return create_ui_response(components)


OFFICE_TOOL_REGISTRY = {
    "excel_generate": {"function": handle_excel_generate, **_EXCEL_METADATA},
    "powerpoint_outline": {"function": handle_ppt_outline, **_PPT_METADATA},
    "word_document": {"function": handle_word_document, **_WORD_METADATA},
    "outlook_email": {"function": handle_outlook_email, **_OUTLOOK_METADATA},
    "outlook_credentials_check": {"function": handle_outlook_credentials_check, **_OUTLOOK_CHECK_METADATA},
    "pitch_template": {"function": handle_pitch_template, **_PITCH_METADATA},
}
