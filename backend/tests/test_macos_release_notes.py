"""Tests for scripts/macos_release_notes.py: entries from the first-parent history of a compare, rendering bounds and the CLI.
GitHub is replaced by a fake `gh` that returns compare pages in the provider's shape.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "macos_release_notes.py"
SPEC = importlib.util.spec_from_file_location("macos_release_notes", SCRIPT)
assert SPEC and SPEC.loader
notes = importlib.util.module_from_spec(SPEC)
sys.modules["macos_release_notes"] = notes
SPEC.loader.exec_module(notes)

BASE = "0" * 40
HEAD = "f" * 40


def commit(sha: str, message: str, *parents: str) -> dict:
    return {"sha": sha, "parents": [{"sha": parent} for parent in parents], "commit": {"message": message}}


def history() -> list[dict]:
    return [
        commit("1" * 40, "spec: record a plan step", BASE),
        commit("2" * 40, "Grant only argv commands", "1" * 40),
        commit("3" * 40, "Merge pull request #469 from AstralDeep/claude/102-parity\n\nmacOS harness parity: approvals\n\nBody", BASE, "2" * 40),
        commit("4" * 40, "Merge branch 'main' into feature", "3" * 40, "1" * 40),
        commit("5" * 40, "Fix the update chip (#470)", "4" * 40),
        commit("6" * 40, "Merge pull request #471 from AstralDeep/codex/x\n\n", "5" * 40, "2" * 40),
        commit(HEAD, "Merge pull request #472 from AstralDeep/codex/y\n\nShip the <b>DMG</b> & notes", "6" * 40, "5" * 40),
    ]


def test_merge_commits_become_titled_entries():
    assert notes.entries_from_compare(history(), HEAD) == [
        (469, "macOS harness parity: approvals"),
        (None, "Fix the update chip (#470)"),
        (471, "Merge pull request #471 from AstralDeep/codex/x"),
        (472, "Ship the <b>DMG</b> & notes"),
    ]


def test_plain_commits_keep_their_subject():
    commits = [commit("1" * 40, "Hotfix the feed URL\n\nDetails", BASE), commit(HEAD, "Merge branch 'release'", "1" * 40)]
    assert notes.entries_from_compare(commits, HEAD) == [(None, "Hotfix the feed URL")]
    assert notes.entries_from_compare(commits[:1], None) == [(None, "Hotfix the feed URL")]
    assert notes.entries_from_compare([], None) == []


def test_rendered_notes_escape_markup_and_drop_duplicates():
    text = notes.render_notes("2.0.1", 72, [
        (472, "Ship the <b>DMG</b> & notes"), (None, "  Fix   the\nchip "), (472, "Ship the <b>DMG</b> & notes"),
    ])
    assert text == "## AstralDeep 2.0.1 (72)\n\n- Ship the &lt;b&gt;DMG&lt;/b&gt; &amp; notes (#472)\n- Fix the chip\n"


def test_empty_range_is_a_maintenance_release():
    assert notes.render_notes("2.0.1", 72, []) == "## AstralDeep 2.0.1 (72)\n\n- Maintenance release.\n"
    assert notes.render_notes("2.0.1", 72, [(None, "   ")]) == "## AstralDeep 2.0.1 (72)\n\n- Maintenance release.\n"


def test_notes_are_bounded_to_64_kib():
    entries = [(number, "x" * 200) for number in range(1, 1000)]
    text = notes.render_notes("2.0.1", 72, entries)
    assert len(text.encode()) <= 64 * 1024
    assert text.endswith("- x" + "x" * 199 + f" (#{text.count(chr(10)) - 3})\n- …and more.\n")
    assert len(text.encode()) > 64 * 1024 - 300


class FakeGitHub:
    def __init__(self, pages):
        self.pages = pages
        self.calls: list[list[str]] = []
        self.returncode = 0

    def run(self, command, **kwargs):
        self.calls.append(command)
        assert kwargs.get("capture_output") and kwargs.get("text") and kwargs.get("check") is False
        body = self.pages if isinstance(self.pages, str) else json.dumps(self.pages)
        return subprocess.CompletedProcess(command, self.returncode, body, "HTTP 404: Not Found")


def argv(output: Path, base: str = BASE) -> list[str]:
    return ["--repo", "AstralDeep/AstralDeep", "--base", base, "--head", HEAD, "--version", "2.0.1", "--build", "72",
            "--output", str(output)]


def test_cli_writes_markdown(tmp_path, monkeypatch):
    commits = history()
    fake = FakeGitHub([{"commits": commits[:4]}, {"commits": commits[4:]}])
    monkeypatch.setattr(notes.subprocess, "run", fake.run)
    output = tmp_path / "notes.md"
    assert notes.main(argv(output)) == 0
    assert fake.calls == [[
        "gh", "api", "--paginate", "--slurp", f"repos/AstralDeep/AstralDeep/compare/{BASE}...{HEAD}?per_page=100",
    ]]
    assert output.read_text(encoding="utf-8") == (
        "## AstralDeep 2.0.1 (72)\n\n- macOS harness parity: approvals (#469)\n- Fix the update chip (#470)\n"
        "- Merge pull request #471 from AstralDeep/codex/x (#471)\n- Ship the &lt;b&gt;DMG&lt;/b&gt; &amp; notes (#472)\n"
    )


def test_cli_without_a_base_writes_a_maintenance_release(tmp_path, monkeypatch):
    fake = FakeGitHub([])
    monkeypatch.setattr(notes.subprocess, "run", fake.run)
    output = tmp_path / "notes.md"
    assert notes.main(argv(output, base="")) == 0
    assert output.read_text() == "## AstralDeep 2.0.1 (72)\n\n- Maintenance release.\n"
    assert fake.calls == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda fake, args: setattr(fake, "returncode", 1), "gh api .* failed: HTTP 404"),
        (lambda fake, args: setattr(fake, "pages", "not json"), "unreadable"),
        (lambda fake, args: setattr(fake, "pages", [{"commits": [{"sha": HEAD}]}]), "unreadable"),
        (lambda fake, args: setattr(fake, "pages", {"message": "Not Found"}), "unreadable"),
        (lambda fake, args: args.__setitem__(3, "short"), "full lowercase commit"),
        (lambda fake, args: args.__setitem__(5, "HEAD"), "full lowercase commit"),
        (lambda fake, args: args.__setitem__(7, "2"), "version"),
        (lambda fake, args: args.__setitem__(9, "0"), "build"),
    ],
)
def test_cli_reports_failures(tmp_path, monkeypatch, capsys, change, message):
    fake = FakeGitHub([{"commits": history()}])
    monkeypatch.setattr(notes.subprocess, "run", fake.run)
    output = tmp_path / "notes.md"
    args = argv(output)
    change(fake, args)
    assert notes.main(args) == 1
    assert not output.exists()
    assert "Mac release notes failed" in capsys.readouterr().err
    with pytest.raises(notes.NotesError, match=message):
        notes.run(notes.build_parser().parse_args(args))
