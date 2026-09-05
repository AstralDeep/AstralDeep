"""Source normalization and deterministic model proposal boundaries."""

from types import SimpleNamespace

import pytest
from persistent_agents.runtime_values import (
    digest,
    extract_result,
    parse_plan,
    parse_step,
)


def test_public_page_content_is_part_of_revision():
    first = SimpleNamespace(error=None, result={"_data": {"url": "https://example.org"}},
                            ui_components=[{"type": "Card", "children": [
                                {"type": "Text", "text": "Release A"}]}])
    second = SimpleNamespace(error=None, result=first.result, ui_components=[
        {"type": "Text", "text": "Release B"}])
    assert digest(extract_result(first)) != digest(extract_result(second))


@pytest.mark.parametrize("response", [
    None, SimpleNamespace(error={"message": "no"}, result=None, ui_components=[]),
    SimpleNamespace(error=None, result=None, ui_components=[
        {"type": "Alert", "variant": "error", "message": "failed"}]),
    SimpleNamespace(error=None, result={}, ui_components=[]),
])
def test_failure_is_never_an_unchanged_success(response):
    with pytest.raises(ValueError):
        extract_result(response)


def test_bounded_plan_and_subset():
    plan = parse_plan('{"tasks":[{"id":"one","instruction":"Analyze",'
                      '"tools":["reader:read"],"depends_on":[]}]}', {"reader:read"}, 8)
    assert plan[0]["id"] == "one"
    with pytest.raises(ValueError):
        parse_plan('{"tasks":[{"id":"one","instruction":"Analyze",'
                   '"tools":["writer:send"],"depends_on":[]}]}', {"reader:read"}, 8)


@pytest.mark.parametrize("body", [
    '{"tasks":[],"consent":true}', '{"tasks":[]}',
    '{"tasks":[{"id":"a","instruction":"x","tools":[],"depends_on":["a"]}]}',
    '{"tasks":[{"id":"a","instruction":"x","tools":[],"depends_on":["missing"]}]}',
])
def test_model_cannot_create_authority_or_invalid_graph(body):
    with pytest.raises(ValueError):
        parse_plan(body, set(), 8)


def test_step_tool_is_exact_and_result_is_bounded():
    assert parse_step('{"kind":"result","text":"Done"}', set())["text"] == "Done"
    assert parse_step('{"kind":"tool","tool":"reader:read","arguments":{}}',
                      {"reader:read"})["tool"] == "reader:read"
    for body in ('{"kind":"stop"}', '{"kind":"result","text":"x","consent":true}',
                 '{"kind":"tool","tool":"writer:send","arguments":{}}'):
        with pytest.raises(ValueError):
            parse_step(body, {"reader:read"})
