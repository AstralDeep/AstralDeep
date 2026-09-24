"""Tests for the dice_roller agent (agents/dice_roller/mcp_tools.py, mcp_server.py):
normalized six-d6 results, refusal of unsupported quantities or non-d6 requests, and
registry-declared bounds.
"""

from __future__ import annotations

import json

import pytest

from agents.dice_roller import mcp_tools
from agents.dice_roller.mcp_server import MCPServer
from shared.protocol import MCPRequest


def test_six_d6_result_metadata_and_visible_values_are_identical(monkeypatch) -> None:
    expected_rolls = [1, 2, 3, 4, 5, 6]
    values = iter(expected_rolls)
    monkeypatch.setattr(mcp_tools.random, "randint", lambda _low, _high: next(values))

    response = mcp_tools.roll_dice(n=6, sides=6)
    data = response["_data"]

    assert data == {
        "tool_name": "roll_dice",
        "n": 6,
        "quantity": 6,
        "unit": "dice",
        "sides": 6,
        "notation": "6d6",
        "bounds": {
            "quantity": {"minimum": 1, "maximum": 100},
            "roll": {"minimum": 1, "maximum": 6},
            "result": {"minimum": 6, "maximum": 36},
        },
        "labels": {
            "quantity": "Number of dice",
            "rolls": "Individual rolls",
            "result": "Total",
        },
        "rolls": expected_rolls,
        "total": 21,
        "result": {"label": "Total", "value": 21, "unit": "pips"},
    }

    visible = json.dumps(response["_ui_components"], sort_keys=True)
    for expected in ("6d6", "Number of dice", "Individual rolls", "Total", "21"):
        assert expected in visible
    for index, roll in enumerate(expected_rolls, start=1):
        assert f"Die {index}: {roll}" in visible


@pytest.mark.parametrize("n", [0, 101, True, 1.5, "6"])
def test_unsupported_dice_quantities_are_refused_instead_of_clamped(n) -> None:
    with pytest.raises(ValueError, match="n must be an integer between 1 and 100"):
        mcp_tools.roll_dice(n=n)


@pytest.mark.parametrize("sides", [1, 20, True, 6.0, "6"])
def test_non_d6_requests_are_refused_instead_of_mislabelled(sides) -> None:
    with pytest.raises(ValueError, match="only 6-sided dice are supported"):
        mcp_tools.roll_dice(n=6, sides=sides)


def test_registry_exposes_machine_checked_quantity_and_side_bounds() -> None:
    tool = mcp_tools.TOOL_REGISTRY["roll_dice"]
    properties = tool["input_schema"]["properties"]

    assert properties["n"] == {
        "type": "integer",
        "description": "Number of six-sided dice to roll (1-100)",
        "minimum": 1,
        "maximum": 100,
        "default": 1,
    }
    assert properties["sides"]["const"] == 6


def test_mcp_refuses_d20_as_non_retryable_invalid_input() -> None:
    response = MCPServer().process_request(
        MCPRequest(
            request_id="dice-d20",
            method="tools/call",
            params={"name": "roll_dice", "arguments": {"n": 6, "sides": 20}},
        )
    )

    assert response.result is None
    assert response.error == {
        "code": -32603,
        "message": "only 6-sided dice are supported",
        "retryable": False,
    }
