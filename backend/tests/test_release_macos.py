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
import shutil
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
FEED = "https://astraldeep.github.io/appcast.xml"
POLICY = "https://astraldeep.github.io/data/mac-update-policy.json"
DMG = "AstralDeep-2.0.0-71-arm64.dmg"
ASSETS = "https://github.com/AstralDeep/AstralDeep/releases/download/macos-v2.0.0-71/"
IDENTITY = {
    "version": "2.0.0", "build": 71, "minimumMacOS": "14.0", "architecture": "arm64",
    "bundleID": "com.personalailabs.astraldeep", "executableSHA256": "e" * 64, "teamID": "593LGH35GS",
}
SITE_FILES = ["downloads.json", "appcast.xml", "mac-update-policy.json", "mac-update-policy.json.sig"]


def item(build: int, version: str, channel: str | None = None, length: int = 111, signature: str = "O" * 86 + "==",
         suffix: str = "zip") -> str:
    tag = f"macos-v{version}-{build}"
    url = f"https://github.com/AstralDeep/AstralDeep/releases/download/{tag}/AstralDeep-{version}-{build}-arm64.{suffix}"
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


def published(build: int, version: str, *, draft: bool = False, tag: str | None = None, assets=None) -> dict:
    return {
        "tag_name": tag or f"macos-v{version}-{build}", "draft": draft, "target_commitish": "c" * 40,
        "assets": [{"name": name, "digest": digest} for name, digest in (assets or {}).items()],
    }


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def write_app(app, info):
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))


