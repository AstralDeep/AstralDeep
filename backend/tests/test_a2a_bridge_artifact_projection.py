"""Tests for shared/a2a_bridge.py's response conversion: every A2A artifact and
message part survives the MCP projection in order with its identity, media type,
and explicit representation for unsupported, oversized, or hostile content.
"""

from __future__ import annotations

from typing import Any, Dict, List

from a2a.types import (
    Artifact,
    Message as A2AMessage,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
)
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

from shared.a2a_bridge import (
    _A2A_PROJECTION_MAX_ARTIFACTS,
    _A2A_PROJECTION_MAX_PARTS,
    _A2A_PROJECTION_MAX_TEXT_CHARS,
    _A2A_PROJECTION_MAX_UI_COMPONENTS,
    a2a_response_to_mcp_response,
    make_data_part,
    make_text_part,
)
from shared.protocol import MCPResponse


def _task(artifacts: List[Artifact], *, status_message: A2AMessage = None) -> Task:
    status = TaskStatus(state=TaskState.TASK_STATE_COMPLETED)
    if status_message is not None:
        status.message.CopyFrom(status_message)
    return Task(
        id="task-abc",
        context_id="ctx-123",
        status=status,
        artifacts=artifacts,
    )


def _data_artifact(artifact_id: str, name: str, payload: Dict[str, Any]) -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        name=name,
        parts=[make_data_part(payload)],
    )


def _message(parts: List[Part]) -> A2AMessage:
    return A2AMessage(message_id="m-1", role=Role.ROLE_AGENT, parts=parts)


def _part_with_media(part: Part, media_type: str) -> Part:
    part.media_type = media_type
    return part


def test_single_structured_artifact_keeps_the_simple_result() -> None:
    task = _task([_data_artifact("a-1", "result", {"answer": 42})])
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.result == {"answer": 42}
    assert response.result_type == "complete"
    assert response.correlation_id == "task-abc"


def test_single_text_part_keeps_the_simple_result() -> None:
    task = _task([Artifact(artifact_id="a-1", name="note", parts=[make_text_part("hi")])])
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.result == "hi"


def test_single_artifact_whose_data_looks_like_an_envelope_stays_verbatim() -> None:
    payload = {"a2a_artifacts": [{"artifact_id": "fake", "parts": []}]}
    task = _task([_data_artifact("a-1", "result", payload)])
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.result == payload


def test_two_structured_artifacts_preserve_ordering_and_identity() -> None:
    task = _task(
        [
            _data_artifact("a-1", "first", {"value": "first"}),
            _data_artifact("a-2", "second", {"value": "second"}),
        ]
    )
    response = a2a_response_to_mcp_response(task, "req-1")
    envelope = response.result
    assert envelope["a2a_artifacts"] == [
        {
            "artifact_id": "a-1",
            "name": "first",
            "parts": [{"data": {"value": "first"}}],
        },
        {
            "artifact_id": "a-2",
            "name": "second",
            "parts": [{"data": {"value": "second"}}],
        },
    ]
    assert "a2a_projection" not in envelope


def test_mixed_text_and_data_parts_preserve_order() -> None:
    artifact = Artifact(
        artifact_id="a-1",
        name="mixed",
        parts=[
            make_text_part("intro"),
            make_data_part({"value": 1}),
            make_text_part("outro"),
        ],
    )
    response = a2a_response_to_mcp_response(_task([artifact]), "req-1")
    parts = response.result["a2a_artifacts"][0]["parts"]
    assert parts == [
        {"text": "intro"},
        {"data": {"value": 1}},
        {"text": "outro"},
    ]


def test_multiple_text_parts_all_survive() -> None:
    artifact = Artifact(
        artifact_id="a-1",
        name="notes",
        parts=[make_text_part("one"), make_text_part("two"), make_text_part("three")],
    )
    response = a2a_response_to_mcp_response(_task([artifact]), "req-1")
    parts = response.result["a2a_artifacts"][0]["parts"]
    assert parts == [{"text": "one"}, {"text": "two"}, {"text": "three"}]


def test_empty_artifacts_keep_their_place_and_identity() -> None:
    task = _task(
        [
            _data_artifact("a-1", "only-value", {"value": 1}),
            Artifact(artifact_id="a-2", name="empty"),
            Artifact(artifact_id="a-3", name="also-empty", parts=[]),
        ]
    )
    response = a2a_response_to_mcp_response(task, "req-1")
    artifacts = response.result["a2a_artifacts"]
    assert [a["artifact_id"] for a in artifacts] == ["a-1", "a-2", "a-3"]
    assert artifacts[1]["parts"] == []
    assert artifacts[2]["parts"] == []


