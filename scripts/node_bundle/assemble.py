"""Assembles the relocatable node pack from this Deep checkout, its pinned components and the Kernel artifacts, and seals it with a manifest, a deterministic archive and a download descriptor.
It reads pins.json and the hashed locks beside it, and writes into one output directory that verify.py and the node installers read.
"""

import argparse
import csv
import json
import os
import platform as platform_module
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import manifest as pack_manifest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PINS = HERE / "pins.json"
BINDING = "config/astral-kernel-binding.json"
PYTHON_HOST_PREFIX = "https://github.com/astral-sh/python-build-standalone/releases/download/"
COMPONENT_BUILD_ORDER = ("astral-primitives", "astral-kernel", "astral-projection", "astral-plane", "lets")
PRUNED_SITE_PACKAGES = ("pip", "setuptools", "wheel", "pkg_resources", "_distutils_hack", "distutils-precedence.pth")
APP_PARTS = ("backend", "config", "scripts/node", "pyproject.toml", ".gitmodules")
KERNEL_ARTIFACTS_FORMAT = "astral.kernel-artifacts/v1"
KERNEL_ARTIFACTS_ENV = "ASTRAL_KERNEL_ARTIFACTS"
BYTECODE_PREFIX = "/astral-node-pack"
FULL_PIN = re.compile(r"[0-9a-f]{40}")
STATUS_ARGS = ("status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none")


class PackError(Exception):
    pass


def load_pins(path: Path = PINS) -> dict:
    return json.loads(path.read_text())


def run(command: list, **kwargs) -> None:
    subprocess.run([str(part) for part in command], check=True, **kwargs)


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *arguments], text=True).strip()


def require_full_pin(commit: str) -> None:
    if not FULL_PIN.fullmatch(commit):
        raise PackError("a source pin must be a full lowercase Git commit SHA")


def require_clean_checkout(path: Path, commit: str | None = None) -> Path:
    try:
        source = path.resolve(strict=True)
        if Path(git(source, "rev-parse", "--show-toplevel")).resolve() != source:
            raise PackError("the source must name the checkout root")
        head = git(source, "rev-parse", "HEAD")
        if commit is not None and head != commit:
            raise PackError(f"source is at {head}, expected {commit}")
        if git(source, *STATUS_ARGS):
            raise PackError("the source must be a clean checkout, including untracked files and submodules")
    except (OSError, subprocess.CalledProcessError) as error:
        raise PackError(f"source {path} is not a usable checkout: {error}") from error
    return source


def download_python(pin: dict, cache: Path) -> Path:
    if not pin["url"].startswith(PYTHON_HOST_PREFIX):
        raise PackError("the Python runtime must come from the python-build-standalone GitHub releases")
    target = cache / urllib.parse.unquote(pin["url"].rsplit("/", 1)[1])
    if not target.is_file():
        cache.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".part")
        with urllib.request.urlopen(pin["url"], timeout=120) as response, partial.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        partial.replace(target)
    if pack_manifest.sha256_file(target) != pin["sha256"]:
        target.unlink()
        raise PackError("the Python runtime archive does not match its pinned SHA-256")
    return target


def member_is_contained(member: tarfile.TarInfo, destination: Path) -> bool:
    target = Path(os.path.normpath(destination / member.name))
    if os.path.isabs(member.name) or not (target == destination or destination in target.parents):
        return False
    if member.issym() or member.islnk():
        link = os.path.normpath((target.parent / member.linkname) if member.issym() else (destination / member.linkname))
        return not os.path.isabs(member.linkname) and (Path(link) == destination or destination in Path(link).parents)
    return member.isfile() or member.isdir()


def extract_members(bundle: tarfile.TarFile, destination: Path) -> None:
    if hasattr(tarfile, "data_filter"):
        bundle.extractall(destination, filter="data")
        return
    destination = Path(os.path.normpath(destination.resolve()))
    members = bundle.getmembers()
    for member in members:
        if not member_is_contained(member, destination):
            raise PackError(f"archive member {member.name} is not a contained file, directory or link")
        member.mode = 0o755 if member.isdir() or member.mode & 0o100 else 0o644
    bundle.extractall(destination, members=members)


def extract_archive(repository: Path, commit: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as archive:
        if subprocess.run(["git", "-C", str(repository), "archive", commit], stdout=archive, check=False).returncode != 0:
            raise PackError(f"git archive failed for {repository}")
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode="r:") as bundle:
            extract_members(bundle, destination)


