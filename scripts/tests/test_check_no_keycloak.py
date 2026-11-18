"""Exercises the no-Keycloak search check against throwaway git repositories and the live Deep tree.
It pins the pattern, exclusion, pending-list and output rules of scripts/check_no_keycloak.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import check_no_keycloak as check  # noqa: E402

EXCLUDE = ["specs/**", "**/tests/**", "**/*Tests/**", "**/src/test/**"]


def _repo(tmp_path: Path, files: dict, track: bool = True) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for rel, body in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body if isinstance(body, bytes) else body.encode())
    if track:
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def _run(repo: Path, pending=None):
    return check.check(repo, {"exclude": EXCLUDE, "pending": pending or []})


def test_planted_hit_under_backend_fails(tmp_path):
    repo = _repo(tmp_path, {"backend/a.py": "x = 'keycloak'\n"})
    assert any(line.startswith("backend/a.py:1:") for line in _run(repo))


def test_clean_contents_with_keycloak_filename_fails(tmp_path):
    repo = _repo(tmp_path, {"docs/keycloak-notes.md": "nothing here\n"})
    assert _run(repo) == ["docs/keycloak-notes.md: path names an identity server"]


@pytest.mark.parametrize("body", ["KeyCloak", "https://iam.ai.uky.edu/x", "/protocol/openid-connect/certs"])
def test_each_pattern_fails(tmp_path, body):
    repo = _repo(tmp_path, {"backend/a.py": body + "\n"})
    assert _run(repo)


@pytest.mark.parametrize(
    "rel",
    ["backend/tests/t.py", "App/AstralCoreTests/T.swift", "x/src/test/K.kt", "specs/001/spec.md"],
)
def test_excluded_trees_pass(tmp_path, rel):
    repo = _repo(tmp_path, {rel: "keycloak\n"})
    assert _run(repo) == []


def test_untracked_file_ignored(tmp_path):
    repo = _repo(tmp_path, {"backend/a.py": "keycloak\n"}, track=False)
    assert _run(repo) == []


def test_binary_file_skipped(tmp_path):
    repo = _repo(tmp_path, {"backend/a.bin": b"\0keycloak\0"})
    assert _run(repo) == []


def test_pending_entry_allows_hit(tmp_path):
    repo = _repo(tmp_path, {"backend/a.py": "keycloak\n"})
    assert _run(repo, [{"path": "backend/a.py", "until": "T5"}]) == []


def test_stale_pending_entry_fails(tmp_path):
    repo = _repo(tmp_path, {"backend/a.py": "clean\n"})
    assert _run(repo, [{"path": "backend/a.py", "until": "T5"}]) == [
        "backend/a.py: pending entry has no hit and is stale"
    ]


def test_bound_by_must_name_the_file(tmp_path):
    repo = _repo(tmp_path, {"scripts/x.py": "keycloak\n", "wf.yml": "run: other.py\n"})
    entry = {"path": "scripts/x.py", "until": "T8.6", "bound_by": "wf.yml"}
    assert "does not name x.py" in _run(repo, [entry])[0]
    (repo / "wf.yml").write_text("run: scripts/x.py\n")
    assert _run(repo, [entry]) == []


def test_t86_entry_needs_bound_by_and_readable_holder(tmp_path):
    repo = _repo(tmp_path, {"scripts/x.py": "keycloak\n"})
    assert "no bound_by" in _run(repo, [{"path": "scripts/x.py", "until": "T8.6"}])[0]
    entry = {"path": "scripts/x.py", "until": "T8.6", "bound_by": "missing.yml"}
    assert "cannot be read" in _run(repo, [entry])[0]


def test_main_exit_codes_and_json(tmp_path, capsys):
    repo = _repo(tmp_path / "r", {"backend/a.py": "keycloak\n"})
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"exclude": [], "pending": []}))
    assert check.main(["--repo", str(repo), "--config", str(cfg), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    cfg.write_text(json.dumps({"exclude": ["backend/**"], "pending": []}))
    assert check.main(["--repo", str(repo), "--config", str(cfg)]) == 0


def test_base_passes_with_populated_list():
    config = json.loads((ROOT / "scripts" / "no_keycloak.json").read_text())
    assert check.check(ROOT, config) == []


def test_bound_by_accepts_module_name_without_suffix(tmp_path):
    repo = _repo(tmp_path, {"scripts/x.py": "keycloak\n", "p.py": "module('x')\n"})
    assert _run(repo, [{"path": "scripts/x.py", "until": "T8.6", "bound_by": "p.py"}]) == []