def test_artifacts_without_ids_project_null_identity() -> None:
    task = _task(
        [
            Artifact(name="anonymous", parts=[make_text_part("hi")]),
            _data_artifact("a-2", "identified", {"value": 1}),
        ]
    )
    response = a2a_response_to_mcp_response(task, "req-1")
    artifacts = response.result["a2a_artifacts"]
    assert artifacts[0]["artifact_id"] is None
    assert artifacts[0]["name"] == "anonymous"
    assert artifacts[1]["artifact_id"] == "a-2"


def test_media_types_are_preserved_on_projected_parts() -> None:
    artifact = Artifact(
        artifact_id="a-1",
        name="typed",
        parts=[
            _part_with_media(make_text_part("plain"), "text/plain"),
            _part_with_media(make_data_part({"k": "v"}), "application/json"),
        ],
    )
    response = a2a_response_to_mcp_response(_task([artifact]), "req-1")
    parts = response.result["a2a_artifacts"][0]["parts"]
    assert parts[0] == {"text": "plain", "media_type": "text/plain"}
    assert parts[1] == {"data": {"k": "v"}, "media_type": "application/json"}


def test_non_dict_data_part_values_are_preserved() -> None:
    parts = [
        Part(data=ParseDict([1, 2, 3], Value())),
        Part(data=ParseDict("scalar", Value())),
    ]
    artifact = Artifact(artifact_id="a-1", name="raw-json", parts=parts)
    response = a2a_response_to_mcp_response(_task([artifact]), "req-1")
    records = response.result["a2a_artifacts"][0]["parts"]
    assert records == [{"data": [1, 2, 3]}, {"data": "scalar"}]


def test_raw_bytes_part_is_represented_without_inlining_bytes() -> None:
    part = Part(raw=b"\x00\x01\x02", filename="blob.bin", media_type="application/octet-stream")
    artifact = Artifact(artifact_id="a-1", name="files", parts=[part])
    response = a2a_response_to_mcp_response(_task([artifact]), "req-1")
    record = response.result["a2a_artifacts"][0]["parts"][0]
    assert record == {
        "unsupported": {
            "kind": "bytes",
            "size_bytes": 3,
            "filename": "blob.bin",
            "media_type": "application/octet-stream",
        }
    }
    assert "\\x00" not in str(response.result)


def test_uri_part_is_represented_explicitly() -> None:
    part = Part(url="https://peer.example/report.pdf", media_type="application/pdf")
    artifact = Artifact(artifact_id="a-1", name="links", parts=[part])
    response = a2a_response_to_mcp_response(_task([artifact]), "req-1")
    record = response.result["a2a_artifacts"][0]["parts"][0]
    assert record == {
        "unsupported": {
            "kind": "uri",
            "uri": "https://peer.example/report.pdf",
            "media_type": "application/pdf",
        }
    }


def test_part_without_content_is_represented_as_unsupported() -> None:
    artifact = Artifact(artifact_id="a-1", name="hollow", parts=[Part()])
    response = a2a_response_to_mcp_response(_task([artifact]), "req-1")
    record = response.result["a2a_artifacts"][0]["parts"][0]
    assert record == {"unsupported": {"kind": "empty"}}


def test_oversize_text_part_is_truncated_explicitly() -> None:
    long_text = "x" * (_A2A_PROJECTION_MAX_TEXT_CHARS + 10)
    artifact = Artifact(artifact_id="a-1", name="long", parts=[make_text_part(long_text)])
    response = a2a_response_to_mcp_response(_task([artifact]), "req-1")
    record = response.result["a2a_artifacts"][0]["parts"][0]
    assert record["text"] == "x" * _A2A_PROJECTION_MAX_TEXT_CHARS
    assert record["omitted_text_chars"] == 10


def test_artifact_overflow_is_counted_not_silently_dropped() -> None:
    artifacts = [
        _data_artifact(f"a-{i}", f"artifact-{i}", {"index": i})
        for i in range(_A2A_PROJECTION_MAX_ARTIFACTS + 3)
    ]
    response = a2a_response_to_mcp_response(_task(artifacts), "req-1")
    envelope = response.result
    assert len(envelope["a2a_artifacts"]) == _A2A_PROJECTION_MAX_ARTIFACTS
    assert envelope["a2a_projection"] == {"omitted_artifacts": 3}
    assert envelope["a2a_artifacts"][0]["parts"][0]["data"] == {"index": 0}


