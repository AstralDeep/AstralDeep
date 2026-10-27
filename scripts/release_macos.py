"""Builds, notarizes, signs and stages a Sparkle-updatable AstralDeep Mac disk image on the owner's Mac or in the release-macos workflow.
Nothing reaches GitHub without --publish; the downloads record, appcast and signed policy become release assets the website discovers.
Policy signatures come from Sparkle's `sign_update -p`, which emits a raw Ed25519 signature over the exact file bytes.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import importlib.util
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
TEAM_ID = "593LGH35GS"
SIGNING_IDENTITY = "Developer ID Application: Personalized AI Labs LLC (593LGH35GS)"
NOTARY_PROFILE = "astral-notary"
BUNDLE_ID = "com.personalailabs.astraldeep"
SCHEME = "AstralAppDirectDownload"
CONFIGURATION = "DirectDownload"
PROJECT = "apple-clients/AstralApp/AstralApp.xcodeproj"
PIN_PATH = "components/AstralProjection"
SPARKLE_PUBLIC_KEY = "eg4hW6POM2f99hfVp8K+U5bx5Hpl8+P+IYLuW0+pPzw="
SPARKLE_NAMESPACE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
FEED_URL = "https://astraldeep.github.io/appcast.xml"
SITE_HOST = "astraldeep.github.io"
SITE_URL = f"https://{SITE_HOST}"
POLICY_URL = f"{SITE_URL}/data/mac-update-policy.json"
LIVE_LIMIT = 2 * 1024 * 1024
SYNC_ATTEMPTS = 10
SYNC_DELAY = 60.0
RELEASE_REPO = "AstralDeep/AstralDeep"
RELEASE_ROOT = f"https://github.com/{RELEASE_REPO}/releases"
FIRST_MINIMUM_BUILD = 70
POLICY_LIFETIME = dt.timedelta(days=180)
POLICY_MESSAGE = "Update required"
STATE_FILE = "release-state.json"
SPARKLE_BIN = "SourcePackages/artifacts/sparkle/Sparkle/bin"
PLACEHOLDER_SHA = "0" * 40
VERSION = re.compile(r"[1-9]\d*\.\d+(?:\.\d+)?")
RELEASE_TAG = re.compile(rf"macos-v({VERSION.pattern})-(\d+)")
COMMIT = re.compile(r"[a-f0-9]{40}")


class ReleaseError(RuntimeError):
    pass


def _load_packager():
    path = Path(__file__).resolve().with_name("package_macos_candidate.py")
    spec = importlib.util.spec_from_file_location("package_macos_candidate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


packager = _load_packager()


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner:
    def __init__(self, *, dry_run: bool, publish: bool, out: Callable[[str], None] = print) -> None:
        self.dry_run = dry_run
        self.publish = publish
        self.out = out
        self.history: list[list[str]] = []

    def run(
        self, argv: Sequence[object], *, cwd: Path | None = None, remote_write: bool = False,
        check: bool = True, preview: str = "",
    ) -> Result:
        command = [str(part) for part in argv]
        if remote_write and not self.publish:
            raise ReleaseError(f"`{' '.join(command[:3])}` writes to a remote and requires --publish")
        self.history.append(command)
        self.out(f"+ {f'(cd {shlex.quote(str(cwd))}) ' if cwd else ''}{shlex.join(command)}")
        if self.dry_run:
            return Result(0, preview)
        result = self._execute(command, cwd)
        if check and result.returncode:
            tail = (result.stderr or result.stdout)[-1500:].strip()
            raise ReleaseError(f"{command[0]} failed with status {result.returncode}: {tail}")
        return result

    def _execute(self, command: list[str], cwd: Path | None) -> Result:
        done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
        return Result(done.returncode, done.stdout, done.stderr)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_appcast(path: Path) -> list[dict[str, object]]:
    try:
        data = path.read_bytes()
        if re.search(rb"<!(?:DOCTYPE|ENTITY)", data):
            raise ReleaseError("Appcast must not declare a document type or entities")
        root = ET.fromstring(data)
    except (ET.ParseError, OSError) as error:
        raise ReleaseError(f"Appcast is unreadable: {error}") from error
    ns = {"sparkle": SPARKLE_NAMESPACE}
    items: list[dict[str, object]] = []
    for item in root.iterfind("./channel/item"):
        enclosure = item.find("enclosure")
        version = item.findtext("sparkle:version", namespaces=ns)
        short = item.findtext("sparkle:shortVersionString", namespaces=ns)
        if enclosure is None or not version or not version.isdigit() or not short:
            raise ReleaseError("Appcast item lacks an enclosure or version")
        length = enclosure.get("length", "")
        items.append({
            "build": int(version), "version": short, "url": enclosure.get("url", ""),
            "length": int(length) if length.isdigit() else -1,
            "signature": enclosure.get(f"{{{SPARKLE_NAMESPACE}}}edSignature", ""),
            "channel": item.findtext("sparkle:channel", namespaces=ns),
        })
    if not items:
        raise ReleaseError("Appcast lists no updates")
    return items


class Release:
    def __init__(
        self, args: argparse.Namespace, runner: Runner, *, deep: Path = ROOT,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
        sleep: Callable[[float], None] = time.sleep, environ: Mapping[str, str] = os.environ,
    ) -> None:
        self.args = args
        self.environ = environ
        self.runner = runner
        self.deep = deep
        self.clock = clock
        self.sleep = sleep
        self.dry = runner.dry_run
        self.out = runner.out
        self.version: str = args.version
        self.build: int = args.build
        self.channel: str = args.channel
        self.tag = f"macos-v{self.version}-{self.build}"
        self.archive_name = f"AstralDeep-{self.version}-{self.build}-arm64.dmg"
        self.stem = self.archive_name.removesuffix(".dmg")
        self.release_url = f"{RELEASE_ROOT}/tag/{self.tag}"
        self.asset_base = f"{RELEASE_ROOT}/download/{self.tag}"
        self.asset_url = f"{self.asset_base}/{self.archive_name}"
        self.projection = args.projection or deep / PIN_PATH
        self.staging = Path(args.staging_dir or deep / "build" / "release-macos" / self.tag / "stage")
        self.work = Path(args.work_dir or self.staging.parent / "work")
        self.state: dict[str, object] = {}
        self.deep_sha = PLACEHOLDER_SHA
        self.latest: dict[str, object] | None = None
        self.live_appcast: Path | None = None

    @property
    def edkey(self) -> list[str]:
        if self.args.ed_key_file:
            return ["--ed-key-file", str(self.args.ed_key_file)]
        return ["--account", self.args.ed_account]

    def notary_args(self) -> list[str]:
        if self.args.notary_key:
            return ["--key", str(self.args.notary_key), "--key-id", self.args.notary_key_id,
                    "--issuer", self.args.notary_issuer]
        return ["--keychain-profile", NOTARY_PROFILE]

    def tool(self, name: str) -> str:
        return str(self.sparkle_bin / name)

    @property
    def sparkle_bin(self) -> Path:
        return Path(self.args.sparkle_bin) if self.args.sparkle_bin else self.work / "dd" / SPARKLE_BIN

    def git(self, repo: Path, *argv: object, check: bool = True, preview: str = "") -> Result:
        return self.runner.run(["git", "-C", repo, *argv], check=check, preview=preview)

    def validate(self) -> None:
        if not VERSION.fullmatch(self.version):
            raise ReleaseError("Version must look like 1.9 or 1.9.1")
        if self.build < 1:
            raise ReleaseError("Build must be a positive integer")
        if not COMMIT.fullmatch(self.args.projection_commit):
            raise ReleaseError("Projection commit must be a full lowercase commit hash")
        if self.channel not in {"stable", "preview"}:
            raise ReleaseError("Channel must be stable or preview")
        if self.args.ed_key_file and self.args.ed_account != "ed25519":
            raise ReleaseError("Use either --ed-account or --ed-key-file, not both")
        notary = (self.args.notary_key, self.args.notary_key_id, self.args.notary_issuer)
        if any(notary) and not all(notary):
            raise ReleaseError("--notary-key, --notary-key-id and --notary-issuer travel together")
        if self.args.minimum_build is not None and not 1 <= self.args.minimum_build <= self.build:
            raise ReleaseError("--minimum-build must be between 1 and the release build")
        notes = Path(self.args.notes)
        if notes.suffix != ".md" or not notes.is_file() or not notes.read_text(encoding="utf-8").strip():
            raise ReleaseError("Release notes must be a non-empty Markdown file")
        if len(notes.read_bytes()) > 64 * 1024:
            raise ReleaseError("Release notes exceed 64 KiB")
        if self.args.publish_staged and not (self.args.publish and self.args.staging_dir):
            raise ReleaseError("--publish-staged needs --publish and the --staging-dir of a finished build")

    def preflight(self) -> None:
        if not self.dry and platform.system() != "Darwin":
            raise ReleaseError("Mac releases are built on macOS")
        needed = ["git", "xcodebuild", "ditto", "xcrun", "codesign", "spctl", "lipo", "hdiutil", "gh", "curl"]
        missing = [name for name in needed if not self.dry and not shutil.which(name)]
        if missing:
            raise ReleaseError(f"Missing tools: {', '.join(missing)}")
        for label, repo in (("Deep", self.deep), ("Projection", self.projection)):
            if self.git(repo, "status", "--porcelain").stdout.strip():
                raise ReleaseError(f"{label} working tree is not clean")
        self.deep_sha = self.git(self.deep, "rev-parse", "HEAD", preview=PLACEHOLDER_SHA).stdout.strip()
        head = self.git(self.projection, "rev-parse", "HEAD", preview=self.args.projection_commit).stdout.strip()
        pin = self.git(self.deep, "ls-tree", "HEAD", PIN_PATH, preview=f"160000 commit {self.args.projection_commit}\t{PIN_PATH}")
        fields = pin.stdout.split()
        pinned = fields[2] if len(fields) >= 3 else ""
        if not (self.args.projection_commit == head == pinned):
            raise ReleaseError("Deep's Projection pin, the Projection checkout and --projection-commit must be the same commit")
        if not COMMIT.fullmatch(self.deep_sha):
            raise ReleaseError("Deep HEAD is not a commit")
        self.latest = self.latest_release()
        if self.latest and self.build <= int(self.latest["build"]):
            raise ReleaseError(
                f"Build {self.build} must exceed the published build {self.latest['build']} ({self.latest['tag']})")
        if self.args.publish_staged:
            return
        for path in (self.staging, self.work):
            if path.exists() and any(path.iterdir()):
                raise ReleaseError(f"{path} is not empty; choose another directory so only the newest archive is staged")

    def latest_release(self) -> dict[str, object] | None:
        listed = self.runner.run(
            ["gh", "api", "--paginate", "--slurp", f"repos/{RELEASE_REPO}/releases?per_page=100"], preview="[]")
        newest: dict[str, object] | None = None
        try:
            for entry in (entry for page in json.loads(listed.stdout) for entry in page):
                match = RELEASE_TAG.fullmatch(str(entry["tag_name"]))
                if entry["draft"] or not match:
                    continue
                build = int(match.group(2))
                if newest is None or build > int(newest["build"]):
                    newest = {
                        "tag": match.group(0), "version": match.group(1), "build": build,
                        "target": str(entry.get("target_commitish", "")),
                        "assets": {str(asset["name"]): asset.get("digest") for asset in entry.get("assets", [])},
                    }
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise ReleaseError(f"GitHub release list is unreadable: {error}") from error
        return newest

    def fetch_live(self, url: str, destination: Path) -> bool:
        if not self.dry:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.unlink(missing_ok=True)
        fetched = self.runner.run([
            "curl", "--silent", "--show-error", "--location", "--proto", "=https", "--proto-redir", "=https",
            "--max-redirs", "2", "--max-filesize", str(LIVE_LIMIT), "--fail", "--output", destination,
            "--write-out", "%{http_code} %{url_effective}", url,
        ], check=False, preview=f"404 {url}")
        code, _, final = fetched.stdout.strip().partition(" ")
        where = urlsplit(final)
        if where.scheme != "https" or where.hostname != SITE_HOST:
            problem = f"ended at {final or 'nothing'}, outside {SITE_HOST}"
        elif code == "404":
            return False
        elif fetched.returncode == 63:
            problem = f"exceeds {LIVE_LIMIT} bytes"
        elif fetched.returncode or code != "200":
            problem = f"answered {code or 'nothing'} (curl status {fetched.returncode})"
        elif not self.dry and (not destination.is_file() or destination.stat().st_size > LIVE_LIMIT):
            problem = "body is missing or too large"
        else:
            return True
        if not self.dry:
            destination.unlink(missing_ok=True)
        raise ReleaseError(f"{url} {problem}")

    def fetch_synced(self, url: str, destination: Path, name: str) -> bool:
        assets = self.latest["assets"] if self.latest else {}
        if name not in assets or self.dry:
            return self.fetch_live(url, destination)
        if not assets[name]:
            raise ReleaseError(f"{self.latest['tag']} lists {name} without a digest; its successor cannot be checked")
        found = False
        for attempt in range(SYNC_ATTEMPTS):
            found = self.fetch_live(url, destination)
            if found and f"sha256:{sha256_file(destination)}" == assets[name]:
                return True
            if attempt + 1 < SYNC_ATTEMPTS:
                self.sleep(SYNC_DELAY)
        if not found:
            raise ReleaseError(f"The site serves no {name} although {self.latest['tag']} published one; refusing to drop it")
        raise ReleaseError(f"The site still serves an older {name} than {self.latest['tag']}; rerun once the site syncs")

    def inspect(self, app: Path) -> dict[str, object]:
        if self.dry:
            return {
                "version": self.version, "build": self.build, "minimumMacOS": "14.0", "architecture": "arm64",
                "bundleID": BUNDLE_ID, "executableSHA256": "0" * 64, "teamID": TEAM_ID,
            }
        try:
            identity = packager.inspect_app(app, TEAM_ID)
        except (packager.CandidateError, OSError, plistlib.InvalidFileException) as error:
            raise ReleaseError(f"App identity check failed: {error}") from error
        expected = {"version": self.version, "build": self.build, "architecture": "arm64"}
        wrong = {key: identity.get(key) for key, value in expected.items() if identity.get(key) != value}
        if wrong:
            raise ReleaseError(f"Built app differs from the requested release: {wrong}")
        return identity

    def verify_sparkle(self, app: Path) -> None:
        framework = app / "Contents/Frameworks/Sparkle.framework/Versions/B"
        if not self.dry:
            info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
            if info.get("SUPublicEDKey") != SPARKLE_PUBLIC_KEY or info.get("SUFeedURL") != FEED_URL:
                raise ReleaseError("Built app does not carry the expected Sparkle feed and public key")
            if info.get("SUVerifyUpdateBeforeExtraction") is not True or "SUAllowsAutomaticUpdates" in info:
                raise ReleaseError("Built app has unexpected Sparkle verification settings")
        for helper in ("Autoupdate", "Updater.app"):
            shown = self.runner.run(
                ["codesign", "--display", "--verbose=2", framework / helper],
                preview=f"TeamIdentifier={TEAM_ID}")
            if f"TeamIdentifier={TEAM_ID}" not in shown.stdout + shown.stderr:
                raise ReleaseError(f"Sparkle {helper} is not signed by team {TEAM_ID}")

    def zip(self, app: Path, destination: Path) -> None:
        self.runner.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, destination])

    def submit(self, path: Path) -> str:
        sent = self.runner.run(
            ["xcrun", "notarytool", "submit", path, *self.notary_args(), "--wait", "--output-format", "json"],
            preview=json.dumps({"id": "<submission-id>", "status": "Accepted"}))
        try:
            outcome = json.loads(sent.stdout)
            status, identifier = str(outcome["status"]), str(outcome["id"])
        except (ValueError, KeyError, TypeError) as error:
            raise ReleaseError("notarytool returned no submission status") from error
        if status != "Accepted":
            raise ReleaseError(f"Notarization {identifier} ended as {status}; see `xcrun notarytool log {identifier}`")
        return identifier

    def staple(self, path: Path) -> None:
        self.runner.run(["xcrun", "stapler", "staple", path])
        self.validate_ticket(path)

    def validate_ticket(self, path: Path) -> None:
        validated = self.runner.run(["xcrun", "stapler", "validate", path], preview="The validate action worked!")
        if "worked" not in validated.stdout:
            raise ReleaseError("Stapled ticket did not validate")

    def build_app(self) -> tuple[Path, dict[str, object]]:
        archive = self.work / "AstralDeep.xcarchive"
        export = self.work / "export"
        options = self.work / "export-options.plist"
        if not self.dry:
            self.work.mkdir(parents=True, exist_ok=True)
            options.write_bytes(plistlib.dumps({
                "method": "developer-id", "teamID": TEAM_ID, "signingStyle": "manual",
                "signingCertificate": "Developer ID Application", "destination": "export",
            }))
        self.runner.run([
            "xcodebuild", "archive", "-project", self.projection / PROJECT, "-scheme", SCHEME,
            "-configuration", CONFIGURATION, "-destination", "generic/platform=macOS",
            "-archivePath", archive, "-derivedDataPath", self.work / "dd",
            f"ASTRAL_DEVELOPMENT_TEAM={TEAM_ID}", "CODE_SIGN_STYLE=Manual", f"CODE_SIGN_IDENTITY={SIGNING_IDENTITY}",
            "ARCHS=arm64", f"MARKETING_VERSION={self.version}", f"CURRENT_PROJECT_VERSION={self.build}",
        ])
        self.runner.run([
            "xcodebuild", "-exportArchive", "-archivePath", archive, "-exportPath", export,
            "-exportOptionsPlist", options,
        ])
        app = export / "AstralDeep.app"
        identity = self.inspect(app)
        self.verify_sparkle(app)
        return app, identity

    def notarize(self, app: Path, identity: dict[str, object]) -> dict[str, str]:
        submission = self.work / "notarize" / f"{self.stem}.zip"
        if not self.dry:
            submission.parent.mkdir(parents=True, exist_ok=True)
        self.zip(app, submission)
        identifier = self.submit(submission)
        self.staple(app)
        gatekeeper = self.assess_app(app)
        if self.inspect(app) != identity:
            raise ReleaseError("App identity changed while notarizing")
        return {"submission": identifier, "gatekeeper": gatekeeper}

    def assess_app(self, app: Path) -> str:
        return self.gatekeeper(self.runner.run(
            ["spctl", "--assess", "--type", "execute", "--verbose=4", app],
            preview=f"{app}: accepted\nsource=Notarized Developer ID"))

    @staticmethod
    def gatekeeper(assessed: Result) -> str:
        text = assessed.stdout + assessed.stderr
        source = re.search(r"source=(.+)", text)
        if "accepted" not in text or not source or "Notarized Developer ID" not in source.group(1):
            raise ReleaseError("Gatekeeper did not accept the app as Notarized Developer ID")
        return f"accepted: {source.group(1).strip()}"

    def package(self, app: Path, identity: dict[str, object]) -> tuple[Path, dict[str, str]]:
        volume = self.work / "volume"
        mount = self.work / "mount"
        image = self.staging / self.archive_name
        if not self.dry:
            for path in (self.staging, volume, mount):
                path.mkdir(parents=True, exist_ok=True)
        self.runner.run(["ditto", app, volume / "AstralDeep.app"])
        if self.dry:
            self.out(f"link {volume}/Applications -> /Applications")
        else:
            (volume / "Applications").symlink_to("/Applications", target_is_directory=True)
        self.runner.run([
            "hdiutil", "create", "-volname", "AstralDeep", "-srcfolder", volume, "-fs", "HFS+", "-format", "UDZO", image,
        ])
        self.runner.run(["codesign", "--sign", SIGNING_IDENTITY, "--timestamp", image])
        identifier = self.submit(image)
        self.staple(image)
        gatekeeper = self.gatekeeper(self.runner.run(
            ["spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=4", image],
            preview=f"{image}: accepted\nsource=Notarized Developer ID"))
        self.runner.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, image])
        try:
            mounted = mount / "AstralDeep.app"
            if self.inspect(mounted) != identity:
                raise ReleaseError("The app inside the disk image differs from the notarized app")
            self.assess_app(mounted)
            self.validate_ticket(mounted)
        finally:
            detached = self.runner.run(["hdiutil", "detach", mount], check=False)
        if detached.returncode:
            raise ReleaseError(f"{mount} could not be detached")
        return image, {"submission": identifier, "gatekeeper": gatekeeper}

    def stage(self, archive: Path) -> dict[str, object]:
        notes = self.staging / f"{self.stem}.md"
        appcast = self.staging / "appcast.xml"
        live = self.work / "live" / "appcast.xml"
        digest = "0" * 64 if self.dry else sha256_file(archive)
        size = 1 if self.dry else archive.stat().st_size
        if not self.dry:
            missing = [name for name in ("sign_update", "generate_appcast") if not Path(self.tool(name)).is_file()]
            if missing:
                raise ReleaseError(f"Sparkle tools not found in {self.sparkle_bin}: {missing}; pass --sparkle-bin")
        found = self.fetch_synced(FEED_URL, live, "appcast.xml")
        self.live_appcast = live if found else None
        if not self.dry:
            shutil.copyfile(self.args.notes, notes)
            if found:
                shutil.copyfile(live, appcast)
            self.state["liveAppcastSha256"] = sha256_file(live) if found else None
        signed = self.runner.run(
            [self.tool("sign_update"), *self.edkey, archive],
            preview=f'sparkle:edSignature="{"A" * 86}==" length="{size}"')
        match = re.search(r'sparkle:edSignature="([^"]+)" length="(\d+)"', signed.stdout)
        if not match or int(match.group(2)) != size:
            raise ReleaseError("sign_update did not report the archive signature and length")
        signature = match.group(1)
        command = [
            self.tool("generate_appcast"), *self.edkey, "--download-url-prefix", f"{self.asset_base}/",
            "--link", SITE_URL, "--maximum-versions", "5", "--maximum-deltas", "0", "--embed-release-notes",
        ]
        if self.channel == "preview":
            command += ["--channel", "preview"]
        self.runner.run([*command, "-o", appcast, self.staging])
        if not self.dry:
            self.check_stage(archive, appcast, signature, size)
        sums = self.staging / "SHA256SUMS"
        if not self.dry:
            sums.write_text(f"{digest}  {self.archive_name}\n", encoding="utf-8")
        return {"sha256": digest, "bytes": size, "signature": signature}

    def check_stage(self, archive: Path, appcast: Path, signature: str, size: int) -> None:
        expected = {archive.name, f"{self.stem}.md", "appcast.xml"}
        present = {entry.name for entry in self.staging.iterdir()}
        if present != expected:
            raise ReleaseError(f"Staging must hold only the newest archive, its notes and the appcast: {sorted(present ^ expected)}")
        items = parse_appcast(appcast)
        newest = max(items, key=lambda item: item["build"])
        want = {
            "build": self.build, "version": self.version, "url": self.asset_url, "length": size,
            "signature": signature, "channel": "preview" if self.channel == "preview" else None,
        }
        wrong = [key for key, value in want.items() if newest.get(key) != value]
        if wrong:
            raise ReleaseError(f"Generated appcast newest item disagrees with the archive: {wrong}")
        if self.live_appcast is not None:
            kept = {item["build"]: item for item in items}
            for old in parse_appcast(self.live_appcast):
                now = kept.get(old["build"])
                if now is not None and (now["url"], now["length"], now["signature"]) != (old["url"], old["length"], old["signature"]):
                    raise ReleaseError(f"generate_appcast rewrote the existing item for build {old['build']}")

    def live_policy(self) -> dict[str, object] | None:
        path = self.work / "live" / "mac-update-policy.json"
        found = self.fetch_synced(POLICY_URL, path, "mac-update-policy.json")
        if not found:
            return None
        try:
            policy = json.loads(path.read_text(encoding="utf-8"))
            return {"minimumBuild": int(policy["minimumBuild"]), "serial": int(policy["serial"])}
        except (ValueError, KeyError, TypeError) as error:
            raise ReleaseError(f"Live update policy is unreadable: {error}") from error

    def policy(self) -> dict[str, object]:
        live = self.live_policy()
        minimum = self.args.minimum_build
        if minimum is None:
            minimum = int(live["minimumBuild"]) if live else FIRST_MINIMUM_BUILD
        if minimum > self.build:
            raise ReleaseError("The policy minimum build exceeds the release build")
        expires = self.args.policy_expires or (self.clock() + POLICY_LIFETIME)
        if expires.tzinfo is None or expires <= self.clock():
            raise ReleaseError("--policy-expires must be a future UTC timestamp")
        body = json.dumps({
            "schemaVersion": 1, "minimumBuild": minimum, "serial": int(live["serial"]) + 1 if live else 1,
            "expires": expires.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "message": POLICY_MESSAGE,
        }) + "\n"
        path = self.staging / "mac-update-policy.json"
        if not self.dry:
            path.write_text(body, encoding="utf-8")
        signed = self.runner.run(
            [self.tool("sign_update"), *self.edkey, "-p", path], preview="A" * 86 + "==")
        signature = signed.stdout.strip()
        try:
            valid = len(base64.b64decode(signature, validate=True)) == 64
        except ValueError:
            valid = False
        if not valid:
            raise ReleaseError("sign_update returned a malformed policy signature")
        if not self.dry:
            (self.staging / "mac-update-policy.json.sig").write_text(signature + "\n", encoding="utf-8")
        self.runner.run([self.tool("sign_update"), *self.edkey, "--verify", path, signature])
        return json.loads(body)

    def downloads_record(self, identity: dict[str, object], archive: dict[str, object]) -> dict[str, object]:
        return {"schemaVersion": 1, "macos": {
            "version": self.version, "build": self.build, "minimumMacOS": identity["minimumMacOS"],
            "architecture": "arm64", "format": "dmg", "bytes": archive["bytes"], "sha256": archive["sha256"],
            "url": self.asset_url, "releaseURL": self.release_url, "checksumsURL": f"{self.asset_base}/SHA256SUMS",
            "source": {"deep": self.deep_sha, "projection": self.args.projection_commit}, "channel": self.channel,
        }}

    def provenance(
        self, identity: dict[str, object], archive: dict[str, object], apple: dict[str, str], image: dict[str, str],
    ) -> dict[str, object]:
        return {
            "schemaVersion": 1, "product": "AstralDeep", "version": self.version, "build": self.build,
            "bundleIdentifier": BUNDLE_ID, "minimumMacOS": identity["minimumMacOS"], "architecture": "arm64",
            "distribution": CONFIGURATION, "channel": self.channel, "tag": self.tag,
            "source": {"deep": self.deep_sha, "projection": self.args.projection_commit},
            "archive": {"filename": self.archive_name, "bytes": archive["bytes"], "sha256": archive["sha256"]},
            "appExecutableSha256": identity["executableSHA256"],
            "apple": {
                "teamIdentifier": TEAM_ID, "notarizationSubmission": apple["submission"],
                "diskImageNotarizationSubmission": image["submission"],
                "stapledTicketValidation": "passed", "strictSignatureValidation": "passed",
                "gatekeeperAssessment": apple["gatekeeper"], "diskImageGatekeeperAssessment": image["gatekeeper"],
            },
            "verification": {
                "diskImageMountedIdentity": "passed", "sparkleEmbeddedKeyAndFeed": "passed",
                "appcastEnclosureSignature": "passed", "updatePolicySignature": "passed",
            },
            "publication": {
                "method": "scripts/release_macos.py --publish", **self.approval(),
                "recordedAt": self.clock().isoformat(), "canonicalProtectedPublisherApproval": False,
                "broaderComputerHarnessQualification": False,
                "scope": "This exact archive, its Sparkle appcast item and the signed minimum-build policy only",
            },
            "limitations": [
                "Apple silicon only; macOS 14 or later.",
                "Published by the owner's Mac or the release-macos workflow; both verify the same identities.",
            ],
        }

    def approval(self) -> dict[str, str]:
        if self.environ.get("GITHUB_ACTIONS") != "true":
            return {"approvalSource": "explicit --publish by the repository owner"}
        run = "/".join(self.environ.get(key, "") for key in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY"))
        return {
            "approvalSource": f"{self.environ.get('GITHUB_EVENT_NAME', '')} on main through .github/workflows/release-macos.yml",
            "workflowRun": f"{run}/actions/runs/{self.environ.get('GITHUB_RUN_ID', '')}",
        }

    def write_outputs(
        self, identity: dict[str, object], archive: dict[str, object], apple: dict[str, str], image: dict[str, str],
        policy: dict[str, object],
    ) -> None:
        provenance = self.provenance(identity, archive, apple, image)
        downloads = self.downloads_record(identity, archive)
        self.state.update({
            "version": self.version, "build": self.build, "channel": self.channel, "tag": self.tag,
            "deep": self.deep_sha, "projection": self.args.projection_commit, "archive": archive, "policy": policy,
        })
        if self.dry:
            self.out(f"write {self.staging}/release-provenance.json, downloads.json, {STATE_FILE}")
            return
        for name, data in (("release-provenance.json", provenance), ("downloads.json", downloads), (STATE_FILE, self.state)):
            (self.staging / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def load_staged(self) -> None:
        self.state = json.loads((self.staging / STATE_FILE).read_text(encoding="utf-8")) if not self.dry else {}
        if self.dry:
            return
        archive = self.staging / self.archive_name
        absent = [path.name for path in self.release_assets() if not path.is_file()]
        if absent:
            raise ReleaseError(f"Staged build is incomplete: {absent}")
        recorded = self.state.get("archive") or {}
        same = (self.state.get("version"), self.state.get("build"), self.state.get("channel")) == (self.version, self.build, self.channel)
        if not same or self.state.get("projection") != self.args.projection_commit or not archive.is_file():
            raise ReleaseError("Staged build does not match the requested release")
        if sha256_file(archive) != recorded.get("sha256") or archive.stat().st_size != recorded.get("bytes"):
            raise ReleaseError("Staged archive changed after it was built")
        if self.state.get("deep") != self.deep_sha:
            raise ReleaseError("Deep moved since the build; rebuild instead of publishing a stale archive")
        live = self.work / "live" / "appcast-now.xml"
        now = sha256_file(live) if self.fetch_live(FEED_URL, live) else None
        if now != self.state.get("liveAppcastSha256"):
            raise ReleaseError("The live appcast changed since staging; rebuild so older items are preserved")

    def wait_for_asset(self, url: str, attempts: int = 30, delay: float = 10.0) -> None:
        for attempt in range(attempts):
            probe = self.runner.run(
                ["curl", "--silent", "--location", "--range", "0-0", "--output", "/dev/null", "--write-out", "%{http_code}", url],
                check=False, preview="206")
            if probe.stdout.strip() in {"200", "206"}:
                return
            if attempt + 1 < attempts:
                self.sleep(delay)
        raise ReleaseError(f"{url} never answered 200")

    def release_assets(self) -> list[Path]:
        names = [
            self.archive_name, "SHA256SUMS", "release-provenance.json", f"{self.stem}.md", "downloads.json",
            "appcast.xml", "mac-update-policy.json", "mac-update-policy.json.sig",
        ]
        return [self.staging / name for name in names]

    def publish_release(self) -> None:
        run = self.runner.run
        existing = run(["gh", "release", "view", self.tag, "--repo", RELEASE_REPO], check=False, preview="release not found")
        if existing.returncode == 0 and not self.dry:
            raise ReleaseError(f"Release {self.tag} already exists; immutable releases cannot be replaced")
        run(["gh", "api", f"repos/{RELEASE_REPO}/commits/{self.deep_sha}", "--jq", ".sha"], preview=self.deep_sha)
        assets = self.release_assets()
        create = ["gh", "release", "create", self.tag, "--repo", RELEASE_REPO, "--target", self.deep_sha,
                  "--title", f"AstralDeep {self.version} ({self.build})", "--notes-file", self.staging / f"{self.stem}.md",
                  "--draft", "--latest=false"]
        if self.channel == "preview":
            create.append("--prerelease")
        run([*create, *assets], remote_write=True)
        edit = ["gh", "release", "edit", self.tag, "--repo", RELEASE_REPO, "--draft=false", "--latest=false"]
        if self.channel == "preview":
            edit.append("--prerelease")
        run(edit, remote_write=True)
        info = run(["gh", "api", f"repos/{RELEASE_REPO}/releases/tags/{self.tag}"],
                   preview=json.dumps({"immutable": True, "draft": False, "prerelease": self.channel == "preview", "assets": []}))
        latest = run(["gh", "api", f"repos/{RELEASE_REPO}/releases/latest", "--jq", ".tag_name"], check=False)
        if self.dry:
            return
        if latest.returncode and "HTTP 404" not in latest.stderr:
            raise ReleaseError(f"Could not confirm the Latest release: {latest.stderr.strip()[-300:]}")
        if not latest.returncode and latest.stdout.strip() == self.tag:
            raise ReleaseError(f"{self.tag} became the repository's Latest release; mark the newest Windows release Latest again")
        published = json.loads(info.stdout)
        if published.get("immutable") is not True or published.get("draft") is not False \
                or published.get("prerelease") is not (self.channel == "preview"):
            raise ReleaseError("The published release is not an immutable release of the expected kind")
        digests = {asset["name"]: asset.get("digest") for asset in published.get("assets", [])}
        for path in assets:
            if digests.get(path.name) != f"sha256:{sha256_file(path)}":
                raise ReleaseError(f"Uploaded asset {path.name} does not match the staged bytes")
        for path in assets:
            self.wait_for_asset(f"{self.asset_base}/{path.name}")

    def execute(self) -> Path:
        self.validate()
        self.preflight()
        if self.args.publish_staged:
            self.load_staged()
            self.out(f"Publishing the staged build in {self.staging}")
        else:
            app, identity = self.build_app()
            apple = self.notarize(app, identity)
            archive_path, image = self.package(app, identity)
            archive = self.stage(archive_path)
            policy = self.policy()
            self.write_outputs(identity, archive, apple, image, policy)
        if not self.args.publish:
            self.out(f"Staged {self.tag} in {self.staging}; nothing was published.")
            self.out("Publish exactly these bytes with the same arguments plus --publish --publish-staged.")
        else:
            self.publish_release()
            self.out(f"Published {self.tag} at {self.release_url}; the website discovers it from the release assets.")
        if self.dry:
            self.out("Dry run: the commands above were printed, not executed.")
        return self.staging


def parse_expiry(text: str) -> dt.datetime:
    try:
        parsed = dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise argparse.ArgumentTypeError("use UTC like 2027-01-01T00:00:00Z") from error
    return parsed.replace(tzinfo=dt.timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projection-commit", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", required=True, type=int)
    parser.add_argument("--channel", choices=("stable", "preview"), default="stable")
    parser.add_argument("--notes", required=True, type=Path)
    parser.add_argument("--ed-account", default="ed25519")
    parser.add_argument("--ed-key-file", type=Path)
    parser.add_argument("--notary-key", type=Path)
    parser.add_argument("--notary-key-id")
    parser.add_argument("--notary-issuer")
    parser.add_argument("--minimum-build", type=int)
    parser.add_argument("--policy-expires", type=parse_expiry)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--publish-staged", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--staging-dir", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--projection", type=Path)
    parser.add_argument("--sparkle-bin", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = Runner(dry_run=args.dry_run, publish=args.publish)
    try:
        Release(args, runner).execute()
    except ReleaseError as error:
        print(f"Mac release failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
