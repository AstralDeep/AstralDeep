"""Loads the committed execution policy vectors for the astral_exec tests and builds loaded policies and facts for them.
The vectors are a copy of the contract file, and test_exec_static.py proves the copy has not drifted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from astral_exec import Policy
from astral_exec.facts import Facts

HERE = Path(__file__).resolve().parent
VECTOR_FILE = HERE / "vectors" / "exec-policy-v1.json"
DOC: dict[str, Any] = json.loads(VECTOR_FILE.read_text(encoding="utf-8"))
CONSTANTS: dict[str, Any] = DOC["constants"]
VALIDATION: list[dict[str, Any]] = DOC["validation"]
SELECTION: list[dict[str, Any]] = DOC["selection"]
PROFILES: list[dict[str, Any]] = DOC["profiles"]
CONTEXT_ERRORS: list[dict[str, Any]] = DOC["context_errors"]
PROFILE_ERRORS: list[dict[str, Any]] = DOC["profile_errors"]


def minimal_policy(**overrides: Any) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "v": 1,
        "reads": [],
        "workspace": "/private/tmp/astral/ws",
        "hosts": [],
        "limits": {
            "memory_bytes": 536870912,
            "processes": 1,
            "open_files": 256,
            "file_bytes": 104857600,
        },
        "time": {"wall_ms": 30000},
    }
    policy.update(overrides)
    return policy


DATA_DIR = "/var/lib/astral/node"
HOME = "/home/test"


def _no_disk(path: str) -> Any:
    raise FileNotFoundError(path)


def loaded(
    policy: Any, platform: str = "linux", data_dir: str = DATA_DIR, home: str = HOME
) -> Policy:
    return Policy._load(policy, platform, data_dir, home, lambda p, pl: p, _no_disk)


def good_facts(**overrides: Any) -> Facts:
    facts = Facts(
        platform="linux",
        landlock_abi=6,
        bwrap=True,
        userns=True,
        sandbox_exec=False,
        app_sandboxed=False,
    )
    facts.update(overrides)
    facts.bwrap_path = "/usr/bin/bwrap"
    return facts
