"""A reply that contains JSON is an answer unless it really is a component payload. Only a
broken component payload asks the model to try again."""

import json
import uuid

import pytest

from shared.feature_flags import flags
from tests import test_turn_seams_loop as seams
from tests.test_turn_seams_loop import _last_assistant_text, _msg, _register, _usage, _ws

orch = seams.orch

_FENCE = '```json\n{"name": "Ada", "age": 36}\n```'
_SCHEMA = '{"type": "object", "properties": {"name": {"type": "string"}}}'
_TYPE_LIST = '{"type": ["string", "null"], "title": "Middle name"}'


async def _turn(o, replies):
    _register(o)
    chat_id = "json-" + uuid.uuid4().hex
    o.history.create_chat(chat_id, user_id="seam-user")
    ws = _ws(o)
    calls = []

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        calls.append(messages)
        return _msg(content=replies[min(len(calls), len(replies)) - 1]), _usage()

    o._call_llm = provider
    await o.handle_chat_message(ws, "Show me some JSON", chat_id, user_id="seam-user")
    return calls, await _last_assistant_text(o, chat_id, "seam-user")


@pytest.fixture
def plain_orch(orch, monkeypatch):
    monkeypatch.setitem(flags._flags, "typesafe_routing", False)
    monkeypatch.setitem(flags._flags, "designer_all_devices", False)
    return orch


@pytest.mark.asyncio
@pytest.mark.parametrize("reply, kept", [
    (_FENCE, "Ada"),
    ('{"name": "Ada"}', "Ada"),
    (_SCHEMA, "properties"),
    (_TYPE_LIST, "Middle name"),
    ("Sea levels rose in the last century [1]", "Sea levels"),
    ("[The tide table](https://example.test/tides) is the best source.", "tide table"),
])
async def test_json_in_a_reply_reaches_the_user_on_the_first_try(plain_orch, user_skills_disabled, reply, kept):
    calls, stored = await _turn(plain_orch, [reply])
    assert len(calls) == 1
    assert kept in stored
    assert "container" not in stored
    assert "SYSTEM RECOVERY" not in json.dumps(calls, default=str)


@pytest.mark.asyncio
async def test_a_component_payload_still_renders(plain_orch, user_skills_disabled):
    calls, stored = await _turn(plain_orch, [json.dumps([{"type": "alert", "message": "Tide is high."}])])
    assert len(calls) == 1
    assert "alert" in stored and "Tide is high." in stored


@pytest.mark.asyncio
async def test_a_broken_component_payload_is_still_retried(plain_orch, user_skills_disabled):
    calls, stored = await _turn(plain_orch, ['[{"type": "alert", "message": "Tide', "Tide is high."])
    assert len(calls) == 2
    assert "SYSTEM RECOVERY" in json.dumps(calls[1], default=str)
    assert "Tide is high." in stored
