"""Verify tool argument schema references stay within the supplied schema and fail closed before dispatch."""

import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from orchestrator.tool_arguments import ArgumentError, ArgumentResult, model_error_text, prepare_arguments


@pytest.fixture
def reference_sources(tmp_path, monkeypatch):
    retrieved = []
    requests = []
    content = b'{"type":"object","required":["retrieved_secret"]}'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    source = tmp_path / "private-schema.json"
    source.write_bytes(content)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ready = threading.Event()

    def serve():
        ready.set()
        server.serve_forever()

    original = urllib.request.urlopen

    def track(request, *args, **kwargs):
        retrieved.append(request.full_url)
        return original(request, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", track)
    thread = threading.Thread(target=serve)
    thread.start()
    ready.wait()
    try:
        yield {
            "http": f"http://127.0.0.1:{server.server_port}/private-schema.json",
            "file": source.as_uri(),
            "retrieved": retrieved,
            "requests": requests,
        }
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("transport", ["http", "file"])
@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
@pytest.mark.parametrize("nested", [False, True])
def test_external_references_are_refused_without_retrieval(reference_sources, transport, keyword, nested):
    uri = reference_sources[transport]
    schema = {keyword: uri}
    if nested:
        base, name = uri.rsplit("/", 1)
        schema = {
            "type": "object",
            "properties": {"payload": {"$id": base + "/scope.json", keyword: name}},
        }
    raw = {"payload": {}} if nested else {}

    result = prepare_arguments(raw, schema)

    assert reference_sources["retrieved"] == []
    assert reference_sources["requests"] == []
    assert result == ArgumentResult(None, False, ArgumentError(
        "schema_reference_unresolvable", "",
        "The tool schema reference cannot be resolved within the supplied schema.", None))
    error_text = model_error_text("lookup", result.error)
    assert uri not in error_text
    assert "private-schema" not in error_text
    assert "retrieved_secret" not in error_text
    assert len(result.error.message) <= 200


@pytest.mark.parametrize("reference", ["#/$defs/missing", "#missing_anchor", "urn:unavailable:private"])
def test_unresolvable_references_return_a_bounded_error(reference):
    result = prepare_arguments({}, {"$ref": reference})
    assert result.args is None
    assert result.error.code == "schema_reference_unresolvable"
    assert result.error.field == ""
    assert result.error.example is None
    assert reference not in result.error.message


@pytest.mark.parametrize("schema", [
    {"$defs": {"payload": {"type": "object", "required": ["name"]}}, "$ref": "#/$defs/payload"},
    {"$defs": {"payload": {"$anchor": "payload", "type": "object", "required": ["name"]}}, "$ref": "#payload"},
    {"$dynamicAnchor": "payload", "type": "object", "required": ["name"],
     "properties": {"child": {"$dynamicRef": "#payload"}}},
    {"$id": "https://schema.invalid/root.json", "$defs": {
        "payload": {"$id": "payload.json", "type": "object", "required": ["name"]}}, "$ref": "payload.json"},
])
def test_local_definitions_anchors_and_resources_remain_enforced(schema):
    valid = {"name": "root", "child": {"name": "child"}}
    assert prepare_arguments(valid, schema) == ArgumentResult(valid, False, None)
    result = prepare_arguments({}, schema)
    assert result.args is None
    assert result.error.code == "schema_mismatch"
    assert result.error.field == "name"


def test_local_dynamic_reference_validates_nested_arguments():
    schema = {"$dynamicAnchor": "node", "type": "object", "required": ["name"],
              "properties": {"child": {"$dynamicRef": "#node"}}}
    result = prepare_arguments({"name": "root", "child": {}}, schema)
    assert result.args is None
    assert result.error.code == "schema_mismatch"
    assert result.error.field == "name"


def test_unresolved_reference_after_repair_never_returns_repaired_arguments():
    result = prepare_arguments('{"name":"root",}', {"$ref": "#missing"})
    assert result.args is None
    assert result.repaired is False
    assert result.error.code == "schema_reference_unresolvable"


@pytest.mark.parametrize("schema", [None, {}, {"type": "invalid"}])
def test_absent_or_unusable_schema_compatibility_is_preserved(schema):
    assert prepare_arguments('{"name":"root",}', schema) == ArgumentResult({"name": "root"}, True, None)