def export_deep(repo: Path, destination: Path) -> str:
    source = require_clean_checkout(repo)
    commit = git(source, "rev-parse", "HEAD")
    extract_archive(source, commit, destination)
    require_clean_checkout(source, commit)
    return commit


def mirror(name: str, repository: str, work: Path) -> Path:
    bare = work / "sources" / f"{name}.git"
    if not bare.is_dir():
        bare.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--quiet", "--bare", repository, bare])
    return bare


def export_component(name: str, repository: str, commit: str, destination: Path, work: Path) -> str:
    require_full_pin(commit)
    override = os.environ.get(f"NODE_BUNDLE_SOURCE_{name.upper().replace('-', '_')}")
    try:
        bare = require_clean_checkout(Path(override), commit) if override else mirror(name, repository, work)
        resolved = subprocess.run(
            ["git", "-C", bare, "rev-parse", "--verify", f"{commit}^{{commit}}"], capture_output=True, text=True, check=False,
        )
        if resolved.returncode != 0:
            run(["git", "-C", bare, "fetch", "--quiet", "origin", commit])
            resolved = subprocess.run(
                ["git", "-C", bare, "rev-parse", "--verify", f"{commit}^{{commit}}"],
                capture_output=True, text=True, check=True,
            )
        full = resolved.stdout.strip()
        if full != commit:
            raise PackError(f"{name} resolved {full}, expected {commit}")
        extract_archive(bare, full, destination)
        if override:
            require_clean_checkout(Path(override), commit)
    except subprocess.CalledProcessError as error:
        raise PackError(f"{name} could not be exported: {error}") from error
    return full


def prune_backend(backend: Path, exclusions: list[dict]) -> None:
    for entry in exclusions:
        target = backend / entry["path"]
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    for tests in sorted(backend.rglob("tests"), reverse=True):
        if tests.is_dir():
            shutil.rmtree(tests)
    for cache in sorted(backend.rglob("__pycache__"), reverse=True):
        shutil.rmtree(cache)


def prune_python(python_root: Path) -> None:
    site = python_root / "lib" / "python3.11" / "site-packages"
    for name in PRUNED_SITE_PACKAGES:
        for match in (site / name, *site.glob(f"{name}-*.dist-info")):
            if match.is_dir():
                shutil.rmtree(match)
            elif match.exists():
                match.unlink()
    for entry in (python_root / "bin").iterdir():
        if not entry.name.startswith("python"):
            entry.unlink()
    for unused in ("test", "idlelib", "turtledemo", "tkinter", "ensurepip"):
        shutil.rmtree(python_root / "lib" / "python3.11" / unused, ignore_errors=True)
    for unused in (
        sorted(python_root.glob("lib/libtcl*")) + sorted(python_root.glob("lib/libtk*"))
        + sorted(python_root.glob("lib/thread*")) + sorted(python_root.glob("lib/tcl*"))
        + sorted(python_root.glob("lib/tk*")) + sorted(python_root.glob("lib/itcl*"))
        + sorted(python_root.glob("lib/python3.11/lib-dynload/_tkinter*"))
    ):
        shutil.rmtree(unused, ignore_errors=True) if unused.is_dir() else unused.unlink()
    shutil.rmtree(python_root / "share", ignore_errors=True)
    shutil.rmtree(python_root / "include", ignore_errors=True)
    for cache in sorted(python_root.rglob("__pycache__"), reverse=True):
        shutil.rmtree(cache)


def strip_install_origins(site_packages: Path) -> None:
    for origin in sorted(site_packages.glob("*.dist-info/direct_url.json")):
        record = origin.with_name("RECORD")
        origin.unlink()
        if record.is_file():
            lines = [line for line in record.read_text().splitlines() if line.split(",")[0] != f"{origin.parent.name}/direct_url.json"]
            record.write_text("\n".join(lines) + "\n")


