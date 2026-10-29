"""Qualifies the pinned Linux sandbox for its registered pilot and operator diagnostics through the LETS executor.
Parent-side checks and the immutable mount adapter constrain ProcessSupervisor effects and refuse unsupported hosts.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import select
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from orchestrator.generated_lets_executor import (
    ProtectedExecutorConfigurationError,
    ProtectedExecutorError,
    ProtectedExecutorRuntime,
    load_protected_executor,
)
from shared.process_supervision import (
    DEFAULT_PROCESS_SUPERVISION_LIMITS,
    OutputStream,
    ProcessOwner,
    ProcessSnapshot,
    ProcessState,
    ProcessSupervisor,
    SupervisedProcess,
    TerminationReason,
)
from shared import external_http
from orchestrator import sandbox_bwrap

QUALIFICATION_OWNER_KIND: Final = "sandbox-qualification"
QUALIFICATION_AGENT_ID: Final = "sandbox-qualification-runner"
TOOL_ID: Final = "sandbox_qualification_scratch_write"
TOOL_SCOPE: Final = "tools:write"

AUTHORIZED_MODE: Final = "authorized"
DEFAULT_PROBE_MODES: Final = (
    "read_secret",
    "write_outside",
    "network_connect",
    "unix_ipc",
    "symlink_escape",
    "exhaust_output",
    "hang",
    "spawn_descendant",
)
ALL_MODES: Final = (AUTHORIZED_MODE, *DEFAULT_PROBE_MODES)

EFFECT_OUTPUT_NAME: Final = "effect-output.txt"
EFFECT_CONTENT_PREFIX: Final = "astral-sandbox-qualification"
EFFECT_SPEC_NAME: Final = "effect-spec.json"
EFFECT_SCRIPT_NAME: Final = "effect.py"
PROFILE_NAME: Final = "srt-profile.json"
SECRET_NAME: Final = "session-secret.txt"
MARKER_PREFIX: Final = "marker-"
STOLEN_SECRET_NAME: Final = "stolen-secret.txt"
DESCENDANT_PID_NAME: Final = "descendant.pid"
DESCENDANT_READY_PREFIX: Final = b"ASTRAL_SRT_DESCENDANT:"
EFFECT_READY_PREFIX: Final = b"ASTRAL_SRT_EFFECT_READY:"
OUTSIDE_WRITE_ROOTS: Final = ("/tmp", "/var/tmp", "/dev/shm")
CONNECT_TARGETS: Final = (
    ("127.0.0.1", 0),
    ("169.254.169.254", 80),
    ("10.203.0.1", 9),
    ("192.0.2.1", 443),
)

SRT_PACKAGE: Final = "@anthropic-ai/sandbox-runtime"
SRT_VERSION: Final = "0.0.78"
SRT_TARBALL_URL: Final = (
    "https://registry.npmjs.org/@anthropic-ai/sandbox-runtime/-/"
    f"sandbox-runtime-{SRT_VERSION}.tgz"
)
SRT_TARBALL_SHA512_HEX: Final = (
    "60021cc9b5d3a7b3199018dab27911d44df2c677fbbba9149325b4bd9aedb0067"
    "db7224631aba05844e6aae3d9b3462f5b4b11d159d978c7d2d20b0dff042255"
)
SRT_TARBALL_SIZE_BYTES: Final = 3_948_109
SRT_MAX_TARBALL_BYTES: Final = 32 * 1024 * 1024
MANIFEST_RELPATH: Final = Path("tooling/sandbox-qualification/manifest.json")
MINIMUM_NODE_VERSION: Final = (22, 12)
MINIMUM_NODE_VERSION_LABEL: Final = "22.12.0"
SUPPORTED_ARCHES: Final = {
    "x86_64": "x64",
    "amd64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
QUALIFIED_ARCHITECTURE_PROFILE: Final = (
    "linux/amd64 and linux/arm64 on ubuntu-24.04 with the distribution bubblewrap, "
    "socat and ripgrep, unprivileged user and network namespaces, and the vendored "
    "apply-seccomp helper"
)

DEGRADATION_MARKERS: Final = (
    "seccomp not available",
    "unix socket access not restricted",
    "apply-seccomp binary not available",
    "allowAllUnixSockets is enabled",
    "Sandbox dependencies not available",
    "Unsupported platform",
)

STATUS_PASSED: Final = "passed"
STATUS_FAILED: Final = "failed"
STATUS_UNAVAILABLE: Final = "unavailable"
STATUS_PROBE: Final = "probe"

EXIT_PASSED: Final = 0
EXIT_USAGE: Final = 2
EXIT_FAILED: Final = 3
EXIT_UNAVAILABLE: Final = 4

_PROBE_TIMEOUT_SECONDS: Final = 30.0


class SandboxQualificationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SrtArtifactPin:
    package: str
    version: str
    tarball_url: str
    sha512_hex: str
    size_bytes: int


@dataclass(frozen=True)
class SrtRuntime:
    node_path: Path
    cli_js: Path
    apply_seccomp_path: Path
    bwrap_path: Path
    socat_path: Path
    artifact_root: Path
    pin: SrtArtifactPin
    dependency_lock_sha256: str | None = None

    @property
    def artifact_sha512(self) -> str:
        return self.pin.sha512_hex


@dataclass(frozen=True)
class SupportCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class SandboxSupport:
    available: bool
    platform: str
    machine: str
    node_version: str | None
    checks: tuple[SupportCheck, ...]

    def failure_details(self) -> tuple[str, ...]:
        return tuple(
            f"{check.name}: {check.detail}" for check in self.checks if not check.passed
        )


@dataclass(frozen=True)
class QualificationCheck:
    name: str
    held: bool
    detail: str


@dataclass(frozen=True)
class QualificationSession:
    qualification_id: str
    session_dir: Path
    scratch_dir: Path
    profile_path: Path
    spec_path: Path
    effect_script_path: Path
    secret_path: Path
    secret_token: str


@dataclass(frozen=True)
class HostProcessIdentity:
    pid: int
    parent_pid: int
    start_time_ticks: int
    pid_namespace_inode: int
    namespace_pids: tuple[int, ...]


@dataclass(frozen=True)
class QualificationRecord:
    status: str
    failure_codes: tuple[str, ...]
    checks: tuple[QualificationCheck, ...]
    modes_run: tuple[str, ...]
    support: SandboxSupport | None
    profile_sha256: str | None
    artifact_sha512: str | None
    receipt_sha256s: tuple[str, ...]
    audit_correlation_id: str | None
    session_dir: str | None
    qualification_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "failure_codes": list(self.failure_codes),
            "checks": [
                {"name": check.name, "held": check.held, "detail": check.detail}
                for check in self.checks
            ],
            "modes_run": list(self.modes_run),
            "support": None
            if self.support is None
            else {
                "available": self.support.available,
                "platform": self.support.platform,
                "machine": self.support.machine,
                "node_version": self.support.node_version,
                "checks": [
                    {
                        "name": check.name,
                        "passed": check.passed,
                        "detail": check.detail,
                    }
                    for check in self.support.checks
                ],
            },
            "profile_sha256": self.profile_sha256,
            "artifact_sha512": self.artifact_sha512,
            "receipt_sha256s": list(self.receipt_sha256s),
            "audit_correlation_id": self.audit_correlation_id,
            "session_dir": self.session_dir,
            "qualification_id": self.qualification_id,
        }


def default_artifact_pin() -> SrtArtifactPin:
    _validate_pin_shape(
        SRT_PACKAGE,
        SRT_VERSION,
        SRT_TARBALL_URL,
        SRT_TARBALL_SHA512_HEX,
        SRT_TARBALL_SIZE_BYTES,
    )
    return SrtArtifactPin(
        package=SRT_PACKAGE,
        version=SRT_VERSION,
        tarball_url=SRT_TARBALL_URL,
        sha512_hex=SRT_TARBALL_SHA512_HEX,
        size_bytes=SRT_TARBALL_SIZE_BYTES,
    )


def _validate_pin_shape(
    package: str, version: str, tarball_url: str, sha512_hex: str, size_bytes: int
) -> None:
    if not package or not version or not version.split("."):
        raise SandboxQualificationError("invalid_artifact_pin")
    if not tarball_url.startswith("https://") or version not in tarball_url:
        raise SandboxQualificationError("invalid_artifact_pin")
    if len(sha512_hex) != 128 or any(
        character not in "0123456789abcdef" for character in sha512_hex
    ):
        raise SandboxQualificationError("invalid_artifact_pin")
    if size_bytes <= 0:
        raise SandboxQualificationError("invalid_artifact_pin")


def _manifest_path() -> Path:
    return Path(__file__).resolve().parents[2] / MANIFEST_RELPATH


def load_declared_manifest() -> dict[str, Any]:
    manifest_path = _manifest_path()
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SandboxQualificationError("qualification_manifest_unavailable") from None
    pin = default_artifact_pin()
    artifact = document.get("artifact")
    runtime = document.get("runtime")
    if not isinstance(artifact, dict) or not isinstance(runtime, dict):
        raise SandboxQualificationError("qualification_manifest_invalid")
    declared = (
        artifact.get("package"),
        artifact.get("version"),
        artifact.get("tarball_url"),
        artifact.get("sha512"),
        artifact.get("size_bytes"),
    )
    expected = (
        pin.package,
        pin.version,
        pin.tarball_url,
        pin.sha512_hex,
        pin.size_bytes,
    )
    if declared != expected:
        raise SandboxQualificationError("qualification_manifest_pin_mismatch")
    minimum = runtime.get("minimum_node_version")
    if minimum != MINIMUM_NODE_VERSION_LABEL:
        raise SandboxQualificationError("qualification_manifest_pin_mismatch")
    lock_path = manifest_path.parent / "package-lock.json"
    try:
        lock_digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    except OSError:
        raise SandboxQualificationError("installation_lock_invalid") from None
    if runtime.get("package_lock_sha256") != lock_digest:
        raise SandboxQualificationError("qualification_manifest_pin_mismatch")
    return document


def verify_artifact_bytes(data: bytes, pin: SrtArtifactPin) -> None:
    if len(data) != pin.size_bytes:
        raise SandboxQualificationError("artifact_size_mismatch")
    if len(data) > SRT_MAX_TARBALL_BYTES:
        raise SandboxQualificationError("artifact_too_large")
    digest = hashlib.sha512(data).hexdigest()
    if digest != pin.sha512_hex:
        raise SandboxQualificationError("artifact_digest_mismatch")


def acquire_srt_artifact(
    cache_dir: Path, pin: SrtArtifactPin, *, allow_download: bool
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"sandbox-runtime-{pin.version}.tgz"
    if target.exists():
        verify_artifact_bytes(target.read_bytes(), pin)
        return target
    if not allow_download:
        raise SandboxQualificationError("artifact_not_cached_download_not_authorized")
    temporary = cache_dir / f".{target.name}.{os.getpid()}.part"
    try:
        data = _download_registry_tarball(pin.tarball_url)
    except external_http.ExternalHttpError as error:
        temporary.unlink(missing_ok=True)
        raise SandboxQualificationError("artifact_download_failed") from error
    verify_artifact_bytes(data, pin)
    temporary.write_bytes(data)
    os.replace(temporary, target)
    return target


def _registry_tarball_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "registry.npmjs.org"
        and parsed.path.endswith(".tgz")
        and not parsed.query
        and not parsed.fragment
        and ".." not in parsed.path.split("/")
    )


def _download_registry_tarball(url: str) -> bytes:
    if not _registry_tarball_url(url):
        raise SandboxQualificationError("installation_lock_invalid")
    response = external_http.request(
        "GET",
        url,
        api_key="",
        timeout=int(_PROBE_TIMEOUT_SECONDS),
        max_response_bytes=SRT_MAX_TARBALL_BYTES,
        allow_redirects=False,
        require_identity_encoding=True,
        trust_environment=False,
    )
    if response.status_code != 200:
        raise SandboxQualificationError("artifact_download_redirect_or_status")
    return response.content


def _locked_installation(
    pin: SrtArtifactPin,
) -> tuple[bytes, bytes, list[tuple[str, str]]]:
    directory = _manifest_path().parent
    try:
        package_bytes = (directory / "package.json").read_bytes()
        lock_bytes = (directory / "package-lock.json").read_bytes()
        package = json.loads(package_bytes)
        lock = json.loads(lock_bytes)
        packages = lock["packages"]
        root = packages[f"node_modules/{pin.package}"]
        expected_sri = (
            "sha512-" + base64.b64encode(bytes.fromhex(pin.sha512_hex)).decode()
        )
        if (
            len(lock_bytes) > 256 * 1024
            or lock["lockfileVersion"] != 3
            or package["dependencies"] != {pin.package: pin.version}
            or packages[""]["dependencies"] != package["dependencies"]
            or root["version"] != pin.version
            or root["resolved"] != pin.tarball_url
            or root["integrity"] != expected_sri
            or len(packages) > 64
        ):
            raise ValueError
        dependencies = []
        for path, entry in packages.items():
            if path == "":
                continue
            if (
                not path.startswith("node_modules/")
                or ".." in path.split("/")
                or entry.get("link")
                or not _registry_tarball_url(entry["resolved"])
                or not entry["integrity"].startswith("sha512-")
            ):
                raise ValueError
            digest = base64.b64decode(entry["integrity"][7:], validate=True)
            if len(digest) != 64:
                raise ValueError
            dependencies.append((entry["resolved"], digest.hex()))
    except (OSError, ValueError, KeyError, TypeError, binascii.Error, AttributeError):
        raise SandboxQualificationError("installation_lock_invalid") from None
    return package_bytes, lock_bytes, dependencies


def install_locked_srt(
    cache_dir: Path, pin: SrtArtifactPin, *, node: Path, allow_download: bool
) -> Path:
    package_bytes, lock_bytes, dependencies = _locked_installation(pin)
    npm = _which_or_none("npm", None)
    if npm is None:
        raise SandboxQualificationError("npm_unavailable")
    tarballs = []
    for url, digest in dependencies:
        target = cache_dir / f"{digest}.tgz"
        if digest == pin.sha512_hex:
            target = acquire_srt_artifact(cache_dir, pin, allow_download=allow_download)
        elif not target.is_file():
            if not allow_download:
                raise SandboxQualificationError("dependency_not_cached")
            try:
                payload = _download_registry_tarball(url)
            except external_http.ExternalHttpError:
                raise SandboxQualificationError("artifact_download_failed") from None
            if hashlib.sha512(payload).hexdigest() != digest:
                raise SandboxQualificationError("dependency_digest_mismatch")
            target.write_bytes(payload)
        if hashlib.sha512(target.read_bytes()).hexdigest() != digest:
            raise SandboxQualificationError("dependency_digest_mismatch")
        tarballs.append(target.resolve())
    installation = Path(tempfile.mkdtemp(prefix="installed-", dir=cache_dir)).resolve()
    try:
        (installation / "package.json").write_bytes(package_bytes)
        (installation / "package-lock.json").write_bytes(lock_bytes)
        user_config = installation / "user.npmrc"
        global_config = installation / "global.npmrc"
        user_config.touch()
        global_config.touch()
        env = child_spawn_env()
        env.update(
            {
                "PATH": f"{node.parent}:{npm.parent}:/usr/bin:/bin",
                "HOME": str(installation),
            }
        )
        common = [
            str(npm),
            "--offline",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--cache",
            str(installation / "npm-cache"),
            "--userconfig",
            str(user_config),
            "--globalconfig",
            str(global_config),
        ]
        for command in (["cache", "add", str(path)] for path in tarballs):
            subprocess.run(
                common + command,
                cwd=installation,
                env=env,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        subprocess.run(
            common + ["ci"],
            cwd=installation,
            env=env,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(installation)
        raise SandboxQualificationError("locked_installation_failed") from None
    return installation / "node_modules" / pin.package


def extract_srt_artifact(
    tarball_path: Path, extract_dir: Path, pin: SrtArtifactPin
) -> Path:
    if not hasattr(tarfile, "data_filter"):
        raise SandboxQualificationError("python_data_filter_unavailable")
    verify_artifact_bytes(tarball_path.read_bytes(), pin)
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tarball_path, "r:gz") as archive:
            archive.extractall(extract_dir, filter="data")
    except (tarfile.TarError, OSError) as error:
        raise SandboxQualificationError("artifact_extraction_failed") from error
    package_root = extract_dir / "package"
    cli_js = package_root / "dist" / "cli.js"
    if not cli_js.is_file():
        raise SandboxQualificationError("artifact_incomplete")
    return package_root


def _which_or_none(name: str, override: str | Path | None) -> Path | None:
    if override is not None:
        path = Path(override)
        return path if path.is_file() and os.access(path, os.X_OK) else None
    located = shutil.which(name)
    return Path(located) if located is not None else None


def prepare_srt_runtime(
    *,
    pin: SrtArtifactPin | None = None,
    cache_dir: Path | None = None,
    allow_download: bool = False,
    node_path: str | Path | None = None,
    bwrap_path: str | Path | None = None,
    socat_path: str | Path | None = None,
    machine: str | None = None,
) -> SrtRuntime:
    resolved_pin = pin if pin is not None else default_artifact_pin()
    if pin is None:
        load_declared_manifest()
    resolved_machine = machine if machine is not None else os.uname().machine
    arch = SUPPORTED_ARCHES.get(resolved_machine)
    if arch is None:
        raise SandboxQualificationError("unsupported_architecture")
    node = _which_or_none("node", node_path)
    if node is None:
        raise SandboxQualificationError("node_unavailable")
    bwrap = _which_or_none("bwrap", bwrap_path)
    if bwrap is None:
        raise SandboxQualificationError("bubblewrap_unavailable")
    socat = _which_or_none("socat", socat_path)
    if socat is None:
        raise SandboxQualificationError("socat_unavailable")
    resolved_cache = (
        cache_dir
        if cache_dir is not None
        else Path(tempfile.gettempdir())
        / f"astral-srt-qualification-{resolved_pin.version}"
    )
    tarball = acquire_srt_artifact(
        resolved_cache, resolved_pin, allow_download=allow_download
    )
    extract_dir = resolved_cache / f"extracted-{resolved_pin.sha512_hex[:16]}"
    package_root = extract_srt_artifact(tarball, extract_dir, resolved_pin)
    apply_seccomp = package_root / "vendor" / "seccomp" / arch / "apply-seccomp"
    cli_js = package_root / "dist" / "cli.js"
    if not apply_seccomp.is_file() or not cli_js.is_file():
        raise SandboxQualificationError("artifact_incomplete")
    if not os.access(apply_seccomp, os.X_OK):
        raise SandboxQualificationError("seccomp_helper_not_executable")
    package_root = install_locked_srt(
        resolved_cache, resolved_pin, node=node, allow_download=allow_download
    )
    cli_js = package_root / "dist" / "cli.js"
    apply_seccomp = package_root / "vendor" / "seccomp" / arch / "apply-seccomp"
    passed, _detail = _run_probe_command(
        [str(node), str(cli_js), "--help"], timeout=_PROBE_TIMEOUT_SECONDS
    )
    if not passed:
        shutil.rmtree(package_root.parents[2])
        raise SandboxQualificationError("srt_cli_unavailable")
    return SrtRuntime(
        node_path=node,
        cli_js=cli_js,
        apply_seccomp_path=apply_seccomp,
        bwrap_path=bwrap,
        socat_path=socat,
        artifact_root=package_root,
        pin=resolved_pin,
        dependency_lock_sha256=hashlib.sha256(
            (package_root.parents[2] / "package-lock.json").read_bytes()
        ).hexdigest(),
    )


def _node_version_tuple(version_output: str) -> tuple[int, ...] | None:
    value = version_output.strip()
    if not value.startswith("v"):
        return None
    parts = value[1:].split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _satisfies_floor(version: tuple[int, ...], floor: tuple[int, ...]) -> bool:
    padded = version + (0,) * (len(floor) - len(version))
    return padded >= floor


def _run_probe_command(argv: Sequence[str], *, timeout: float) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=child_spawn_env(),
            cwd="/",
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        detail = type(error).__name__
        return False, detail
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        return False, (stderr[0][:200] if stderr else f"exit={completed.returncode}")
    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    return True, (stdout.splitlines()[0][:200] if stdout else "ok")


def probe_support(
    *,
    runtime: SrtRuntime | None = None,
    allow_download: bool = False,
    node_path: str | Path | None = None,
    bwrap_path: str | Path | None = None,
    socat_path: str | Path | None = None,
    machine: str | None = None,
    cache_dir: Path | None = None,
) -> SandboxSupport:
    checks: list[SupportCheck] = []
    platform_name = sys.platform
    checks.append(
        SupportCheck("platform_linux", platform_name.startswith("linux"), platform_name)
    )
    checks.append(SupportCheck("nonroot_user", os.geteuid() != 0, "non-root required"))
    resolved_machine = machine if machine is not None else os.uname().machine
    arch = SUPPORTED_ARCHES.get(resolved_machine)
    checks.append(
        SupportCheck(
            "architecture_supported",
            arch is not None,
            QUALIFIED_ARCHITECTURE_PROFILE if arch is not None else resolved_machine,
        )
    )

    node = _which_or_none("node", node_path)
    node_version: str | None = None
    if node is None:
        checks.append(SupportCheck("node_floor", False, "node not found on PATH"))
    else:
        passed, detail = _run_probe_command(
            [str(node), "--version"], timeout=_PROBE_TIMEOUT_SECONDS
        )
        node_version = detail if passed else None
        version = _node_version_tuple(detail) if passed else None
        floor_ok = version is not None and _satisfies_floor(
            version, MINIMUM_NODE_VERSION
        )
        checks.append(
            SupportCheck(
                "node_floor",
                floor_ok,
                detail
                if floor_ok
                else f"node >= {MINIMUM_NODE_VERSION_LABEL} required",
            )
        )

    for name, override, code in (
        ("bubblewrap_present", bwrap_path, "bwrap"),
        ("socat_present", socat_path, "socat"),
        ("ripgrep_present", None, "rg"),
    ):
        located = _which_or_none(code, override)
        checks.append(
            SupportCheck(
                name,
                located is not None,
                "ok" if located is not None else f"{code} not found on PATH",
            )
        )

    resolved_runtime: SrtRuntime | None
    try:
        resolved_runtime = (
            runtime
            if runtime is not None
            else prepare_srt_runtime(
                cache_dir=cache_dir,
                allow_download=allow_download,
                node_path=node_path,
                bwrap_path=bwrap_path,
                socat_path=socat_path,
                machine=machine,
            )
        )
        checks.append(
            SupportCheck(
                "artifact_ready",
                True,
                f"sha512:{resolved_runtime.pin.sha512_hex[:16]}",
            )
        )
    except SandboxQualificationError as error:
        resolved_runtime = None
        checks.append(SupportCheck("artifact_ready", False, error.code))

    if resolved_runtime is not None:
        checks.append(
            SupportCheck(
                "dependency_lock",
                resolved_runtime.dependency_lock_sha256 is not None,
                resolved_runtime.dependency_lock_sha256 or "unbound injected runtime",
            )
        )
        passed, detail = _run_probe_command(
            [str(resolved_runtime.node_path), str(resolved_runtime.cli_js), "--help"],
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        checks.append(SupportCheck("srt_cli_ready", passed, detail))
        passed, detail = _run_probe_command(
            [
                str(resolved_runtime.bwrap_path),
                "--unshare-user",
                "--unshare-net",
                "--dev-bind",
                "/",
                "/",
                str(resolved_runtime.apply_seccomp_path),
                "/bin/true",
            ],
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        checks.append(
            SupportCheck(
                "sandbox_namespace_probe",
                passed,
                detail if passed else f"host refused sandbox primitives: {detail}",
            )
        )
    else:
        checks.append(SupportCheck("srt_cli_ready", False, "runtime unavailable"))
        checks.append(
            SupportCheck("sandbox_namespace_probe", False, "runtime unavailable")
        )

    return SandboxSupport(
        available=all(check.passed for check in checks),
        platform=platform_name,
        machine=resolved_machine,
        node_version=node_version,
        checks=tuple(checks),
    )


def build_srt_settings(
    session: QualificationSession, runtime: SrtRuntime
) -> dict[str, Any]:
    return {
        "network": {
            "allowedDomains": [],
            "deniedDomains": ["*"],
            "strictAllowlist": True,
        },
        "filesystem": {
            "denyRead": ["/"],
            "allowRead": [
                "/bin",
                "/usr",
                "/lib",
                "/lib64",
                "/etc/ld.so.cache",
                "/etc/ld.so.conf",
                "/etc/ld.so.conf.d",
                str(runtime.artifact_root / "vendor" / "seccomp"),
                str(session.effect_script_path),
                str(session.spec_path),
                str(session.scratch_dir),
            ],
            "allowWrite": [str(session.scratch_dir)],
            "denyWrite": ["/tmp/claude", str(session.session_dir)],
        },
        "seccomp": {"applyPath": str(runtime.apply_seccomp_path)},
        "bwrapPath": str(session.session_dir / "bwrap-seal.py"),
        "socatPath": str(runtime.socat_path),
    }


def canonical_profile_bytes(settings: Mapping[str, Any]) -> bytes:
    return json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_srt_settings(
    settings: Mapping[str, Any],
    *,
    session: QualificationSession,
    runtime: SrtRuntime,
) -> None:
    network = settings.get("network")
    filesystem = settings.get("filesystem")
    if not isinstance(network, dict) or not isinstance(filesystem, dict):
        raise SandboxQualificationError("profile_invalid")
    if network.get("allowedDomains") != []:
        raise SandboxQualificationError("profile_network_not_denied")
    if network.get("deniedDomains") != ["*"]:
        raise SandboxQualificationError("profile_network_not_denied")
    if network.get("strictAllowlist") is not True:
        raise SandboxQualificationError("profile_network_not_denied")
    if network.get("allowAllUnixSockets") or network.get("allowLocalBinding"):
        raise SandboxQualificationError("profile_network_not_denied")
    if filesystem.get("denyRead") != ["/"]:
        raise SandboxQualificationError("profile_reads_not_allow_only")
    allow_read = filesystem.get("allowRead")
    if not isinstance(allow_read, list) or not allow_read:
        raise SandboxQualificationError("profile_reads_not_allow_only")
    runtime_prefixes = (
        "/bin",
        "/usr",
        "/lib",
        "/lib64",
        "/etc/ld.so",
        str(runtime.artifact_root / "vendor" / "seccomp"),
    )
    session_paths = {
        str(session.effect_script_path),
        str(session.spec_path),
        str(session.scratch_dir),
    }
    for entry in allow_read:
        if not isinstance(entry, str) or not entry.startswith("/"):
            raise SandboxQualificationError("profile_reads_not_allow_only")
        if entry in session_paths:
            continue
        if not any(
            entry == prefix or entry.startswith(prefix) for prefix in runtime_prefixes
        ):
            raise SandboxQualificationError("profile_reads_not_allow_only")
    if "credentials" in settings:
        raise SandboxQualificationError("profile_writes_not_scratch_only")
    if filesystem.get("allowWrite") != [str(session.scratch_dir)]:
        raise SandboxQualificationError("profile_writes_not_scratch_only")
    deny_write = filesystem.get("denyWrite")
    if (
        not isinstance(deny_write, list)
        or "/tmp/claude" not in deny_write
        or str(session.session_dir) not in deny_write
    ):
        raise SandboxQualificationError("profile_writes_not_scratch_only")
    for entry in deny_write:
        if not isinstance(entry, str) or not Path(entry).is_absolute():
            raise SandboxQualificationError("profile_writes_not_scratch_only")
        if session.scratch_dir.resolve().is_relative_to(Path(entry).resolve()):
            raise SandboxQualificationError("profile_scratch_denied")
    seccomp = settings.get("seccomp")
    if not isinstance(seccomp, dict) or seccomp.get("applyPath") != str(
        runtime.apply_seccomp_path
    ):
        raise SandboxQualificationError("profile_seccomp_not_pinned")
    if settings.get("bwrapPath") != str(session.session_dir / "bwrap-seal.py"):
        raise SandboxQualificationError("profile_seccomp_not_pinned")
    if settings.get("socatPath") != str(runtime.socat_path):
        raise SandboxQualificationError("profile_seccomp_not_pinned")


def write_immutable_profile(
    session: QualificationSession, settings: Mapping[str, Any], runtime: SrtRuntime
) -> bytes:
    validate_srt_settings(settings, session=session, runtime=runtime)
    for path, (content, mode) in _bwrap_files(session, runtime).items():
        _write_exclusive_artifact(path, content, mode=mode)
    payload = canonical_profile_bytes(settings)
    _write_exclusive_artifact(session.profile_path, payload, mode=0o400)
    return payload


def _bwrap_files(session: QualificationSession, runtime: SrtRuntime):
    interpreter = sys.executable
    if not Path(interpreter).is_absolute() or any(value.isspace() for value in interpreter):
        raise SandboxQualificationError("session_bwrap_changed")
    script = session.session_dir / "bwrap-seal.py"
    source = (f"#!{interpreter}\n").encode("utf-8") + Path(sandbox_bwrap.__file__).read_bytes()
    config = {"binary": str(runtime.bwrap_path), "scratch": str(session.scratch_dir)}
    return {
        script: (source, 0o500),
        script.with_suffix(".json"): (canonical_profile_bytes(config), 0o400),
    }


def verify_bwrap_bundle(session: QualificationSession, runtime: SrtRuntime) -> None:
    try:
        expected = {**_bwrap_files(session, runtime),
                    session.profile_path: (canonical_profile_bytes(build_srt_settings(session, runtime)), 0o400)}
        for path, (content, mode) in expected.items():
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                        or metadata.st_uid != os.getuid() or metadata.st_mode & 0o7777 != mode
                        or metadata.st_size != len(content) or stream.read(len(content) + 1) != content):
                    raise ValueError("immutable bundle changed")
    except (OSError, ValueError, TypeError):
        raise SandboxQualificationError("session_bwrap_changed") from None


def _write_exclusive_artifact(path: Path, payload: bytes, *, mode: int) -> None:
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
        )
    except FileExistsError:
        raise SandboxQualificationError("session_artifact_exists") from None
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)


def child_spawn_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    for key, value in (extra or {}).items():
        if not key.startswith("ASTRAL_SRT_QUAL_"):
            raise SandboxQualificationError("spawn_env_key_rejected")
        env[key] = value
    return env


def sandboxed_effect_argv(
    runtime: SrtRuntime, profile_path: Path, command: Sequence[str]
) -> tuple[str, ...]:
    return (
        str(runtime.node_path),
        str(runtime.cli_js),
        "--settings",
        str(profile_path),
        "--",
        *command,
    )


def render_effect_script() -> str:
    return """import json
