"""Tests for the legacy VITE_-prefixed env shim (shared/__init__.py): unprefixed names
pass through untouched, legacy names backfill one-way with a deprecation warning, and
unprefixed wins when both are set.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = str(Path(__file__).resolve().parents[1])

_PROBE = (
    "import shared, os;"
    "print(os.getenv('USE_MOCK_AUTH'), os.getenv('KEYCLOAK_AUTHORITY'),"
    "      os.getenv('KEYCLOAK_CLIENT_ID'), os.getenv('VITE_USE_MOCK_AUTH'))"
)


def _probe(env_overrides):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("VITE_", "USE_MOCK", "KEYCLOAK"))}
    env.update(env_overrides)
    env["PYTHONPATH"] = BACKEND
    out = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True,
                         text=True, env=env, cwd=BACKEND)
    assert out.returncode == 0, out.stderr
    return out.stdout.split(), out.stderr


def test_unprefixed_names_pass_through_untouched():
    vals, stderr = _probe({"USE_MOCK_AUTH": "true",
                           "KEYCLOAK_AUTHORITY": "https://kc.example/realms/x",
                           "KEYCLOAK_CLIENT_ID": "astral-frontend"})
    assert vals[0] == "true"
    assert vals[1] == "https://kc.example/realms/x"
    assert vals[2] == "astral-frontend"
    assert vals[3] == "None"
    assert "deprecated" not in stderr


def test_legacy_vite_names_backfill_with_deprecation_warning():
    vals, stderr = _probe({"VITE_USE_MOCK_AUTH": "true",
                           "VITE_KEYCLOAK_AUTHORITY": "https://old.example",
                           "VITE_KEYCLOAK_CLIENT_ID": "legacy-client"})
    assert vals[0] == "true"
    assert vals[1] == "https://old.example"
    assert vals[2] == "legacy-client"
    assert "deprecated" in stderr


def test_unprefixed_name_wins_when_both_set():
    vals, _ = _probe({"USE_MOCK_AUTH": "false", "VITE_USE_MOCK_AUTH": "true"})
    assert vals[0] == "false"
