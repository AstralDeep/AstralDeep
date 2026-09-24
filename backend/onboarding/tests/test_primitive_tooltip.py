"""Tests for the additive tooltip field on AstralPrimitives' base Component: default
value, subclass serialization, absence on legacy payloads, and round-trip
persistence.
"""

from __future__ import annotations

import pytest

from astralprims import (
    Alert,
    Button,
    Card,
    Primitive,
    Container,
    Image,
    Input,
    MetricCard,
    ProgressBar,
    Table,
    Text,
)


def test_base_component_defaults_tooltip_to_none():
    c = Primitive(type="text")
    assert c.tooltip is None


@pytest.mark.parametrize(
    "ctor,extras",
    [
        (Container, {}),
        (Text, {}),
        (Button, {"label": "x"}),
        (Card, {}),
        (Table, {"headers": [], "rows": []}),
        (Alert, {"message": "x"}),
        (ProgressBar, {"value": 1.0}),
        (MetricCard, {"title": "x", "value": "1"}),
        (Image, {"url": "data:"}),
        (Input, {}),
    ],
)
def test_subclasses_serialize_with_tooltip(ctor, extras):
    inst = ctor(tooltip="hello", **extras)
    payload = inst.to_dict()
    assert payload.get("tooltip") == "hello"
    rebuilt = Primitive.from_dict(payload)
    assert rebuilt.tooltip == "hello"


def test_tooltip_absent_when_unset():
    rebuilt = Primitive.from_dict({"type": "text", "value": "hi"})
    assert rebuilt.tooltip is None


def test_tooltip_persists_through_roundtrip():
    btn = Button(label="hi", tooltip="click me")
    payload = btn.to_dict()
    assert payload["tooltip"] == "click me"
    again = Button(**{k: v for k, v in payload.items() if k in Button.model_fields})
    assert again.tooltip == "click me"
