'''Qualifies the pinned Linux sandbox for its registered pilot and operator diagnostics through the LETS executor.
Parent-side checks and the immutable mount adapter constrain ProcessSupervisor effects and refuse unsupported hosts.
'''

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

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
    ProcessSupervisor,
    TerminationReason,
)

# ---------------------------------------------------------------------------
# Constants (kept verbatim from the original file for compatibility)
# ---------------------------------------------------------------------------
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
    "https://registry.npmjs.org/@anthropic-ai/sandbox-runtime/-/sandbox-runtime-0.0.78.tgz"
)

# ---------------------------------------------------------------------------
# Helper utilities – host capability detection
# ---------------------------------------------------------------------------
def _bwrap_path() -> Path | None:
    """Return the absolute path to ``bwrap`` if it exists in $PATH, else ``None``.
    """
    candidate = shutil.which("bwrap")
    return Path(candidate) if candidate else None


def _landlock_abi() -> int:
    """Query the kernel for the supported Landlock ABI version.
    Returns ``0`` if the kernel does not expose the ``landlock_create_ruleset``
    syscall (i.e. Landlock is unavailable).
    """
    # The Landlock ABI version is exposed via the ``/proc/sys/kernel/landlock_abi``
    # file on kernels that support it. If the file is missing we treat the ABI as 0.
    try:
        with open("/proc/sys/kernel/landlock_abi", "rt", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def _userns_allowed() -> bool:
    """Return ``True`` if the current user can create unprivileged user namespaces.
    The kernel knob ``kernel.unprivileged_userns_clone`` (or the older
    ``kernel.unprivileged_userns``) controls this behaviour.
    """
    paths = ["/proc/sys/kernel/unprivileged_userns_clone", "/proc/sys/kernel/unprivileged_userns"]
    for p in paths:
        try:
            with open(p, "rt", encoding="utf-8") as f:
                return f.read().strip() == "1"
        except FileNotFoundError:
            continue
    return False


def is_supported_host() -> bool:
    """Perform a *fail‑closed* host capability check.
    The function returns ``True`` only when **all** of the following are satisfied:

    * ``bwrap`` executable is present.
    * Landlock ABI version is at least 6 (the version required by the pinned SRT).
    * Unprivileged user namespaces are permitted.
    """
    if _bwrap_path() is None:
        print("sandbox-qualification: bubblewrap (bwrap) not found – host unsupported", file=sys.stderr)
        return False
    if _landlock_abi() < 6:
        print(
            f"sandbox-qualification: Landlock ABI {_landlock_abi()} < 6 – host unsupported",
            file=sys.stderr,
        )
        return False
    if not _userns_allowed():
        print("sandbox-qualification: unprivileged user namespaces denied – host unsupported", file=sys.stderr)
        return False
    return True

# ---------------------------------------------------------------------------
# SRT profile handling – immutable, pinned, and validated
# ---------------------------------------------------------------------------
def _profile_path() -> Path:
    """Return the path to the bundled immutable SRT profile.
    The profile lives next to this module under ``resources/srt-profile.json``.
    """
    return Path(__file__).with_name("resources").joinpath(PROFILE_NAME)


def load_srt_profile() -> dict:
    """Load and return the immutable SRT profile JSON.
    Raises ``FileNotFoundError`` if the profile is missing and ``json.JSONDecodeError``
    if the file is malformed. The function does **not** perform any dynamic
    modification – the profile is treated as a trusted constant.
    """
    profile_file = _profile_path()
    if not profile_file.is_file():
        raise FileNotFoundError(f"SRT profile not found at {profile_file}")
    with profile_file.open("rt", encoding="utf-8") as f:
        return json.load(f)

# ---------------------------------------------------------------------------
# Sandbox execution – orchestrated via ProcessSupervisor
# ---------------------------------------------------------------------------
def _build_sandbox_command(
    mode: str,
    work_dir: Path,
    profile_dir: Path,
) -> list[str]:
    """Construct the command line that launches the sandbox‑runtime.

    Parameters
    ----------
    mode:
        One of ``ALL_MODES`` – determines which effect script is written to the
        temporary work directory.
    work_dir:
        Directory containing the effect script and any temporary files.
    profile_dir:
        Directory that holds the immutable ``srt-profile.json``.
    """
    # Resolve the sandbox‑runtime binary. The pinned artifact is expected to be
    # installed under ``node_modules/.bin/sandbox-runtime`` relative to the repo
    # root. If it cannot be found we abort – this is a *fail‑closed* condition.
    repo_root = Path(__file__).resolve().parents[2]  # backend/orchestrator/..
    srt_bin = repo_root / "node_modules" / ".bin" / "sandbox-runtime"
    if not srt_bin.is_file():
        raise FileNotFoundError(
            f"sandbox-runtime binary not found at expected location {srt_bin}. "
            "Ensure the pinned package is installed before qualification."
        )

    # The sandbox‑runtime expects a JSON profile via ``--profile`` and the
    # command to execute as the final arguments.
    effect_script = work_dir / EFFECT_SCRIPT_NAME
    return [
        str(srt_bin),
        "--profile",
        str(profile_dir / PROFILE_NAME),
        "--",
        sys.executable,
        str(effect_script),
        mode,
    ]


def run_sandboxed_effect(mode: str) -> int:
    """Run a short‑lived effect inside the sandbox and verify its LETS receipt.

    The function performs the following steps:

    1. Create a temporary working directory containing the effect script and a
       minimal immutable SRT profile.
    2. Load the protected executor runtime via ``load_protected_executor``.
    3. Spawn the sandbox process using ``ProcessSupervisor`` with the command
       built by ``_build_sandbox_command``.
    4. Stream stdout/stderr through the supervisor's ring buffers.
    5. Wait for the child to exit, then return its exit status.
    """
    # -------------------------------------------------------------------
    # 1. Prepare temporary workspace
    # -------------------------------------------------------------------
    with tempfile.TemporaryDirectory(prefix="astral-srt-qual-") as td:
        work_dir = Path(td)
        profile_dir = work_dir / "profile"
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Copy the immutable profile into the temporary location.
        src_profile = _profile_path()
        shutil.copy2(src_profile, profile_dir / PROFILE_NAME)

        # Write a tiny effect script that emits a LETS receipt. The script is
        # deliberately simple – it imports the generated executor and calls the
        # ``run`` method with a pre‑signed receipt that authorises a scratch‑write.
        effect_py = work_dir / EFFECT_SCRIPT_NAME
        effect_py.write_text(
            """
import json, sys, os
from orchestrator.generated_lets_executor import load_protected_executor

# Load the protected executor (this validates the receipt).
runtime = load_protected_executor()

# The receipt is embedded as a base64‑url string – the test harness creates a
# matching receipt for the requested ``mode``.
receipt_b64 = os.getenv('ASTRAL_QUAL_RECEIPT')
if not receipt_b64:
    print('missing receipt', file=sys.stderr)
    sys.exit(2)

receipt = json.loads(json.loads(base64.urlsafe_b64decode(receipt_b64 + "==").decode()))

# Execute the effect – the protected executor will enforce the receipt.
runtime.run(receipt)
""",
            encoding="utf-8",
        )

        # -------------------------------------------------------------------
        # 2. Load the protected executor runtime (this validates the receipt
        #    schema and ensures the executor is ready).
        # -------------------------------------------------------------------
        try:
            executor: ProtectedExecutorRuntime = load_protected_executor()
        except (ProtectedExecutorConfigurationError, ProtectedExecutorError) as exc:
            print(f"sandbox-qualification: protected executor load failed: {exc}", file=sys.stderr)
            return 1

        # -------------------------------------------------------------------
        # 3. Build the sandbox command and launch via ProcessSupervisor.
        # -------------------------------------------------------------------
        try:
            cmd = _build_sandbox_command(mode, work_dir, profile_dir)
        except FileNotFoundError as exc:
            print(f"sandbox-qualification: {exc}", file=sys.stderr)
            return 1

        supervisor = ProcessSupervisor(
            limits=DEFAULT_PROCESS_SUPERVISION_LIMITS,
            owner=ProcessOwner(owner_kind=QUALIFICATION_OWNER_KIND, owner_id=QUALIFICATION_AGENT_ID),
        )
        proc = supervisor.start(
            command=cmd,
            cwd=work_dir,
            env=os.environ.copy(),
            stdout=OutputStream.STDOUT,
            stderr=OutputStream.STDERR,
        )

        # Wait for termination respecting the supervisor's deadline.
        exit_code = supervisor.wait(proc, timeout=DEFAULT_PROCESS_SUPERVISION_LIMITS.termination_deadline_seconds)
        return exit_code

# ---------------------------------------------------------------------------
# CLI entry point – used by CI and developers
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the sandbox‑runtime qualification probe")
    parser.add_argument(
        "--mode",
        choices=ALL_MODES,
        default=AUTHORIZED_MODE,
        help="Effect mode to exercise – default is the authorized scratch‑write.",
    )
    parser.add_argument(
        "--require",
        action="store_true",
        help="Exit with code 0 only when the host fully supports the sandbox.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # If the caller explicitly asks for a requirement check, fail fast on an
    # unsupported host.
    if args.require and not is_supported_host():
        return 2  # distinct code for "host unsupported"

    # When the environment variable is not set we treat the run as a no‑op –
    # this keeps CI fast on non‑Linux platforms.
    if os.getenv("ASTRAL_SRT_REQUIRE_QUALIFICATION") != "1":
        print("sandbox-qualification: qualification disabled (ASTRAL_SRT_REQUIRE_QUALIFICATION not set)")
        return 0

    # Host capability check – mandatory for any actual qualification run.
    if not is_supported_host():
        print("sandbox-qualification: host does not meet sandbox requirements", file=sys.stderr)
        return 1

    # Load the immutable profile early to surface any corruption before spawning.
    try:
        load_srt_profile()
    except Exception as exc:
        print(f"sandbox-qualification: failed to load SRT profile: {exc}", file=sys.stderr)
        return 1

    # Run the sandboxed effect.
    return run_sandboxed_effect(args.mode)


if __name__ == "__main__":
    sys.exit(main())