class FakeRunner(release.Runner):
    """Simulates the toolchain: creates artifacts, records history and fails commands on request."""

    def __init__(self, env, **kwargs):
        super().__init__(out=env.lines.append, **kwargs)
        self.env = env
        self.fail: list = []
        self.probes: list[str] = []
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
        self.listing: str | None = None
        self.latest_tag: str | None = "v0.6.0"
        self.mounted: list[Path] = []

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
            Path(command[-1]).mkdir(parents=True)

    def _hdiutil(self, command, cwd):
        if command[1] == "create":
            Path(command[-1]).write_bytes(b"dmg-bytes-" + command[-1].encode())
        elif command[1] == "attach":
            mount = Path(command[command.index("-mountpoint") + 1])
            (mount / "AstralDeep.app").mkdir(parents=True, exist_ok=True)
            self.mounted.append(mount)
        elif command[1] == "detach":
            self.mounted.remove(Path(command[2]))

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
        archive = next(staging.glob("*.dmg"))
        build, version = self.env.build, self.env.version
        channel = "preview" if "--channel" in command else None
        kept = re.findall(r"<item>.*?</item>", out.read_text(), re.S) if out.exists() else []
        fresh = self.generate_item or item(build, version, channel, archive.stat().st_size, SIGNATURE, "dmg")
        out.write_text(feed(fresh, *kept))
        if self.extra_stage_file:
            (staging / "old_updates").mkdir()

    def _gh(self, command, cwd):
        if command[1:3] == ["release", "view"]:
            return release.Result(0 if self.release_exists else 1, "", "release not found")
        if command[1:3] == ["release", "create"]:
            self.uploads = [Path(part) for part in command[command.index("--latest=false") + 1:] if part != "--prerelease"]
            return None
        if command[1] == "api" and command[2].endswith("/releases/latest"):
            if self.latest_tag is None:
                return release.Result(1, "", "gh: Not Found (HTTP 404)")
            return release.Result(0, self.latest_tag + "\n")
        if command[1] == "api" and command[-1].endswith("/releases?per_page=100"):
            return release.Result(0, self.listing if self.listing is not None else json.dumps([self.env.releases]))
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
        if "--range" in command:
            return release.Result(0, self.probes.pop(0) if self.probes else "206")
        url = command[-1]
        status, code, body, final = self.env.site.get(url, (22, 404, b"", url))
        if code == 200 and status in {0, 63}:
            Path(command[command.index("--output") + 1]).write_bytes(body)
        return release.Result(status, f"{code} {final}")


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.lines: list[str] = []
        self.deep = tmp_path / "deep"
        self.projection = self.deep / "components" / "AstralProjection"
        self.sparkle = tmp_path / "bin"
        self.version, self.build = "2.0.0", 71
        self.deep_sha = DEEP
        self.site: dict[str, tuple[int, int, bytes, str]] = {}
        self.releases = [published(69, "1.8"), published(0, "", tag="v0.6.0")]
        for path in (self.projection, self.sparkle):
            path.mkdir(parents=True)
        for tool in ("sign_update", "generate_appcast"):
            (self.sparkle / tool).write_text("#!/bin/sh\n")
        self.notes = tmp_path / "notes.md"
        self.notes.write_text("## 2.0.0\n\n- Updates inside the app.\n")
        monkeypatch.setattr(release.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(release.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(release.packager, "inspect_app", lambda app, team: dict(IDENTITY))
        self.info = {
            "SUPublicEDKey": release.SPARKLE_PUBLIC_KEY, "SUFeedURL": release.FEED_URL,
            "SUVerifyUpdateBeforeExtraction": True,
        }

    def serve(self, url, body, *, code=200, status=0, final=None):
        self.site[url] = (status, code, body.encode() if isinstance(body, str) else body, final or url)

    def args(self, *extra):
        stage = self.tmp / "out" / "stage"
        return release.build_parser().parse_args([
            "--projection-commit", PROJECTION, "--version", self.version, "--build", str(self.build),
            "--notes", str(self.notes), "--staging-dir", str(stage), "--work-dir", str(self.tmp / "out" / "work"),
            "--projection", str(self.projection), "--sparkle-bin", str(self.sparkle), *extra,
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

    @property
    def work(self):
        return self.tmp / "out" / "work"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def label(command):
    name = Path(command[0]).name
    if name == "git":
        return "git " + command[3]
    if name in {"xcrun", "gh", "hdiutil"}:
        return f"{name} {command[1]}"
    return name


def tools(runner):
    return [label(command) for command in runner.history]


def fetches(runner):
    return [c for c in runner.history if c[0] == "curl" and "--max-filesize" in c]


def test_archive_is_a_dmg_named_for_the_release(env):
    subject, _ = env.release()
    assert subject.tag == "macos-v2.0.0-71"
    assert subject.archive_name == DMG
    assert subject.asset_url == ASSETS + DMG


def test_build_without_publish_stages_only_the_newest_archive_and_writes_every_artifact(env):
    subject, runner = env.release()
    assert subject.execute() == env.stage
    names = sorted(path.name for path in env.stage.iterdir())
    assert names == sorted([
        DMG, "AstralDeep-2.0.0-71-arm64.md", "appcast.xml", "SHA256SUMS", "release-provenance.json",
        "downloads.json", "mac-update-policy.json", "mac-update-policy.json.sig", release.STATE_FILE,
    ])
    archive = env.stage / DMG
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert (env.stage / "SHA256SUMS").read_text() == f"{digest}  {DMG}\n"
    record = json.loads((env.stage / "downloads.json").read_text())["macos"]
    assert list(record) == [
        "version", "build", "minimumMacOS", "architecture", "format", "bytes", "sha256", "url", "releaseURL",
        "checksumsURL", "source", "channel",
    ]
    assert record["format"] == "dmg" and record["version"] == "2.0.0" and record["build"] == 71
    assert record["sha256"] == digest and record["bytes"] == archive.stat().st_size and record["channel"] == "stable"
    assert record["url"] == ASSETS + DMG and record["checksumsURL"] == ASSETS + "SHA256SUMS"
    assert record["source"] == {"deep": DEEP, "projection": PROJECTION}
    provenance = json.loads((env.stage / "release-provenance.json").read_text())
    assert provenance["tag"] == "macos-v2.0.0-71" and provenance["archive"]["sha256"] == digest
    assert provenance["archive"]["filename"] == DMG
    assert provenance["apple"]["notarizationSubmission"] == "sub-1"
    assert provenance["apple"]["diskImageNotarizationSubmission"] == "sub-1"
    assert provenance["apple"]["gatekeeperAssessment"] == "accepted: Notarized Developer ID"
    assert provenance["apple"]["diskImageGatekeeperAssessment"] == "accepted: Notarized Developer ID"
    assert provenance["verification"]["diskImageMountedIdentity"] == "passed"
    assert provenance["appExecutableSha256"] == "e" * 64 and provenance["distribution"] == "DirectDownload"
    assert not any("protected Mac publisher" in line for line in provenance["limitations"])
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
    assert not any(c[0] == "npm" or c[:3] == ["gh", "release", "create"] or "push" in c for c in runner.history)
    assert any("nothing was published" in line for line in env.lines)


def test_steps_run_in_the_documented_order(env):
    subject, runner = env.release()
    subject.execute()
    assert tools(runner) == [
        "git status", "git status", "git rev-parse", "git rev-parse", "git ls-tree", "gh api",
        "xcodebuild", "xcodebuild", "codesign", "codesign",
        "ditto", "xcrun notarytool", "xcrun stapler", "xcrun stapler", "spctl",
        "ditto", "hdiutil create", "codesign", "xcrun notarytool", "xcrun stapler", "xcrun stapler", "spctl",
        "hdiutil attach", "spctl", "xcrun stapler", "hdiutil detach",
        "curl", "sign_update", "generate_appcast", "curl", "sign_update", "sign_update",
    ]


def test_package_builds_signs_notarizes_and_staples_the_dmg(env):
    subject, runner = env.release()
    subject.execute()
    dmg = str(env.stage / DMG)
    volume = env.work / "volume"
    mount = str(env.work / "mount")
    joined = [" ".join(c) for c in runner.history]

    def at(predicate):
        return next(i for i, c in enumerate(runner.history) if predicate(c))

    copy = at(lambda c: c[0] == "ditto" and c[1] != "-c")
    assert runner.history[copy][1:] == [str(env.work / "export/AstralDeep.app"), str(volume / "AstralDeep.app")]
    assert (volume / "Applications").is_symlink() and str((volume / "Applications").readlink()) == "/Applications"
    create = at(lambda c: c[:2] == ["hdiutil", "create"])
    assert runner.history[create][2:] == [
        "-volname", "AstralDeep", "-srcfolder", str(volume), "-fs", "HFS+", "-format", "UDZO", dmg,
    ]
    sign = at(lambda c: c[0] == "codesign" and c[-1] == dmg)
    assert runner.history[sign] == ["codesign", "--sign", release.SIGNING_IDENTITY, "--timestamp", dmg]
    notarize = at(lambda c: c[:3] == ["xcrun", "notarytool", "submit"] and c[3] == dmg)
    assert runner.history[notarize][4:] == ["--keychain-profile", "astral-notary", "--wait", "--output-format", "json"]
    staple = at(lambda c: c[:3] == ["xcrun", "stapler", "staple"] and c[-1] == dmg)
    validate = at(lambda c: c[:3] == ["xcrun", "stapler", "validate"] and c[-1] == dmg)
    assess = at(lambda c: c[0] == "spctl" and c[-1] == dmg)
    assert runner.history[assess] == [
        "spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=4", dmg,
    ]
    attach = at(lambda c: c[:2] == ["hdiutil", "attach"])
    assert runner.history[attach] == ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, dmg]
    mounted = at(lambda c: c[0] == "spctl" and c[-1] == f"{mount}/AstralDeep.app")
    assert runner.history[mounted][1:4] == ["--assess", "--type", "execute"]
    detach = at(lambda c: c[:2] == ["hdiutil", "detach"])
    assert runner.history[detach] == ["hdiutil", "detach", mount]
    assert create < sign < notarize < staple < validate < assess < attach < mounted < detach
    assert runner.mounted == []
    assert not any(" -x " in line for line in joined)


def test_package_refuses_app_identity_change_inside_dmg(env, monkeypatch):
    def inspect(app, team):
        return {**IDENTITY, "executableSHA256": "f" * 64} if "mount" in str(app) else dict(IDENTITY)

    monkeypatch.setattr(release.packager, "inspect_app", inspect)
    subject, runner = env.release()
    with pytest.raises(release.ReleaseError, match="disk image differs from the notarized app"):
        subject.execute()
    assert tools(runner)[-1] == "hdiutil detach" and runner.mounted == []
    assert not any(c[0].endswith(("sign_update", "generate_appcast")) for c in runner.history)


def test_a_failed_detach_stops_the_release(env):
    subject, runner = env.release()
    runner.fail.append(lambda c: c[:2] == ["hdiutil", "detach"])
    with pytest.raises(release.ReleaseError, match="could not be detached"):
        subject.execute()
    assert not any(c[0].endswith("sign_update") for c in runner.history)


def test_disk_image_notarization_and_gatekeeper_problems_stop_the_release(env):
    app = env.tmp / "AstralDeep.app"
    for patch, message in (
        ({"notary": {"status": "Invalid", "id": "sub-9"}}, "ended as Invalid"),
        ({"stapler_output": "failed"}, "did not validate"),
        ({"spctl_output": "accepted\nsource=Developer ID"}, "did not accept"),
    ):
        subject, runner = env.release()
        for key, value in patch.items():
            setattr(runner, key, value)
        with pytest.raises(release.ReleaseError, match=message):
            subject.package(app, dict(IDENTITY))
        assert not any(c[:2] == ["hdiutil", "attach"] for c in runner.history)
        shutil.rmtree(env.tmp / "out")


def test_notary_key_file_mode_replaces_the_keychain_profile(env):
    key = env.tmp / "AuthKey.p8"
    subject, runner = env.release(
        "--notary-key", str(key), "--notary-key-id", "KEY1234567", "--notary-issuer", "11111111-2222-3333-4444-555555555555")
    subject.execute()
    submissions = [c for c in runner.history if c[:3] == ["xcrun", "notarytool", "submit"]]
    assert len(submissions) == 2
    for command in submissions:
        assert command[4:10] == [
            "--key", str(key), "--key-id", "KEY1234567", "--issuer", "11111111-2222-3333-4444-555555555555",
        ]
        assert "--keychain-profile" not in command


@pytest.mark.parametrize(
    "flags",
    [
        ["--notary-key", "k.p8"], ["--notary-key-id", "KEY"], ["--notary-issuer", "ISSUER"],
        ["--notary-key", "k.p8", "--notary-key-id", "KEY"], ["--notary-key-id", "KEY", "--notary-issuer", "ISSUER"],
    ],
)
def test_notary_key_flags_must_travel_together(env, flags):
    subject, _ = env.release(*flags)
    with pytest.raises(release.ReleaseError, match="travel together"):
        subject.validate()


def test_live_feed_is_fetched_from_the_site(env):
    env.serve(FEED, feed(item(69, "1.8", "preview")))
    live_policy = {"schemaVersion": 1, "minimumBuild": 68, "serial": 4, "expires": "2027-01-01T00:00:00Z", "message": "x"}
    env.serve(POLICY, json.dumps(live_policy))
    subject, runner = env.release()
    subject.execute()
    assert [c[-1] for c in fetches(runner)] == [FEED, POLICY]
    for command in fetches(runner):
        for flag, value in (("--max-filesize", "2097152"), ("--proto", "=https"), ("--proto-redir", "=https"),
                            ("--max-redirs", "2")):
            assert command[command.index(flag) + 1] == value
        assert "--fail" in command and "--location" in command
    items = release.parse_appcast(env.stage / "appcast.xml")
    assert [entry["build"] for entry in items] == [71, 69]
    assert items[1]["channel"] == "preview" and items[0]["channel"] is None
    state = json.loads((env.stage / "release-state.json").read_text())
    assert state["liveAppcastSha256"] == hashlib.sha256(feed(item(69, "1.8", "preview")).encode()).hexdigest()
    policy = json.loads((env.stage / "mac-update-policy.json").read_text())
    assert (policy["minimumBuild"], policy["serial"]) == (68, 5)
    for retired in ("--website", "--no-pull-request"):
        with pytest.raises(SystemExit):
            release.build_parser().parse_args([
                "--projection-commit", PROJECTION, "--version", "2.0.0", "--build", "71", "--notes", "n.md", retired, "x",
            ])


@pytest.mark.parametrize(
    ("status", "code", "final", "message"),
    [
        (63, 200, FEED, "exceeds 2097152 bytes"),
        (0, 200, "https://example.com/appcast.xml", "outside astraldeep.github.io"),
        (0, 200, "http://astraldeep.github.io/appcast.xml", "outside astraldeep.github.io"),
        (22, 500, FEED, "answered 500"),
        (47, 301, FEED, "answered 301"),
        (22, 404, "https://example.com/missing", "outside astraldeep.github.io"),
    ],
)
def test_live_feed_refuses_oversized_or_redirected_bodies(env, status, code, final, message):
    env.serve(FEED, feed(item(69, "1.8")), code=code, status=status, final=final)
    subject, runner = env.release()
    destination = env.tmp / "live" / "appcast.xml"
    with pytest.raises(release.ReleaseError, match=message):
        subject.fetch_live(FEED, destination)
    assert not destination.exists()
    subject, runner = env.release()
    with pytest.raises(release.ReleaseError, match=message):
        subject.execute()
    assert not any(c[0].endswith("generate_appcast") for c in runner.history)


def test_live_feed_body_must_exist_and_stay_bounded(env):
    subject, runner = env.release()
    destination = env.tmp / "live" / "appcast.xml"
    runner._curl = lambda command, cwd: release.Result(0, f"200 {FEED}")
    with pytest.raises(release.ReleaseError, match="body is missing or too large"):
        subject.fetch_live(FEED, destination)

    def oversized(command, cwd):
        Path(command[command.index("--output") + 1]).write_bytes(b"x" * (2097152 + 1))
        return release.Result(0, f"200 {FEED}")

    runner._curl = oversized
    with pytest.raises(release.ReleaseError, match="body is missing or too large"):
        subject.fetch_live(FEED, destination)
    assert not destination.exists()


@pytest.mark.parametrize("status", [22, 56])
def test_a_site_404_means_absent_whatever_curl_status_http2_reports(env, status):
    env.serve(FEED, "", code=404, status=status)
    subject, _ = env.release()
    assert subject.fetch_live(FEED, env.tmp / "live" / "appcast.xml") is False
    assert not (env.tmp / "live" / "appcast.xml").exists()


def test_a_missing_live_feed_is_allowed_only_before_the_pipeline_published_one(env):
    subject, _ = env.release()
    assert subject.fetch_live(FEED, env.tmp / "live" / "appcast.xml") is False
    live = feed(item(70, "1.9", suffix="dmg"))
    env.releases = [published(70, "1.9", assets={"appcast.xml": digest(live), "mac-update-policy.json": digest("{}")})]
    subject, runner = env.release()
    with pytest.raises(release.ReleaseError, match="serves no appcast.xml although macos-v1.9-70 published one"):
        subject.execute()
    assert not any(c[0].endswith("generate_appcast") for c in runner.history)
    assert subject.sleeps == [60.0] * (release.SYNC_ATTEMPTS - 1)
    shutil.rmtree(env.tmp / "out")
    env.serve(FEED, live)
    subject, runner = env.release()
    with pytest.raises(release.ReleaseError, match="serves no mac-update-policy.json although macos-v1.9-70"):
        subject.execute()
    assert any(c[0].endswith("generate_appcast") for c in runner.history)


def test_a_stale_site_is_awaited_and_then_refused(env):
    current = feed(item(70, "1.9", suffix="dmg"), item(69, "1.8"))
    env.releases = [published(70, "1.9", assets={"appcast.xml": digest(current)})]
    env.serve(FEED, feed(item(69, "1.8")))
    subject, runner = env.release()
    with pytest.raises(release.ReleaseError, match="still serves an older appcast.xml than macos-v1.9-70"):
        subject.execute()
    assert subject.sleeps == [60.0] * (release.SYNC_ATTEMPTS - 1)
    assert len(fetches(runner)) == release.SYNC_ATTEMPTS
    assert not any(c[0].endswith("generate_appcast") for c in runner.history)


def test_the_stage_waits_for_the_site_to_serve_the_newest_release(env):
    stale, current = feed(item(69, "1.8")), feed(item(70, "1.9", suffix="dmg"), item(69, "1.8"))
    policy = json.dumps({"schemaVersion": 1, "minimumBuild": 70, "serial": 3, "expires": "2027-01-01T00:00:00Z", "message": "x"})
    env.releases = [published(70, "1.9", assets={"appcast.xml": digest(current), "mac-update-policy.json": digest(policy)})]
    env.serve(POLICY, policy)
    subject, runner = env.release()
    answers = [stale, stale, current]

    def site(command, cwd):
        if command[-1] == FEED:
            env.serve(FEED, answers.pop(0) if answers else current)
        return FakeRunner._curl(runner, command, cwd)

    runner._curl = site
    subject.execute()
    assert subject.sleeps == [60.0, 60.0]
    assert [entry["build"] for entry in release.parse_appcast(env.stage / "appcast.xml")] == [71, 70, 69]
    assert json.loads((env.stage / "mac-update-policy.json").read_text())["serial"] == 4


def test_a_published_site_file_without_a_digest_is_refused(env):
    env.releases = [published(70, "1.9", assets={"appcast.xml": None})]
    env.serve(FEED, feed(item(70, "1.9", suffix="dmg")))
    subject, _ = env.release()
    with pytest.raises(release.ReleaseError, match="lists appcast.xml without a digest"):
        subject.execute()


def test_latest_release_bounds_the_build(env):
    env.releases = [
        published(70, "1.9"), published(99, "9.0.0", draft=True), published(0, "", tag="apple-v9.9"),
        published(0, "", tag="macos-vbeta-200"), published(69, "1.8"),
    ]
    env.build = 70
    subject, _ = env.release()
    with pytest.raises(release.ReleaseError, match=r"must exceed the published build 70 \(macos-v1.9-70\)"):
        subject.preflight()
    env.build = 71
    subject, runner = env.release()
    subject.preflight()
    assert subject.latest == {
        "tag": "macos-v1.9-70", "version": "1.9", "build": 70, "target": "c" * 40, "assets": {},
    }
    listing = next(c for c in runner.history if c[:2] == ["gh", "api"])
    assert listing == ["gh", "api", "--paginate", "--slurp", "repos/AstralDeep/AstralDeep/releases?per_page=100"]


def test_latest_release_reads_every_page_and_tolerates_an_empty_repository(env):
    subject, runner = env.release()
    runner.listing = json.dumps([[published(69, "1.8")], [published(80, "2.0.5")]])
    assert subject.latest_release()["build"] == 80
    runner.listing = json.dumps([[]])
    assert subject.latest_release() is None


@pytest.mark.parametrize("listing", ["not json", json.dumps({"message": "Not Found"}), json.dumps([[{"tag_name": "macos-v1.8-69"}]])])
def test_latest_release_refuses_an_unreadable_listing(env, listing):
    subject, runner = env.release()
    runner.listing = listing
    with pytest.raises(release.ReleaseError, match="release list is unreadable"):
        subject.latest_release()


def test_version_and_build_are_passed_as_xcodebuild_overrides(env):
    subject, runner = env.release()
    subject.execute()
    archive = next(c for c in runner.history if c[:2] == ["xcodebuild", "archive"])
    assert "MARKETING_VERSION=2.0.0" in archive and "CURRENT_PROJECT_VERSION=71" in archive


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
    assert plistlib.loads((env.work / "export-options.plist").read_bytes()) == {
        "method": "developer-id", "teamID": "593LGH35GS", "signingStyle": "manual",
        "signingCertificate": "Developer ID Application", "destination": "export",
    }
    assert by["ditto"][0][:6] == ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(env.work / "export/AstralDeep.app")]
    assert by["xcrun notarytool"][0][3:] == [
        str(env.work / "notarize/AstralDeep-2.0.0-71-arm64.zip"), "--keychain-profile", "astral-notary", "--wait",
        "--output-format", "json",
    ]
    assert by["spctl"][0][1:4] == ["--assess", "--type", "execute"]
    generate = next(c for c in runner.history if c[0].endswith("generate_appcast"))
    assert generate[1:3] == ["--ed-key-file", str(env.tmp / "key")]
    for flag, value in (
        ("--download-url-prefix", ASSETS), ("--link", "https://astraldeep.github.io"), ("--maximum-versions", "5"),
        ("--maximum-deltas", "0"), ("--channel", "preview"), ("-o", str(env.stage / "appcast.xml")),
    ):
        assert generate[generate.index(flag) + 1] == value
    assert "--embed-release-notes" in generate and generate[-1] == str(env.stage)
    sign = next(c for c in runner.history if c[0].endswith("sign_update"))
    assert sign[1:3] == ["--ed-key-file", str(env.tmp / "key")] and sign[-1] == str(env.stage / DMG)
    assert any(c[0].endswith("sign_update") and "-p" in c and c[-1].endswith("mac-update-policy.json") for c in runner.history)
    assert any(c[0].endswith("sign_update") and "--verify" in c for c in runner.history)
    state = json.loads((env.stage / "release-state.json").read_text())
    assert state["channel"] == "preview"
    assert release.parse_appcast(env.stage / "appcast.xml")[0]["channel"] == "preview"


def test_account_signing_is_the_default(env):
    subject, runner = env.release()
    subject.execute()
    generate = next(c for c in runner.history if c[0].endswith("generate_appcast"))
    assert generate[1:3] == ["--account", "ed25519"]


@pytest.mark.parametrize(
    "matcher",
    [
        lambda c: c[:2] == ["gh", "api"],
        lambda c: c[0] == "xcodebuild" and "archive" in c,
        lambda c: c[0] == "xcodebuild" and "-exportArchive" in c,
        lambda c: c[0] == "codesign",
        lambda c: c[0] == "ditto" and "-c" in c,
        lambda c: c[:2] == ["xcrun", "notarytool"],
        lambda c: c[:3] == ["xcrun", "stapler", "staple"],
        lambda c: c[:3] == ["xcrun", "stapler", "validate"],
        lambda c: c[0] == "spctl",
        lambda c: c[0] == "ditto" and "-c" not in c,
        lambda c: c[:2] == ["hdiutil", "create"],
        lambda c: c[0] == "codesign" and c[-1].endswith(".dmg"),
        lambda c: c[:2] == ["xcrun", "notarytool"] and c[3].endswith(".dmg"),
        lambda c: c[0] == "spctl" and c[-1].endswith(".dmg"),
        lambda c: c[:2] == ["hdiutil", "attach"],
        lambda c: c[0].endswith("sign_update") and c[-1].endswith(".dmg"),
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
    assert not any(c[:3] == ["gh", "release", "create"] for c in runner.history)


def test_a_failing_git_check_stops_before_any_build(env):
    subject, runner = env.release()
    runner.fail.append(lambda c: c[0] == "git")
    with pytest.raises(release.ReleaseError):
        subject.execute()
    assert [c[0] for c in runner.history] == ["git"]


def test_remote_writes_require_publish_even_when_called_directly(env):
    runner = release.Runner(dry_run=False, publish=False, out=env.lines.append)
    for command in (["gh", "release", "create", "x"], ["gh", "release", "edit", "x"]):
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
    assert not any(c[:3] == ["gh", "release", "create"] or c[0] == "npm" for c in runner.history)
    printed = [line for line in env.lines if line.startswith("+ ")]
    assert len(printed) == len(runner.history) >= 25
    assert any("xcodebuild archive" in line for line in printed) and any("hdiutil create" in line for line in printed)
    assert any(line.startswith("link ") and line.endswith("-> /Applications") for line in env.lines)
    assert any(line.startswith("write ") for line in env.lines)
    assert env.lines[-1].startswith("Dry run:")


def test_dry_run_with_publish_prints_only_release_commands(env):
    subject, runner = env.release("--publish", dry=True)
    subject.execute()
    joined = [" ".join(c) for c in runner.history]
    assert any(c.startswith("gh release create macos-v2.0.0-71") and "--draft" in c and "--prerelease" not in c for c in joined)
    assert any(c.startswith("gh release edit macos-v2.0.0-71") and "--draft=false" in c for c in joined)
    assert not any(c.startswith(("gh pr", "npm", "git push")) or " push " in c for c in joined)
    assert not env.stage.exists()


def test_publish_uploads_site_files_as_release_assets(env):
    subject, runner = env.release("--publish", "--channel", "preview")
    subject.execute()
    create = runner.last_create()
    assert "--draft" in create and "--prerelease" in create and "--latest=false" in create
    assert create[create.index("--target") + 1] == DEEP and create[create.index("--repo") + 1] == "AstralDeep/AstralDeep"
    assert create[create.index("--notes-file") + 1] == str(env.stage / "AstralDeep-2.0.0-71-arm64.md")
    assert [path.name for path in runner.uploads] == [
        DMG, "SHA256SUMS", "release-provenance.json", "AstralDeep-2.0.0-71-arm64.md", *SITE_FILES,
    ]
    assert release.STATE_FILE not in [path.name for path in runner.uploads]
    joined = [" ".join(c) for c in runner.history]

    def first(prefix):
        return next(i for i, c in enumerate(joined) if c.startswith(prefix))

    assert first("gh release create") < first("gh release edit") < first("curl --silent --location --range")
    probes = [c[-1] for c in runner.history if c[0] == "curl" and "--range" in c]
    assert probes == [ASSETS + path.name for path in runner.uploads]
    assert not any(c[0] in {"npm", "git"} and ("push" in c or "commit" in c) for c in runner.history)
    assert not any(c[:3] == ["gh", "pr", "create"] for c in runner.history)
    assert any(line.startswith("Published macos-v2.0.0-71") for line in env.lines)


def test_stable_channel_is_not_prerelease_and_not_latest(env):
    subject, runner = env.release("--publish")
    subject.execute()
    create = runner.last_create()
    assert "--latest=false" in create and "--prerelease" not in create
    edit = next(c for c in runner.history if c[:3] == ["gh", "release", "edit"])
    assert "--prerelease" not in edit and "--latest=false" in edit
    joined = [" ".join(c) for c in runner.history]
    check = next(i for i, c in enumerate(joined) if c.startswith("gh api repos/AstralDeep/AstralDeep/releases/latest"))
    assert joined.index(" ".join(edit)) < check


@pytest.mark.parametrize(("latest", "message"), [("macos-v2.0.0-71", "became the repository's Latest release"),
                                                  ("broken", "Could not confirm the Latest release")])
def test_publish_refuses_a_release_that_became_latest(env, latest, message):
    subject, runner = env.release("--publish")
    runner.latest_tag = latest
    if latest == "broken":
        runner.fail.append(lambda c: c[:3] == ["gh", "api", "repos/AstralDeep/AstralDeep/releases/latest"])
    with pytest.raises(release.ReleaseError, match=message):
        subject.execute()
    assert not any(c[0] == "curl" and "--range" in c for c in runner.history)


def test_publish_accepts_a_repository_without_a_latest_release(env):
    subject, runner = env.release("--publish")
    runner.latest_tag = None
    subject.execute()
    assert any(line.startswith("Published macos-v2.0.0-71") for line in env.lines)


def test_provenance_names_the_workflow_run_that_published(env):
    environ = {"GITHUB_ACTIONS": "true", "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "AstralDeep/AstralDeep",
               "GITHUB_RUN_ID": "123", "GITHUB_EVENT_NAME": "workflow_run"}
    subject, _ = env.release(environ=environ)
    subject.execute()
    publication = json.loads((env.stage / "release-provenance.json").read_text())["publication"]
    assert publication["approvalSource"] == "workflow_run on main through .github/workflows/release-macos.yml"
    assert publication["workflowRun"] == "https://github.com/AstralDeep/AstralDeep/actions/runs/123"
    shutil.rmtree(env.tmp / "out")
    subject, _ = env.release(environ={})
    subject.execute()
    publication = json.loads((env.stage / "release-provenance.json").read_text())["publication"]
    assert publication["approvalSource"] == "explicit --publish by the repository owner" and "workflowRun" not in publication


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
    assert not any(c[0] == "curl" and "--range" in c for c in runner.history)


def test_publish_rejects_asset_digests_that_differ_from_the_staged_bytes(env):
    subject, runner = env.release("--publish")
    runner.altered_digest = True
    with pytest.raises(release.ReleaseError, match="does not match the staged bytes"):
        subject.execute()


def test_asset_wait_retries_until_the_url_answers(env):
    subject, runner = env.release("--publish")
    runner.probes = ["404", "000", "200"]
    subject.execute()
    assert subject.sleeps == [10.0, 10.0]
    probes = [c for c in runner.history if c[0] == "curl" and "--range" in c]
    assert probes[0][-1] == ASSETS + DMG


def test_asset_wait_gives_up_after_the_attempt_limit(env):
    subject, _ = env.release()
    subject.runner.probes = ["404"] * 5
    with pytest.raises(release.ReleaseError, match="never answered 200"):
        subject.wait_for_asset("https://example.invalid/asset", attempts=3, delay=0.5)
    assert subject.sleeps == [0.5, 0.5]


def test_publish_staged_publishes_the_exact_bytes_without_rebuilding(env):
    env.serve(FEED, feed(item(69, "1.8")))
    subject, _ = env.release()
    subject.execute()
    before = (env.stage / DMG).read_bytes()
    again, runner = env.release("--publish", "--publish-staged")
    again.execute()
    assert not any(c[0] in {"xcodebuild", "ditto", "codesign", "spctl", "hdiutil"} or c[:2] == ["xcrun", "notarytool"]
                   for c in runner.history)
    assert not any(c[0].endswith(("sign_update", "generate_appcast")) for c in runner.history)
    assert (env.stage / DMG).read_bytes() == before
    assert [c[-1] for c in fetches(runner)] == [FEED]
    assert any(c[:3] == ["gh", "release", "create"] for c in runner.history)
    assert any("Publishing the staged build" in line for line in env.lines)


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (lambda e: (e.stage / DMG).write_bytes(b"other"), "changed after it was built"),
        (lambda e: (e.stage / "SHA256SUMS").unlink(), "incomplete"),
        (lambda e: (e.stage / "downloads.json").unlink(), "incomplete"),
        (lambda e: e.serve(FEED, feed(item(69, "1.8"))), "live appcast changed"),
        (lambda e: setattr(e, "deep_sha", "c" * 40), "Deep moved"),
        (lambda e: setattr(e, "releases", [published(71, "2.0.0")]), "must exceed"),
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
        (["--minimum-build", "72"], "between 1 and the release build"),
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
    for dirty, name in (("deep", "Deep"), ("AstralProjection", "Projection")):
        subject, runner = env.release()
        runner.dirty = {dirty}
        with pytest.raises(release.ReleaseError, match=f"{name} working tree is not clean"):
            subject.preflight()
    subject, runner = env.release()
    runner.pin = "c" * 40
    with pytest.raises(release.ReleaseError, match="same commit"):
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


def test_preflight_requires_empty_directories(env):
    env.stage.mkdir(parents=True)
    (env.stage / "old.dmg").write_text("x")
    subject, _ = env.release()
    with pytest.raises(release.ReleaseError, match="is not empty"):
        subject.preflight()


def test_preflight_reports_environment_problems(env, monkeypatch):
    subject, _ = env.release()
    monkeypatch.setattr(release.platform, "system", lambda: "Linux")
    with pytest.raises(release.ReleaseError, match="built on macOS"):
        subject.preflight()
    monkeypatch.setattr(release.platform, "system", lambda: "Darwin")
    for tool in ("gh", "hdiutil", "curl"):
        monkeypatch.setattr(release.shutil, "which", lambda name, missing=tool: None if name == missing else "/bin/x")
        subject, _ = env.release()
        with pytest.raises(release.ReleaseError, match=f"Missing tools: {tool}"):
            subject.preflight()


def test_identity_checks_translate_packager_failures_and_version_drift(env, monkeypatch):
    subject, _ = env.release()
    app = env.tmp / "AstralDeep.app"

    def refuse(app_path, team):
        raise release.packager.CandidateError("App requires the expected timestamped Developer ID")

    monkeypatch.setattr(release.packager, "inspect_app", refuse)
    with pytest.raises(release.ReleaseError, match="App identity check failed"):
        subject.inspect(app)
    monkeypatch.setattr(release.packager, "inspect_app", lambda a, t: {**IDENTITY, "build": 70, "version": "1.9"})
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


def test_identity_changes_during_notarization_are_refused(env, monkeypatch):
    app = env.tmp / "work" / "AstralDeep.app"
    subject, _ = env.release()
    subject.work = env.tmp / "work"
    monkeypatch.setattr(release.packager, "inspect_app", lambda a, t: {**IDENTITY, "executableSHA256": "f" * 64})
    with pytest.raises(release.ReleaseError, match="changed while notarizing"):
        subject.notarize(app, dict(IDENTITY))


def test_stage_rejects_unusable_signing_output_and_extra_files(env):
    for output in ("garbage", 'sparkle:edSignature="abc" length="3"'):
        subject, runner = env.release()
        runner.signing_output = output
        with pytest.raises(release.ReleaseError, match="did not report the archive signature"):
            subject.execute()
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
    assert subject.tool("sign_update") == str(env.work / "dd/SourcePackages/artifacts/sparkle/Sparkle/bin/sign_update")


def test_generated_appcast_must_describe_the_archive_and_preserve_old_items(env):
    subject, runner = env.release()
    runner.generate_item = item(71, "2.0.0", None, 1, SIGNATURE, "dmg")
    with pytest.raises(release.ReleaseError, match=r"disagrees with the archive: \['length'\]"):
        subject.execute()
    shutil.rmtree(env.tmp / "out")
    env.serve(FEED, feed(item(69, "1.8", None, 111, "O" * 86 + "==")))
    subject, runner = env.release()
    original = runner._generate_appcast

    def rewrite(command, cwd):
        original(command, cwd)
        out = Path(command[command.index("-o") + 1])
        out.write_text(out.read_text().replace("O" * 86, "R" * 86))

    runner._generate_appcast = rewrite
    with pytest.raises(release.ReleaseError, match="rewrote the existing item for build 69"):
        subject.execute()


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
    env.serve(POLICY, "{")
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
    argv = ["--projection-commit", PROJECTION, "--version", "2.0.0", "--build", "71", "--notes", str(env.notes), "--dry-run"]

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
