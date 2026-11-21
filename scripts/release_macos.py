"""Builds, notarizes, signs and stages a Sparkle-updatable AstralDeep Mac disk image on the owner's Mac or in the release-macos workflow.
Nothing reaches GitHub without --publish; the downloads record, appcast and signed policy become release assets the website discovers.
Policy signatures come from Sparkle's `sign_update -p`, which emits a raw Ed25519 signature over the exact file bytes.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fnmatch
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
PROFILE_PATH = "config/apple/AstralDeep-DeveloperID.provisionprofile"
PROFILE_SHA256 = "b7936b1610f47d58531526100214340f8ff959374830f8f008e4bc1e4fef5c8e"
PROFILE_UUID = "f63c6cd4-51bd-4d18-ae4b-0ed4d6f583f5"
PROFILE_INSTALL_DIRS = ("Library/Developer/Xcode/UserData/Provisioning Profiles", "Library/MobileDevice/Provisioning Profiles")
DIRECT_ENTITLEMENTS = "apple-clients/AstralApp/AstralApp-DirectDownload.entitlements"
ASSOCIATED_DOMAINS = ("applinks:astraldeep.github.io", "webcredentials:astraldeep.github.io")
RESTRICTED_ENTITLEMENT_PREFIXES = ("com.apple.developer.", "com.apple.application-identifier", "keychain-access-groups")
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
NODE_PACK_METADATA_PATH = "apple-clients/NodePack/pins.json"
NODE_PACK_RELEASE_PATHS = {
    ".github/workflows/release-macos.yml", "backend/tests/test_release_macos.py",
    "backend/tests/test_release_macos_workflow.py", "components/AstralProjection",
    "config/astral-composition.json", "scripts/release_macos.py", PROFILE_PATH,
}


BUILD_RECEIPT = "node-pack-build.json"
BUILD_RECEIPT_KEYS = {"platform", "deep_commit", "kernel_artifacts", "builder_base", "relocation_image", "apt_snapshot", "builder_image_id"}
PACK_PLATFORMS = ("macos-arm64", "linux-aarch64", "linux-x86_64")
NODE_BUNDLE_DIR = Path(__file__).resolve().parent / "node_bundle"


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


FAILURE_OUTPUT_LIMIT = 400_000


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
            self.out(f"{command[0]} output before the failure:\n{failure_output(result)}")
            raise ReleaseError(f"{command[0]} failed with status {result.returncode}: {failure_detail(result)}")
        return result

    def _execute(self, command: list[str], cwd: Path | None) -> Result:
        done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
        return Result(done.returncode, done.stdout, done.stderr)


def failure_output(result: Result) -> str:
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return combined[-FAILURE_OUTPUT_LIMIT:]


def failure_detail(result: Result) -> str:
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    errors = [line.strip() for line in combined.splitlines() if " error: " in line or line.startswith("error: ")]
    unique = list(dict.fromkeys(errors))
    if unique:
        return "\n".join(unique)[:6000]
    return (result.stderr or result.stdout)[-1500:].strip()


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
        self.pack_sources: list[tuple[Path, str, str]] = []

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
        if self.args.node_pack_dir is not None and self.args.node_bundle_dir is not None:
            raise ReleaseError("Use either --node-pack-dir or --node-bundle-dir, not both")
        if self.args.node_pack_dir is None and self.args.node_bundle_dir is None:
            raise ReleaseError("--node-pack-dir is required for a Mac release with a node offer")
        if self.args.node_bundle_dir is not None and not self.dry and not self.args.node_team_id:
            raise ReleaseError("--node-team-id is required to publish a node bundle")

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
        self.node_pack = self.verify_node_bundle() if self.args.node_bundle_dir else self.verify_node_pack()
        self.latest = self.latest_release()
        if self.latest and self.build <= int(self.latest["build"]):
            raise ReleaseError(
                f"Build {self.build} must exceed the published build {self.latest['build']} ({self.latest['tag']})")
        if self.args.publish_staged:
            return
        for path in (self.staging, self.work):
            if path.exists() and any(path.iterdir()):
                raise ReleaseError(f"{path} is not empty; choose another directory so only the newest archive is staged")

    def verify_node_pack(self) -> dict[str, object]:
        folder = Path(self.args.node_pack_dir)
        if self.dry:
            return self.dry_node_pack(folder)
        raw, descriptor, manifest, archive_path = self.read_pack(folder, "macos-arm64")
        pins = json.loads((self.projection / "apple-clients/NodePack/pins.json").read_text(encoding="utf-8"))
        if manifest.get("deep", {}).get("commit") != pins["deep"]["commit"] \
                or manifest.get("kernel", {}).get("commit") != pins["kernel"]["commit"] \
                or descriptor.get("version") != manifest.get("version"):
            raise ReleaseError("Node pack source pins or version do not match Projection")
        source_proof = self.verify_node_pack_sources(manifest, pins)
        found = self.pack_digests(folder, raw, descriptor, manifest, archive_path)
        found["source_equivalence"] = source_proof
        self.pack_sources = [(folder / "node-pack.json", "node-pack.json", found["descriptor_sha256"]),
                             (folder / "node-pack-manifest.json", "node-pack-manifest.json", found["manifest_sha256"]),
                             (archive_path, found["archive_name"], found["archive_sha256"])]
        return found

    def read_pack(self, folder: Path, platform: str) -> tuple[bytes, dict, dict, Path]:
        descriptor_path = folder / "node-pack.json"
        manifest_path = folder / "node-pack-manifest.json"
        try:
            if not folder.is_dir() or folder.is_symlink() or any(path.is_symlink() for path in folder.iterdir()):
                raise ReleaseError("Node pack assets must be regular files in one directory")
            raw = descriptor_path.read_bytes()
            descriptor = json.loads(raw)
            archive_name = descriptor["archive"]["file"]
            manifest = json.loads(manifest_path.read_bytes())
            archive_path = folder / archive_name
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ReleaseError(f"Node pack is incomplete or malformed: {error}") from error
        if not isinstance(archive_name, str) \
                or not re.fullmatch(rf"astral-node-pack-[A-Za-z0-9.-]+-{re.escape(platform)}\.tar\.gz", archive_name):
            raise ReleaseError("Node pack archive name is invalid")
        required = {"node-pack.json", "node-pack-manifest.json", archive_name}
        present = {path.name for path in folder.iterdir()}
        receipt_path = folder / BUILD_RECEIPT if platform.startswith("linux-") else None
        allowed = [required] + ([required | {BUILD_RECEIPT}] if receipt_path else [])
        if present not in allowed:
            raise ReleaseError("Node pack folder must hold exactly its descriptor, manifest and archive")
        files = [descriptor_path, manifest_path, archive_path] + ([folder / BUILD_RECEIPT] if BUILD_RECEIPT in present else [])
        if not all(path.is_file() for path in files):
            raise ReleaseError("Node pack assets must be regular files")
        if descriptor.get("format") != "astral.nodepack-descriptor/v1" or descriptor.get("platform") != platform:
            raise ReleaseError("Node pack descriptor format or platform is invalid")
        if manifest.get("format") != "astral.nodepack-manifest/v1" or manifest.get("platform") != platform:
            raise ReleaseError("Node pack manifest format or platform is invalid")
        if receipt_path is not None and receipt_path.exists():
            self.check_build_receipt(receipt_path, platform, manifest)
        return raw, descriptor, manifest, archive_path

    def check_build_receipt(self, path: Path, platform: str, manifest: dict) -> None:
        try:
            receipt = json.loads(path.read_bytes())
        except (OSError, ValueError) as error:
            raise ReleaseError(f"Node pack build receipt is unreadable: {error}") from error
        if not isinstance(receipt, dict) or set(receipt) != BUILD_RECEIPT_KEYS:
            raise ReleaseError("Node pack build receipt has unexpected fields")
        artifacts = receipt["kernel_artifacts"]
        if not isinstance(artifacts, dict) or set(artifacts) != {"wheel", "mailbox"} \
                or not all(isinstance(value, str) and value for value in artifacts.values()) \
                or not all(isinstance(receipt[key], str) and receipt[key] for key in BUILD_RECEIPT_KEYS - {"kernel_artifacts"}):
            raise ReleaseError("Node pack build receipt has malformed values")
        deep = manifest.get("deep")
        if receipt["platform"] != platform or receipt["deep_commit"] != (deep.get("commit") if isinstance(deep, dict) else None):
            raise ReleaseError(f"Node pack {platform} build receipt names another platform or source commit")

    def pack_digests(
        self, folder: Path, raw: bytes, descriptor: dict, manifest: dict, archive_path: Path,
    ) -> dict[str, object]:
        archive_info = descriptor["archive"]
        digest = sha256_file(archive_path)
        manifest_digest = sha256_file(folder / "node-pack-manifest.json")
        if archive_info.get("sha256") != digest or archive_info.get("size") != archive_path.stat().st_size \
                or descriptor.get("manifest_sha256") != manifest_digest \
                or descriptor.get("installed_bytes") != manifest.get("installed_bytes") \
                or descriptor.get("version") != manifest.get("version"):
            raise ReleaseError("Node pack descriptor, manifest and archive disagree")
        return {"archive_name": archive_path.name, "descriptor_sha256": hashlib.sha256(raw).hexdigest(),
                "archive_sha256": digest, "manifest_sha256": manifest_digest}

    def verify_node_bundle(self) -> dict[str, object]:
        root = Path(self.args.node_bundle_dir)
        if self.dry:
            return self.dry_node_pack(root)
        if not root.is_dir() or root.is_symlink() or {path.name for path in root.iterdir()} != set(PACK_PLATFORMS):
            raise ReleaseError("Node bundle folder must hold exactly the macos-arm64, linux-aarch64 and linux-x86_64 packs")
        composition = json.loads((self.deep / "config/astral-composition.json").read_text(encoding="utf-8"))
        for name, entry in composition["components"].items():
            tree = self.git(self.deep, "ls-tree", "HEAD", entry["path"]).stdout.split()
            if len(tree) < 3 or tree[:3] != ["160000", "commit", entry["commit"]]:
                raise ReleaseError(f"Release {name} gitlink differs from composition")
        found: dict[str, dict[str, object]] = {}
        manifests: dict[str, dict] = {}
        for target in PACK_PLATFORMS:
            folder = root / target
            raw, descriptor, manifest, archive_path = self.read_pack(folder, target)
            self.verify_bundle_sources(manifest, composition, target)
            found[target] = self.pack_digests(folder, raw, descriptor, manifest, archive_path)
            manifests[target] = manifest
        record = manifests["macos-arm64"].get("signing")
        if not isinstance(record, dict) or not record.get("files") \
                or record.get("sealed_from_manifest_sha256") in (None, found["macos-arm64"]["manifest_sha256"]):
            raise ReleaseError("The macOS node pack manifest predates signing")
        signing = self.signing_module()
        try:
            checked = signing.verify_signed_pack(root / "macos-arm64" / "node-pack.json", team=self.args.node_team_id)
        except (signing.PackError, OSError, ValueError) as error:
            raise ReleaseError(f"The macOS node pack signature does not verify: {error}") from error
        self.pack_sources = []
        extras: dict[str, object] = {}
        for target in PACK_PLATFORMS:
            folder, entry = root / target, found[target]
            if target == "macos-arm64":
                names = ("node-pack.json", "node-pack-manifest.json")
            else:
                names = (f"node-pack-{target}.json", f"node-pack-{target}-manifest.json")
                extras[target] = {"descriptor": names[0], "manifest": names[1], **entry}
            self.pack_sources += [(folder / "node-pack.json", names[0], entry["descriptor_sha256"]),
                                  (folder / "node-pack-manifest.json", names[1], entry["manifest_sha256"]),
                                  (folder / entry["archive_name"], entry["archive_name"], entry["archive_sha256"])]
        mac = found["macos-arm64"]
        return {**mac, "source_equivalence": {
                    "releaseDeep": self.deep_sha, "kernel": composition["components"]["astral-kernel"]["commit"],
                    "verified": "release-commit-and-composition-gitlinks"},
                "signing": {"identity": record.get("identity"), "signedFiles": checked["files"],
                            "hardened": checked["hardened"], "teamID": self.args.node_team_id},
                "linux": extras}

    def dry_node_pack(self, folder: Path) -> dict[str, object]:
        archive_name = "astral-node-pack-3.11.18-dry-dry-macos-arm64.tar.gz"
        self.pack_sources = [(folder / name, name, "0" * 64) for name in
                             ("node-pack.json", "node-pack-manifest.json", archive_name)]
        return {"archive_name": archive_name, "descriptor_sha256": "0" * 64, "archive_sha256": "0" * 64,
                "manifest_sha256": "0" * 64}

    def verify_bundle_sources(self, manifest: dict, composition: dict, platform: str) -> None:
        if manifest.get("deep", {}).get("commit") != self.deep_sha:
            raise ReleaseError(f"Node pack {platform} was built from a source commit other than the release commit")
        components = composition["components"]
        if set(manifest.get("components", {})) != set(components):
            raise ReleaseError(f"Node pack {platform} component inventory differs from the release composition")
        for name, entry in components.items():
            if manifest["components"][name] != {"repository": entry["repository"], "commit": entry["commit"]}:
                raise ReleaseError(f"Node pack {platform} {name} differs from the release composition")
        if manifest.get("kernel", {}).get("commit") != components["astral-kernel"]["commit"]:
            raise ReleaseError(f"Node pack {platform} kernel differs from the release composition")

    def signing_module(self):
        if str(NODE_BUNDLE_DIR) not in sys.path:
            sys.path.insert(0, str(NODE_BUNDLE_DIR))
        import sign_macos

        return sign_macos

    def verify_node_pack_sources(self, manifest: dict[str, object], pins: dict[str, object]) -> dict[str, object]:
        packed_deep = pins["deep"]["commit"]
        if not COMMIT.fullmatch(packed_deep):
            raise ReleaseError("Node pack Deep pin is not a full commit")
        ancestor = self.git(self.deep, "merge-base", "--is-ancestor", packed_deep, self.deep_sha, check=False)
        if ancestor.returncode:
            raise ReleaseError("Node pack Deep source is not an ancestor of the release")
        changed = self.git(self.deep, "diff", "--name-only", packed_deep, self.deep_sha).stdout.splitlines()
        if any(path not in NODE_PACK_RELEASE_PATHS and not path.startswith(("specs/", "docs/")) for path in changed):
            raise ReleaseError("Release changes runtime or policy paths after the packed Deep source")
        packed = json.loads(self.git(self.deep, "show", f"{packed_deep}:config/astral-composition.json").stdout)
        current = json.loads((self.deep / "config/astral-composition.json").read_text(encoding="utf-8"))
        prior_projection = packed["components"]["astral-projection"]["commit"]
        released_projection = self.args.projection_commit
        components = packed["components"]
        if set(manifest.get("components", {})) != set(components):
            raise ReleaseError("Node pack component inventory differs from packed Deep composition")
        for name, entry in components.items():
            expected = {"repository": entry["repository"], "commit": entry["commit"]}
            if manifest["components"][name] != expected:
                raise ReleaseError(f"Node pack {name} differs from packed Deep composition")
            tree = self.git(self.deep, "ls-tree", packed_deep, entry["path"]).stdout.split()
            if len(tree) < 3 or tree[:3] != ["160000", "commit", entry["commit"]]:
                raise ReleaseError(f"Packed Deep {name} gitlink differs from composition")
        normalized = json.loads(json.dumps(packed))
        normalized["components"]["astral-projection"]["commit"] = released_projection
        if normalized != current:
            raise ReleaseError("Release composition changes beyond the Projection metadata pin")
        for name, entry in current["components"].items():
            tree = self.git(self.deep, "ls-tree", "HEAD", entry["path"]).stdout.split()
            if len(tree) < 3 or tree[:3] != ["160000", "commit", entry["commit"]]:
                raise ReleaseError(f"Release {name} gitlink differs from composition")
        projection_paths = self.git(self.projection, "diff", "--name-only", prior_projection, released_projection).stdout.splitlines()
        if projection_paths != [NODE_PACK_METADATA_PATH]:
            raise ReleaseError("Projection changed beyond node pack pin metadata")
        prior_pins = json.loads(self.git(self.projection, "show", f"{prior_projection}:{NODE_PACK_METADATA_PATH}").stdout)
        prior_pins["deep"]["commit"] = packed_deep
        if prior_pins != pins or pins["kernel"]["commit"] != components["astral-kernel"]["commit"]:
            raise ReleaseError("Projection node pack pins changed beyond the packed Deep source")
        return {"packedDeep": packed_deep, "releaseDeep": self.deep_sha,
                "packedProjection": prior_projection, "releaseProjection": released_projection,
                "kernel": pins["kernel"]["commit"], "deepChangedPaths": changed,
                "projectionChangedPaths": projection_paths,
                "verified": "all-component-gitlinks-and-closed-runtime-delta"}

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
            if info.get("ASTRALNodePackURL") != f"{self.asset_base}/node-pack.json" \
                    or info.get("ASTRALNodePackSHA256") != self.node_pack["descriptor_sha256"]:
                raise ReleaseError("Built app has no exact immutable node pack offer")
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

    def expand_prefix(self, value: object) -> object:
        if isinstance(value, list):
            return [self.expand_prefix(item) for item in value]
        if isinstance(value, str):
            return value.replace("$(AppIdentifierPrefix)", f"{TEAM_ID}.").replace("$(TeamIdentifierPrefix)", f"{TEAM_ID}.")
        return value

    def profile_allows(self, granted: object, wanted: object) -> bool:
        if granted == "*":
            return True
        if isinstance(granted, list):
            patterns = [str(item) for item in granted]
            wanted_items = wanted if isinstance(wanted, list) else [wanted]
            return all(any(fnmatch.fnmatchcase(str(item), pattern) for pattern in patterns) for item in wanted_items)
        return granted == wanted

    def prepare_profile(self) -> None:
        source = self.deep / PROFILE_PATH
        if not source.is_file() or sha256_file(source) != PROFILE_SHA256:
            raise ReleaseError(f"{PROFILE_PATH} is missing or differs from the pinned Developer ID profile")
        if self.dry:
            return
        decoded = self.runner.run(["security", "cms", "-D", "-i", source])
        try:
            profile = plistlib.loads(decoded.stdout.encode("utf-8"))
            entitlements = profile["Entitlements"]
            team, application = profile["TeamIdentifier"], entitlements["com.apple.application-identifier"]
            platform_names, everywhere = profile["Platform"], profile["ProvisionsAllDevices"]
            expires, uuid = profile["ExpirationDate"], profile["UUID"]
        except (ValueError, KeyError, TypeError, plistlib.InvalidFileException) as error:
            raise ReleaseError("The Developer ID profile could not be decoded") from error
        if uuid != PROFILE_UUID:
            raise ReleaseError(f"The Developer ID profile UUID {uuid} differs from the pinned {PROFILE_UUID}")
        if team != [TEAM_ID] or entitlements.get("com.apple.developer.team-identifier") != TEAM_ID:
            raise ReleaseError(f"The Developer ID profile does not belong to team {TEAM_ID}")
        if application != f"{TEAM_ID}.{BUNDLE_ID}":
            raise ReleaseError(f"The Developer ID profile is not for {BUNDLE_ID}")
        if platform_names != ["OSX"] or everywhere is not True:
            raise ReleaseError("The profile is not a macOS Developer ID profile")
        if expires.replace(tzinfo=dt.timezone.utc) <= self.clock():
            raise ReleaseError(f"The Developer ID profile expired on {expires:%Y-%m-%d}")
        wanted = plistlib.loads((self.projection / DIRECT_ENTITLEMENTS).read_bytes())
        for key, value in wanted.items():
            if key.startswith(RESTRICTED_ENTITLEMENT_PREFIXES) and not self.profile_allows(
                    entitlements.get(key), self.expand_prefix(value)):
                raise ReleaseError(f"The Developer ID profile does not allow the {key} entitlement")
        home = Path(self.environ.get("HOME") or Path.home())
        for folder in PROFILE_INSTALL_DIRS:
            destination = home / folder
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination / f"{PROFILE_UUID}.provisionprofile")

    def verify_profile(self, app: Path) -> None:
        if self.dry:
            return
        embedded = app / "Contents/embedded.provisionprofile"
        if not embedded.is_file() or sha256_file(embedded) != PROFILE_SHA256:
            raise ReleaseError("The exported app does not embed the pinned Developer ID profile")
        shown = self.runner.run(["codesign", "--display", "--entitlements", ":-", "--xml", app])
        try:
            signed = plistlib.loads(shown.stdout.encode("utf-8"))
        except (ValueError, plistlib.InvalidFileException) as error:
            raise ReleaseError("The signed entitlements of the exported app are unreadable") from error
        domains = signed.get("com.apple.developer.associated-domains")
        if not isinstance(domains, list) or any(domain not in domains for domain in ASSOCIATED_DOMAINS):
            raise ReleaseError("The signed app lacks the associated domains for the sign-in callback")

    def build_app(self) -> tuple[Path, dict[str, object]]:
        self.prepare_profile()
        archive = self.work / "AstralDeep.xcarchive"
        export = self.work / "export"
        options = self.work / "export-options.plist"
        offer_plist = self.work / "node-offer-info.plist"
        if not self.dry:
            self.work.mkdir(parents=True, exist_ok=True)
            options.write_bytes(plistlib.dumps({
                "method": "developer-id", "teamID": TEAM_ID, "signingStyle": "manual",
                "signingCertificate": "Developer ID Application", "destination": "export",
                "provisioningProfiles": {BUNDLE_ID: PROFILE_UUID},
            }))
            source_plist = self.projection / "apple-clients/AstralApp/Info.plist"
            info = plistlib.loads(source_plist.read_bytes())
            info["ASTRALNodePackURL"] = f"{self.asset_base}/node-pack.json"
            info["ASTRALNodePackSHA256"] = self.node_pack["descriptor_sha256"]
            offer_plist.write_bytes(plistlib.dumps(info))
        self.runner.run([
            "xcodebuild", "archive", "-project", self.projection / PROJECT, "-scheme", SCHEME,
            "-configuration", CONFIGURATION, "-destination", "generic/platform=macOS",
            "-archivePath", archive, "-derivedDataPath", self.work / "dd",
            f"ASTRAL_DEVELOPMENT_TEAM={TEAM_ID}", f"DEVELOPMENT_TEAM={TEAM_ID}", "CODE_SIGN_STYLE=Manual",
            f"CODE_SIGN_IDENTITY={SIGNING_IDENTITY}",
            "ARCHS=arm64", f"MARKETING_VERSION={self.version}", f"CURRENT_PROJECT_VERSION={self.build}",
            f"INFOPLIST_FILE={offer_plist}", f"ASTRAL_PROFILE_DIRECT={PROFILE_UUID}",
        ])
        self.runner.run([
            "xcodebuild", "-exportArchive", "-archivePath", archive, "-exportPath", export,
            "-exportOptionsPlist", options,
        ])
        app = export / "AstralDeep.app"
        self.verify_profile(app)
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
            "nodePack": {"descriptorURL": f"{self.asset_base}/node-pack.json", **self.node_pack},
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
            "nodePack": self.node_pack,
        })
        if self.dry:
            self.out(f"write {self.staging}/release-provenance.json, downloads.json, {STATE_FILE}")
            return
        self.stage_node_pack()
        for name, data in (("release-provenance.json", provenance), ("downloads.json", downloads), (STATE_FILE, self.state)):
            (self.staging / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def stage_node_pack(self) -> None:
        for source, name, digest in self.pack_sources:
            target = self.staging / name
            if target.exists():
                raise ReleaseError(f"Staged asset {name} already exists; release assets are never overwritten")
            shutil.copy2(source, target)
            if sha256_file(target) != digest:
                raise ReleaseError(f"Staged asset {name} differs from the verified bytes")

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
        if self.state.get("nodePack") != self.node_pack:
            raise ReleaseError("The node pack changed since staging")
        for _, name, digest in self.pack_sources:
            if sha256_file(self.staging / name) != digest:
                raise ReleaseError(f"Staged node pack asset {name} changed")
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
            *(name for _, name, _ in self.pack_sources),
        ]
        return [self.staging / name for name in names]

    def publish_release(self) -> None:
        if not self.runner.publish:
            raise ReleaseError("Publishing a release requires --publish")
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
        edit = ["gh", "release", "edit", self.tag, "--repo", RELEASE_REPO, "--draft=false"]
        if self.channel == "preview":
            edit.extend(["--prerelease", "--latest=false"])
        else:
            edit.append("--latest")
        run(edit, remote_write=True)
        info = run(["gh", "api", f"repos/{RELEASE_REPO}/releases/tags/{self.tag}"],
                   preview=json.dumps({"immutable": True, "draft": False, "prerelease": self.channel == "preview", "assets": []}))
        latest = run(["gh", "api", f"repos/{RELEASE_REPO}/releases/latest", "--jq", ".tag_name"], check=False)
        if self.dry:
            return
        if latest.returncode and "HTTP 404" not in latest.stderr:
            raise ReleaseError(f"Could not confirm the Latest release: {latest.stderr.strip()[-300:]}")
        is_latest = latest.returncode == 0 and latest.stdout.strip() == self.tag
        if self.channel == "stable" and not is_latest:
            raise ReleaseError(f"GitHub did not mark stable release {self.tag} as Latest")
        if self.channel == "preview" and is_latest:
            raise ReleaseError(f"GitHub marked preview release {self.tag} as Latest")
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
    parser.add_argument("--node-pack-dir", type=Path)
    parser.add_argument("--node-bundle-dir", type=Path)
    parser.add_argument("--node-team-id")
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