import os
import socket
import subprocess
import sys
import time

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    spec = json.load(handle)

scratch = spec["scratch"]
mode = spec["mode"]
observed = "ok"
extra = {}


def write_marker() -> None:
    payload = {"mode": mode, "observed": observed}
    payload.update(extra)
    with open(os.path.join(scratch, "marker-" + mode + ".json"), "w", encoding="utf-8") as sink:
        json.dump(payload, sink, sort_keys=True)


if mode == "authorized":
    with open(os.path.join(scratch, spec["output_name"]), "w", encoding="utf-8") as sink:
        sink.write(spec["content"])
elif mode == "read_secret":
    try:
        with open(spec["secret_path"], "rb") as handle:
            data = handle.read()
        with open(os.path.join(scratch, "stolen-secret.txt"), "wb") as sink:
            sink.write(data)
    except OSError as error:
        observed = type(error).__name__
elif mode == "write_outside":
    attempts = {}
    for path in spec["outside_paths"]:
        try:
            with open(path, "w", encoding="utf-8") as sink:
                sink.write("violation")
            attempts[path] = "ok"
        except OSError as error:
            attempts[path] = type(error).__name__
    observed = "mixed"
    extra["attempts"] = attempts
elif mode == "network_connect":
    attempts = {}
    for target in spec["connect_targets"]:
        host = target["host"]
        port = int(target["port"])
        label = host + ":" + str(port)
        try:
            with socket.create_connection((host, port), timeout=spec["connect_timeout"]):
                attempts[label] = "ok"
        except OSError as error:
            attempts[label] = type(error).__name__
    observed = "mixed"
    extra["attempts"] = attempts
