"""Tests for scripts/release_macos.py: step ordering, failure stops, publish gating and staged artifacts.
Every external command is replaced by a recording fake that creates the files the real tool would.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import importlib.util
import json
import plistlib
import re
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release_macos.py"
SPEC = importlib.util.spec_from_file_location("release_macos", SCRIPT)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
sys.modules["release_macos"] = release
SPEC.loader.exec_module(release)

PROJECTION = "b" * 40
DEEP = "a" * 40
SIGNATURE = "S" * 86 + "=="
NOW = dt.datetime(2026, 10, 26, 12, 0, 0, tzinfo=dt.timezone.utc)
IDENTITY = {
    "version": "1.9", "build": 70, "minimumMacOS": "14.0", "architecture": "arm64",
    "bundleID": "com.personalailabs.astraldeep", "executableSHA256": "e" * 64, "teamID": "593LGH35GS",
}


def item(build: int, version: str, channel: str | None = None, length: int = 111, signature: str = "O" * 86 + "==") -> str:
    tag = f"macos-v{version}-{build}"
    url = f"https://github.com/AstralDeep/AstralDeep/releases/download/{tag}/AstralDeep-{version}-{build}-arm64.zip"
    extra = f"<sparkle:channel>{channel}</sparkle:channel>" if channel else ""
    return (
        f"<item><title>{version}</title><sparkle:version>{build}</sparkle:version>"
        f"<sparkle:shortVersionString>{version}</sparkle:shortVersionString>{extra}"
        f'<enclosure url="{url}" length="{length}" type="application/octet-stream" sparkle:edSignature="{signature}"/></item>'
    )


def feed(*items: str) -> str:
    return (
        '<?xml version="1.0" standalone="yes"?><rss version="2.0" '
        f'xmlns:sparkle="{release.SPARKLE_NAMESPACE}"><channel><title>AstralDeep</title>{"".join(items)}</channel></rss>'
    )


def write_app(app, info):
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))


class FakeRunner(release.Runner):
    """Simulates the toolchain: creates artifacts, records history and fails commands on request."""

    def __init__(self, env, **kwargs):
        super().__init__(out=env.lines.append, **kwargs)
        self.env = env
        self.fail: list = []
        self.curl: list[str] = []
        self.dirty: set[str] = set()
        self.release_exists = False
        self.immutable = True
        self.altered_digest = False
        self.uploads: list[Path] = []
        self.notary = {"status": "Accepted", "id": "sub-1"}
        self.stapler_output = "The validate action worked!"
        self.spctl_output = "accepted\nsource=Notarized Developer ID"
        self.codesign_output = "TeamIdentifier=593LGH35GS"
        self.signing_output = None
        self.policy_signature = SIGNATURE
        self.generate_item = None
        self.extra_stage_file = False
        self.pin = PROJECTION

    def _execute(self, command, cwd):
        for matcher in self.fail:
            if matcher(command):
                return release.Result(1, "", "intentional failure")
        name = command[0]
        handler = getattr(self, "_" + Path(name).name, None)
        if handler is None:
            return release.Result(0)
        return handler(command, cwd) or release.Result(0)

    def _git(self, command, cwd):
        repo = command[2]
        sub = command[3]
        if sub == "status":
            return release.Result(0, " M file\n" if Path(repo).name in self.dirty else "")
        if sub == "rev-parse":
            return release.Result(0, (PROJECTION if Path(repo) == self.env.projection else self.env.deep_sha) + "\n")
        if sub == "ls-tree":
            return release.Result(0, f"160000 commit {self.pin}\tcomponents/AstralProjection\n")
        return release.Result(0)

    def _xcodebuild(self, command, cwd):
        if "archive" in command:
            Path(command[command.index("-archivePath") + 1]).mkdir(parents=True)
        else:
            write_app(Path(command[command.index("-exportPath") + 1]) / "AstralDeep.app", self.env.info)

    def _ditto(self, command, cwd):
        if command[1] == "-c":
            Path(command[-1]).write_bytes(b"zip-bytes-" + command[-1].encode())
        else:
            (Path(command[-1]) / "AstralDeep.app").mkdir(parents=True)

    def _xcrun(self, command, cwd):
        if command[1] == "notarytool":
            return release.Result(0, json.dumps(self.notary))
        if command[2] == "validate":
            return release.Result(0, self.stapler_output)

    def _codesign(self, command, cwd):
        return release.Result(0, "", self.codesign_output)

    def _spctl(self, command, cwd):
        return release.Result(0, "", self.spctl_output)

    def _sign_update(self, command, cwd):
        if "--verify" in command:
            return None
        if "-p" in command:
            return release.Result(0, self.policy_signature + "\n")
        size = Path(command[-1]).stat().st_size
        if self.signing_output is not None:
            return release.Result(0, self.signing_output)
        return release.Result(0, f'sparkle:edSignature="{SIGNATURE}" length="{size}"\n')

    def _generate_appcast(self, command, cwd):
        out = Path(command[command.index("-o") + 1])
        staging = Path(command[-1])
        archive = next(staging.glob("*.zip"))
        build, version = self.env.build, self.env.version
        channel = "preview" if "--channel" in command else None
        kept = re.findall(r"<item>.*?</item>", out.read_text(), re.S) if out.exists() else []
        fresh = self.generate_item or item(build, version, channel, archive.stat().st_size, SIGNATURE)
        out.write_text(feed(fresh, *kept))
        if self.extra_stage_file:
            (staging / "old_updates").mkdir()

    def _gh(self, command, cwd):
        if command[1:3] == ["release", "view"]:
            return release.Result(0 if self.release_exists else 1, "", "release not found")
        if command[1:3] == ["release", "create"]:
            self.uploads = [Path(part) for part in command if part.endswith((".zip", "SHA256SUMS", "release-provenance.json"))]
            return None
        if command[1] == "api" and "/releases/tags/" in command[2]:
            assets = [
                {"name": path.name, "digest": "sha256:" + ("0" * 64 if self.altered_digest else hashlib.sha256(path.read_bytes()).hexdigest())}
                for path in self.uploads
            ]
            return release.Result(0, json.dumps({
                "immutable": self.immutable, "draft": False, "prerelease": "--prerelease" in self.last_create(), "assets": assets,
            }))
        if command[1] == "api":
            return release.Result(0, self.env.deep_sha)

    def last_create(self):
        return next(c for c in reversed(self.history) if c[:3] == ["gh", "release", "create"])

    def _curl(self, command, cwd):
        return release.Result(0, self.curl.pop(0) if self.curl else "206")


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.lines: list[str] = []
        self.deep = tmp_path / "deep"
        self.projection = self.deep / "components" / "AstralProjection"
        self.website = tmp_path / "astraldeep.github.io"
        self.sparkle = tmp_path / "bin"
        self.version, self.build = "1.9", 70
        self.deep_sha = DEEP
        for path in (self.projection, self.website / "data", self.website / "site" / "data", self.sparkle):
            path.mkdir(parents=True)
        for tool in ("sign_update", "generate_appcast"):
            (self.sparkle / tool).write_text("#!/bin/sh\n")
        self.write_downloads(69, "1.8")
        self.notes = tmp_path / "notes.md"
        self.notes.write_text("## 1.9\n\n- Updates inside the app.\n")
        monkeypatch.setattr(release.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(release.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(release.packager, "inspect_app", lambda app, team: dict(IDENTITY))
        self.info = {
            "SUPublicEDKey": release.SPARKLE_PUBLIC_KEY, "SUFeedURL": release.FEED_URL,
            "SUVerifyUpdateBeforeExtraction": True,
        }

    def write_downloads(self, build, version):
        record = {"schemaVersion": 1, "macos": {"version": version, "build": build}}
        (self.website / "data" / "downloads.json").write_text(json.dumps(record))

    def args(self, *extra):
        stage = self.tmp / "out" / "stage"
        return release.build_parser().parse_args([
            "--projection-commit", PROJECTION, "--version", self.version, "--build", str(self.build),
            "--notes", str(self.notes), "--staging-dir", str(stage), "--work-dir", str(self.tmp / "out" / "work"),
            "--website", str(self.website), "--projection", str(self.projection), "--sparkle-bin", str(self.sparkle), *extra,
        ])

    def release(self, *extra, runner=None, dry=False, **kwargs):
        args = self.args(*extra)
        runner = runner or FakeRunner(self, dry_run=dry, publish=args.publish)
        runner.dry_run = dry
        sleeps = []
        subject = release.Release(args, runner, deep=self.deep, clock=lambda: NOW, sleep=sleeps.append, **kwargs)
        subject.sleeps = sleeps
        return subject, runner

    @property
    def stage(self):
        return self.tmp / "out" / "stage"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def label(command):
    name = Path(command[0]).name
    if name == "git":
        return "git " + command[3]
    if name in {"xcrun", "gh"}:
        return f"{name} {command[1]}"
    return name


def tools(runner):
    return [label(command) for command in runner.history]


def test_build_without_publish_stages_only_the_newest_archive_and_writes_every_artifact(env):
    subject, runner = env.release()
    assert subject.execute() == env.stage
    names = sorted(path.name for path in env.stage.iterdir())
    assert names == sorted([
        "AstralDeep-1.9-70-arm64.zip", "AstralDeep-1.9-70-arm64.md", "appcast.xml", "SHA256SUMS", "release-provenance.json",
        "downloads.json", "mac-update-policy.json", "mac-update-policy.json.sig", release.STATE_FILE,
    ])
    archive = env.stage / "AstralDeep-1.9-70-arm64.zip"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert (env.stage / "SHA256SUMS").read_text() == f"{digest}  AstralDeep-1.9-70-arm64.zip\n"
    record = json.loads((env.stage / "downloads.json").read_text())["macos"]
    assert list(record) == [
        "version", "build", "minimumMacOS", "architecture", "format", "bytes", "sha256", "url", "releaseURL",
        "checksumsURL", "source", "channel",
    ]
    assert record["sha256"] == digest and record["bytes"] == archive.stat().st_size and record["channel"] == "stable"
    assert record["url"] == "https://github.com/AstralDeep/AstralDeep/releases/download/macos-v1.9-70/AstralDeep-1.9-70-arm64.zip"
    assert record["source"] == {"deep": DEEP, "projection": PROJECTION}
    provenance = json.loads((env.stage / "release-provenance.json").read_text())
    assert provenance["tag"] == "macos-v1.9-70" and provenance["archive"]["sha256"] == digest
    assert provenance["apple"]["notarizationSubmission"] == "sub-1"
    assert provenance["apple"]["gatekeeperAssessment"] == "accepted: Notarized Developer ID"
    assert provenance["appExecutableSha256"] == "e" * 64 and provenance["distribution"] == "DirectDownload"
    assert set(provenance) >= {
        "schemaVersion", "product", "version", "build", "bundleIdentifier", "minimumMacOS", "architecture", "distribution",
        "channel", "tag", "source", "archive", "appExecutableSha256", "apple", "verification", "publication", "limitations",
    }
    policy = json.loads((env.stage / "mac-update-policy.json").read_text())
    assert policy == {
        "schemaVersion": 1, "minimumBuild": 70, "serial": 1, "expires": "2027-04-24T12:00:00Z", "message": "Update required",
    }
    assert (env.stage / "mac-update-policy.json.sig").read_text() == SIGNATURE + "\n"
    assert len(base64.b64decode(SIGNATURE)) == 64
    assert not any(c[0] in {"gh", "npm", "curl"} or c[:4] == ["git", "-C", str(env.website), "push"] for c in runner.history)
    assert any("nothing was published" in line for line in env.lines)


def test_steps_run_in_the_documented_order(env):
    subject, runner = env.release()
    subject.execute()
    names = tools(runner)
    expected = [
        "git status", "git status", "git rev-parse", "git rev-parse", "git ls-tree", "xcodebuild", "xcodebuild", "codesign",
        "codesign", "ditto", "xcrun notarytool", "xcrun stapler", "xcrun stapler", "spctl", "ditto", "ditto", "spctl",
        "xcrun stapler", "sign_update", "generate_appcast", "sign_update", "sign_update",
    ]
    assert names == expected


def test_command_lines_use_the_documented_flags(env):
    subject, runner = env.release("--channel", "preview", "--ed-key-file", str(env.tmp / "key"), "--minimum-build", "70")
    subject.execute()
    by = {}
    for command in runner.history:
        by.setdefault(label(command), []).append(command)
    archive = by["xcodebuild"][0]
    assert archive[1:3] == ["archive", "-project"] and "AstralAppDirectDownload" in archive
    assert "ASTRAL_DEVELOPMENT_TEAM=593LGH35GS" in archive and "-configuration" in archive and "DirectDownload" in archive
    assert any(arg.startswith("CODE_SIGN_IDENTITY=Developer ID Application") for arg in archive)
    assert "ARCHS=arm64" in archive
    export = by["xcodebuild"][1]
    assert export[1] == "-exportArchive"
    assert plistlib.loads((env.tmp / "out" / "work" / "export-options.plist").read_bytes()) == {
        "method": "developer-id", "teamID": "593LGH35GS", "signingStyle": "manual",
        "signingCertificate": "Developer ID Application", "destination": "export",
    }
    assert by["ditto"][0][:6] == ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(env.tmp / "out/work/export/AstralDeep.app")]
    assert by["xcrun notarytool"][0][3:] == [
        str(env.tmp / "out/work/notarize/AstralDeep-1.9-70-arm64.zip"), "--keychain-profile", "astral-notary", "--wait",
        "--output-format", "json",
    ]
    assert by["spctl"][0][1:4] == ["--assess", "--type", "execute"]
    generate = next(c for c in runner.history if c[0].endswith("generate_appcast"))
    base = "https://github.com/AstralDeep/AstralDeep/releases/download/macos-v1.9-70/"
    assert generate[1:3] == ["--ed-key-file", str(env.tmp / "key")]
    for flag, value in (
        ("--download-url-prefix", base), ("--link", "https://astraldeep.github.io"), ("--maximum-versions", "5"),
        ("--maximum-deltas", "0"), ("--channel", "preview"), ("-o", str(env.stage / "appcast.xml")),
    ):
        assert generate[generate.index(flag) + 1] == value
    assert "--embed-release-notes" in generate and generate[-1] == str(env.stage)
    sign = next(c for c in runner.history if c[0].endswith("sign_update"))
    assert sign[1:3] == ["--ed-key-file", str(env.tmp / "key")]
    assert any(c[0].endswith("sign_update") and "-p" in c and c[-1].endswith("mac-update-policy.json") for c in runner.history)
    assert any(c[0].endswith("sign_update") and "--verify" in c for c in runner.history)
    state = json.loads((env.stage / "release-state.json").read_text())
    assert state["channel"] == "preview"
    assert parse_channel(env.stage / "appcast.xml") == "preview"


def parse_channel(path):
    return release.parse_appcast(path)[0]["channel"]


def test_account_signing_is_the_default(env):
    subject, runner = env.release()
    subject.execute()
    generate = next(c for c in runner.history if c[0].endswith("generate_appcast"))
    assert generate[1:3] == ["--account", "ed25519"]


def test_live_appcast_is_copied_so_older_items_survive(env):
    (env.website / "site" / "appcast.xml").write_text(feed(item(69, "1.8", "preview")))
    subject, runner = env.release()
    subject.execute()
    items = release.parse_appcast(env.stage / "appcast.xml")
    assert [entry["build"] for entry in items] == [70, 69]
    assert items[1]["channel"] == "preview" and items[0]["channel"] is None
    state = json.loads((env.stage / "release-state.json").read_text())
    assert state["liveAppcastSha256"] == hashlib.sha256((env.website / "site" / "appcast.xml").read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "matcher",
    [
        lambda c: c[0] == "xcodebuild" and "archive" in c,
        lambda c: c[0] == "xcodebuild" and "-exportArchive" in c,
        lambda c: c[0] == "codesign",
        lambda c: c[0] == "ditto" and "-c" in c,
        lambda c: c[:2] == ["xcrun", "notarytool"],
        lambda c: c[:3] == ["xcrun", "stapler", "staple"],
        lambda c: c[:3] == ["xcrun", "stapler", "validate"],
        lambda c: c[0] == "spctl",
        lambda c: c[0] == "ditto" and "-x" in c,
        lambda c: c[0].endswith("sign_update") and c[-1].endswith(".zip"),
        lambda c: c[0].endswith("generate_appcast"),
        lambda c: c[0].endswith("sign_update") and "-p" in c,
        lambda c: c[0].endswith("sign_update") and "--verify" in c,
    ],
)
def test_a_failing_step_stops_everything_after_it(env, matcher):
    subject, runner = env.release()
    runner.fail.append(matcher)
    with pytest.raises(release.ReleaseError, match="failed with status 1"):
        subject.execute()
    assert matcher(runner.history[-1])
    assert not any(c[0] in {"gh", "npm"} for c in runner.history)


def test_a_failing_git_check_stops_before_any_build(env):
    subject, runner = env.release()
    runner.fail.append(lambda c: c[0] == "git")
    with pytest.raises(release.ReleaseError):
        subject.execute()
    assert [c[0] for c in runner.history] == ["git"]


def test_remote_writes_require_publish_even_when_called_directly(env):
    runner = release.Runner(dry_run=False, publish=False, out=env.lines.append)
    for command in (["gh", "release", "create", "x"], ["git", "push"], ["gh", "pr", "create"]):
        with pytest.raises(release.ReleaseError, match="requires --publish"):
            runner.run(command, remote_write=True)
    assert runner.history == []
    subject, fake = env.release()
    with pytest.raises(release.ReleaseError, match="requires --publish"):
        subject.publish_release()
    assert not any(c[:3] == ["gh", "release", "create"] for c in fake.history)


def test_dry_run_prints_the_plan_and_executes_nothing(env):
    subject, runner = env.release(dry=True)
    subject.execute()
    assert not env.stage.exists() and not (env.tmp / "out").exists()
    assert [c for c in tools(runner) if c.startswith(("gh", "npm", "curl"))] == []
    printed = [line for line in env.lines if line.startswith("+ ")]
    assert len(printed) == len(runner.history) >= 20
    assert any("xcodebuild archive" in line for line in printed) and any("notarytool submit" in line for line in printed)
    assert any(line.startswith("write ") for line in env.lines)
    assert env.lines[-1].startswith("Dry run:")


def test_dry_run_with_publish_prints_the_release_and_website_commands_only(env):
    subject, runner = env.release("--publish", dry=True)
    subject.execute()
    joined = [" ".join(c) for c in runner.history]
    assert any(c.startswith("gh release create macos-v1.9-70") and "--draft" in c and "--prerelease" not in c for c in joined)
    assert any(c.startswith("gh release edit macos-v1.9-70") and "--draft=false" in c for c in joined)
    assert any(c.startswith("gh pr create") for c in joined) and any(" push -u origin release/macos-v1.9-70" in c for c in joined)
    assert not env.stage.exists()


def test_publish_creates_a_draft_uploads_assets_publishes_then_prepares_the_website(env):
    subject, runner = env.release("--publish", "--channel", "preview")
    subject.execute()
    joined = [" ".join(c) for c in runner.history]
    create = next(c for c in runner.history if c[:3] == ["gh", "release", "create"])
    assert "--draft" in create and "--prerelease" in create and "--latest=false" in create
    assert create[create.index("--target") + 1] == DEEP and create[create.index("--repo") + 1] == "AstralDeep/AstralDeep"
    assert [Path(p).name for p in create if p.endswith((".zip", "SHA256SUMS", ".json"))] == [
        "AstralDeep-1.9-70-arm64.zip", "SHA256SUMS", "release-provenance.json",
    ]

    def first(prefix):
        return next(i for i, c in enumerate(joined) if c.startswith(prefix))

    assert first("gh release create") < first("gh release edit") < first("curl") < first("npm test")
    assert first("npm test") < first(f"git -C {env.website} add") < first(f"git -C {env.website} commit")
    site = [c for c in runner.history if c[:3] == ["git", "-C", str(env.website)]]
    assert [c[3] for c in site if c[3] in {"status", "fetch", "switch", "add", "commit", "push"}] == [
        "status", "fetch", "switch", "add", "commit", "push",
    ]
    assert next(c for c in site if c[3] == "switch")[4:] == ["-c", "release/macos-v1.9-70", "origin/main"]
    npm = [c for c in runner.history if c[0] == "npm"]
    assert [c[1:] for c in npm] == [["test"], ["run", "check"], ["run", "build"]]
    assert (env.website / "site" / "appcast.xml").read_text() == (env.stage / "appcast.xml").read_text()
    assert json.loads((env.website / "data" / "downloads.json").read_text())["macos"]["build"] == 70
    assert (env.website / "site/data/mac-update-policy.json.sig").read_text() == SIGNATURE + "\n"
    assert any(c[:3] == ["gh", "pr", "create"] and "AstralDeep/astraldeep.github.io" in c for c in runner.history)
    assert sum(1 for c in runner.history if c[0] == "curl") == 3


def test_stable_release_is_not_marked_prerelease(env):
    subject, runner = env.release("--publish", "--no-pull-request")
    subject.execute()
    assert "--prerelease" not in runner.last_create()
    assert not any(c[:3] == ["gh", "pr", "create"] or "push" in c for c in runner.history)
    assert any(c[3:4] == ["commit"] for c in runner.history if c[0] == "git")


def test_publish_refuses_an_existing_release(env):
    subject, runner = env.release("--publish")
    runner.release_exists = True
    with pytest.raises(release.ReleaseError, match="already exists"):
        subject.execute()
    assert not any(c[:3] == ["gh", "release", "create"] for c in runner.history)


def test_publish_rejects_mutable_or_altered_releases(env):
    subject, runner = env.release("--publish")
    runner.immutable = False
    with pytest.raises(release.ReleaseError, match="not an immutable release"):
        subject.execute()
    assert not any(c[0] in {"npm", "curl"} for c in runner.history)


def test_publish_rejects_asset_digests_that_differ_from_the_staged_bytes(env):
    subject, runner = env.release("--publish")
    runner.altered_digest = True
    with pytest.raises(release.ReleaseError, match="does not match the staged bytes"):
        subject.execute()


def test_asset_wait_retries_until_the_url_answers(env):
    subject, runner = env.release("--publish")
    runner.curl = ["404", "000", "200"]
    subject.execute()
    assert subject.sleeps == [10.0, 10.0]
    probes = [c for c in runner.history if c[0] == "curl"]
    assert "--range" in probes[0] and probes[0][-1].startswith("https://github.com/AstralDeep/AstralDeep/releases/download/macos-v1.9-70/")


def test_asset_wait_gives_up_after_the_attempt_limit(env):
    subject, _ = env.release()
    subject.runner.curl = ["404"] * 5
    with pytest.raises(release.ReleaseError, match="never answered 200"):
        subject.wait_for_asset("https://example.invalid/asset", attempts=3, delay=0.5)
    assert subject.sleeps == [0.5, 0.5]


@pytest.mark.parametrize("failing", ["test", "check", "build"])
def test_website_failures_stop_before_commit_and_push(env, failing):
    subject, runner = env.release("--publish")
    runner.fail.append(lambda c: c[0] == "npm" and c[-1] == failing)
    with pytest.raises(release.ReleaseError, match="npm failed"):
        subject.execute()
    assert not any(c[0] == "git" and "commit" in c for c in runner.history)
    assert not any("push" in c for c in runner.history)


def test_publish_staged_publishes_the_exact_bytes_without_rebuilding(env):
    subject, _ = env.release()
    subject.execute()
    before = (env.stage / "AstralDeep-1.9-70-arm64.zip").read_bytes()
    again, runner = env.release("--publish", "--publish-staged")
    again.execute()
    assert not any(c[0] in {"xcodebuild", "ditto", "codesign", "spctl"} or c[:2] == ["xcrun", "notarytool"] for c in runner.history)
    assert not any(c[0].endswith(("sign_update", "generate_appcast")) for c in runner.history)
    assert (env.stage / "AstralDeep-1.9-70-arm64.zip").read_bytes() == before
    assert any(c[:3] == ["gh", "release", "create"] for c in runner.history)
    assert any("Publishing the staged build" in line for line in env.lines)


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (lambda e: (e.stage / "AstralDeep-1.9-70-arm64.zip").write_bytes(b"other"), "changed after it was built"),
        (lambda e: (e.stage / "SHA256SUMS").unlink(), "incomplete"),
        (lambda e: (e.website / "site" / "appcast.xml").write_text(feed(item(69, "1.8"))), "live appcast changed"),
        (lambda e: setattr(e, "deep_sha", "c" * 40), "Deep moved"),
        (lambda e: e.write_downloads(70, "1.9"), "must exceed"),
    ],
)
def test_publish_staged_refuses_stale_or_altered_builds(env, tamper, message):
    subject, _ = env.release()
    subject.execute()
    tamper(env)
    again, runner = env.release("--publish", "--publish-staged")
    with pytest.raises(release.ReleaseError, match=message):
        again.execute()
    assert not any(c[:3] == ["gh", "release", "create"] for c in runner.history)


def test_publish_staged_refuses_a_state_for_another_release(env):
    subject, _ = env.release()
    subject.execute()
    state = json.loads((env.stage / release.STATE_FILE).read_text())
    state["projection"] = "d" * 40
    (env.stage / release.STATE_FILE).write_text(json.dumps(state))
    again, _ = env.release("--publish", "--publish-staged")
    with pytest.raises(release.ReleaseError, match="does not match"):
        again.execute()


def test_publish_staged_dry_run_prints_commands_only(env):
    again, runner = env.release("--publish", "--publish-staged", dry=True)
    again.execute()
    assert any(c[:3] == ["gh", "release", "create"] for c in runner.history)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--publish-staged"], "needs --publish"),
        (["--ed-key-file", "k", "--ed-account", "other"], "either --ed-account or --ed-key-file"),
        (["--minimum-build", "71"], "between 1 and the release build"),
        (["--minimum-build", "0"], "between 1 and the release build"),
    ],
)
def test_arguments_are_validated(env, extra, message):
    subject, _ = env.release(*extra)
    with pytest.raises(release.ReleaseError, match=message):
        subject.validate()


def test_version_build_commit_and_notes_are_validated(env):
    for field, value, message in (
        ("version", "1", "Version must"), ("build", 0, "Build must"), ("channel", "beta", "Channel must"),
    ):
        subject, _ = env.release()
        setattr(subject, field, value)
        with pytest.raises(release.ReleaseError, match=message):
            subject.validate()
    subject, _ = env.release()
    subject.args.projection_commit = "ABC"
    with pytest.raises(release.ReleaseError, match="full lowercase commit"):
        subject.validate()
    for content, name, message in (("", "notes.md", "non-empty Markdown"), ("x", "notes.txt", "non-empty Markdown"),
                                   ("x" * (64 * 1024 + 1), "notes.md", "exceed 64 KiB")):
        subject, _ = env.release()
        path = env.tmp / name
        path.write_text(content)
        subject.args.notes = path
        with pytest.raises(release.ReleaseError, match=message):
            subject.validate()


def test_preflight_rejects_unclean_or_mismatched_sources(env):
    for dirty, label in (("deep", "Deep"), ("AstralProjection", "Projection")):
        subject, runner = env.release()
        runner.dirty = {dirty}
        with pytest.raises(release.ReleaseError, match=f"{label} working tree is not clean"):
            subject.preflight()
    subject, runner = env.release()
    runner.pin = "c" * 40
    with pytest.raises(release.ReleaseError, match="same commit"):
        subject.preflight()
    subject, runner = env.release("--publish")
    runner.dirty = {"astraldeep.github.io"}
    with pytest.raises(release.ReleaseError, match="Website working tree is not clean"):
        subject.preflight()


def test_preflight_rejects_malformed_pin_output_and_bad_deep_head(env):
    subject, runner = env.release()
    runner.pin = ""
    runner._git = lambda command, cwd: release.Result(0, PROJECTION + "\n" if command[3] == "rev-parse" else "")
    with pytest.raises(release.ReleaseError, match="same commit"):
        subject.preflight()
    env.deep_sha = "not-a-commit"
    subject, runner = env.release()
    with pytest.raises(release.ReleaseError, match="Deep HEAD is not a commit"):
        subject.preflight()


def test_preflight_requires_a_strictly_greater_build_and_empty_directories(env):
    env.write_downloads(70, "1.9")
    subject, _ = env.release()
    with pytest.raises(release.ReleaseError, match="must exceed the published build 70"):
        subject.preflight()
    env.write_downloads(69, "1.8")
    env.stage.mkdir(parents=True)
    (env.stage / "old.zip").write_text("x")
    subject, _ = env.release()
    with pytest.raises(release.ReleaseError, match="is not empty"):
        subject.preflight()


def test_preflight_reports_environment_problems(env, monkeypatch):
    subject, _ = env.release()
    monkeypatch.setattr(release.platform, "system", lambda: "Linux")
    with pytest.raises(release.ReleaseError, match="built on macOS"):
        subject.preflight()
    monkeypatch.setattr(release.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(release.shutil, "which", lambda name: None if name == "gh" else "/bin/x")
    subject, _ = env.release("--publish")
    with pytest.raises(release.ReleaseError, match="Missing tools: gh"):
        subject.preflight()


def test_downloads_record_must_be_readable_but_may_be_null(env):
    (env.website / "data" / "downloads.json").write_text(json.dumps({"schemaVersion": 1, "macos": None}))
    subject, _ = env.release()
    assert subject.live_downloads() is None
    (env.website / "data" / "downloads.json").write_text("{")
    with pytest.raises(release.ReleaseError, match="unreadable"):
        subject.live_downloads()
    (env.website / "data" / "downloads.json").unlink()
    with pytest.raises(release.ReleaseError, match="unreadable"):
        subject.live_downloads()
    dry, _ = env.release(dry=True)
    assert dry.live_downloads() is None


def test_identity_checks_translate_packager_failures_and_version_drift(env, monkeypatch):
    subject, _ = env.release()
    app = env.tmp / "AstralDeep.app"

    def refuse(app_path, team):
        raise release.packager.CandidateError("App requires the expected timestamped Developer ID")

    monkeypatch.setattr(release.packager, "inspect_app", refuse)
    with pytest.raises(release.ReleaseError, match="App identity check failed"):
        subject.inspect(app)
    monkeypatch.setattr(release.packager, "inspect_app", lambda a, t: {**IDENTITY, "build": 69, "version": "1.8"})
    with pytest.raises(release.ReleaseError, match="differs from the requested release"):
        subject.inspect(app)
    monkeypatch.setattr(release.packager, "inspect_app", lambda a, t: {**IDENTITY, "architecture": "x86_64"})
    with pytest.raises(release.ReleaseError, match="differs"):
        subject.inspect(app)


def test_sparkle_configuration_inside_the_built_app_is_enforced(env):
    good = {"SUPublicEDKey": release.SPARKLE_PUBLIC_KEY, "SUFeedURL": release.FEED_URL, "SUVerifyUpdateBeforeExtraction": True}
    for index, (patch, message) in enumerate((
        ({"SUPublicEDKey": "other"}, "expected Sparkle feed and public key"),
        ({"SUFeedURL": "http://example.com/appcast.xml"}, "expected Sparkle feed and public key"),
        ({"SUVerifyUpdateBeforeExtraction": False}, "unexpected Sparkle verification"),
        ({"SUAllowsAutomaticUpdates": True}, "unexpected Sparkle verification"),
    )):
        app = env.tmp / f"bad-{index}.app"
        write_app(app, {**good, **patch})
        subject, _ = env.release()
        with pytest.raises(release.ReleaseError, match=message):
            subject.verify_sparkle(app)
    app = env.tmp / "good.app"
    write_app(app, good)
    subject, runner = env.release()
    subject.verify_sparkle(app)
    runner.codesign_output = "TeamIdentifier=OTHERTEAM1"
    with pytest.raises(release.ReleaseError, match="Autoupdate is not signed by team"):
        subject.verify_sparkle(app)


def test_notarization_problems_stop_the_release(env):
    app = env.tmp / "work" / "AstralDeep.app"
    for patch, message in (
        ({"notary": {"status": "Invalid", "id": "sub-9"}}, "ended as Invalid"),
        ({"stapler_output": "failed"}, "did not validate"),
        ({"spctl_output": "rejected"}, "did not accept"),
        ({"spctl_output": "accepted\nsource=Developer ID"}, "did not accept"),
    ):
        subject, runner = env.release()
        for key, value in patch.items():
            setattr(runner, key, value)
        (env.tmp / "work").mkdir(exist_ok=True)
        subject.work = env.tmp / "work"
        with pytest.raises(release.ReleaseError, match=message):
            subject.notarize(app, dict(IDENTITY))
    subject, runner = env.release()
    subject.work = env.tmp / "work"
    runner.notary = {"unexpected": True}
    with pytest.raises(release.ReleaseError, match="no submission status"):
        subject.notarize(app, dict(IDENTITY))
    runner._xcrun = lambda command, cwd: release.Result(0, "not json") if command[1] == "notarytool" else None
    with pytest.raises(release.ReleaseError, match="no submission status"):
        subject.notarize(app, dict(IDENTITY))


def test_identity_changes_during_notarization_or_extraction_are_refused(env, monkeypatch):
    app = env.tmp / "work" / "AstralDeep.app"
    subject, _ = env.release()
    subject.work = env.tmp / "work"
    monkeypatch.setattr(release.packager, "inspect_app", lambda a, t: {**IDENTITY, "executableSHA256": "f" * 64})
    with pytest.raises(release.ReleaseError, match="changed while notarizing"):
        subject.notarize(app, dict(IDENTITY))
    subject, _ = env.release()
    with pytest.raises(release.ReleaseError, match="Fresh extraction"):
        subject.package(app, dict(IDENTITY))


def test_stage_rejects_unusable_signing_output_and_extra_files(env):
    for output in ("garbage", 'sparkle:edSignature="abc" length="3"'):
        subject, runner = env.release()
        runner.signing_output = output
        with pytest.raises(release.ReleaseError, match="did not report the archive signature"):
            subject.execute()
        import shutil
        shutil.rmtree(env.tmp / "out")
    subject, runner = env.release()
    runner.extra_stage_file = True
    with pytest.raises(release.ReleaseError, match="only the newest archive"):
        subject.execute()


def test_stage_requires_the_sparkle_tools(env):
    (env.sparkle / "sign_update").unlink()
    subject, _ = env.release()
    with pytest.raises(release.ReleaseError, match="Sparkle tools not found"):
        subject.execute()


def test_default_sparkle_tools_come_from_the_archive_derived_data(env):
    args = env.args()
    args.sparkle_bin = None
    subject = release.Release(args, FakeRunner(env, dry_run=False, publish=False), deep=env.deep, clock=lambda: NOW)
    assert subject.tool("sign_update") == str(env.tmp / "out/work/dd/SourcePackages/artifacts/sparkle/Sparkle/bin/sign_update")


def test_generated_appcast_must_describe_the_archive_and_preserve_old_items(env):
    subject, runner = env.release()
    runner.generate_item = item(70, "1.9", None, 1, SIGNATURE)
    with pytest.raises(release.ReleaseError, match=r"disagrees with the archive: \['length'\]"):
        subject.execute()
    import shutil
    shutil.rmtree(env.tmp / "out")
    (env.website / "site" / "appcast.xml").write_text(feed(item(69, "1.8", None, 111, "O" * 86 + "==")))
    subject, runner = env.release()
    original = runner._generate_appcast

    def rewrite(command, cwd):
        original(command, cwd)
        out = Path(command[command.index("-o") + 1])
        out.write_text(out.read_text().replace("O" * 86, "R" * 86))

    runner._generate_appcast = rewrite
    with pytest.raises(release.ReleaseError, match="rewrote the existing item for build 69"):
        subject.execute()


def test_policy_inherits_the_live_minimum_and_increments_the_serial(env):
    live = {"schemaVersion": 1, "minimumBuild": 68, "serial": 4, "expires": "2027-01-01T00:00:00Z", "message": "x"}
    (env.website / "site" / "data" / "mac-update-policy.json").write_text(json.dumps(live))
    subject, _ = env.release()
    subject.execute()
    policy = json.loads((env.stage / "mac-update-policy.json").read_text())
    assert (policy["minimumBuild"], policy["serial"]) == (68, 5)


def test_policy_options_override_and_are_bounded(env):
    subject, _ = env.release("--minimum-build", "65", "--policy-expires", "2027-02-03T04:05:06Z")
    subject.execute()
    policy = json.loads((env.stage / "mac-update-policy.json").read_text())
    assert (policy["minimumBuild"], policy["expires"]) == (65, "2027-02-03T04:05:06Z")
    for expires in ("2026-10-26T11:59:59Z", "2026-10-26T12:00:00Z"):
        subject, _ = env.release("--policy-expires", expires)
        subject.staging = env.tmp / "elsewhere"
        subject.staging.mkdir(exist_ok=True)
        with pytest.raises(release.ReleaseError, match="future UTC"):
            subject.policy()
    subject, _ = env.release()
    subject.args.policy_expires = dt.datetime(2030, 1, 1)
    with pytest.raises(release.ReleaseError, match="future UTC"):
        subject.policy()
    subject, _ = env.release()
    subject.args.minimum_build = 99
    with pytest.raises(release.ReleaseError, match="exceeds the release build"):
        subject.policy()


def test_policy_rejects_a_malformed_signature_and_an_unreadable_live_policy(env):
    for signature in ("", "not base64!!", base64.b64encode(b"short").decode()):
        subject, runner = env.release()
        runner.policy_signature = signature
        subject.staging = env.tmp / "policy-stage"
        subject.staging.mkdir(exist_ok=True)
        with pytest.raises(release.ReleaseError, match="malformed policy signature"):
            subject.policy()
    (env.website / "site" / "data" / "mac-update-policy.json").write_text("{")
    subject, _ = env.release()
    with pytest.raises(release.ReleaseError, match="Live update policy is unreadable"):
        subject.live_policy()


def test_appcast_parser_rejects_hostile_or_incomplete_feeds(env):
    path = env.tmp / "appcast.xml"
    for text, message in (
        ("<rss", "unreadable"), (feed(), "lists no updates"), (feed("<item><title>x</title></item>"), "lacks an enclosure"),
        (feed(item(70, "1.9").replace("<sparkle:version>70", "<sparkle:version>seventy")), "lacks an enclosure or version"),
        ('<!DOCTYPE rss [<!ENTITY a "b">]>' + feed(item(70, "1.9")), "must not declare"),
    ):
        path.write_text(text)
        with pytest.raises(release.ReleaseError, match=message):
            release.parse_appcast(path)
    with pytest.raises(release.ReleaseError, match="unreadable"):
        release.parse_appcast(env.tmp / "missing.xml")
    path.write_text(feed(item(70, "1.9").replace('length="111"', 'length="big"')))
    assert release.parse_appcast(path)[0]["length"] == -1


def test_runner_executes_real_commands_and_reports_failures(env):
    runner = release.Runner(dry_run=False, publish=False, out=env.lines.append)
    assert runner.run([sys.executable, "-c", "print('ok')"]).stdout == "ok\n"
    assert runner.run([sys.executable, "-c", "import sys; sys.exit(3)"], check=False).returncode == 3
    with pytest.raises(release.ReleaseError, match="failed with status 3"):
        runner.run([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])
    assert env.lines[0].startswith("+ ") and runner.history[0][1] == "-c"
    runner.run([sys.executable, "-c", "pass"], cwd=env.tmp)
    assert any(line.startswith(f"+ (cd {env.tmp}") for line in env.lines)


def test_sha256_file_streams_the_whole_file(tmp_path):
    path = tmp_path / "big.bin"
    data = b"astral" * 400000
    path.write_bytes(data)
    assert release.sha256_file(path) == hashlib.sha256(data).hexdigest()


def test_main_reports_release_errors_and_succeeds_otherwise(env, monkeypatch, capsys):
    argv = ["--projection-commit", PROJECTION, "--version", "1.9", "--build", "70", "--notes", str(env.notes), "--dry-run"]

    class Failing:
        def __init__(self, *_args, **_kwargs):
            pass

        def execute(self):
            raise release.ReleaseError("boom")

    monkeypatch.setattr(release, "Release", Failing)
    assert release.main(argv) == 1
    assert "Mac release failed: boom" in capsys.readouterr().err

    class Fine(Failing):
        def execute(self):
            return Path(".")

    monkeypatch.setattr(release, "Release", Fine)
    assert release.main(argv) == 0


def test_expiry_argument_must_be_utc_text():
    assert release.parse_expiry("2027-01-01T00:00:00Z") == dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc)
    with pytest.raises(argparse.ArgumentTypeError):
        release.parse_expiry("2027-01-01")
