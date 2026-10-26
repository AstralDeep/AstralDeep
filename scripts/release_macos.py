"""Builds, notarizes, signs and stages a Sparkle-updatable AstralDeep Mac release on the owner's Mac.
Nothing reaches GitHub without --publish; the website change is committed on a new branch and offered as a pull request.
Policy signatures come from Sparkle's `sign_update -p`, which emits a raw Ed25519 signature over the exact file bytes.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import importlib.util
import json
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

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
SITE_URL = "https://astraldeep.github.io"
RELEASE_REPO = "AstralDeep/AstralDeep"
SITE_REPO = "AstralDeep/astraldeep.github.io"
RELEASE_ROOT = f"https://github.com/{RELEASE_REPO}/releases"
FIRST_MINIMUM_BUILD = 70
POLICY_LIFETIME = dt.timedelta(days=180)
POLICY_MESSAGE = "Update required"
STATE_FILE = "release-state.json"
SPARKLE_BIN = "SourcePackages/artifacts/sparkle/Sparkle/bin"
PLACEHOLDER_SHA = "0" * 40
VERSION = re.compile(r"[1-9]\d*\.\d+(?:\.\d+)?")
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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.args = args
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
        self.archive_name = f"AstralDeep-{self.version}-{self.build}-arm64.zip"
        self.stem = self.archive_name.removesuffix(".zip")
        self.release_url = f"{RELEASE_ROOT}/tag/{self.tag}"
        self.asset_base = f"{RELEASE_ROOT}/download/{self.tag}"
        self.asset_url = f"{self.asset_base}/{self.archive_name}"
        self.projection = args.projection or deep / PIN_PATH
        self.website = args.website or deep.parent / "astraldeep.github.io"
        self.staging = Path(args.staging_dir or deep / "build" / "release-macos" / self.tag / "stage")
        self.work = Path(args.work_dir or self.staging.parent / "work")
        self.state: dict[str, object] = {}
        self.deep_sha = PLACEHOLDER_SHA
        self.live_appcast: Path | None = None

    @property
    def edkey(self) -> list[str]:
        if self.args.ed_key_file:
            return ["--ed-key-file", str(self.args.ed_key_file)]
        return ["--account", self.args.ed_account]

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
        needed = ["git", "xcodebuild", "ditto", "xcrun", "codesign", "spctl", "lipo"]
        if self.args.publish:
            needed += ["gh", "npm", "curl"]
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
        if self.args.publish:
            if self.git(self.website, "status", "--porcelain").stdout.strip():
                raise ReleaseError("Website working tree is not clean")
        live = self.live_downloads()
        if live and self.build <= int(live["build"]):
            raise ReleaseError(f"Build {self.build} must exceed the published build {live['build']}")
        if self.args.publish_staged:
            return
        for path in (self.staging, self.work):
            if path.exists() and any(path.iterdir()):
                raise ReleaseError(f"{path} is not empty; choose another directory so only the newest archive is staged")

    def live_downloads(self) -> dict[str, object] | None:
        path = self.website / "data" / "downloads.json"
        if self.dry and not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("macos")
        except (OSError, ValueError) as error:
            raise ReleaseError(f"Website downloads record is unreadable: {error}") from error

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
        submission = self.work / "notarize" / self.archive_name
        if not self.dry:
            submission.parent.mkdir(parents=True, exist_ok=True)
        self.zip(app, submission)
        sent = self.runner.run(
            ["xcrun", "notarytool", "submit", submission, "--keychain-profile", NOTARY_PROFILE, "--wait",
             "--output-format", "json"],
            preview=json.dumps({"id": "<submission-id>", "status": "Accepted"}))
        try:
            outcome = json.loads(sent.stdout)
            status, identifier = str(outcome["status"]), str(outcome["id"])
        except (ValueError, KeyError, TypeError) as error:
            raise ReleaseError("notarytool returned no submission status") from error
        if status != "Accepted":
            raise ReleaseError(f"Notarization {identifier} ended as {status}; see `xcrun notarytool log {identifier}`")
        self.runner.run(["xcrun", "stapler", "staple", app])
        validated = self.runner.run(["xcrun", "stapler", "validate", app], preview="The validate action worked!")
        if "worked" not in validated.stdout:
            raise ReleaseError("Stapled ticket did not validate")
        assessed = self.runner.run(
            ["spctl", "--assess", "--type", "execute", "--verbose=4", app],
            preview=f"{app}: accepted\nsource=Notarized Developer ID")
        gatekeeper = self.gatekeeper(assessed)
        if self.inspect(app) != identity:
            raise ReleaseError("App identity changed while notarizing")
        return {"submission": identifier, "gatekeeper": gatekeeper}

    @staticmethod
    def gatekeeper(assessed: Result) -> str:
        text = assessed.stdout + assessed.stderr
        source = re.search(r"source=(.+)", text)
        if "accepted" not in text or not source or "Notarized Developer ID" not in source.group(1):
            raise ReleaseError("Gatekeeper did not accept the app as Notarized Developer ID")
        return f"accepted: {source.group(1).strip()}"

    def package(self, app: Path, identity: dict[str, object]) -> Path:
        if not self.dry:
            self.staging.mkdir(parents=True, exist_ok=True)
        archive = self.staging / self.archive_name
        self.zip(app, archive)
        extracted = self.work / "extract"
        if not self.dry:
            extracted.mkdir(parents=True, exist_ok=True)
        self.runner.run(["ditto", "-x", "-k", archive, extracted])
        copy = extracted / "AstralDeep.app"
        if self.inspect(copy) != identity:
            raise ReleaseError("Fresh extraction of the archive differs from the notarized app")
        self.gatekeeper(self.runner.run(
            ["spctl", "--assess", "--type", "execute", "--verbose=4", copy],
            preview=f"{copy}: accepted\nsource=Notarized Developer ID"))
        self.runner.run(["xcrun", "stapler", "validate", copy], preview="The validate action worked!")
        return archive

    def stage(self, archive: Path) -> dict[str, object]:
        notes = self.staging / f"{self.stem}.md"
        appcast = self.staging / "appcast.xml"
        live = self.website / "site" / "appcast.xml"
        digest = "0" * 64 if self.dry else sha256_file(archive)
        size = 1 if self.dry else archive.stat().st_size
        if not self.dry:
            missing = [name for name in ("sign_update", "generate_appcast") if not Path(self.tool(name)).is_file()]
            if missing:
                raise ReleaseError(f"Sparkle tools not found in {self.sparkle_bin}: {missing}; pass --sparkle-bin")
            shutil.copyfile(self.args.notes, notes)
            if live.is_file():
                shutil.copyfile(live, appcast)
                self.state["liveAppcastSha256"] = sha256_file(live)
            else:
                self.state["liveAppcastSha256"] = None
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
        live = self.website / "site" / "appcast.xml"
        if live.is_file():
            kept = {item["build"]: item for item in items}
            for old in parse_appcast(live):
                now = kept.get(old["build"])
                if now is not None and (now["url"], now["length"], now["signature"]) != (old["url"], old["length"], old["signature"]):
                    raise ReleaseError(f"generate_appcast rewrote the existing item for build {old['build']}")

    def live_policy(self) -> dict[str, object] | None:
        path = self.website / "site" / "data" / "mac-update-policy.json"
        if not path.is_file():
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
            "architecture": "arm64", "format": "zip", "bytes": archive["bytes"], "sha256": archive["sha256"],
            "url": self.asset_url, "releaseURL": self.release_url, "checksumsURL": f"{self.asset_base}/SHA256SUMS",
            "source": {"deep": self.deep_sha, "projection": self.args.projection_commit}, "channel": self.channel,
        }}

    def provenance(self, identity: dict[str, object], archive: dict[str, object], apple: dict[str, str]) -> dict[str, object]:
        return {
            "schemaVersion": 1, "product": "AstralDeep", "version": self.version, "build": self.build,
            "bundleIdentifier": BUNDLE_ID, "minimumMacOS": identity["minimumMacOS"], "architecture": "arm64",
            "distribution": CONFIGURATION, "channel": self.channel, "tag": self.tag,
            "source": {"deep": self.deep_sha, "projection": self.args.projection_commit},
            "archive": {"filename": self.archive_name, "bytes": archive["bytes"], "sha256": archive["sha256"]},
            "appExecutableSha256": identity["executableSHA256"],
            "apple": {
                "teamIdentifier": TEAM_ID, "notarizationSubmission": apple["submission"],
                "stapledTicketValidation": "passed", "strictSignatureValidation": "passed",
                "gatekeeperAssessment": apple["gatekeeper"],
            },
            "verification": {
                "archiveFreshExtraction": "passed", "sparkleEmbeddedKeyAndFeed": "passed",
                "appcastEnclosureSignature": "passed", "updatePolicySignature": "passed",
            },
            "publication": {
                "method": "scripts/release_macos.py --publish", "approvalSource": "explicit --publish by the repository owner",
                "recordedAt": self.clock().isoformat(), "canonicalProtectedPublisherApproval": False,
                "broaderComputerHarnessQualification": False,
                "scope": "This exact archive, its Sparkle appcast item and the signed minimum-build policy only",
            },
            "limitations": [
                "Apple silicon only; macOS 14 or later.",
                "Published from the owner's Mac; the protected Mac publisher is not yet configured.",
            ],
        }

    def write_outputs(
        self, identity: dict[str, object], archive: dict[str, object], apple: dict[str, str], policy: dict[str, object],
    ) -> None:
        provenance = self.provenance(identity, archive, apple)
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
        required = [
            self.archive_name, f"{self.stem}.md", "appcast.xml", "SHA256SUMS", "release-provenance.json",
            "downloads.json", "mac-update-policy.json", "mac-update-policy.json.sig",
        ]
        absent = [name for name in required if not (self.staging / name).is_file()]
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
        live = self.website / "site" / "appcast.xml"
        now = sha256_file(live) if live.is_file() else None
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

    def publish_release(self) -> None:
        run = self.runner.run
        existing = run(["gh", "release", "view", self.tag, "--repo", RELEASE_REPO], check=False, preview="release not found")
        if existing.returncode == 0 and not self.dry:
            raise ReleaseError(f"Release {self.tag} already exists; immutable releases cannot be replaced")
        run(["gh", "api", f"repos/{RELEASE_REPO}/commits/{self.deep_sha}", "--jq", ".sha"], preview=self.deep_sha)
        assets = [self.staging / self.archive_name, self.staging / "SHA256SUMS", self.staging / "release-provenance.json"]
        create = ["gh", "release", "create", self.tag, "--repo", RELEASE_REPO, "--target", self.deep_sha,
                  "--title", f"AstralDeep {self.version} ({self.build})", "--notes-file", self.staging / f"{self.stem}.md",
                  "--draft", "--latest=false"]
        if self.channel == "preview":
            create.append("--prerelease")
        run([*create, *assets], remote_write=True)
        edit = ["gh", "release", "edit", self.tag, "--repo", RELEASE_REPO, "--draft=false"]
        if self.channel == "preview":
            edit.append("--prerelease")
        run(edit, remote_write=True)
        info = run(["gh", "api", f"repos/{RELEASE_REPO}/releases/tags/{self.tag}"],
                   preview=json.dumps({"immutable": True, "draft": False, "prerelease": self.channel == "preview", "assets": []}))
        if self.dry:
            return
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

    def publish_website(self) -> str:
        site = self.website
        branch = f"release/{self.tag}"
        self.git(site, "fetch", "origin", "main")
        self.git(site, "switch", "-c", branch, "origin/main")
        targets = {
            "appcast.xml": "site/appcast.xml", "downloads.json": "data/downloads.json",
            "mac-update-policy.json": "site/data/mac-update-policy.json",
            "mac-update-policy.json.sig": "site/data/mac-update-policy.json.sig",
        }
        if not self.dry:
            for source, target in targets.items():
                destination = site / target
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.staging / source, destination)
        else:
            self.out(f"copy {', '.join(targets)} from {self.staging} into {site}")
        for script in ("test", "run check", "run build"):
            self.runner.run(["npm", *script.split()], cwd=site)
        self.git(site, "add", *targets.values())
        self.git(site, "commit", "-m", f"Publish AstralDeep {self.version} ({self.build}) for Mac updates")
        if self.args.no_pull_request:
            return branch
        self.runner.run(["git", "-C", site, "push", "-u", "origin", branch], remote_write=True)
        self.runner.run([
            "gh", "pr", "create", "--repo", SITE_REPO, "--base", "main", "--head", branch,
            "--title", f"Publish AstralDeep {self.version} ({self.build}) for Mac updates",
            "--body", f"Appcast, Downloads record and signed minimum-build policy for {self.tag}.",
        ], remote_write=True)
        return branch

    def execute(self) -> Path:
        self.validate()
        self.preflight()
        if self.args.publish_staged:
            self.load_staged()
            self.out(f"Publishing the staged build in {self.staging}")
        else:
            app, identity = self.build_app()
            apple = self.notarize(app, identity)
            archive_path = self.package(app, identity)
            archive = self.stage(archive_path)
            policy = self.policy()
            self.write_outputs(identity, archive, apple, policy)
        if not self.args.publish:
            self.out(f"Staged {self.tag} in {self.staging}; nothing was published.")
            self.out("Publish exactly these bytes with the same arguments plus --publish --publish-staged.")
        else:
            self.publish_release()
            branch = self.publish_website()
            self.out(f"Published {self.tag}; website branch {branch}")
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
    parser.add_argument("--minimum-build", type=int)
    parser.add_argument("--policy-expires", type=parse_expiry)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--publish-staged", action="store_true")
    parser.add_argument("--no-pull-request", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--staging-dir", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--website", type=Path)
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
