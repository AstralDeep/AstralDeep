"""Tests for HTML artifacts (orchestrator/artifact_html.py, agents/artifacts, api.py export):
size caps, fragment wrapping, stripped elements, the CDN allowlist, CSP injection, the
publish/update tools with version retention, the agent binding and the download fallback.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ["USE_MOCK_AUTH"] = "true"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from orchestrator import artifact_html, artifact_versions  # noqa: E402
from orchestrator.api import export_router  # noqa: E402
from orchestrator.artifact_html import CSP_META, VIEWPORT_META, sanitize_html, validate_html  # noqa: E402
from orchestrator.plane_repository_context import ApplicationPlaneSource  # noqa: E402
from shared.feature_flags import flags  # noqa: E402
from shared.protocol import MCPRequest  # noqa: E402


def _clean(html, title="Tip Calculator", kind="app"):
    document, errors, removed = sanitize_html(html, title=title, kind=kind)
    return document, errors, removed


def _ok(html, **kw):
    document, errors, removed = _clean(html, **kw)
    assert errors == []
    return document, removed


def test_csp_meta_matches_policy():
    assert artifact_html.CSP.startswith("default-src 'none'; script-src 'unsafe-inline' https://cdnjs.cloudflare.com")
    for directive in ("connect-src 'none'", "frame-src 'none'", "form-action 'none'", "base-uri 'none'",
                      "img-src data: blob:", "font-src data:", "https://fonts.gstatic.com",
                      "style-src 'unsafe-inline'", "https://fonts.googleapis.com"):
        assert directive in artifact_html.CSP
    script_src = artifact_html.CSP.split("; ")[1]
    assert "fonts.googleapis.com" not in script_src and "esm.sh" in script_src


def test_fragment_is_wrapped_with_csp_first_and_viewport():
    document, removed = _ok('<h1>Hi</h1><button onclick="go()">Go</button><script>function go(){}</script>',
                            title="Tip <Calc>")
    assert removed == []
    assert document.startswith('<!doctype html>\n<html lang="en"><head>' + CSP_META + VIEWPORT_META)
    assert '<meta charset="utf-8">' in document
    assert document.index('<meta charset="utf-8">') + 22 < 1024
    assert "<title>Tip &lt;Calc&gt;</title>" in document
    assert document.endswith('<body><h1>Hi</h1><button onclick="go()">Go</button>'
                             '<script>function go(){}</script></body></html>')


def test_document_head_gets_csp_as_first_child_and_keeps_viewport():
    html = ('<!DOCTYPE html><html lang="en"><head><title>T</title>'
            '<meta name="viewport" content="width=device-width"></head><body>&copy; &#169; '
            '<!-- note --><p>x</p></body></html>')
    document, _ = _ok(html)
    assert document.startswith('<!DOCTYPE html><html lang="en"><head>' + CSP_META + "<title>T</title>")
    assert VIEWPORT_META not in document
    assert "&copy; &#169; <!-- note -->" in document


def test_documents_without_head_or_doctype_are_completed():
    document, _ = _ok("<html><body><p>a</p></body></html>")
    assert document.startswith("<!doctype html>\n<html><head>" + CSP_META + VIEWPORT_META + "</head><body>")
    body_only, _ = _ok("<body><p>b</p></body>")
    assert body_only.startswith("<!doctype html>\n<head>" + CSP_META)
    assert body_only.index("<head>") < body_only.index("<body>")


def test_size_caps():
    _, errors, _ = _clean("x" * (artifact_html.MAX_INPUT_BYTES + 1))
    assert errors == [f"html is {artifact_html.MAX_INPUT_BYTES + 1} bytes; the input limit is "
                      f"{artifact_html.MAX_INPUT_BYTES} bytes"]
    _, errors, _ = _clean("<p>" + "a" * artifact_html.MAX_WIRE_BYTES + "</p>")
    assert len(errors) == 1 and "once encoded for delivery" in errors[0]
    _, errors, _ = _clean("<p>" + "é" * (artifact_html.MAX_WIRE_BYTES // 5) + "</p>")
    assert errors and "non-ASCII" in errors[0]
    _, errors, _ = _clean("<p>" + "a" * 1000 + "</p>")
    assert errors == []


@pytest.mark.parametrize("html,label", [
    ('<base href="https://evil.example/">', "<base>"),
    ('<meta http-equiv="Refresh" content="0;url=https://evil.example">', '<meta http-equiv="refresh">'),
    ('<embed src="x.swf">', "<embed>"),
    ('<embed src="x.swf"/>', "<embed>"),
])
def test_void_elements_are_stripped(html, label):
    document, removed = _ok(f"<p>keep</p>{html}<p>after</p>")
    assert removed == [label]
    assert "<p>keep</p><p>after</p>" in document
    assert "evil.example" not in document and "x.swf" not in document


def test_iframe_and_frame_content_is_stripped():
    document, removed = _ok('<p>keep</p><iframe src="https://evil.example"><p>inner</p></iframe>'
                            '<frame src="x"><p>after</p>')
    assert removed == ["<iframe>", "<frame>"]
    assert "<p>keep</p><p>after</p>" in document and "inner" not in document and "evil" not in document


@pytest.mark.parametrize("tag", ["object", "frameset"])
def test_subtree_elements_are_stripped(tag):
    html = f'<p>keep</p><{tag} data="x" src="x"><{tag}><p>inner</p></{tag}><p>fallback</p></{tag}><p>after</p>'
    document, removed = _ok(html)
    assert removed == [f"<{tag}>"]
    assert "<p>keep</p><p>after</p>" in document and "inner" not in document and "fallback" not in document


def test_self_closed_iframe_and_stray_end_tags():
    document, removed = _ok('<iframe src="x"/><p>ok</p></iframe></br></embed>')
    assert removed == ["<iframe>"]
    assert "<p>ok</p></body>" in document


def test_unclosed_frame_is_an_error():
    _, errors, _ = _clean("<p>a</p><iframe src='x'><p>lost</p>")
    assert errors == ["<iframe> is not allowed and was never closed; remove it"]


def test_external_form_is_unwrapped_and_relative_form_kept():
    document, removed = _ok('<form action="https://evil.example/steal" method="post"><input name="q">'
                            '<button>Send</button></form><form action="#"><input name="r"></form>'
                            '<form action="javascript:alert(1)"/>')
    assert sorted(removed) == ['<form action="https://evil.example/steal">', '<form action="javascript:alert(1)">']
    assert '<input name="q"><button>Send</button><form action="#"><input name="r"></form>' in document
    assert "evil.example" not in document


@pytest.mark.parametrize("html", [
    '<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>',
    '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>',
    '<script src="//unpkg.com/react@18.3.1/umd/react.production.min.js"></script>',
    '<script type="module">import x from "https://esm.sh/preact@10.19.3"; x;</script>',
    '<script src="https://ESM.SH/htm@3.1.1"></script>',
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter&amp;display=swap">',
    '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css">',
    '<link rel="preconnect" href="https://fonts.gstatic.com">',
    '<link rel="icon" href="data:image/svg+xml,%3Csvg%3E">',
    '<img src="data:image/png;base64,iVBORw0KGgo=" alt="dot">',
    '<img src="blob:https://x/1">',
    '<img src="https://unpkg.com/a@1.0.0/logo.png" srcset="https://unpkg.com/a@1.0.0/l2.png 2x, data:image/png;base64,AA 3x">',
    '<picture><source srcset="https://cdn.jsdelivr.net/gh/a/b@1.0.0/x.webp"></picture>',
    '<video src="data:video/mp4;base64,AA" poster="https://cdnjs.cloudflare.com/p.png"></video>',
    '<audio src="https://cdn.jsdelivr.net/npm/s@1.0.0/a.mp3"></audio>',
    '<style>@import "https://fonts.googleapis.com/css2?family=Inter"; .a{background:url(data:image/png;base64,AA)}'
    ' .b{background:url(#grad)} @font-face{src:url("https://fonts.gstatic.com/s/inter.woff2")}</style>',
    '<div style="background-image:url(\'https://cdnjs.cloudflare.com/x.png\')">x</div>',
    '<a href="https://anywhere.example/">links are not loads</a><img src="#">',
])
def test_allowlisted_and_inline_resources_pass(html):
    document, _ = _ok(html)
    assert html.split(">")[0] in document


@pytest.mark.parametrize("html,expected", [
    ('<script src="https://evil.example/a.js"></script>',
     "<script src>: https://evil.example/a.js is not allowed here; load it only from "
     "cdnjs.cloudflare.com, cdn.jsdelivr.net, unpkg.com, esm.sh, or inline it"),
    ('<script src="https://fonts.googleapis.com/x.js"></script>', "<script src>: https://fonts.googleapis.com/x.js"),
    ('<script src="data:text/javascript,alert(1)"></script>', "<script src>: data:text/javascript"),
    ('<script src="http://unpkg.com/x@1.0.0"></script>', "must use https"),
    ('<script src="app.js"></script>', "<script src>: relative URL app.js cannot load"),
    ('<svg><script href="https://evil.example/s.js"></script></svg>', "<script href>"),
    ('<link rel="stylesheet" href="https://evil.example/a.css">', "<link href>: https://evil.example/a.css"),
    ('<link rel="stylesheet" href="https://fonts.gstatic.com/a.css">', "<link href>: https://fonts.gstatic.com/a.css"),
    ('<link rel="stylesheet" href="data:text/css,body{}">', "<link href>: data:text/css"),
    ('<link rel="preload" href="https://tracker.example/p">', "<link href>: https://tracker.example/p"),
    ('<img src="logo.png">', "<img src>: relative URL logo.png"),
    ('<img src="https://imgur.com/a.png">', "<img src>: https://imgur.com/a.png"),
    ('<img srcset="https://unpkg.com/a.png 1x, https://bad.example/b.png 2x">', "<img srcset>: https://bad.example/b.png"),
    ('<source src="https://bad.example/v.mp4">', "<source src>: https://bad.example/v.mp4"),
    ('<video poster="https://bad.example/p.png"></video>', "<video poster>: https://bad.example/p.png"),
    ('<audio src="https://bad.example/a.mp3"></audio>', "<audio src>: https://bad.example/a.mp3"),
    ('<track src="https://bad.example/t.vtt">', "<track src>: https://bad.example/t.vtt"),
    ('<font src="https://bad.example/f.woff">x</font>', "<font src>: https://bad.example/f.woff"),
    ('<style>@import "https://evil.example/x.css";</style>', "<style>: https://evil.example/x.css"),
    ('<style>body{background:url(//evil.example/a.png)}</style>', "<style>: https://evil.example/a.png"),
    ('<p style="background:url(http://evil.example/a.png)">x</p>', "<p style>: http://evil.example/a.png"),
    ('<img src="https://[bad">', "is not a valid URL"),
])
def test_disallowed_resources_are_precise_errors(html, expected):
    _, errors, _ = _clean(html)
    assert len(errors) == 1 and expected in errors[0], errors


def test_errors_are_deduplicated_and_capped():
    repeated = '<img src="https://bad.example/a.png">' * 3
    _, errors, _ = _clean(repeated)
    assert len(errors) == 1
    many = "".join(f'<img src="https://bad{i}.example/a.png">' for i in range(30))
    _, errors, _ = _clean(many)
    assert len(errors) == artifact_html.MAX_ERRORS + 1
    assert errors[-1] == "and 10 more problems"


@pytest.mark.parametrize("title,kind,html,expected", [
    ("", "page", "<p>x</p>", "title is required"),
    ("t" * 121, "page", "<p>x</p>", "title is 121 characters"),
    ("Ok", "slides", "<p>x</p>", "kind must be one of page, app, visualization"),
    ("Ok", "page", "   ", "html must be a non-empty string"),
    ("Ok", "page", None, "html must be a non-empty string"),
    (None, "page", 5, "title is required"),
])
def test_title_kind_and_html_are_validated(title, kind, html, expected):
    _, errors = validate_html(html, title=title, kind=kind)
    assert any(expected in e for e in errors), errors


def test_validate_html_returns_document_and_errors():
    document, errors = validate_html("<p>x</p>", title="Ok", kind="page")
    assert errors == [] and CSP_META in document


def test_revalidation_is_idempotent_and_foreign_policies_are_kept():
    document, _ = validate_html("<p>x</p>", title="Ok", kind="page")
    again, errors = validate_html(document, title="Ok", kind="page")
    assert errors == [] and again == document and again.count(CSP_META) == 1
    stricter = '<meta http-equiv="Content-Security-Policy" content="script-src \'none\'">'
    page, _ = validate_html(f"<html><head>{stricter}</head><body></body></html>", title="Ok", kind="page")
    assert page.index(CSP_META) < page.index(stricter)


def test_srcset_parsing_handles_data_urls_and_descriptors():
    assert artifact_html._srcset_urls("a.png 1x, data:image/png;base64,AA 2x,b.png") == [
        "a.png", "data:image/png;base64,AA", "b.png"]
    assert artifact_html._srcset_urls("one.png, two.png 480w") == ["one.png", "two.png"]
    assert artifact_html._srcset_urls(" , ") == []


def test_source_params_drop_html_for_artifact_tool_only():
    params = {"title": "T", "html": "<p>x</p>"}
    clean = artifact_html.source_params("artifacts-1", params)
    assert clean["title"] == "T" and "html" not in clean and len(clean["html_sha256"]) == 64
    assert artifact_html.source_params("general-1", params) is params
    assert artifact_html.source_params("artifacts-1", {"title": "T"}) == {"title": "T"}
    assert artifact_html.source_params("artifacts-1", None) is None


def test_download_link(monkeypatch):
    monkeypatch.setitem(flags._flags, "artifact_export", True)
    link = artifact_html.download_link("chat 1")
    assert link({"component_id": "art_0123456789abcdef"}) == \
        "/api/export/component/art_0123456789abcdef.html?chat_id=chat%201"
    assert link({"component_id": "wc_x"}) is None and link({}) is None
    assert artifact_html.download_link(None) is None
    monkeypatch.setitem(flags._flags, "artifact_export", False)
    assert artifact_html.download_link("chat") is None


def test_prompt_addendum_carries_design_rules():
    text = artifact_html.SYSTEM_PROMPT_ADDENDUM
    for rule in ("publish_artifact", "update_artifact", "2-4 words", ":root",
                 "@media (prefers-color-scheme: dark)", ':root:not([data-theme=\\"light\\"])'.replace("\\", ""),
                 ':root[data-theme="dark"]', "explicit background", "16 px side gutters", "375 px",
                 "cdnjs.cloudflare.com, cdn.jsdelivr.net, unpkg.com or esm.sh", "two weeks",
                 "Google Fonts", "inline", "Do not publish ordinary answers", "640 KiB"):
        assert rule in text, rule


def test_publish_success_returns_identity_and_component():
    response = artifact_html.publish({"title": "  Tip   Calculator ", "kind": "app",
                                      "html": "<p>x</p><iframe src='y'></iframe>"})
    assert response.error is None
    component = response.ui_components[0]
    assert set(component) == {"type", "id", "component_id", "title", "kind", "version", "html", "created_at"}
    assert component["type"] == "html_artifact" and component["kind"] == "app"
    assert component["id"] == component["component_id"] and artifact_html.ARTIFACT_ID.match(component["id"])
    assert component["title"] == "Tip Calculator" and component["version"] == 1
    assert component["created_at"].endswith("Z") and "T" in component["created_at"]
    assert CSP_META in component["html"] and "iframe" not in component["html"]
    assert response.result == {"artifact_id": component["id"], "version": 1, "title": "Tip Calculator",
                               "removed": ["<iframe>"]}
    default_kind = artifact_html.publish({"title": "Chart", "html": "<p>y</p>"})
    assert default_kind.ui_components[0]["kind"] == "page"
    assert "removed" not in default_kind.result


def test_publish_failure_emits_nothing():
    response = artifact_html.publish({"title": "Bad", "html": '<script src="https://evil.example/x.js"></script>'})
    assert response.error is None and not response.ui_components
    assert response.result["status"] == "rejected"
    assert "call publish_artifact again" in response.result["message"]
    assert response.result["errors"][0].startswith("<script src>: https://evil.example/x.js")


class _Versions:
    def __init__(self):
        self.rows = []

    def archive(self, transaction, *, owner_id, conversation_id, component_id, component, reason, retain):
        number = 1 + max((r.version_number for r in self.rows), default=0)
        self.rows.append(types.SimpleNamespace(
            version_id=number, version_number=number, reason=reason, created_at="2026-11-01T00:00:00Z",
            component=dict(component), owner=owner_id, chat=conversation_id, cid=component_id))
        self.rows = self.rows[-retain:]
        return self.rows[-1]

    def list_for_component(self, transaction, *, owner_id, conversation_id, component_id, limit):
        mine = [r for r in self.rows if (r.owner, r.chat, r.cid) == (owner_id, conversation_id, component_id)]
        return list(reversed(mine))[:limit]


class _Runtime:
    @contextmanager
    def transaction(self):
        yield object()


def _plane(versions):
    return ApplicationPlaneSource(plane_runtime=_Runtime(),
                                  plane_repositories=types.SimpleNamespace(
                                      artifacts=types.SimpleNamespace(versions=versions)))


class _Workspace:
    def __init__(self):
        self.rows = {}

    def get_by_component_id(self, chat_id, user_id, component_id):
        return self.rows.get((chat_id, user_id, component_id))


def _published(workspace, chat="chat-1", owner="owner"):
    response = artifact_html.publish({"title": "Board", "kind": "visualization", "html": "<p>v1</p>"})
    component = response.ui_components[0]
    workspace.rows[(chat, owner, component["id"])] = {"component_data": component}
    return component


def test_update_increments_version_and_keeps_five():
    versions, workspace = _Versions(), _Workspace()
    orch = types.SimpleNamespace(workspace=workspace)
    component = _published(workspace)
    plane = _plane(versions)
    for expected in range(2, 9):
        response = artifact_html.update(orch, owner="owner", chat_id="chat-1", plane_source=plane,
                                        args={"artifact_id": component["id"], "html": f"<p>v{expected}</p>"})
        updated = response.ui_components[0]
        assert response.result == {"artifact_id": component["id"], "version": expected, "title": "Board"}
        assert updated["version"] == expected and updated["kind"] == "visualization"
        assert updated["component_id"] == component["id"] and f"<p>v{expected}</p>" in updated["html"]
        workspace.rows[("chat-1", "owner", component["id"])] = {"component_data": updated}
    assert len(versions.rows) == artifact_versions.RETAIN == 5
    assert [r.component["version"] for r in versions.rows] == [3, 4, 5, 6, 7]
    assert {r.reason for r in versions.rows} == {"refine"}
    history = updated["versions"]
    assert len(history) == 5 and history[0]["version_no"] == max(r.version_number for r in versions.rows)


@pytest.mark.parametrize("args,expected", [
    ({"artifact_id": "nope", "html": "<p>x</p>"}, "artifact_id must be the id returned"),
    ({"artifact_id": "art_0000000000000000", "html": "<p>x</p>"}, "is not an artifact in this conversation"),
])
def test_update_rejects_unknown_artifacts(args, expected):
    versions = _Versions()
    response = artifact_html.update(types.SimpleNamespace(workspace=_Workspace()), owner="o", chat_id="c",
                                    args=args, plane_source=_plane(versions))
    assert response.result["status"] == "rejected" and expected in response.result["errors"][0]
    assert not response.ui_components and versions.rows == []


def test_update_rejects_non_html_rows_and_invalid_pages():
    versions, workspace = _Versions(), _Workspace()
    orch = types.SimpleNamespace(workspace=workspace)
    workspace.rows[("c", "o", "art_1111111111111111")] = {"component_data": {"type": "table"}}
    response = artifact_html.update(orch, owner="o", chat_id="c", plane_source=_plane(versions),
                                    args={"artifact_id": "art_1111111111111111", "html": "<p>x</p>"})
    assert response.result["status"] == "rejected"
    component = _published(workspace, chat="c", owner="o")
    workspace.rows[("c", "o", component["id"])]["component_data"]["version"] = "bogus"
    bad = artifact_html.update(orch, owner="o", chat_id="c", plane_source=_plane(versions),
                               args={"artifact_id": component["id"], "html": "<img src='x.png'>"})
    assert bad.result["status"] == "rejected" and "call update_artifact again" in bad.result["message"]
    assert versions.rows == []
    good = artifact_html.update(orch, owner="o", chat_id="c", plane_source=_plane(versions),
                                args={"artifact_id": component["id"], "html": "<p>y</p>"})
    assert good.result["version"] == 2


def test_update_is_scoped_to_owner_and_conversation():
    versions, workspace = _Versions(), _Workspace()
    component = _published(workspace, chat="chat-a", owner="alice")
    orch = types.SimpleNamespace(workspace=workspace)
    for owner, chat in (("bob", "chat-a"), ("alice", "chat-b")):
        response = artifact_html.update(orch, owner=owner, chat_id=chat, plane_source=_plane(versions),
                                        args={"artifact_id": component["id"], "html": "<p>z</p>"})
        assert response.result["status"] == "rejected"
    assert versions.rows == []


def _request(name, arguments, request_id="req-1"):
    from shared.agent_runtime import AgentRuntime
    request = MCPRequest(request_id=request_id, method="tools/call",
                         params={"name": name, "arguments": dict(arguments)})
    runtime = AgentRuntime(ws=None, msg=request, agent_id="artifacts-1", loop=MagicMock())
    request.params["arguments"]["_runtime"] = runtime
    return request


def _binding(**over):
    binding = {"agent_id": "artifacts-1", "requester_verified": True,
               "requester_owner": "owner", "requester_chat": "chat-1"}
    binding.update(over)
    return binding


def test_mcp_server_publishes_through_dispatch_binding():
    from agents.artifacts.mcp_server import MCPServer
    orch = types.SimpleNamespace(_dispatch_context={"req-1": _binding()})
    server = MCPServer(orch)
    response = server.process_request(_request("publish_artifact", {
        "title": "Clock", "html": "<p>t</p>", "user_id": "mallory", "session_id": "other", "_x": 1}))
    assert response.request_id == "req-1" and response.error is None
    assert response.ui_components[0]["type"] == "html_artifact"
    listing = server.process_request(MCPRequest(request_id="r2", method="tools/list", params={}))
    tools = {t["name"]: t for t in listing.result["tools"]}
    assert set(tools) == {"publish_artifact", "update_artifact"}
    assert {t["scope"] for t in tools.values()} == {"tools:write"}
    assert tools["update_artifact"]["input_schema"]["required"] == ["artifact_id", "html"]


@pytest.mark.parametrize("binding", [
    None, _binding(agent_id="evidence-1"), _binding(requester_verified=False),
    _binding(requester_owner=""), _binding(requester_chat=None),
])
def test_mcp_server_refuses_without_verified_binding(binding):
    from agents.artifacts.mcp_server import MCPServer
    orch = types.SimpleNamespace(_dispatch_context={"req-1": binding} if binding else {})
    response = MCPServer(orch).process_request(_request("publish_artifact", {"title": "T", "html": "<p>x</p>"}))
    assert response.error["code"] == "artifact_not_authorized" and not response.ui_components


def test_mcp_server_refuses_malformed_calls():
    from agents.artifacts.mcp_server import MCPServer
    server = MCPServer(types.SimpleNamespace(_dispatch_context={"req-1": _binding()}))
    unknown = server.process_request(_request("delete_everything", {}))
    assert unknown.error["code"] == "artifact_not_authorized"
    no_runtime = MCPRequest(request_id="req-1", method="tools/call",
                            params={"name": "publish_artifact", "arguments": {"title": "T", "html": "x"}})
    assert server.process_request(no_runtime).error["code"] == "artifact_not_authorized"


def test_mcp_server_update_uses_plane_source(monkeypatch):
    from agents.artifacts import mcp_server
    versions, workspace = _Versions(), _Workspace()
    component = _published(workspace)
    orch = types.SimpleNamespace(_dispatch_context={"req-1": _binding()}, workspace=workspace,
                                 plane_repository_source=_plane(versions))
    response = mcp_server.MCPServer(orch).process_request(
        _request("update_artifact", {"artifact_id": component["id"], "html": "<p>v2</p>"}))
    assert response.result["version"] == 2 and len(versions.rows) == 1


def test_mcp_server_reports_storage_failures():
    from agents.artifacts import mcp_server
    workspace = _Workspace()
    component = _published(workspace)
    orch = types.SimpleNamespace(_dispatch_context={"req-1": _binding()}, workspace=workspace)
    response = mcp_server.MCPServer(orch).process_request(
        _request("update_artifact", {"artifact_id": component["id"], "html": "<p>v2</p>"}))
    assert response.error == {"code": "artifact_unavailable", "retryable": True,
                              "message": "The artifact could not be saved. Try again."}


def test_agent_card_registration_and_security_review(monkeypatch, tmp_path):
    from agents.artifacts.artifacts_agent import ArtifactsAgent
    from orchestrator import local_agents
    from orchestrator.tool_security import ToolSecurityAnalyzer
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "artifacts-key.pem"))
    agent = ArtifactsAgent(types.SimpleNamespace(), port=8399)
    assert agent.card.agent_id == "artifacts-1"
    assert {s.id: s.scope for s in agent.card.skills} == {"publish_artifact": "tools:write",
                                                          "update_artifact": "tools:write"}
    assert "artifacts-1" in local_agents.FIRST_PARTY_PUBLIC_AGENT_IDS
    assert local_agents._load_agent_class("artifacts") is ArtifactsAgent
    analyzer = ToolSecurityAnalyzer()
    assert [analyzer.analyze_tool(s.id, s.description, s.input_schema) for s in agent.card.skills] == [None, None]


USER_ID = "pytest-artifact-user"
CHAT_ID = "chat-artifact-1"


def _token(payload):
    body = base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


AUTH = {"Authorization": f"Bearer {_token({'sub': USER_ID})}"}


@pytest.fixture()
def export_client(monkeypatch):
    monkeypatch.setitem(flags._flags, "artifact_export", True)
    orch = MagicMock()
    orch.workspace.aget_by_component_id = AsyncMock(return_value=None)
    orch._canvas_components = MagicMock(return_value=[])
    app = FastAPI()
    app.include_router(export_router)
    app.state.orchestrator = orch
    return TestClient(app), orch


def test_export_html_artifact_download(export_client, monkeypatch):
    import audit.hooks as hooks
    client, orch = export_client
    record = AsyncMock()
    monkeypatch.setattr(hooks, "record_workspace_event", record)
    component = artifact_html.publish({"title": "Tip Calculator", "html": "<p>x</p>"}).ui_components[0]
    orch.workspace.aget_by_component_id.return_value = {"component_data": component}
    response = client.get(f"/api/export/component/{component['id']}.html", params={"chat_id": CHAT_ID},
                          headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="Tip_Calculator.html"'
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.text.count(CSP_META) == 1
    orch.workspace.aget_by_component_id.assert_awaited_once_with(CHAT_ID, USER_ID, component["id"])
    assert record.await_args.kwargs["detail"] == {"format": "html", "version": 1}


def test_export_html_artifact_refusals(export_client, monkeypatch):
    client, orch = export_client
    url = "/api/export/component/art_0123456789abcdef.html"
    assert client.get(url, params={"chat_id": CHAT_ID}, headers=AUTH).status_code == 404
    orch.workspace.aget_by_component_id.return_value = {"component_data": {"type": "table"}}
    response = client.get(url, params={"chat_id": CHAT_ID}, headers=AUTH)
    assert response.status_code == 422 and response.json()["error"] == "not_an_html_artifact"
    orch.workspace.aget_by_component_id.return_value = {"component_data": {
        "type": "html_artifact", "title": "T", "kind": "page", "html": '<img src="https://bad.example/x">'}}
    response = client.get(url, params={"chat_id": CHAT_ID}, headers=AUTH)
    assert response.status_code == 422 and response.json()["error"] == "artifact_invalid"
    monkeypatch.setitem(flags._flags, "artifact_export", False)
    assert client.get(url, params={"chat_id": CHAT_ID}, headers=AUTH).status_code == 404


def test_canvas_export_flattens_artifacts(export_client):
    client, orch = export_client
    page = artifact_html.publish({"title": "Page", "html": "<p>secret-page</p>"}).ui_components[0]
    frame = {"type": "artifact", "id": "art_aaaaaaaaaaaaaaaa", "title": "F", "kind": "report", "version": 1,
             "sections": [{"id": "s1", "heading": "Numbers", "layout": "stack",
                           "children": [{"type": "text", "content": "inside-frame", "component_id": "wc_t"}]}]}
    orch._canvas_components.return_value = [frame, page]
    response = client.get(f"/api/export/canvas/{CHAT_ID}.html", headers=AUTH)
    assert response.status_code == 200
    assert "inside-frame" in response.text and "Numbers" in response.text
    assert "secret-page" not in response.text and "unsupported component" not in response.text


def test_declarations_and_processing_instructions_survive_or_are_skipped():
    document, _ = _ok('<?xml-stylesheet x?><![CDATA[raw]]><object><!DOCTYPE x><?pi y?><![CDATA[gone]]></object><p>k</p>')
    assert "<?xml-stylesheet x?>" in document and "<![CDATA[raw]]>" in document
    assert "gone" not in document and "pi y" not in document and "DOCTYPE x" not in document
    conditional, _ = _ok("<![if !IE]><p>modern</p><![endif]>")
    assert "<![if !IE]><p>modern</p><![endif]>" in conditional


def test_export_html_audit_failure_still_downloads(export_client, monkeypatch):
    import audit.hooks as hooks
    client, orch = export_client
    monkeypatch.setattr(hooks, "record_workspace_event", AsyncMock(side_effect=RuntimeError("audit down")))
    component = artifact_html.publish({"title": "Page", "html": "<p>x</p>"}).ui_components[0]
    orch.workspace.aget_by_component_id.return_value = {"component_data": component}
    response = client.get(f"/api/export/component/{component['id']}.html", params={"chat_id": CHAT_ID},
                          headers=AUTH)
    assert response.status_code == 200 and "<p>x</p>" in response.text


def test_share_snapshot_flattens_artifacts(monkeypatch):
    from orchestrator import artifact_share
    from orchestrator.api import share_router
    monkeypatch.setitem(flags._flags, "artifact_sharing", True)
    minted = {}

    class _Store:
        async def mint(self, **kwargs):
            minted.update(kwargs)
            return {"id": 1, "share_url": "https://x/s/1", "created_at": "t", "expires_at": "t"}

    monkeypatch.setattr(artifact_share, "get_share_store", lambda: _Store())
    orch = MagicMock()
    page = artifact_html.publish({"title": "Page", "html": "<p>secret-page</p>"}).ui_components[0]
    orch.workspace.aget_by_component_id = AsyncMock(return_value={"component_data": page})
    app = FastAPI()
    app.include_router(share_router)
    app.state.orchestrator = orch
    response = TestClient(app).post("/api/share", json={"chat_id": CHAT_ID, "scope": "component",
                                                         "component_id": page["id"]}, headers=AUTH)
    assert response.status_code == 201
    assert minted["snapshot_json"][0]["type"] == "text"
    assert "secret-page" not in minted["snapshot_html"]
