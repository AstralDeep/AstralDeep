"""Starts the packed AstralDeep orchestrator with the personal profile from an env file, bound to a loopback address and port.

The node's supervisor runs it with the pack's own Python in isolated mode with bytecode writing off. The orchestrator itself is unchanged and runs from the pack's backend directory.
"""

import argparse
import os
import runpy
import sys
from collections.abc import Mapping
from pathlib import Path

DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
PACK_ROOT = Path(__file__).resolve().parent.parent
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key.replace("_", "").isalnum():
            raise ValueError(f"{path.name} line {number} is not KEY=VALUE")
        values[key] = value.strip().strip("'\"")
    return values


def native_environment(root: Path = PACK_ROOT, base: Mapping[str, str] | None = None) -> dict[str, str]:
    base = os.environ if base is None else base
    values: dict[str, str] = {}
    if (root / "bin").is_dir():
        values["PATH"] = os.pathsep.join([str(root / "bin"), base.get("PATH") or DEFAULT_PATH])
    if (root / "lib" / "libmagic.dylib").exists():
        values["DYLD_FALLBACK_LIBRARY_PATH"] = os.pathsep.join([str(root / "lib"), "/usr/lib", "/lib"])
    database = root / "share" / "magic" / "magic.mgc"
    if database.is_file():
        values["MAGIC"] = str(database)
    return values


def configure(arguments: argparse.Namespace, root: Path = PACK_ROOT) -> dict[str, str]:
    if arguments.host not in LOOPBACK_HOSTS and not arguments.allow_remote:
        raise SystemExit(f"refusing to bind {arguments.host}: pass --allow-remote to serve other machines")
    values = parse_env_file(arguments.env_file)
    if values.get("ASTRAL_NODE_PROFILE") != "personal":
        raise SystemExit("the env file does not select the personal profile")
    values["ORCHESTRATOR_PORT"] = str(arguments.port)
    values["PYTHONDONTWRITEBYTECODE"] = "1"
    values["PYTHONUNBUFFERED"] = "1"
    for key, value in native_environment(root).items():
        values.setdefault(key, value)
    mailbox = root / "bin" / "astral-mailbox"
    if "ASTRAL_MAILBOX_BIN" not in values and mailbox.is_file():
        values["ASTRAL_MAILBOX_BIN"] = str(mailbox)
    return values


def pin_bind_host(host: str) -> None:
    import uvicorn

    original = uvicorn.Config.__init__

    def bound(self, *args, **kwargs):
        kwargs["host"] = host
        original(self, *args, **kwargs)

    uvicorn.Config.__init__ = bound


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--log-dir", type=Path)
    arguments = parser.parse_args(argv)

    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    values = configure(arguments)
    os.environ.update(values)
    if arguments.log_dir is not None:
        from node_logging import capture_output, secret_values

        capture_output(arguments.log_dir, secret_values(values))
    backend = PACK_ROOT / "app" / "backend"
    os.chdir(backend)
    sys.path.insert(0, str(backend))
    pin_bind_host(arguments.host)
    runpy.run_path(str(backend / "orchestrator" / "orchestrator.py"), run_name="__main__")


if __name__ == "__main__":
    main()
