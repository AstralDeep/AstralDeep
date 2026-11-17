"""Checks the pin file names one full kernel revision, an official Python build and hashed locks for every platform."""

import json
import re

from node_bundle_support import assemble as ap


def test_pins_name_a_full_kernel_commit_and_no_deep_commit():
    pins = ap.load_pins()
    assert re.fullmatch(r"[0-9a-f]{40}", pins["kernel"]["commit"])
    assert "commit" not in pins["deep"]


def test_every_platform_pins_an_official_python_and_existing_locks():
    for name, target in ap.load_pins()["platforms"].items():
        assert target["python"]["url"].startswith(ap.PYTHON_HOST_PREFIX), name
        assert re.fullmatch(r"[0-9a-f]{64}", target["python"]["sha256"])
        assert (ap.HERE / target["node_lock"]).is_file()


def test_every_locked_requirement_carries_a_hash():
    for lock in (*ap.HERE.glob("node-requirements.*.lock"), ap.HERE / "build-requirements.lock"):
        entries = [line for line in lock.read_text().splitlines() if re.match(r"^[A-Za-z0-9_.\[\]-]+==", line)]
        assert entries, lock.name
        text = lock.read_text()
        for entry in entries:
            block = text[text.index(entry):].split("\n    # via", 1)[0]
            assert "--hash=sha256:" in block, entry


def test_the_pin_file_round_trips_as_json():
    assert json.loads(ap.PINS.read_text())["format"] == "astral.nodepack-pins/v1"