elif mode == "unix_ipc":
    attempts = {}
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(spec["ipc_path"])
            attempts["connect"] = "ok"
        finally:
            client.close()
    except OSError as error:
        attempts["connect"] = type(error).__name__
    try:
        bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            bound.bind(os.path.join(scratch, "ipc-probe.sock"))
            attempts["bind"] = "ok"
        finally:
            bound.close()
    except OSError as error:
        attempts["bind"] = type(error).__name__
    observed = "mixed"
    extra["attempts"] = attempts
elif mode == "symlink_escape":
    attempts = {}
    link = os.path.join(scratch, spec["link_name"])
    try:
        os.symlink(spec["secret_path"], link)
        attempts["symlink"] = "ok"
    except OSError as error:
        attempts["symlink"] = type(error).__name__
    try:
        with open(link, "w", encoding="utf-8") as sink:
            sink.write("violation")
        attempts["write_through"] = "ok"
    except OSError as error:
        attempts["write_through"] = type(error).__name__
    observed = "mixed"
    extra["attempts"] = attempts
elif mode == "spawn_descendant":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(%d)" % spec["descendant_hold_seconds"], spec["descendant_identity"]]
    )
    with open(os.path.join(scratch, spec["descendant_pid_file"]), "w", encoding="utf-8") as sink:
        sink.write(str(child.pid))
    print("ASTRAL_SRT_DESCENDANT:" + str(child.pid), flush=True)
    time.sleep(spec["hold_seconds"])
