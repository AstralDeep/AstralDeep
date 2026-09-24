"""Dice Roller's sole tool: rolls a bounded set of six-sided dice with normalized
metadata, registered in TOOL_REGISTRY for mcp_server.py.
"""

import os
import random
import sys
from typing import Any, Dict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from astralprims import (
    Card, Divider, Grid, List_, MetricCard, Text
)

REQUIRED_CREDENTIALS = []

DIE_SIDES = 6
MIN_DICE = 1
MAX_DICE = 100


def roll_dice(n: int = 1, sides: int = DIE_SIDES) -> Dict[str, Any]:
    if type(n) is not int or not MIN_DICE <= n <= MAX_DICE:
        raise ValueError(
            f"n must be an integer between {MIN_DICE} and {MAX_DICE}"
        )
    if type(sides) is not int or sides != DIE_SIDES:
        raise ValueError(f"only {DIE_SIDES}-sided dice are supported")

    notation = f"{n}d{sides}"
    rolls = [random.randint(1, sides) for _ in range(n)]
    total = sum(rolls)
    bounds = {
        "quantity": {"minimum": MIN_DICE, "maximum": MAX_DICE},
        "roll": {"minimum": 1, "maximum": sides},
        "result": {"minimum": n, "maximum": n * sides},
    }
    labels = {
        "quantity": "Number of dice",
        "rolls": "Individual rolls",
        "result": "Total",
    }

    components = [
        Card(
            title=f"Dice Roll Results ({notation})",
            content=[
                Grid(
                    columns=2,
                    children=[
                        MetricCard(
                            title=labels["result"],
                            value=str(total),
                            variant="default"
                        ),
                        MetricCard(
                            title=labels["quantity"],
                            value=str(n),
                            variant="default"
                        ),
                    ]
                ),
                Divider(variant="solid"),
                Text(
                    content=(
                        f'{labels["rolls"]} '
                        f'({bounds["roll"]["minimum"]}–'
                        f'{bounds["roll"]["maximum"]} each):'
                    ),
                    variant="subheading",
                ),
                List_(
                    items=[
                        f"Die {index + 1}: {roll}"
                        for index, roll in enumerate(rolls)
                    ],
                    ordered=False,
                    variant="default"
                ),
            ]
        ),
    ]

    return {
        "_ui_components": [component.to_dict() for component in components],
        "_data": {
            "tool_name": "roll_dice",
            "n": n,
            "quantity": n,
            "unit": "dice",
            "sides": sides,
            "notation": notation,
            "bounds": bounds,
            "labels": labels,
            "rolls": rolls,
            "total": total,
            "result": {
                "label": labels["result"],
                "value": total,
                "unit": "pips",
            },
        }
    }


TOOL_REGISTRY = {
    "roll_dice": {
        "function": roll_dice,
        "description": "Rolls 1-100 six-sided dice and reports normalized results",
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "Number of six-sided dice to roll (1-100)",
                    "minimum": MIN_DICE,
                    "maximum": MAX_DICE,
                    "default": 1
                },
                "sides": {
                    "type": "integer",
                    "description": "Sides per die; this tool supports d6 only",
                    "const": DIE_SIDES,
                    "default": DIE_SIDES
                }
            },
            "required": []
        },
        "scope": "tools:read"
    }
}
