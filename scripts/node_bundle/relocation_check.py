"""Unpacks an assembled node pack into a path containing a space and runs it from there with a minimal environment and bytecode writing off.
It proves the interpreter, the native libraries and the node's own modules resolve from the pack alone and that nothing writes into the tree.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import manifest as pack_manifest
import verify

sys.path.insert(0, str(Path(__file__).resolve().parent / "linux"))
import elfaudit  # noqa: E402

SPACED_NAME = "relocated copy"
CHILD_ENV = {"PATH": "/usr/bin:/bin"}
BAKED_ROOTS = ("python/lib/python3.11", "app/backend", "launcher")
CHECKED_HASH_FLAGS = 0b11
MODULES = ("astral_kernel", "rote", "astralplane", "astralprojection", "astralprims", "lets")
BACKEND_MODULES = ("astral_exec", "orchestrator.orchestrator")
CHILD_TIMEOUT = 300

CHILD_SCRIPT = r"""
import importlib, os, sys

root = os.path.realpath(sys.argv[1])
modules, backend_modules = sys.argv[2].split(","), sys.argv[3].split(",")
failed = []

def inside(path):
    return os.path.realpath(path).startswith(root + os.sep)

def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (" " + detail if detail else ""))
    if not ok:
        failed.append(label)

check("interpreter inside the pack", inside(sys.executable), sys.executable)
check("bytecode writing is off", sys.dont_write_bytecode)
check("isolated mode", bool(sys.flags.isolated))
for name in modules:
    loaded = importlib.import_module(name)
    check("import " + name, inside(loaded.__file__), os.path.dirname(loaded.__file__))
sys.path.insert(0, os.path.join(root, "app", "backend"))
os.chdir(os.path.join(root, "app", "backend"))
for name in backend_modules:
    loaded = importlib.import_module(name)
    check("import " + name, inside(loaded.__file__), os.path.dirname(loaded.__file__))
