import json
from types import SimpleNamespace

from orchestrator.orchestrator import _history_assistant_message


def _call(name, arguments, call_id="call_1"):
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments))


def test_plain_assistant_messages_are_returned_unchanged():
    message = SimpleNamespace(role="assistant", content="hello", tool_calls=None)
    assert _history_assistant_message(message) is message


def test_valid_tool_call_arguments_are_kept_and_reasoning_is_dropped():
    message = SimpleNamespace(content=None, reasoning_content="thinking", tool_calls=[_call("read_file", '{"path": "/a"}')])
    history = _history_assistant_message(message)
    assert history == {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "/a"}'}}]}


def test_non_json_tool_call_arguments_become_an_empty_object():
    message = SimpleNamespace(content="", tool_calls=[
        _call("write_file", '{"path": "/a", "content": "unterminated', "call_1"),
        _call("list_dir", "", "call_2"),
        _call("wait", None, "call_3"),
    ])
    history = _history_assistant_message(message)
    assert [call["function"]["arguments"] for call in history["tool_calls"]] == ["{}", "{}", "{}"]
    assert [call["id"] for call in history["tool_calls"]] == ["call_1", "call_2", "call_3"]


def test_raw_control_characters_inside_strings_are_kept_and_re_escaped():
    raw = '{"path": "/a.mjs", "content": "line one' + "\n" + 'line' + "\t" + 'two"}'
    message = SimpleNamespace(content=None, tool_calls=[_call("write_file", raw)])
    arguments = _history_assistant_message(message)["tool_calls"][0]["function"]["arguments"]
    assert json.loads(arguments) == {"path": "/a.mjs", "content": "line one\nline\ttwo"}
    assert "\n" not in arguments