def prune_records(site_packages: Path) -> None:
    for record in sorted(site_packages.glob("*.dist-info/RECORD")):
        with record.open(newline="") as handle:
            rows = list(csv.reader(handle))
        kept = [row for row in rows if row and os.path.lexists(os.path.normpath(site_packages / row[0]))]
        with record.open("w", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(kept)


def copy_launcher(stage: Path, source_root: Path) -> None:
    target = stage / "launcher"
    target.mkdir()
    for source in sorted(source_root.glob("*.py")):
        shutil.copy2(source, target / source.name)


def bake_bytecode(python: Path, stage: Path) -> None:
    roots = [stage / "python" / "lib" / "python3.11", stage / "app" / "backend", stage / "app" / "scripts",
             stage / "launcher"]
    run([python, "-I", "-B", "-m", "compileall", "-q", "-f", "-j", "0", "--invalidation-mode", "checked-hash",
         "-s", stage, "-p", BYTECODE_PREFIX, *roots])


def scan_for_leaks(stage: Path, forbidden: list[Path]) -> None:
    needles = [str(path).encode() for path in forbidden]
    longest = max(len(needle) for needle in needles)
    leaks = []
    for current, _, names in os.walk(stage, followlinks=False):
        for name in names:
            path = Path(current) / name
            if path.is_symlink():
                continue
            tail = b""
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    window = tail + block
                    if any(needle in window for needle in needles):
                        leaks.append(path.relative_to(stage).as_posix())
                        break
                    tail = window[-longest:]
    if leaks:
        raise PackError("build paths leaked into the pack: " + ", ".join(sorted(leaks)[:10]))


def installed_distributions(python: Path) -> list[dict]:
    code = (
        "import importlib.metadata as m, json;"
        "print(json.dumps(sorted([d.metadata['Name'], d.version] for d in m.distributions())))"
    )
    output = subprocess.run([str(python), "-I", "-c", code], capture_output=True, text=True, check=True)
    return [{"name": name, "version": version} for name, version in json.loads(output.stdout)]


def effective_exclusions(pins: dict, target: dict) -> list[dict]:
    allowed = set(target.get("allowed_packages", []))
    return [entry for entry in pins["excluded_packages"] if entry["name"] not in allowed]


def verify_exclusions(distributions: list[dict], excluded: list[dict]) -> None:
    names = {entry["name"].lower().replace("_", "-") for entry in distributions}
    present = [entry["name"] for entry in excluded if entry["name"].lower().replace("_", "-") in names]
    if present:
        raise PackError(f"excluded packages are present: {', '.join(present)}")


def kernel_source(components_root: Path, pins: dict, spec: dict) -> tuple[str, dict | None]:
    if "binding" not in spec:
        return pins["kernel"]["commit"], None
    binding = json.loads((components_root / BINDING).read_text())
    entries = binding["artifacts"][spec["binding"]]
    return binding["source"]["commit"], entries


def read_kernel_artifacts(directory: Path, pins: dict, spec: dict, commit: str | None = None) -> dict:
    manifest_path = directory / spec["manifest"]
    try:
        receipt = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as error:
        raise PackError(f"{directory} has no readable {spec['manifest']}: {error}") from error
    commit = commit or pins["kernel"]["commit"]
    if receipt.get("format") != KERNEL_ARTIFACTS_FORMAT or receipt.get("kind") != spec["kind"]:
        raise PackError(f"{manifest_path} is not a {spec['kind']} kernel artifact receipt")
    if receipt.get("source_commit") != commit:
        raise PackError(f"kernel artifacts come from {receipt.get('source_commit')}, pins.json names {commit}")
    if receipt.get("target") != spec["target"]:
        raise PackError(f"kernel artifacts target {receipt.get('target')}, expected {spec['target']}")
    wheels = [entry for entry in receipt["artifacts"] if Path(entry["path"]).match(spec["wheel_pattern"])]
    if len(wheels) != 1:
        raise PackError("the kernel receipt must name exactly one wheel")
    wheel = directory / wheels[0]["path"]
    if not wheel.is_file() or pack_manifest.sha256_file(wheel) != wheels[0]["sha256"]:
        raise PackError(f"{wheel.name} does not match the kernel receipt")
    found = {"wheel": wheel, "version": receipt["version"]}
    mailboxes = [entry for entry in receipt["artifacts"] if entry["path"] != wheels[0]["path"]]
    if len(mailboxes) == 1:
        found["mailbox"] = directory / mailboxes[0]["path"]
    return found


def check_binding(kernel: dict, entries: dict, mailbox: Path | None) -> Path:
    mailbox = mailbox or kernel.get("mailbox")
    if mailbox is None:
        raise PackError("a Linux pack needs the Kernel mailbox")
    for label, path in (("wheel", kernel["wheel"]), ("mailbox", mailbox)):
        if path.name != entries[label]["file"] or pack_manifest.sha256_file(path) != entries[label]["sha256"]:
            raise PackError(f"the Kernel {label} {path.name} does not match {BINDING}")
    return mailbox


KERNEL_FILES = ("bin/astral-mailbox", "python/lib/python3.11/site-packages/astral_kernel/libastral_kernel_ffi.so")


KERNEL_LIBRARY = "astral_kernel/libastral_kernel_ffi.so"


def bound_kernel_digests(wheel: Path, mailbox: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        library = pack_manifest.sha256_bytes(archive.read(KERNEL_LIBRARY))
    return {KERNEL_FILES[0]: pack_manifest.sha256_file(mailbox), KERNEL_FILES[1]: library}


def seal_linux(stage: Path, spec: dict, bound: dict[str, str]) -> dict:
    sys.path.insert(0, str(HERE / "linux"))
    import elfaudit
    import natives

    received = {name: pack_manifest.sha256_file(stage / name) for name in KERNEL_FILES if (stage / name).is_file()}
    if received != bound:
        raise PackError("the Kernel files staged in the pack are not the bytes of the bound wheel and mailbox")
    bundled = natives.bundle(stage, spec["natives"], platform_module.machine())
    floor = tuple(int(part) for part in spec["glibc_floor"].split("."))
    problems, newest = elfaudit.audit_tree(stage, floor)
    if problems:
        raise PackError("ELF audit failed: " + "; ".join(problems[:10]))
    return {
        "glibc_floor": spec["glibc_floor"],
        "glibc_required": ".".join(map(str, newest)),
        "natives": {**bundled, "source_image": spec["natives"]["source_image"]},
        "kernel_search_path_patch": {
            name: {"bound_sha256": digest, "shipped_sha256": pack_manifest.sha256_file(stage / name)}
            for name, digest in received.items()
        },
    }


def build_environment(epoch: str) -> dict:
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
    environment.update(SOURCE_DATE_EPOCH=epoch, PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONHASHSEED="0")
    return environment


def assemble(platform: str, output: Path, kernel_dir: Path, work: Path, mailbox: Path | None = None,
             repo: Path = REPO) -> Path:
    stage = work / "stage"
    shutil.rmtree(stage, ignore_errors=True)
    app = stage / "app"
    components_root = work / "components-root"
    shutil.rmtree(components_root, ignore_errors=True)

    deep_commit = export_deep(repo, components_root)
    recipe = components_root / "scripts" / "node_bundle"
    pins = load_pins(recipe / "pins.json")
    if platform not in pins["platforms"]:
        raise PackError(f"{platform} packs are not built yet")
    target = pins["platforms"][platform]
    kernel_commit, binding_entries = kernel_source(components_root, pins, target["kernel_artifacts"])
    require_full_pin(kernel_commit)
    kernel = read_kernel_artifacts(kernel_dir, pins, target["kernel_artifacts"], kernel_commit)
    if binding_entries is not None:
        mailbox = check_binding(kernel, binding_entries, mailbox)
    if mailbox is not None and not mailbox.is_file():
        raise PackError(f"{mailbox} is not a file")
    exclusions = effective_exclusions(pins, target)
    environment = build_environment(git(repo, "log", "-1", "--format=%ct", deep_commit))
    composition = json.loads((components_root / "config" / "astral-composition.json").read_text())
    component_inputs = {}
    for name, component in composition["components"].items():
        full = export_component(
            Path(component["path"]).name, component["repository"], component["commit"],
            components_root / component["path"], work,
        )
        component_inputs[name] = {"repository": component["repository"], "commit": full}

    python_archive = download_python(target["python"], work / "downloads")
    with tarfile.open(python_archive) as bundle:
        extract_members(bundle, stage)
    python_root = stage / "python"
    python = python_root / "bin" / "python3.11"

    node_lock = recipe / target["node_lock"]
    build_lock = recipe / "build-requirements.lock"
    tooling = work / "build-venv"
    shutil.rmtree(tooling, ignore_errors=True)
    run([python, "-m", "venv", tooling], env=environment)
    tool_python = tooling / "bin" / "python"
    run([tool_python, "-m", "pip", "install", "--quiet", "--require-hashes", "--no-deps", "-r", build_lock],
        env=environment)
    wheelhouse = work / "wheelhouse"
    shutil.rmtree(wheelhouse, ignore_errors=True)
    run([tool_python, "-m", "pip", "wheel", "--quiet", "--require-hashes", "--no-build-isolation", "--no-deps",
         "-r", node_lock, "-w", wheelhouse], env=environment)
    component_wheels = work / "component-wheels"
    shutil.rmtree(component_wheels, ignore_errors=True)
    lock = component_wheels / "astral-component-wheels.lock.json"
    installer = components_root / "scripts" / "install_local_components.py"
    run([tool_python, installer, "build", "--root", components_root, "--wheel-dir", component_wheels,
         "--lock", lock], env=environment)

    run([python, "-m", "ensurepip", "--default-pip"], stdout=subprocess.DEVNULL, env=environment)
    run([python, "-m", "pip", "install", "--quiet", "--no-index", "--no-deps", "--no-compile",
         *sorted(wheelhouse.glob("*.whl")), kernel["wheel"]], env=environment)
    run([python, installer, "install", "--root", components_root, "--lock", lock], env=environment)
    run([python, installer, "verify", "--root", components_root, "--lock", lock], env=environment)
    prune_python(python_root)
    site_packages = python_root / "lib" / "python3.11" / "site-packages"
    strip_install_origins(site_packages)
    prune_records(site_packages)
    distributions = installed_distributions(python)
    verify_exclusions(distributions, exclusions)

    for relative in APP_PARTS:
        source = components_root / relative
        if source.is_dir():
            shutil.copytree(source, app / relative)
        elif source.is_file():
            (app / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, app / relative)
    prune_backend(app / "backend", pins["backend_exclusions"])

    if mailbox is not None:
        (stage / "bin").mkdir()
        shutil.copy2(mailbox, stage / "bin" / "astral-mailbox")
        (stage / "bin" / "astral-mailbox").chmod(0o755)
    copy_launcher(stage, components_root / "scripts" / "node_bundle" / "launcher")
    native_inputs = {}
    if "linux" in target:
        native_inputs = seal_linux(stage, target["linux"], bound_kernel_digests(kernel["wheel"], mailbox))
    bake_bytecode(python, stage)
    scan_for_leaks(stage, [work, repo, components_root])

    python_pin = target["python"]
    version = f"{python_pin['version']}-{deep_commit[:8]}-{kernel_commit[:7]}"
    inputs = {
        "version": version,
        "platform": platform,
        "pack_format": pins["pack_format"],
        "python": {**python_pin, "archive_sha256": python_pin["sha256"]},
        "deep": {"repository": pins["deep"]["repository"], "commit": deep_commit},
        "components": component_inputs,
        "component_wheels": json.loads(lock.read_text()),
        "kernel": {
            "repository": pins["kernel"]["repository"],
            "commit": kernel_commit,
            "wheel": {"file": kernel["wheel"].name, "sha256": pack_manifest.sha256_file(kernel["wheel"])},
            "mailbox": (
                {"file": mailbox.name, "sha256": pack_manifest.sha256_file(mailbox)} if mailbox is not None else None
            ),
        },
        "requirements": {
            "node_lock_sha256": pack_manifest.sha256_file(node_lock),
            "build_lock_sha256": pack_manifest.sha256_file(build_lock),
        },
        "installed_distributions": distributions,
        "excluded_packages": exclusions,
        **native_inputs,
        "kept_server_parts": pins["kept_server_parts"],
        "excluded_backend_paths": pins["backend_exclusions"],
    }
    sealed = pack_manifest.write_manifest(stage, inputs)

    output.mkdir(parents=True, exist_ok=True)
    archive_name = f"{pack_manifest.ARCHIVE_PREFIX}-{version}-{platform}.tar.gz"
    archive = pack_manifest.write_archive(stage, output / archive_name)
    manifest_sha = pack_manifest.sha256_file(stage / pack_manifest.MANIFEST_NAME)
    descriptor = pack_manifest.descriptor(sealed, archive, manifest_sha)
    (output / "node-pack.json").write_text(json.dumps(descriptor, indent=1, sort_keys=True) + "\n")
    shutil.copy2(stage / pack_manifest.MANIFEST_NAME, output / "node-pack-manifest.json")
    return output / "node-pack.json"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="macos-arm64")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--kernel-dir", type=Path,
                        default=Path(os.environ[KERNEL_ARTIFACTS_ENV]) if KERNEL_ARTIFACTS_ENV in os.environ else None,
                        help=f"verified Kernel artifacts folder, or set {KERNEL_ARTIFACTS_ENV}")
    parser.add_argument("--mailbox", type=Path, help="astral-mailbox binary to ship in bin/")
    arguments = parser.parse_args(argv)
    if arguments.kernel_dir is None:
        sys.exit(f"node pack: pass --kernel-dir or set {KERNEL_ARTIFACTS_ENV}")
    try:
        descriptor = assemble(arguments.platform, arguments.out, arguments.kernel_dir, arguments.work,
                              arguments.mailbox)
    except (PackError, subprocess.CalledProcessError, OSError) as error:
        sys.exit(f"node pack: {error}")
    summary = json.loads(descriptor.read_text())
    print(f"{descriptor}: archive {summary['archive']['size']} bytes, installed {summary['installed_bytes']} bytes")


if __name__ == "__main__":
    main()