def test_part_overflow_is_counted_not_silently_dropped() -> None:
    parts = [make_text_part(f"part-{i}") for i in range(_A2A_PROJECTION_MAX_PARTS + 2)]
    artifact = Artifact(artifact_id="a-1", name="many", parts=parts)
    response = a2a_response_to_mcp_response(_task([artifact]), "req-1")
    envelope = response.result
    projected = envelope["a2a_artifacts"][0]["parts"]
    assert len(projected) == _A2A_PROJECTION_MAX_PARTS
    assert projected[0] == {"text": "part-0"}
    assert envelope["a2a_projection"] == {"omitted_parts": 2}


def test_ui_component_overflow_is_counted_not_silently_dropped() -> None:
    components = [{"type": f"comp-{i}"} for i in range(_A2A_PROJECTION_MAX_UI_COMPONENTS + 4)]
    artifact = Artifact(
        artifact_id="a-1",
        name="ui",
        parts=[make_data_part({"_ui_components": components})],
    )
    response = a2a_response_to_mcp_response(_task([artifact]), "req-1")
    assert response.ui_components == components[:_A2A_PROJECTION_MAX_UI_COMPONENTS]
    assert response.result["a2a_projection"] == {"omitted_ui_components": 4}


def test_ui_component_cap_applies_across_artifacts() -> None:
    def ui_artifact(artifact_id: str, marker: str) -> Artifact:
        return Artifact(
            artifact_id=artifact_id,
            name="ui",
            parts=[make_data_part({"_ui_components": [{"marker": marker}, {"marker": f"{marker}-b"}]})],
        )

    task = _task([ui_artifact("a-1", "first"), ui_artifact("a-2", "second")])
    response = a2a_response_to_mcp_response(task, "req-1")
    markers = [c["marker"] for c in response.ui_components]
    assert markers == ["first", "first-b", "second", "second-b"]


def test_hostile_non_list_ui_payload_is_represented_not_fatal() -> None:
    for hostile in ["boom", {"deep": {"nested": 1}}, 7.5, True]:
        artifact = Artifact(
            artifact_id="a-1",
            name="hostile",
            parts=[make_data_part({"_ui_components": hostile})],
        )
        response = a2a_response_to_mcp_response(_task([artifact]), "req-1")
        assert isinstance(response, MCPResponse)
        assert response.ui_components is None
        record = response.result["a2a_artifacts"][0]["parts"][0]
        assert record["invalid_ui_components"]["value_kind"] == type(hostile).__name__
        assert record["invalid_ui_components"]["value"] == hostile


def test_hostile_oversize_ui_payload_is_reduced_to_a_bounded_preview() -> None:
    hostile = {"blob": "y" * (_A2A_PROJECTION_MAX_TEXT_CHARS + 10)}
    artifact = Artifact(
        artifact_id="a-1",
        name="hostile",
        parts=[make_data_part({"_ui_components": hostile})],
    )
    response = a2a_response_to_mcp_response(_task([artifact]), "req-1")
    record = response.result["a2a_artifacts"][0]["parts"][0]["invalid_ui_components"]
    assert record["value_kind"] == "dict"
    assert len(record["value_prefix"]) == _A2A_PROJECTION_MAX_TEXT_CHARS
    assert record["omitted_chars"] > 0


def test_ui_part_with_companion_keys_keeps_the_remainder_as_data() -> None:
    artifact = Artifact(
        artifact_id="a-1",
        name="ui-and-data",
        parts=[make_data_part({"_ui_components": [{"type": "Text"}], "_data": {"answer": 7}})],
    )
    response = a2a_response_to_mcp_response(_task([artifact]), "req-1")
    assert response.ui_components == [{"type": "Text"}]
    assert response.result == {"_data": {"answer": 7}}


def test_message_with_single_data_part_keeps_the_simple_result() -> None:
    response = a2a_response_to_mcp_response(
        _message([make_data_part({"answer": 42})]), "req-1"
    )
    assert response.result == {"answer": 42}