elif mode == "exhaust_output":
    with open(os.path.join(scratch, spec["output_name"]), "w", encoding="utf-8") as sink:
        sink.write(spec["content"])
    chunk = "x" * 4096 + "\\n"
    for _ in range(spec["exhaust_chunks"]):
        sys.stdout.write(chunk)
    sys.stdout.flush()
elif mode == "hang":
    print("ASTRAL_SRT_EFFECT_READY:hang", flush=True)
    time.sleep(spec["hold_seconds"])

write_marker()
"""


def prepare_session(
    *,
    session_dir: Path | None = None,
    scratch_root: Path | None = None,
    qualification_id: str | None = None,
) -> QualificationSession:
    qualification_id = qualification_id or uuid.uuid4().hex
    if len(qualification_id) != 32 or any(
        c not in "0123456789abcdef" for c in qualification_id
    ):
        raise SandboxQualificationError("qualification_id_invalid")
    if session_dir is None:
        resolved_dir = Path(tempfile.mkdtemp(prefix="astral-srt-qualification-"))
    else:
        selected = Path(session_dir).absolute()
        resolved_dir = selected.parent.resolve() / selected.name
        resolved_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            resolved_dir.mkdir(mode=0o700)
        except FileExistsError:
            raise SandboxQualificationError("session_directory_not_fresh") from None
    scratch_dir = (
        Path(scratch_root).resolve() / qualification_id
        if scratch_root is not None
        else resolved_dir.with_name(f"{resolved_dir.name}-scratch-{qualification_id}")
    )
    if scratch_dir.is_relative_to(resolved_dir) or resolved_dir.is_relative_to(
        scratch_dir
    ):
        raise SandboxQualificationError("scratch_session_overlap")
    scratch_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(scratch_dir, 0o700)
    secret_token = os.urandom(32).hex()
    session = QualificationSession(
        qualification_id=qualification_id,
        session_dir=resolved_dir,
        scratch_dir=scratch_dir,
        profile_path=resolved_dir / PROFILE_NAME,
        spec_path=resolved_dir / EFFECT_SPEC_NAME,
        effect_script_path=resolved_dir / EFFECT_SCRIPT_NAME,
        secret_path=resolved_dir / SECRET_NAME,
        secret_token=secret_token,
    )
    _write_exclusive_artifact(
        session.effect_script_path, render_effect_script().encode(), mode=0o400
    )
    _write_exclusive_artifact(session.secret_path, secret_token.encode(), mode=0o600)
    return session


def effect_content(session: QualificationSession) -> str:
    return f"{EFFECT_CONTENT_PREFIX}:{session.qualification_id}\n"


def effect_arguments(session: QualificationSession, mode: str) -> dict[str, str]:
    return {"mode": mode, "qualification_id": session.qualification_id}


def build_effect_spec(
    session: QualificationSession,
    *,
    mode: str,
    connect_port: int | None = None,
    ipc_path: str | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "scratch": str(session.scratch_dir),
        "mode": mode,
        "output_name": EFFECT_OUTPUT_NAME,
        "content": effect_content(session),
    }
    if mode == "read_secret":
        spec["secret_path"] = str(session.secret_path)
    elif mode == "write_outside":
        spec["outside_paths"] = [
            f"{root}/astral-srt-{session.qualification_id}.txt"
            for root in OUTSIDE_WRITE_ROOTS
        ]
        spec["outside_paths"].append(str(session.session_dir / "outside.txt"))
    elif mode == "network_connect":
        targets = [
            {"host": host, "port": connect_port if host == "127.0.0.1" else port}
            for host, port in CONNECT_TARGETS
        ]
        spec["connect_targets"] = targets
        spec["connect_timeout"] = 1.5
    elif mode == "unix_ipc":
        spec["ipc_path"] = ipc_path or str(session.scratch_dir / "probe-listen.sock")
    elif mode == "symlink_escape":
        spec["secret_path"] = str(session.secret_path)
        spec["link_name"] = "escape-symlink"
    elif mode == "spawn_descendant":
        spec["descendant_pid_file"] = DESCENDANT_PID_NAME
        spec["descendant_hold_seconds"] = 300
        spec["descendant_identity"] = session.qualification_id
        spec["hold_seconds"] = 120
    elif mode == "exhaust_output":
        spec["exhaust_chunks"] = 700
    elif mode == "hang":
        spec["hold_seconds"] = 120
    return spec


def write_effect_spec(session: QualificationSession, spec: Mapping[str, Any]) -> None:
    if session.spec_path.is_symlink():
        raise SandboxQualificationError("session_artifact_exists")
    temporary = session.session_dir / f".effect-spec-{uuid.uuid4().hex}"
    try:
        _write_exclusive_artifact(
            temporary,
            json.dumps(spec, sort_keys=True, separators=(",", ":")).encode(),
            mode=0o400,
        )
        os.replace(temporary, session.spec_path)
    finally:
        temporary.unlink(missing_ok=True)


def _ipc_listener_path(session: QualificationSession) -> Path:
    return Path(tempfile.gettempdir()) / f"astral-srt-{session.qualification_id}.sock"


class ProbeTcpListener:
    def __init__(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.socket.setblocking(False)

    @property
    def port(self) -> int:
        assert self.socket.getsockname() is not None
        return int(self.socket.getsockname()[1])

    def pending_connections(self) -> int:
        readable, _, _ = select.select([self.socket], [], [], 0)
        return len(readable)

    def close(self) -> None:
        self.socket.close()


class ProbeUnixListener:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        if self.path.exists():
            self.path.unlink()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(self.path))
        self.socket.listen(1)
        self.socket.setblocking(False)

    def pending_connections(self) -> int:
        readable, _, _ = select.select([self.socket], [], [], 0)
        return len(readable)

    def close(self) -> None:
        self.socket.close()
        self.path.unlink(missing_ok=True)


def verify_effect_receipt(
    executor_runtime: ProtectedExecutorRuntime,
    *,
    permit: object,
    arguments: Mapping[str, object],
) -> None:
    executor_runtime.verify_and_claim(
        metadata=permit,
        final_arguments=dict(arguments),
        tool_id=TOOL_ID,
        tool_scope=TOOL_SCOPE,
    )


def receipt_digest(permit: object) -> str:
    return hashlib.sha256(
        json.dumps(permit, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def audit_correlation_id(permit: object) -> str | None:
    if not isinstance(permit, Mapping):
        return None
    context = permit.get("context")
    if not isinstance(context, Mapping):
        return None
    value = context.get("audit_correlation_id")
    return value if isinstance(value, str) else None


def output_lines(snapshot_lines: Sequence[bytes]) -> list[str]:
    return [line.decode("utf-8", errors="replace") for line in snapshot_lines]


def degradation_hits(text: str) -> tuple[str, ...]:
    return tuple(marker for marker in DEGRADATION_MARKERS if marker in text)


def snapshot_degradation_hits(process: SupervisedProcess) -> tuple[str, ...]:
    snapshot = process.snapshot()
    text = "\n".join(
        output_lines(snapshot.stdout.lines) + output_lines(snapshot.stderr.lines)
    )
    return degradation_hits(text)


def secret_material_present(
    session: QualificationSession, process: SupervisedProcess
) -> bool:
    token = session.secret_token.encode("utf-8")
    for path in sorted(session.scratch_dir.rglob("*")):
        if path.is_file() and token in path.read_bytes():
            return True
    snapshot = process.snapshot()
    for stream in (snapshot.stdout, snapshot.stderr):
        for line in stream.lines:
            if token in line:
                return True
    return False


def await_effect(
    process: SupervisedProcess, *, deadline_seconds: float
) -> tuple[ProcessSnapshot, TerminationReason | None, bool]:
    try:
        snapshot = process.wait(timeout=max(deadline_seconds, 0.1))
        reason = snapshot.termination_reason
        return snapshot, reason, reason is TerminationReason.CANCEL
    except subprocess.TimeoutExpired:
        snapshot = process.terminate(reason=TerminationReason.CANCEL)
        return snapshot, TerminationReason.CANCEL, True
    finally:
        process.terminate(reason=TerminationReason.CANCEL)


def _effect_python() -> str:
    candidate = shutil.which("python3", path="/usr/bin:/usr/local/bin:/bin")
    try:
        resolved = Path(candidate).resolve(strict=True) if candidate else None
        if resolved is None or not any(resolved.is_relative_to(root) for root in ("/usr", "/bin")):
            raise ValueError
        return candidate
    except (OSError, ValueError, TypeError):
        raise SandboxQualificationError("effect_python_unavailable") from None


def launch_sandboxed_effect(
    supervisor: ProcessSupervisor,
    session: QualificationSession,
    runtime: SrtRuntime,
    profile_path: Path,
) -> SupervisedProcess:
    if profile_path != session.profile_path:
        raise SandboxQualificationError("session_bwrap_changed")
    verify_bwrap_bundle(session, runtime)
    command = (
        _effect_python(),
        str(session.effect_script_path),
        str(session.spec_path),
    )
    return supervisor.spawn(
        process_id=uuid.uuid4(),
        owner=ProcessOwner(
            owner_kind=QUALIFICATION_OWNER_KIND, owner_id=session.qualification_id
        ),
        argv=sandboxed_effect_argv(runtime, profile_path, command),
        cwd=str(session.scratch_dir),
        env=child_spawn_env(),
    )


def _marker_path(session: QualificationSession, mode: str) -> Path:
    return session.scratch_dir / f"{MARKER_PREFIX}{mode}.json"


def read_marker(session: QualificationSession, mode: str) -> dict[str, Any] | None:
    path = _marker_path(session, mode)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _check(name: str, held: bool, detail: str) -> QualificationCheck:
    return QualificationCheck(name=name, held=held, detail=detail)


def authorized_effect_checks(
    session: QualificationSession,
    snapshot: ProcessSnapshot,
    *,
    marker_mode: str = AUTHORIZED_MODE,
) -> tuple[QualificationCheck, ...]:
    output_path = session.scratch_dir / EFFECT_OUTPUT_NAME
    expected = effect_content(session)
    if not output_path.is_file():
        return (_check("authorized_effect", False, "authorized_output_absent"),)
    actual = output_path.read_text(encoding="utf-8")
    if actual != expected:
        return (_check("authorized_effect", False, "authorized_content_mismatch"),)
    marker = read_marker(session, marker_mode)
    if marker is None or marker.get("observed") != "ok":
        return (_check("authorized_effect", False, "effect_marker_incomplete"),)
    digest = hashlib.sha256(actual.encode("utf-8")).hexdigest()
    return (
        _check("authorized_effect", True, f"sha256:{digest}"),
        _check(
            "effect_state",
            snapshot.state in {ProcessState.EXITED},
            str(snapshot.state.value),
        ),
    )


def secret_read_checks(
    session: QualificationSession, process: SupervisedProcess
) -> tuple[QualificationCheck, ...]:
    if (session.scratch_dir / STOLEN_SECRET_NAME).exists():
        return (_check("secret_exfiltration", False, "stolen_secret_file_present"),)
    if secret_material_present(session, process):
        return (_check("secret_exfiltration", False, "secret_material_observed"),)
    marker = read_marker(session, "read_secret")
    if marker is None:
        return (_check("secret_exfiltration", False, "probe_incomplete"),)
    if marker.get("observed") == "ok":
        return (_check("secret_exfiltration", False, "secret_read_reported_ok"),)
    return (_check("secret_exfiltration", True, str(marker.get("observed"))),)


def outside_write_checks(
    session: QualificationSession, spec: Mapping[str, Any]
) -> tuple[QualificationCheck, ...]:
    outside_paths = spec.get("outside_paths", [])
    for path in outside_paths:
        if Path(path).exists():
            return (_check("outside_writes", False, f"outside_file_present:{path}"),)
    marker = read_marker(session, "write_outside")
    attempts = marker.get("attempts") if isinstance(marker, dict) else None
    if not isinstance(attempts, dict) or not attempts:
        return (_check("outside_writes", False, "probe_incomplete"),)
    escaped = sorted(label for label, outcome in attempts.items() if outcome == "ok")
    if escaped:
        return (_check("outside_writes", False, "outside_write_reported_ok"),)
    return (_check("outside_writes", True, f"denied:{len(attempts)}"),)


def network_checks(
    listener: ProbeTcpListener, marker: dict[str, Any] | None
) -> tuple[QualificationCheck, ...]:
    if listener.pending_connections():
        return (_check("network_denial", False, "listener_contacted"),)
    attempts = marker.get("attempts") if isinstance(marker, dict) else None
    if not isinstance(attempts, dict) or not attempts:
        return (_check("network_denial", False, "probe_incomplete"),)
    reached = sorted(label for label, outcome in attempts.items() if outcome == "ok")
    if reached:
        return (_check("network_denial", False, "connection_reported_ok"),)
    return (_check("network_denial", True, f"denied:{len(attempts)}"),)


def ipc_checks(
    session: QualificationSession,
    listener: ProbeUnixListener,
    marker: dict[str, Any] | None,
) -> tuple[QualificationCheck, ...]:
    if listener.pending_connections():
        return (_check("ipc_denial", False, "unix_listener_contacted"),)
    if (session.scratch_dir / "ipc-probe.sock").exists():
        return (_check("ipc_denial", False, "unix_socket_created_in_scratch"),)
    attempts = marker.get("attempts") if isinstance(marker, dict) else None
    if not isinstance(attempts, dict) or not attempts:
        return (_check("ipc_denial", False, "probe_incomplete"),)
    succeeded = sorted(label for label, outcome in attempts.items() if outcome == "ok")
    if succeeded:
        return (_check("ipc_denial", False, "ipc_reported_ok"),)
    return (_check("ipc_denial", True, f"denied:{len(attempts)}"),)


def symlink_checks(session: QualificationSession) -> tuple[QualificationCheck, ...]:
    if session.secret_path.read_text(encoding="utf-8") != session.secret_token:
        return (_check("symlink_containment", False, "secret_file_modified"),)
    marker = read_marker(session, "symlink_escape")
    attempts = marker.get("attempts") if isinstance(marker, dict) else None
    if not isinstance(attempts, dict) or "write_through" not in attempts:
        return (_check("symlink_containment", False, "probe_incomplete"),)
    if attempts.get("write_through") == "ok":
        return (_check("symlink_containment", False, "symlink_write_reported_ok"),)
    return (_check("symlink_containment", True, str(attempts.get("write_through"))),)


def _host_process_stat(pid: int, proc_root: Path) -> tuple[str, int, int]:
    text = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    if text.partition(" ")[0] != str(pid):
        raise ValueError
    fields = text.rpartition(")")[2].split()
    return fields[0], int(fields[1]), int(fields[19])


def _host_process_identity(pid: int, proc_root: Path) -> HostProcessIdentity:
    state, parent, started = _host_process_stat(pid, proc_root)
    if state in {"Z", "X", "x"}:
        raise ProcessLookupError
    directory = proc_root / str(pid)
    rows = (directory / "status").read_text(encoding="utf-8").splitlines()
    namespace_pids = next(
        tuple(map(int, row.partition(":")[2].split()))
        for row in rows
        if row.startswith("NSpid:")
    )
    if (
        not namespace_pids
        or namespace_pids[0] != pid
        or any(value <= 0 for value in namespace_pids)
    ):
        raise ValueError
    return HostProcessIdentity(
        pid, parent, started, (directory / "ns" / "pid").stat().st_ino, namespace_pids
    )


def capture_descendant_identity(
    process: SupervisedProcess,
    session: QualificationSession,
    *,
    timeout: float,
    proc_root: Path = Path("/proc"),
) -> HostProcessIdentity:
    try:
        line = process.wait_for_line(
            OutputStream.STDOUT, prefix=DESCENDANT_READY_PREFIX, timeout=timeout
        )
        namespace_pid = int(line[len(DESCENDANT_READY_PREFIX) :].strip())
        marker_pid = int(
            (session.scratch_dir / DESCENDANT_PID_NAME).read_text().strip()
        )
        if marker_pid != namespace_pid or namespace_pid <= 0:
            raise ValueError
        root_identity = _host_process_identity(process.pid, proc_root)
        table = {}
        for index, directory in enumerate(proc_root.iterdir()):
            if index >= 65_536:
                raise ValueError
            if not directory.name.isdecimal():
                continue
            try:
                identity = _host_process_identity(int(directory.name), proc_root)
            except (OSError, ValueError, IndexError, StopIteration):
                continue
            table[identity.pid] = identity
        candidates = []
        for identity in table.values():
            if (
                identity.pid_namespace_inode == root_identity.pid_namespace_inode
                or len(identity.namespace_pids) <= len(root_identity.namespace_pids)
                or identity.namespace_pids[-1] != namespace_pid
                or not _descendant_command_matches(identity.pid, session, proc_root)
            ):
                continue
            parent = identity.parent_pid
            lineage = [identity]
            seen = {identity.pid}
            while parent in table and parent not in seen and len(seen) <= 256:
                lineage.append(table[parent])
                if parent == root_identity.pid:
                    candidates.append((identity, lineage))
                    break
                seen.add(parent)
                parent = table[parent].parent_pid
        if (
            len(candidates) != 1
            or _host_process_identity(process.pid, proc_root) != root_identity
        ):
            raise ValueError
        candidate, lineage = candidates[0]
        if any(
            _host_process_identity(entry.pid, proc_root) != entry for entry in lineage
        ):
            raise ValueError
        if not _descendant_command_matches(candidate.pid, session, proc_root):
            raise ValueError
        return candidate
    except (OSError, ValueError, IndexError, StopIteration, EOFError, TimeoutError):
        raise SandboxQualificationError(
            "descendant_host_identity_unavailable"
        ) from None


def _descendant_command_matches(
    pid: int, session: QualificationSession, proc_root: Path
) -> bool:
    try:
        arguments = (
            (proc_root / str(pid) / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        )
        expected = [
            os.fsencode(_effect_python()),
            b"-c",
            b"import time; time.sleep(300)",
            session.qualification_id.encode(),
        ]
        return arguments == expected and (proc_root / str(pid) / "exe").samefile(
            Path(os.fsdecode(expected[0])).resolve()
        )
    except OSError:
        return False


def _descendant_identity_label(identity: HostProcessIdentity) -> str:
    return f"host_pid={identity.pid};start_ticks={identity.start_time_ticks};pidns={identity.pid_namespace_inode};namespace_pid={identity.namespace_pids[-1]}"


def descendant_cleanup_check(
    identity: HostProcessIdentity | None, *, proc_root: Path = Path("/proc")
) -> QualificationCheck:
    if not isinstance(identity, HostProcessIdentity):
        return _check(
            "descendant_cleanup", False, "descendant_host_identity_unavailable"
        )
    try:
        state, _parent, started = _host_process_stat(identity.pid, proc_root)
        terminated = started != identity.start_time_ticks or state in {"Z", "X", "x"}
    except FileNotFoundError:
        terminated = True
    except (OSError, ValueError, IndexError):
        return _check(
            "descendant_cleanup", False, "descendant_host_identity_unavailable"
        )
    return _check(
        "descendant_cleanup",
        terminated,
        ("descendant_terminated;" if terminated else "descendant_alive;")
        + _descendant_identity_label(identity),
    )


def output_bound_checks(
    snapshot: ProcessSnapshot, *, expect_dropped: bool
) -> tuple[QualificationCheck, ...]:
    stdout = snapshot.stdout
    stderr = snapshot.stderr
    ring_capacity = DEFAULT_PROCESS_SUPERVISION_LIMITS.ring_capacity_bytes_per_stream
    held = (
        stdout.retained_bytes <= ring_capacity
        and stderr.retained_bytes <= ring_capacity
    )
    checks = [
        _check(
            "output_bound",
            held,
            f"stdout_total={stdout.total_bytes},stderr_total={stderr.total_bytes}",
        )
    ]
    if expect_dropped and stdout.dropped_bytes == 0:
        checks.append(_check("output_exhausted", False, "no_dropped_output_observed"))
    elif expect_dropped:
        checks.append(
            _check("output_exhausted", True, f"dropped_bytes={stdout.dropped_bytes}")
        )
    return tuple(checks)


def tree_cleanup_checks(snapshot: Any) -> tuple[QualificationCheck, ...]:
    failures = []
    if not snapshot.process_tree_terminated:
        failures.append("tree_alive")
    if not snapshot.readers_joined:
        failures.append("readers_running")
    if not snapshot.pipes_closed:
        failures.append("pipes_open")
    if snapshot.cleanup_error:
        failures.append("cleanup_error")
    return (
        _check(
            "tree_cleanup",
            not failures,
            ";".join(failures) if failures else "terminal",
        ),
    )


def degradation_checks(process: SupervisedProcess) -> tuple[QualificationCheck, ...]:
    hits = snapshot_degradation_hits(process)
    if hits:
        return (_check("degradation_scan", False, ";".join(hits)),)
    return (_check("degradation_scan", True, "clean"),)


def launch_outcome_checks(snapshot: ProcessSnapshot) -> tuple[QualificationCheck, ...]:
    terminal = snapshot.state in {
        ProcessState.EXITED,
        ProcessState.FAILED,
        ProcessState.KILLED,
    }
    return (_check("launch_completed", terminal, str(snapshot.state.value)),)


def append_hash_chained_entry(log_path: Path, payload: Mapping[str, Any]) -> str:
    previous = ""
    index = 0
    if log_path.exists():
        lines = [
            line
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if lines:
            try:
                previous = str(json.loads(lines[-1])["entry_sha256"])
            except (ValueError, KeyError):
                raise SandboxQualificationError("qualification_log_corrupt") from None
            index = len(lines)
    body = {
        "index": index,
        "previous_entry_sha256": previous,
        "payload": dict(payload),
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    entry = dict(body)
    entry["entry_sha256"] = hashlib.sha256(encoded).hexdigest()
    with log_path.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
    return entry["entry_sha256"]


def _fail_closed_record(
    reason_codes: Sequence[str],
    checks: Sequence[QualificationCheck],
    *,
    support: SandboxSupport | None,
    session: QualificationSession | None,
    profile_sha256: str | None,
    artifact_sha512: str | None,
    receipt_sha256s: Sequence[str],
    audit_correlation_id_value: str | None,
    modes_run: Sequence[str],
) -> QualificationRecord:
    return QualificationRecord(
        status=STATUS_FAILED,
        failure_codes=tuple(reason_codes),
        checks=tuple(checks),
        modes_run=tuple(modes_run),
        support=support,
        profile_sha256=profile_sha256,
        artifact_sha512=artifact_sha512,
        receipt_sha256s=tuple(receipt_sha256s),
        audit_correlation_id=audit_correlation_id_value,
        session_dir=str(session.session_dir) if session is not None else None,
        qualification_id=session.qualification_id if session is not None else None,
    )


def run_sandbox_qualification(
    *,
    executor_runtime: ProtectedExecutorRuntime,
    receipt_factory: Callable[[Mapping[str, object]], object],
    support: SandboxSupport | None = None,
    session_dir: Path | None = None,
    scratch_root: Path | None = None,
    per_launch_deadline_seconds: float = 30.0,
    hang_deadline_seconds: float = 2.0,
    artifact_cache_dir: Path | None = None,
    allow_artifact_download: bool = False,
    modes: Sequence[str] = ALL_MODES,
    supervisor: ProcessSupervisor | None = None,
    runtime: SrtRuntime | None = None,
    record_log_path: Path | None = None,
    qualification_id: str | None = None,
) -> QualificationRecord:
    if AUTHORIZED_MODE not in modes:
        raise SandboxQualificationError("authorized_mode_required")
    for mode in modes:
        if mode not in ALL_MODES:
            raise SandboxQualificationError("unknown_probe_mode")
    diagnostic_only = supervisor is not None or runtime is not None
    resolved_support = (
        support
        if support is not None
        else probe_support(
            allow_download=allow_artifact_download, cache_dir=artifact_cache_dir
        )
    )
    if not resolved_support.available:
        return QualificationRecord(
            status=STATUS_UNAVAILABLE,
            failure_codes=(),
            checks=(),
            modes_run=(),
            support=resolved_support,
            profile_sha256=None,
            artifact_sha512=None,
            receipt_sha256s=(),
            audit_correlation_id=None,
            session_dir=None,
        )

    session = prepare_session(
        session_dir=session_dir,
        scratch_root=scratch_root,
        qualification_id=qualification_id,
    )
    try:
        resolved_runtime = (
            runtime
            if runtime is not None
            else prepare_srt_runtime(
                cache_dir=artifact_cache_dir,
                allow_download=allow_artifact_download,
            )
        )
    except SandboxQualificationError as error:
        return _fail_closed_record(
            (error.code,),
            (),
            support=resolved_support,
            session=session,
            profile_sha256=None,
            artifact_sha512=None,
            receipt_sha256s=(),
            audit_correlation_id_value=None,
            modes_run=(),
        )
    settings = build_srt_settings(session, resolved_runtime)
    validate_srt_settings(settings, session=session, runtime=resolved_runtime)
    profile_bytes = write_immutable_profile(session, settings, resolved_runtime)
    profile_sha256 = hashlib.sha256(profile_bytes).hexdigest()
    resolved_supervisor = supervisor if supervisor is not None else ProcessSupervisor()
    receipt_digests: list[str] = []
    correlation_id: str | None = None
    checks: list[QualificationCheck] = []
    modes_run: list[str] = []
    listener: ProbeTcpListener | None = None
    unix_listener: ProbeUnixListener | None = None

    try:
        for mode in modes:
            if mode == "network_connect" and listener is None:
                listener = ProbeTcpListener()
            if mode == "unix_ipc" and unix_listener is None:
                unix_listener = ProbeUnixListener(_ipc_listener_path(session))
            arguments = effect_arguments(session, mode)
            try:
                permit = receipt_factory(arguments)
            except Exception as error:
                return _fail_closed_record(
                    ("receipt_factory_failed", type(error).__name__),
                    checks,
                    support=resolved_support,
                    session=session,
                    profile_sha256=profile_sha256,
                    artifact_sha512=resolved_runtime.artifact_sha512,
                    receipt_sha256s=receipt_digests,
                    audit_correlation_id_value=correlation_id,
                    modes_run=modes_run,
                )
            receipt_digests.append(receipt_digest(permit))
            if correlation_id is None:
                correlation_id = audit_correlation_id(permit)
            try:
                verify_effect_receipt(
                    executor_runtime, permit=permit, arguments=arguments
                )
            except ProtectedExecutorError as error:
                checks.append(_check("receipt_enforcement", False, error.code))
                return _fail_closed_record(
                    (f"receipt_enforcement:{error.code}",),
                    checks,
                    support=resolved_support,
                    session=session,
                    profile_sha256=profile_sha256,
                    artifact_sha512=resolved_runtime.artifact_sha512,
                    receipt_sha256s=receipt_digests,
                    audit_correlation_id_value=correlation_id,
                    modes_run=modes_run,
                )
            checks.append(_check("receipt_enforcement", True, mode))
            spec = build_effect_spec(
                session,
                mode=mode,
                connect_port=listener.port if listener is not None else 0,
                ipc_path=str(_ipc_listener_path(session)),
            )
            write_effect_spec(session, spec)
            try:
                process = launch_sandboxed_effect(
                    resolved_supervisor, session, resolved_runtime, session.profile_path
                )
            except (OSError, SandboxQualificationError) as error:
                checks.append(
                    _check(
                        "launch_completed",
                        False,
                        f"launch_failed:{type(error).__name__}",
                    )
                )
                return _fail_closed_record(
                    (f"launch_completed:launch_failed:{type(error).__name__}",),
                    checks,
                    support=resolved_support,
                    session=session,
                    profile_sha256=profile_sha256,
                    artifact_sha512=resolved_runtime.artifact_sha512,
                    receipt_sha256s=receipt_digests,
                    audit_correlation_id_value=correlation_id,
                    modes_run=modes_run,
                )
            deadline = (
                hang_deadline_seconds
                if mode in {"hang", "spawn_descendant"}
                else per_launch_deadline_seconds
            )
            descendant_identity = None
            if mode == "spawn_descendant":
                try:
                    descendant_identity = capture_descendant_identity(
                        process, session, timeout=per_launch_deadline_seconds
                    )
                    checks.append(
                        _check(
                            "descendant_pid_namespace",
                            True,
                            _descendant_identity_label(descendant_identity),
                        )
                    )
                except SandboxQualificationError as error:
                    checks.append(_check("descendant_pid_namespace", False, error.code))
            elif mode == "hang":
                try:
                    process.wait_for_line(
                        OutputStream.STDOUT,
                        prefix=EFFECT_READY_PREFIX,
                        timeout=per_launch_deadline_seconds,
                    )
                    checks.append(_check("effect_ready", True, mode))
                except (TimeoutError, EOFError):
                    checks.append(_check("effect_ready", False, "startup_not_observed"))
            snapshot, reason, timed_out = await_effect(
                process,
                deadline_seconds=deadline,
            )
            modes_run.append(mode)
            checks.extend(launch_outcome_checks(snapshot))
            checks.extend(tree_cleanup_checks(snapshot))
            checks.extend(degradation_checks(process))
            if mode == AUTHORIZED_MODE:
                checks.extend(authorized_effect_checks(session, snapshot))
            elif mode == "read_secret":
                checks.extend(secret_read_checks(session, process))
            elif mode == "write_outside":
                checks.extend(outside_write_checks(session, spec))
            elif mode == "network_connect":
                checks.extend(network_checks(listener, read_marker(session, mode)))
            elif mode == "unix_ipc":
                checks.extend(
                    ipc_checks(session, unix_listener, read_marker(session, mode))
                )
            elif mode == "symlink_escape":
                checks.extend(symlink_checks(session))
            elif mode == "hang":
                if not timed_out:
                    checks.append(
                        _check("deadline_enforcement", False, "hang_not_cancelled")
                    )
                else:
                    checks.append(
                        _check(
                            "deadline_enforcement",
                            True,
                            str(reason.value if reason is not None else "cancel"),
                        )
                    )
            elif mode == "spawn_descendant":
                checks.append(descendant_cleanup_check(descendant_identity))
                if not timed_out:
                    checks.append(
                        _check("deadline_enforcement", False, "hang_not_cancelled")
                    )
                else:
                    checks.append(
                        _check(
                            "deadline_enforcement",
                            True,
                            str(reason.value if reason is not None else "cancel"),
                        )
                    )
            elif mode == "exhaust_output":
                checks.extend(output_bound_checks(snapshot, expect_dropped=True))
                checks.extend(
                    authorized_effect_checks(session, snapshot, marker_mode=mode)
                )
            if any(not check.held for check in checks):
                return _fail_closed_record(
                    tuple(
                        f"{check.name}:{check.detail}"
                        for check in checks
                        if not check.held
                    ),
                    checks,
                    support=resolved_support,
                    session=session,
                    profile_sha256=profile_sha256,
                    artifact_sha512=resolved_runtime.artifact_sha512,
                    receipt_sha256s=receipt_digests,
                    audit_correlation_id_value=correlation_id,
                    modes_run=modes_run,
                )
        if diagnostic_only:
            record = QualificationRecord(
                status=STATUS_FAILED,
                failure_codes=("diagnostic_runner_configuration",),
                checks=tuple(checks),
                modes_run=tuple(modes_run),
                support=resolved_support,
                profile_sha256=profile_sha256,
                artifact_sha512=resolved_runtime.artifact_sha512,
                receipt_sha256s=tuple(receipt_digests),
                audit_correlation_id=correlation_id,
                session_dir=str(session.session_dir),
                qualification_id=session.qualification_id,
            )
        else:
            record = QualificationRecord(
                status=STATUS_PASSED if tuple(modes) == ALL_MODES else STATUS_PROBE,
                failure_codes=(),
                checks=tuple(checks),
                modes_run=tuple(modes_run),
                support=resolved_support,
                profile_sha256=profile_sha256,
                artifact_sha512=resolved_runtime.artifact_sha512,
                receipt_sha256s=tuple(receipt_digests),
                audit_correlation_id=correlation_id,
                session_dir=str(session.session_dir),
                qualification_id=session.qualification_id,
            )
    finally:
        try:
            for snapshot in resolved_supervisor.snapshots():
                if snapshot.owner == ProcessOwner(
                    QUALIFICATION_OWNER_KIND, session.qualification_id
                ):
                    resolved_supervisor.terminate(
                        snapshot.process_id, reason=TerminationReason.CANCEL
                    )
        finally:
            if listener is not None:
                listener.close()
            if unix_listener is not None:
                unix_listener.close()
    if record_log_path is not None:
        append_hash_chained_entry(record_log_path, {"record": record.to_dict()})
    return record


def permit_file_factory(permit_path: Path) -> Callable[[Mapping[str, object]], object]:
    try:
        permit = json.loads(Path(permit_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SandboxQualificationError("permit_file_unreadable") from None

    def factory(_arguments: Mapping[str, object]) -> object:
        if permit is None:
            raise SandboxQualificationError("permit_file_exhausted")
        return permit

    return factory


def permit_bundle_factory(
    path: Path,
) -> tuple[str, Callable[[Mapping[str, object]], object]]:
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError
        document = json.loads(path.read_bytes())
        identity = document["qualification_id"]
        permits = document["permits"]
        if (
            not isinstance(identity, str)
            or len(identity) != 32
            or any(c not in "0123456789abcdef" for c in identity)
            or not isinstance(permits, dict)
            or set(permits) != set(ALL_MODES)
        ):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise SandboxQualificationError("permit_bundle_invalid") from None

    def factory(arguments: Mapping[str, object]) -> object:
        mode = arguments.get("mode")
        if (
            arguments != {"qualification_id": identity, "mode": mode}
            or mode not in ALL_MODES
        ):
            raise SandboxQualificationError("permit_bundle_arguments")
        if mode not in permits:
            raise SandboxQualificationError("permit_bundle_exhausted")
        return permits.pop(mode)

    return identity, factory


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sandbox-qualification",
        description="Opt-in Linux sandbox-runtime qualification runner.",
    )
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="Run only the support probe and print the qualification record.",
    )
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument(
        "--permit-file",
        type=Path,
        default=None,
        help="Path to one operator-minted LETS permit for a single authorized run.",
    )
    sources.add_argument(
        "--permit-bundle",
        type=Path,
        help="Full-profile permits from normal authorization, keyed by mode and bound to one planned qualification ID.",
    )
    sources.add_argument(
        "--write-permit-plan",
        type=Path,
        help="Write arguments for normal authorization of every profile mode; no effect starts.",
    )
    parser.add_argument(
        "--qualification-id",
        help="The planned ID bound into a single authorized permit.",
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        default=None,
        help="Directory for qualification session artifacts.",
    )
    parser.add_argument(
        "--scratch-root",
        type=Path,
        default=None,
        help="Directory under which the scratch effect directory is created.",
    )
    parser.add_argument(
        "--artifact-cache-dir",
        type=Path,
        default=None,
        help="Directory for the digest-pinned sandbox-runtime artifact cache.",
    )
    parser.add_argument(
        "--allow-artifact-download",
        action="store_true",
        help="Allow the one-time digest-pinned artifact download from the npm registry.",
    )
    parser.add_argument(
        "--record-log",
        type=Path,
        default=None,
        help="Append-only hash-chained qualification log path.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    support: SandboxSupport | None = None,
    executor_runtime: ProtectedExecutorRuntime | None = None,
    receipt_factory: Callable[[Mapping[str, object]], object] | None = None,
    runtime: SrtRuntime | None = None,
) -> int:
    parser = build_cli_parser()
    options = parser.parse_args(list(argv) if argv is not None else None)
    if options.write_permit_plan is not None:
        identity = uuid.uuid4().hex
        plan = {
            "qualification_id": identity,
            "tool_id": TOOL_ID,
            "scope": TOOL_SCOPE,
            "effects": [
                {"mode": mode, "qualification_id": identity} for mode in ALL_MODES
            ],
        }
        descriptor = os.open(
            options.write_permit_plan, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(plan, handle, sort_keys=True)
        print(json.dumps({"status": "permit_plan", "qualification_id": identity}))
        return EXIT_PASSED
    if options.permit_file is not None and options.qualification_id is None:
        parser.error("--permit-file requires its planned --qualification-id")
    resolved_support = (
        support
        if support is not None
        else probe_support(
            allow_download=options.allow_artifact_download,
            cache_dir=options.artifact_cache_dir,
        )
    )
    if resolved_support.available and not options.probe_only:
        resolved_executor = executor_runtime
        if resolved_executor is None:
            try:
                resolved_executor = load_protected_executor(
                    os.environ, agent_id=QUALIFICATION_AGENT_ID
                )
            except ProtectedExecutorConfigurationError as error:
                record = _fail_closed_record(
                    (f"executor_configuration:{error.code}",),
                    (),
                    support=resolved_support,
                    session=None,
                    profile_sha256=None,
                    artifact_sha512=None,
                    receipt_sha256s=(),
                    audit_correlation_id_value=None,
                    modes_run=(),
                )
                print(json.dumps(record.to_dict(), sort_keys=True))
                return EXIT_FAILED
        factory = receipt_factory
        qualification_id = options.qualification_id
        if factory is None:
            if options.permit_bundle is not None:
                qualification_id, factory = permit_bundle_factory(options.permit_bundle)
            elif options.permit_file is not None:
                factory = permit_file_factory(options.permit_file)
            else:
                print(
                    json.dumps(
                        {
                            "status": STATUS_FAILED,
                            "failure_codes": ["missing_permit_source"],
                        },
                        sort_keys=True,
                    )
                )
                return EXIT_FAILED
        record = run_sandbox_qualification(
            executor_runtime=resolved_executor,
            receipt_factory=factory,
            support=resolved_support,
            session_dir=options.session_dir,
            scratch_root=options.scratch_root,
            artifact_cache_dir=options.artifact_cache_dir,
            allow_artifact_download=options.allow_artifact_download,
            modes=(AUTHORIZED_MODE,) if options.permit_file is not None else ALL_MODES,
            runtime=runtime,
            record_log_path=options.record_log,
            qualification_id=qualification_id,
        )
    else:
        record = QualificationRecord(
            status=STATUS_UNAVAILABLE
            if not resolved_support.available
            else STATUS_PROBE,
            failure_codes=(),
            checks=(),
            modes_run=(),
            support=resolved_support,
            profile_sha256=None,
            artifact_sha512=None,
            receipt_sha256s=(),
            audit_correlation_id=None,
            session_dir=None,
        )
    print(json.dumps(record.to_dict(), sort_keys=True))
    if record.status in {STATUS_PASSED, STATUS_PROBE}:
        return EXIT_PASSED
    if record.status == STATUS_UNAVAILABLE:
        return EXIT_UNAVAILABLE
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
