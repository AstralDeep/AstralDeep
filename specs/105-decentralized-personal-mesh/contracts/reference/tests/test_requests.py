"""Checks of the request fold beyond the committed vectors: arrival-order independence and the rule that a request ends in exactly one state."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

import generate_vectors  # noqa: F401
from astral_mesh_ref import requests

VECTORS = Path(__file__).resolve().parents[2] / "vectors"
CASES = [
    c
    for c in json.loads((VECTORS / "requests.json").read_text(encoding="utf-8"))["cases"]
    if c["kind"] == "request_fold"
]
STATES = {"pending", "claimed", "answered", "refused", "cancelled"}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fold_does_not_depend_on_arrival_order(case: dict) -> None:
    expected = case["expect"]["ok"]
    rng = random.Random(7)
    for _ in range(20):
        events = list(case["input"]["events"])
        rng.shuffle(events)
        assert requests.fold(events, case["input"]["devices"].get) == expected


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_every_request_has_one_state_and_matching_fields(case: dict) -> None:
    for view in case["expect"]["ok"]["requests"]:
        assert view["state"] in STATES
        assert (view["turn"] is not None) == (view["state"] == "answered")
        assert (view["code"] is not None) == (view["state"] == "refused")
        if view["state"] in ("claimed", "answered"):
            assert view["claim"] is not None
