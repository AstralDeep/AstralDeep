"""Fails when mock-auth names reappear anywhere in the tracked tree outside specs/.
The only files allowed to carry them are the production posture refusal, the doctor and qualification checks that mirror it, and the tests that prove the refusal."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = ("USE_MOCK_AUTH", "dev-token", "dev-mcp-token")
SKIPPED_PREFIXES = ("specs/",)

ALLOWED: dict[str, tuple[tuple[str, ...], str]] = {
    "backend/orchestrator/session_store.py": (("USE_MOCK_AUTH",), "the production posture refusal and development warning"),
    "scripts/install_doctor.py": (("USE_MOCK_AUTH",), "mirrors the posture refusal for operators"),
    "scripts/run_backend_web_qualification.py": (("USE_MOCK_AUTH",), "qualification refuses a leftover setting"),
    "backend/tests/test_no_mock_auth.py": (FORBIDDEN, "the guard itself"),
    "backend/tests/conftest.py": (("USE_MOCK_AUTH",), "scrubs the leftover variable from the ambient environment"),
    "backend/tests/helpers/server_identity.py": (("USE_MOCK_AUTH",), "scrubs the leftover variable from the ambient environment"),
    "backend/tests/test_env_compat.py": (("USE_MOCK_AUTH",), "proves the legacy VITE name is not backfilled"),
    "backend/tests/test_fail_closed_boot.py": (("USE_MOCK_AUTH",), "proves the boot refusal and development warning"),
    "backend/tests/test_install_doctor.py": (("USE_MOCK_AUTH",), "proves the doctor agrees with the boot refusal"),
    "backend/tests/test_node_posture.py": (("USE_MOCK_AUTH",), "proves a personal node refuses the leftover setting"),
    "backend/tests/test_node_bearer_refusal.py": (FORBIDDEN, "proves the removed flag and literal tokens change nothing"),
    "backend/tests/test_docker_free_gates.py": (("USE_MOCK_AUTH",), "proves a production boot with the flag set false still refuses a placeholder secret"),
    "backend/tests/test_node_delegation.py": (("USE_MOCK_AUTH", "dev-token"), "proves the removed flag and literal token are refused"),
}


def violations(root: Path, files: list[str]) -> list[str]:
    found = []
    for name in files:
        if name.startswith(SKIPPED_PREFIXES):
            continue
        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        text = path.read_bytes().decode("utf-8", errors="ignore")
        allowed = ALLOWED.get(name, ((), ""))[0]
        found.extend(f"{name}: {token}" for token in FORBIDDEN if token in text and token not in allowed)
    return found


def tracked_files() -> list[str]:
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=REPOSITORY_ROOT, capture_output=True, check=True, timeout=60
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout")
    return [name for name in listing.decode("utf-8").split("\0") if name]


def test_the_tree_carries_no_mock_auth_outside_the_allowed_files():
    assert violations(REPOSITORY_ROOT, tracked_files()) == []


def test_every_allowed_file_still_needs_its_exception():
    stale = []
    for name, (tokens, _reason) in ALLOWED.items():
        path = REPOSITORY_ROOT / name
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        stale.extend(f"{name}: {token}" for token in tokens if token not in text)
    assert stale == []


@pytest.mark.parametrize("token", FORBIDDEN)
def test_a_planted_file_fails_the_guard(tmp_path, token):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "planted.py").write_text(f"value = '{token}'\n", encoding="utf-8")
    assert violations(tmp_path, ["scripts/planted.py"]) == [f"scripts/planted.py: {token}"]


def test_specs_are_exempt_and_binary_files_are_tolerated(tmp_path):
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "note.md").write_text("USE_MOCK_AUTH", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00dev-token")
    assert violations(tmp_path, ["specs/note.md"]) == []
    assert violations(tmp_path, ["blob.bin"]) == ["blob.bin: dev-token"]


def test_an_allowed_file_may_not_use_other_tokens(tmp_path):
    (tmp_path / "backend" / "orchestrator").mkdir(parents=True)
    (tmp_path / "backend/orchestrator/session_store.py").write_text("dev-mcp-token USE_MOCK_AUTH", encoding="utf-8")
    assert violations(tmp_path, ["backend/orchestrator/session_store.py"]) == [
        "backend/orchestrator/session_store.py: dev-mcp-token"
    ]