sys.exit(1 if failed else 0)
"""


NATIVE_TOOLS = ("pdftoppm", "pdfinfo")
NATIVE_SCRIPT = r"""
import ctypes, os, sys
root = sys.argv[1]
ctypes.CDLL(os.path.join(root, "native", "lib", "libmagic.so.1"))
import magic
kind = magic.from_buffer(b"%PDF-1.4\n1 0 obj\n", mime=True)
print("magic:", kind)
sys.exit(0 if kind == "application/pdf" else 1)
"""


LOADED_PATTERN = re.compile(r"calling init: (\S+)")
LOAD_PROBE = "import astral_kernel, PIL._imaging, bcrypt, cryptography.hazmat.bindings._rust, psycopg2, numpy"


class RelocationError(Exception):
    pass


def locate_pack(source: Path, scratch: Path) -> tuple[Path, dict]:
    target = scratch / SPACED_NAME / "pack"
    if (source / "node-pack.json").is_file():
        verify.install(str(source / "node-pack.json"), None, target)
    elif (source / "python" / "bin" / "python3.11").is_file():
        target.parent.mkdir(parents=True)
        shutil.copytree(source, target, symlinks=True)
    else:
        raise RelocationError(f"{source} holds neither node-pack.json nor an unpacked pack")
    manifest = json.loads((target / pack_manifest.MANIFEST_NAME).read_text())
    return target, manifest


def bytecode_files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("__pycache__/*")}


def baked_bytecode_problems(root: Path) -> list[str]:
    problems = []
    for relative in BAKED_ROOTS:
        if not (root / relative).is_dir():
            continue
        caches = sorted((root / relative).rglob("__pycache__/*.pyc"))
        if not caches:
            problems.append(f"no baked bytecode under {relative}")
        problems += [
            f"bytecode is not checked-hash: {cache.relative_to(root).as_posix()}"
            for cache in caches if int.from_bytes(cache.read_bytes()[4:8], "little") != CHECKED_HASH_FLAGS
        ]
    return problems


def run_child(root: Path, modules: tuple, backend_modules: tuple) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(root / "python" / "bin" / "python3.11"), "-I", "-B", "-c", CHILD_SCRIPT, str(root),
         ",".join(modules), ",".join(backend_modules)],
        env=CHILD_ENV, cwd=root.parent, capture_output=True, text=True, timeout=CHILD_TIMEOUT, check=False,
    )


def run_launcher_help(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(root / "python" / "bin" / "python3.11"), "-I", "-B", str(root / "launcher" / "astral_node_launch.py"),
         "--help"],
        env=CHILD_ENV, cwd=root.parent, capture_output=True, text=True, timeout=CHILD_TIMEOUT, check=False,
    )


def native_problems(root: Path) -> list[str]:
    problems = []
    for tool in NATIVE_TOOLS:
        result = subprocess.run(
            [str(root / "native" / "bin" / tool), "-v"], env=CHILD_ENV, capture_output=True, text=True,
            timeout=CHILD_TIMEOUT, check=False,
        )
        if result.returncode != 0:
            problems.append(f"native {tool} -v exited {result.returncode}: {result.stderr.strip()[-200:]}")
    magic = subprocess.run(
        [str(root / "python" / "bin" / "python3.11"), "-I", "-B", "-c", NATIVE_SCRIPT, str(root)],
        env={**CHILD_ENV, "MAGIC": str(root / "native" / "share" / "magic.mgc")}, capture_output=True, text=True,
        timeout=CHILD_TIMEOUT, check=False,
    )
    if magic.returncode != 0:
        problems.append(f"libmagic did not identify a PDF from the pack: {(magic.stdout + magic.stderr).strip()[-300:]}")
    return problems


def foreign_libraries(trace: str, root: Path) -> list[str]:
    found = []
    for path in sorted(set(LOADED_PATTERN.findall(trace))):
        name = os.path.basename(path)
        inside = os.path.realpath(path).startswith(str(root.resolve()) + os.sep)
        if not inside and name not in elfaudit.SYSTEM_LIBRARIES and not name.startswith("ld-linux"):
            found.append(path)
    return found


def loaded_library_problems(root: Path) -> list[str]:
    environment = {**CHILD_ENV, "LD_DEBUG": "libs"}
    runs = {
        "interpreter": [str(root / "python" / "bin" / "python3.11"), "-I", "-B", "-c", LOAD_PROBE],
        "mailbox": [str(root / "bin" / "astral-mailbox"), "--help"],
    }
    problems = []
    for label, command in runs.items():
        if not Path(command[0]).is_file():
            continue
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=CHILD_TIMEOUT, check=False)
        if label == "interpreter" and result.returncode != 0:
            problems.append(f"loading the native modules failed: {result.stderr.strip()[-300:]}")
        problems += [f"{label} loaded {path} from outside the pack" for path in foreign_libraries(result.stderr, root)]
    return problems


def check_pack(source: Path, modules: tuple = MODULES, backend_modules: tuple = BACKEND_MODULES) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="node-pack-relocation-") as scratch:
        root, manifest = locate_pack(source, Path(scratch))
        problems = baked_bytecode_problems(root)
        print(f"{'PASS' if not problems else 'FAIL'} baked bytecode is present and checked-hash")
        cached = bytecode_files(root)
        child = run_child(root, modules, backend_modules)
        print(child.stdout, end="")
        failures = problems[: verify.PROBLEM_LIMIT]
        failures += [] if child.returncode == 0 else [f"child exited {child.returncode}: {child.stderr.strip()[-400:]}"]
        launcher = run_launcher_help(root)
        print(f"{'PASS' if launcher.returncode == 0 else 'FAIL'} launcher --help")
        if launcher.returncode != 0:
            failures.append(f"launcher --help exited {launcher.returncode}: {launcher.stderr.strip()[-400:]}")
        if (root / "native").is_dir():
            native = native_problems(root) + loaded_library_problems(root)
            print(f"{'PASS' if not native else 'FAIL'} native tools and libmagic run from the pack")
            failures += native
        caches = sorted(bytecode_files(root) - cached)
        print(f"{'PASS' if not caches else 'FAIL'} no bytecode written")
        if caches:
            failures.append("bytecode caches appeared: " + ", ".join(caches[:5]))
        problems = verify.tree_problems(root, manifest)
        print(f"{'PASS' if not problems else 'FAIL'} tree still matches its manifest")
        failures += problems[: verify.PROBLEM_LIMIT]
        return failures


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", type=Path, help="assembled output folder or unpacked pack root")
    arguments = parser.parse_args(argv)
    try:
        failures = check_pack(arguments.pack)
    except (RelocationError, verify.VerifyError, OSError, subprocess.SubprocessError) as error:
        sys.exit(f"relocation: {error}")
    if failures:
        sys.exit("relocation: " + "; ".join(failures))
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
