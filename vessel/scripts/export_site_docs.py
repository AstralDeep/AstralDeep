"""Builds the documentation site from one commit's docs and writes astral-docs.json beside the pages.
The manifest names the source commit, subdirectory, requirement pins and every file's size and SHA-256."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

MANIFEST_NAME = "astral-docs.json"
MANIFEST_FORMAT = "astraldeep.docs-export/v1"
REPOSITORY = Path(__file__).resolve().parents[2]


class ExportError(ValueError):
    pass


def git(repository: Path, *arguments: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(["git", "-C", str(repository), *arguments], capture_output=True)
    if result.returncode != 0:
        raise ExportError(f"git {arguments[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout if binary else result.stdout.decode()


def resolve_commit(repository: Path, commit: str) -> str:
    return git(repository, "rev-parse", "--verify", f"{commit}^{{commit}}").strip()


def require_clean(repository: Path) -> None:
    if git(repository, "status", "--porcelain").strip():
        raise ExportError("the working tree has uncommitted changes")


def clean_subdir(subdir: str) -> str:
    path = PurePosixPath(subdir)
    if path.is_absolute() or ".." in path.parts:
        raise ExportError("the subdirectory must stay inside the repository")
    return "." if str(path) == "." else str(path)


def extract(repository: Path, commit: str, subdir: str, target: Path) -> Path:
    arguments = ["archive", "--format=tar", commit]
    if subdir != ".":
        arguments += ["--", subdir]
    archive = git(repository, *arguments, binary=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter="data")
    root = target if subdir == "." else target / subdir
    if not (root / "mkdocs.yml").is_file():
        raise ExportError(f"{subdir} holds no mkdocs.yml at {commit}")
    return root


def requirement_pins(root: Path) -> list[str]:
    path = root / "docs" / "requirements.txt"
    if not path.is_file():
        raise ExportError("the commit has no docs/requirements.txt")
    lines = (line.strip() for line in path.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def build_site(root: Path, pins: list[str], out: Path, scratch: Path) -> None:
    environment = scratch / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    python = environment / "bin" / "python"
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", *pins], check=True)
    subprocess.run(
        [str(python), "-m", "mkdocs", "build", "--strict", "-f", "mkdocs.yml", "-d", str(out)],
        check=True,
        cwd=root,
    )


def file_entries(out: Path) -> list[dict[str, object]]:
    entries = []
    for path in sorted(p for p in out.rglob("*") if p.is_file() and p.name != MANIFEST_NAME):
        content = path.read_bytes()
        entries.append(
            {
                "path": path.relative_to(out).as_posix(),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return entries


def export(repository: Path, commit: str, subdir: str, out: Path) -> dict[str, object]:
    subdir = clean_subdir(subdir)
    require_clean(repository)
    full = resolve_commit(repository, commit)
    if out.exists() and any(out.iterdir()):
        raise ExportError("the output directory is not empty")
    with tempfile.TemporaryDirectory() as scratch_name:
        scratch = Path(scratch_name)
        root = extract(repository, full, subdir, scratch / "source")
        pins = requirement_pins(root)
        out.mkdir(parents=True, exist_ok=True)
        build_site(root, pins, out, scratch)
    manifest = {
        "format": MANIFEST_FORMAT,
        "commit": full,
        "subdirectory": subdir,
        "requirements": pins,
        "files": file_entries(out),
    }
    (out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--commit", required=True)
    parser.add_argument("--subdir", required=True, help="'.' for the old stand-alone layout, 'vessel' for Deep")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    arguments = parser.parse_args(argv)
    try:
        manifest = export(arguments.repository, arguments.commit, arguments.subdir, arguments.out)
    except (ExportError, subprocess.CalledProcessError) as exc:
        print(f"export refused: {exc}", file=sys.stderr)
        return 1
    print(f"exported {len(manifest['files'])} files from {manifest['commit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
