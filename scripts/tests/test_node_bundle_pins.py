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


def test_the_container_entry_point_is_left_out_and_nothing_a_user_runs_names_docker():
    pins = ap.load_pins()
    left_out = {entry["path"] for entry in pins["backend_exclusions"]}
    assert "start-docker.sh" in left_out
    for path in sorted((ap.HERE / "launcher").glob("*.py")) + sorted((ap.REPO / "scripts" / "node").glob("*")):
        text = path.read_text(errors="replace").lower()
        assert "start-docker" not in text and "docker" not in text, path.name


def test_the_kernel_pin_is_the_commit_of_the_pinned_component():
    import subprocess

    listing = subprocess.run(
        ["git", "ls-tree", "HEAD", "components/AstralKernel"], cwd=ap.REPO, capture_output=True, text=True, check=False
    )
    if listing.returncode != 0 or not listing.stdout.startswith("160000 commit "):
        import pytest

        pytest.skip("the checkout carries no Kernel gitlink")
    assert listing.stdout.split()[2] == ap.load_pins()["kernel"]["commit"]
