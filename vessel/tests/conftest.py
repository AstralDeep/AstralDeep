# Shared fixtures: a tiny agent package whose tools run in the sandbox.

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


def _symlinks_available() -> bool:
    try:
        with tempfile.TemporaryDirectory() as folder:
            os.symlink("nowhere", os.path.join(folder, "link"))
            return True
    except (NotImplementedError, OSError):
        return False


needs_bash = pytest.mark.skipif(not shutil.which("bash"), reason="the bash tool needs a POSIX shell on PATH")
needs_symlink = pytest.mark.skipif(not _symlinks_available(), reason="creating symlinks needs an unavailable privilege")
needs_posix_modes = pytest.mark.skipif(
    sys.platform.startswith("win32"),
    reason="Windows chmod reports 0o666 for every writable file")
needs_posix_units = pytest.mark.skipif(
    sys.platform.startswith("win32"),
    reason="the golden units and launcher paths are systemd and launchd artifacts")
needs_posix_signals = pytest.mark.skipif(
    sys.platform.startswith("win32"),
    reason="signal delivery semantics are POSIX; Windows terminate cannot be ignored")
needs_node_pack = pytest.mark.skipif(
    sys.platform.startswith("win32"),
    reason="the node pack launcher is a POSIX shell script")
needs_sh_scripts = pytest.mark.skipif(
    sys.platform.startswith("win32"),
    reason="the test doubles are POSIX shell scripts")
needs_host_backend = pytest.mark.skipif(
    sys.platform.startswith("win32"),
    reason="the process exec backend confines with Landlock and /proc, which are Linux")
needs_posix_specials = pytest.mark.skipif(
    not _symlinks_available() or not hasattr(os, "mkfifo"),
    reason="the outbox link and fifo checks need symlink and mkfifo support")


def host_platform() -> str:
    # Windows has no node pack; the runtime machinery tests use the Linux pack's shape.
    from astralvessel import bundle as bundle_module
    return bundle_module.platform_name() if hasattr(os, "uname") else "linux-x86_64"


@pytest.fixture
def package_dir(tmp_path: Path) -> Path:
    root = tmp_path / "agent"
    (root / "tools").mkdir(parents=True)
    work = tmp_path / "work"
    (work / "notes").mkdir(parents=True)
    (work / "notes" / "ada-lovelace.md").write_text("---\ntitle: Ada Lovelace\n---\n# Ada\nAnalytical engine.\n")
    (root / "tools" / "echo.toml").write_text(
        f'name = "echo"\ndescription = "Echo the input"\ncommand = ["{Path(sys.executable).as_posix()}", "-c", '
        '"import sys,json; print(json.dumps(json.load(sys.stdin)))"]\n'
        '[parameters]\ntype = "object"\n[parameters.properties.word]\ntype = "string"\n')
    (root / "tools" / "slow.toml").write_text(
        f'name = "slow"\ndescription = "Sleep for ten seconds, then echo the input"\n'
        f'command = ["{Path(sys.executable).as_posix()}", "-c", '
        '"import sys,json,time; time.sleep(10); print(json.dumps(json.load(sys.stdin)))"]\n'
        '[parameters]\ntype = "object"\n[parameters.properties.word]\ntype = "string"\n')
    (root / "vessel.toml").write_text(f"""
[agent]
name = "tester"
description = "A test agent."

[tools]
builtin = ["read", "grep", "glob", "list", "bash"]
declared = ["tools/echo.toml", "tools/slow.toml"]

[sandbox]
workdir = "{work.as_posix()}"
deadline_seconds = 20
tool_timeout_seconds = 5
""")
    return root


def _astral_exec_source() -> Path | None:
    configured = os.environ.get("VESSEL_ASTRAL_EXEC")
    found = Path(configured) if configured else Path(__file__).resolve().parents[2] / "backend" / "astral_exec"
    return found if (found / "__init__.py").is_file() else None


def pytest_configure(config):
    # Tests that use astral_exec import the copy of the Deep checkout's package, never a Deep module otherwise.
    try:
        import astral_exec  # noqa: F401
    except ImportError:
        source = _astral_exec_source()
        if source is not None:
            holder = Path(tempfile.mkdtemp(prefix="vessel-exec-"))
            shutil.copytree(source, holder / "astral_exec", ignore=shutil.ignore_patterns("__pycache__"))
            sys.path.insert(0, str(holder))