def test_message_with_multiple_parts_preserves_all_of_them() -> None:
    response = a2a_response_to_mcp_response(
        _message([make_text_part("hello"), make_data_part({"k": 1}), make_text_part("bye")]),
        "req-1",
    )
    assert response.result == {
        "a2a_parts": [{"text": "hello"}, {"data": {"k": 1}}, {"text": "bye"}]
    }


def test_message_ui_components_accumulate_across_parts() -> None:
    response = a2a_response_to_mcp_response(
        _message(
            [
                make_data_part({"_ui_components": [{"type": "Text", "text": "a"}]}),
                make_data_part({"_ui_components": [{"type": "Text", "text": "b"}]}),
            ]
        ),
        "req-1",
    )
    assert response.ui_components == [
        {"type": "Text", "text": "a"},
        {"type": "Text", "text": "b"},
    ]
    assert response.result is None


def test_message_unsupported_part_is_represented_explicitly() -> None:
    response = a2a_response_to_mcp_response(
        _message([Part(raw=b"abc", filename="f.bin")]), "req-1"
    )
    assert response.result == {
        "a2a_parts": [{"unsupported": {"kind": "bytes", "size_bytes": 3, "filename": "f.bin"}}]
    }


def test_message_empty_parts_still_project_no_result() -> None:
    response = a2a_response_to_mcp_response(_message([]), "req-1")
    assert response.result is None
    assert response.ui_components is None


def test_message_part_overflow_is_counted_not_silently_dropped() -> None:
    parts = [make_text_part(f"part-{i}") for i in range(_A2A_PROJECTION_MAX_PARTS + 1)]
    response = a2a_response_to_mcp_response(_message(parts), "req-1")
    assert len(response.result["a2a_parts"]) == _A2A_PROJECTION_MAX_PARTS
    assert response.result["a2a_projection"] == {"omitted_parts": 1}


def test_message_ui_component_overflow_is_counted_not_silently_dropped() -> None:
    components = [
        {"type": f"comp-{i}"} for i in range(_A2A_PROJECTION_MAX_UI_COMPONENTS + 1)
    ]
    response = a2a_response_to_mcp_response(
        _message([make_data_part({"_ui_components": components})]), "req-1"
    )
    assert len(response.ui_components) == _A2A_PROJECTION_MAX_UI_COMPONENTS
    assert response.result["a2a_projection"] == {"omitted_ui_components": 1}


def test_unexpected_response_type_reports_an_error() -> None:
    response = a2a_response_to_mcp_response(object(), "req-1")
    assert response.result is None
    assert response.error == {
        "code": -32603,
        "message": "Unexpected response type: <class 'object'>",
        "retryable": False,
    }


def test_status_message_fills_result_only_when_artifacts_are_empty() -> None:
    status_message = _message([make_text_part("done")])
    response = a2a_response_to_mcp_response(_task([], status_message=status_message), "req-1")
    assert response.result == "done"


def test_status_message_does_not_overwrite_artifact_results() -> None:
    status_message = _message([make_text_part("status note")])
    task = _task([_data_artifact("a-1", "result", {"value": 1})], status_message=status_message)
    response = a2a_response_to_mcp_response(task, "req-1")
    assert response.result == {"value": 1}


def test_multi_part_status_message_is_preserved_in_full() -> None:
    status_message = _message([make_text_part("alpha"), make_text_part("beta")])
    response = a2a_response_to_mcp_response(_task([], status_message=status_message), "req-1")
    assert response.result == {"a2a_parts": [{"text": "alpha"}, {"text": "beta"}]}


def test_status_message_ui_components_are_still_extracted() -> None:
    status_message = _message([make_data_part({"_ui_components": [{"type": "Alert"}]})])
    response = a2a_response_to_mcp_response(_task([], status_message=status_message), "req-1")
    assert response.ui_components == [{"type": "Alert"}]
    assert response.result is None


def test_projection_envelope_stays_json_serializable() -> None:
    import json

    artifact = Artifact(
        artifact_id="a-1",
        name="mixed",
        parts=[
            make_text_part("t"),
            make_data_part({"k": [1, 2, {"nested": True}]}),
            Part(raw=b"abc"),
            Part(url="https://x.example/f", media_type="text/html"),
        ],
    )
    response = a2a_response_to_mcp_response(_task([artifact]), "req-1")
    encoded = json.dumps(response.result)
    decoded = json.loads(encoded)
    assert decoded["a2a_artifacts"][0]["artifact_id"] == "a-1"
    assert len(decoded["a2a_artifacts"][0]["parts"]) == 4
