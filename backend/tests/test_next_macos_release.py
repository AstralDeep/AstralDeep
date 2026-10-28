"""Tests for scripts/next_macos_release.py: tag grammar, source version agreement, the version and build rule, and the CLI.
GitHub is replaced by a fake `gh`; commit ancestry uses a small real Git repository.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "next_macos_release.py"
SPEC = importlib.util.spec_from_file_location("next_macos_release", SCRIPT)
assert SPEC and SPEC.loader
nxt = importlib.util.module_from_spec(SPEC)
sys.modules["next_macos_release"] = nxt
SPEC.loader.exec_module(nxt)

HEAD = "a" * 40
OLD = "b" * 40


def configuration(ident: str, name: str, version: str | None) -> str:
    setting = f"\t\t\t\tMARKETING_VERSION = {version};\n" if version is not None else ""
    return (
        f"\t\t{ident} /* {name} */ = {{\n\t\t\tisa = XCBuildConfiguration;\n\t\t\tbuildSettings = {{\n"
        f"\t\t\t\tCURRENT_PROJECT_VERSION = 70;\n{setting}\t\t\t}};\n\t\t\tname = {name};\n\t\t}};\n"
    )


def project(*blocks: str) -> str:
    return "// !$*UTF8*$!\n{\n\tobjects = {\n" + "".join(blocks) + "\t};\n}\n"


def newest(version: str, build: int, target: str = OLD) -> dict:
    return {"tag": f"macos-v{version}-{build}", "version": version, "build": build, "target": target}


def test_parse_tag_accepts_only_the_macos_namespace():
    assert nxt.parse_tag("macos-v2.0.0-71") == ("2.0.0", 71)
    assert nxt.parse_tag("macos-v1.8-69") == ("1.8", 69)
    for tag in ("v0.6.0", "apple-v1.7", "macos-v2.0.0", "macos-v02.0-71", "macos-v2-71", "macos-v2.0.0-71-rc", "xmacos-v2.0-1"):
        assert nxt.parse_tag(tag) is None


def test_source_version_reads_the_direct_download_configurations_and_requires_agreement():
    text = project(
        configuration("AF01", "DirectDownload", None),
        configuration("AF02", "DirectDownload", "2.0.0"),
        configuration("AF03", "DirectDownload", '"2.0.0"'),
        configuration("AF04", "Release", "1.9"),
        configuration("AF05", "Debug", "0.1"),
    )
    assert nxt.source_version(text) == "2.0.0"
    for broken, message in (
        (project(configuration("AF02", "DirectDownload", "2.0.0"), configuration("AF03", "DirectDownload", "2.1")), "disagree"),
        (project(configuration("AF04", "Release", "2.0.0")), "no DirectDownload"),
        (project(configuration("AF01", "DirectDownload", None)), "no DirectDownload"),
        (project(configuration("AF02", "DirectDownload", "$(VERSION)")), "not a release version"),
    ):
        with pytest.raises(ValueError, match=message):
            nxt.source_version(broken)


def test_first_release_after_source_bump_is_major_minor_zero():
    result = nxt.decide("2.0.0", newest("1.8", 69), HEAD)
    assert result == {
        "skip": False, "reason": "release macos-v2.0.0-71", "version": "2.0.0", "build": 71,
        "previous_tag": "macos-v1.8-69", "previous_target": OLD,
    }
    assert nxt.decide("2.0", newest("1.8", 69), HEAD)["version"] == "2.0.0"
    assert nxt.decide("2.1.3", newest("2.0.4", 80), HEAD)["version"] == "2.1.3"


def test_patch_bumps_when_source_minor_is_not_newer():
    result = nxt.decide("2.0.0", newest("2.0.0", 71), HEAD)
    assert (result["version"], result["build"], result["skip"]) == ("2.0.1", 72, False)
    assert nxt.decide("2.0", newest("2.0", 75), HEAD)["version"] == "2.0.1"
    assert nxt.decide("1.9", newest("2.0.3", 79), HEAD)["version"] == "2.0.4"
    assert nxt.decide("2.0.9", newest("2.0.1", 72), HEAD)["version"] == "2.0.2"


def test_minor_bump_in_source_resets_patch():
    result = nxt.decide("2.1", newest("2.0.4", 80), HEAD)
    assert (result["version"], result["build"]) == ("2.1.0", 81)
    assert nxt.decide("3.0", newest("2.9.9", 99), HEAD)["version"] == "3.0.0"


def test_first_release_ever_starts_after_build_seventy():
    result = nxt.decide("2.0", None, HEAD)
    assert result == {
        "skip": False, "reason": "release macos-v2.0.0-71", "version": "2.0.0", "build": 71,
        "previous_tag": None, "previous_target": None,
    }
    assert nxt.decide("2.0.0", newest("1.0", 12), HEAD)["build"] == 71


def test_skips_when_head_is_already_released():
    result = nxt.decide("2.0.0", newest("2.0.0", 71, HEAD), HEAD)
    assert result["skip"] is True and result["reason"] == "head is already released as macos-v2.0.0-71"


def test_skips_when_head_is_older_than_the_newest_release():
    result = nxt.decide("2.0.0", newest("2.0.1", 72), HEAD, descends=False)
    assert result["skip"] is True
    assert result["reason"] == f"head is older than macos-v2.0.1-72 ({OLD}); nothing to release"


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


def test_descends_uses_real_commit_ancestry(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "one")
    first = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "two")
    second = git(tmp_path, "rev-parse", "HEAD")
    assert nxt.descends(tmp_path, first, second) is True
    assert nxt.descends(tmp_path, second, first) is False
    with pytest.raises(nxt.NextReleaseError, match="cannot compare"):
        nxt.descends(tmp_path, "c" * 40, second)


class FakeGitHub:
    def __init__(self, releases, refs, tags=None):
        self.releases = releases
        self.refs = refs
        self.tags = tags or {}
        self.calls: list[list[str]] = []
        self.failing: set[str] = set()
        self.ancestors: set[tuple[str, str]] | None = None

    def run(self, command, **kwargs):
        self.calls.append(command)
        assert kwargs.get("capture_output") and kwargs.get("text") and kwargs.get("check") is False
        if command[0] == "git":
            related = self.ancestors is None or (command[-2], command[-1]) in self.ancestors
            return subprocess.CompletedProcess(command, 0 if related else 1, "", "")
        path = command[-1]
        if path in self.failing:
            return subprocess.CompletedProcess(command, 1, "", "HTTP 404")
        if path.endswith("/releases?per_page=100"):
            body = self.releases
        elif "/git/ref/tags/" in path:
            body = self.refs[path.rsplit("/", 1)[1]]
        else:
            body = self.tags[path.rsplit("/", 1)[1]]
        return subprocess.CompletedProcess(command, 0, body if isinstance(body, str) else json.dumps(body), "")


def entry(tag: str, draft: bool = False) -> dict:
    return {"tag_name": tag, "draft": draft}


def test_newest_release_picks_the_highest_published_build_and_follows_annotated_tags(monkeypatch):
    fake = FakeGitHub(
        [[entry("macos-v1.8-69"), entry("v0.6.0"), entry("macos-v9.9.9-99", draft=True)], [entry("macos-v2.0.0-71")]],
        {"macos-v2.0.0-71": {"ref": "refs/tags/macos-v2.0.0-71", "object": {"type": "tag", "sha": "1" * 40}}},
        {"1" * 40: {"object": {"type": "tag", "sha": "2" * 40}}, "2" * 40: {"object": {"type": "commit", "sha": OLD}}},
    )
    monkeypatch.setattr(nxt.subprocess, "run", fake.run)
    assert nxt.newest_release("AstralDeep/AstralDeep") == newest("2.0.0", 71)
    assert fake.calls[0] == ["gh", "api", "--paginate", "--slurp", "repos/AstralDeep/AstralDeep/releases?per_page=100"]
    assert fake.calls[1] == ["gh", "api", "repos/AstralDeep/AstralDeep/git/ref/tags/macos-v2.0.0-71"]
    fake.releases = [[entry("v0.6.0")], []]
    assert nxt.newest_release("AstralDeep/AstralDeep") is None


@pytest.mark.parametrize(
    ("ref", "tags", "message"),
    [
        ({"ref": "refs/tags/macos-v2.0.0-71", "object": {"type": "tree", "sha": OLD}}, {}, "does not name a commit"),
        ({"ref": "refs/tags/other", "object": {"type": "commit", "sha": OLD}}, {}, "does not name a commit"),
        ({"ref": "refs/tags/macos-v2.0.0-71", "object": {"type": "commit", "sha": "short"}}, {}, "does not name a commit"),
        ({"ref": "refs/tags/macos-v2.0.0-71", "object": {"type": "tag", "sha": "1" * 40}},
         {"1" * 40: {"object": {"type": "tag", "sha": "1" * 40}}}, "does not name a commit"),
        ({"ref": "refs/tags/macos-v2.0.0-71"}, {}, "is unreadable"),
        ({"ref": "refs/tags/macos-v2.0.0-71", "object": {"type": "tag", "sha": "1" * 40}}, {"1" * 40: []}, "is unreadable"),
    ],
)
def test_newest_release_refuses_tags_that_do_not_resolve_to_a_commit(monkeypatch, ref, tags, message):
    fake = FakeGitHub([[entry("macos-v2.0.0-71")]], {"macos-v2.0.0-71": ref}, tags)
    monkeypatch.setattr(nxt.subprocess, "run", fake.run)
    with pytest.raises(nxt.NextReleaseError, match=message):
        nxt.newest_release("AstralDeep/AstralDeep")


@pytest.mark.parametrize("releases", ["not json", {"message": "Bad credentials"}, [[{"tag_name": "macos-v2.0.0-71"}]]])
def test_newest_release_refuses_an_unreadable_listing(monkeypatch, releases):
    fake = FakeGitHub(releases, {})
    monkeypatch.setattr(nxt.subprocess, "run", fake.run)
    with pytest.raises(nxt.NextReleaseError, match="unreadable"):
        nxt.newest_release("AstralDeep/AstralDeep")


def test_gh_failures_are_reported(monkeypatch):
    fake = FakeGitHub([[]], {})
    fake.failing.add("repos/AstralDeep/AstralDeep/releases?per_page=100")
    monkeypatch.setattr(nxt.subprocess, "run", fake.run)
    with pytest.raises(nxt.NextReleaseError, match="gh api .* failed: HTTP 404"):
        nxt.newest_release("AstralDeep/AstralDeep")


def write_project(tmp_path: Path, version: str) -> Path:
    path = tmp_path / "project.pbxproj"
    path.write_text(project(configuration("AF02", "DirectDownload", version)))
    return path


def test_cli_writes_github_output(tmp_path, monkeypatch, capsys):
    fake = FakeGitHub(
        [[entry("macos-v2.0.0-71")]],
        {"macos-v2.0.0-71": {"ref": "refs/tags/macos-v2.0.0-71", "object": {"type": "commit", "sha": OLD}}},
    )
    monkeypatch.setattr(nxt.subprocess, "run", fake.run)
    output = tmp_path / "github-output"
    output.write_text("earlier=kept\n")
    code = nxt.main([
        "--repo", "AstralDeep/AstralDeep", "--head", HEAD, "--pbxproj", str(write_project(tmp_path, "2.0.0")),
        "--checkout", str(tmp_path), "--github-output", str(output),
    ])
    assert code == 0
    assert output.read_text() == (
        "earlier=kept\nversion=2.0.1\nbuild=72\nskip=false\nprevious_tag=macos-v2.0.0-71\n"
        f"previous_target={OLD}\n"
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["version"] == "2.0.1" and printed["reason"] == "release macos-v2.0.1-72"
    assert ["git", "-C", str(tmp_path), "merge-base", "--is-ancestor", OLD, HEAD] in fake.calls


def test_cli_skips_a_released_head_and_handles_an_empty_history(tmp_path, monkeypatch, capsys):
    fake = FakeGitHub(
        [[entry("macos-v2.0.0-71")]],
        {"macos-v2.0.0-71": {"ref": "refs/tags/macos-v2.0.0-71", "object": {"type": "commit", "sha": HEAD}}},
    )
    monkeypatch.setattr(nxt.subprocess, "run", fake.run)
    output = tmp_path / "out"
    argv = ["--repo", "AstralDeep/AstralDeep", "--head", HEAD, "--pbxproj", str(write_project(tmp_path, "2.0.0")),
            "--github-output", str(output)]
    assert nxt.main(argv) == 0
    assert "skip=true\n" in output.read_text()
    assert not any(command[0] == "git" for command in fake.calls)
    fake.releases = [[]]
    output.unlink()
    assert nxt.main(argv) == 0
    assert output.read_text() == "version=2.0.0\nbuild=71\nskip=false\nprevious_tag=\nprevious_target=\n"
    capsys.readouterr()
    assert nxt.main(argv[:-2]) == 0
    assert json.loads(capsys.readouterr().out)["build"] == 71


def released_at_old(monkeypatch, ancestors):
    fake = FakeGitHub(
        [[entry("macos-v2.0.1-72")]],
        {"macos-v2.0.1-72": {"ref": "refs/tags/macos-v2.0.1-72", "object": {"type": "commit", "sha": OLD}}},
    )
    fake.ancestors = ancestors
    monkeypatch.setattr(nxt.subprocess, "run", fake.run)
    return fake


def test_cli_skips_with_a_notice_when_head_is_older_than_the_newest_release(tmp_path, monkeypatch, capsys):
    fake = released_at_old(monkeypatch, {(HEAD, OLD)})
    output = tmp_path / "out"
    argv = ["--repo", "AstralDeep/AstralDeep", "--head", HEAD, "--pbxproj", str(write_project(tmp_path, "2.0.0")),
            "--github-output", str(output)]
    assert nxt.main(argv) == 0
    assert "skip=true\n" in output.read_text()
    printed = capsys.readouterr().out.splitlines()
    assert printed[0] == f"::notice::head is older than macos-v2.0.1-72 ({OLD}); nothing to release"
    assert json.loads(printed[1])["skip"] is True
    assert [c[-2:] for c in fake.calls if c[0] == "git"] == [[OLD, HEAD], [HEAD, OLD]]


def test_cli_fails_when_head_and_the_newest_release_have_diverged(tmp_path, monkeypatch, capsys):
    released_at_old(monkeypatch, set())
    output = tmp_path / "out"
    argv = ["--repo", "AstralDeep/AstralDeep", "--head", HEAD, "--pbxproj", str(write_project(tmp_path, "2.0.0")),
            "--github-output", str(output)]
    assert nxt.main(argv) == 1
    assert f"head and macos-v2.0.1-72 ({OLD}) have diverged" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--head", "abc"], "full lowercase commit"),
        (["--pbxproj", "missing.pbxproj"], "cannot read"),
    ],
)
def test_cli_reports_bad_input(tmp_path, monkeypatch, capsys, argv, message):
    fake = FakeGitHub([[]], {})
    monkeypatch.setattr(nxt.subprocess, "run", fake.run)
    base = {"--repo": "AstralDeep/AstralDeep", "--head": HEAD, "--pbxproj": str(write_project(tmp_path, "2.0.0"))}
    base.update(dict(zip(argv[::2], argv[1::2])))
    if base["--pbxproj"] == "missing.pbxproj":
        base["--pbxproj"] = str(tmp_path / "missing.pbxproj")
    assert nxt.main([part for pair in base.items() for part in pair]) == 1
    assert message in capsys.readouterr().err


def test_cli_reports_a_disagreeing_source_and_github_errors(tmp_path, monkeypatch, capsys):
    fake = FakeGitHub([[]], {})
    fake.failing.add("repos/AstralDeep/AstralDeep/releases?per_page=100")
    monkeypatch.setattr(nxt.subprocess, "run", fake.run)
    argv = ["--repo", "AstralDeep/AstralDeep", "--head", HEAD, "--pbxproj", str(write_project(tmp_path, "2.0.0"))]
    assert nxt.main(argv) == 1
    assert "Next Mac release failed: gh api" in capsys.readouterr().err
    path = tmp_path / "bad.pbxproj"
    path.write_text(project(configuration("AF04", "Release", "2.0.0")))
    assert nxt.main(["--repo", "AstralDeep/AstralDeep", "--head", HEAD, "--pbxproj", str(path)]) == 1
    assert "no DirectDownload" in capsys.readouterr().err
