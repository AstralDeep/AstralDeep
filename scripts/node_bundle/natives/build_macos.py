"""Builds the macOS node pack's native tools from the pinned sources in sources.lock.json and checks an assembled pack against the macOS 14 rules.
assemble.py calls build() for the natives folder it stages into the pack, and --check runs every pack rule and a relocated functional probe.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import assemble  # noqa: E402
import manifest as pack_manifest  # noqa: E402
import relocation_check  # noqa: E402
import verify  # noqa: E402
from natives import macho  # noqa: E402

LOCK_FORMAT = "astral.nodepack-natives-sources/v1"
RECEIPT_FORMAT = "astral.nodepack-natives/v1"
LICENSE_ROOT = "licenses/natives"
RECEIPT_PATH = f"{LICENSE_ROOT}/build-receipt.json"
LOCK_COPY_PATH = f"{LICENSE_ROOT}/sources.lock.json"
PROBE = HERE / "probe.py"
HOST_PREFIXES = "/opt/homebrew;/usr/local"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
BUILD_TOOLS = ("cmake", "ninja")
DIGEST = re.compile(r"[0-9a-f]{64}")
TARGET = re.compile(r"\d{1,2}\.\d{1,2}")
SOURCE_KINDS = {"source", "binary"}
PROBE_TIMEOUT = 900
PROBE_ENV = {"PATH": "/usr/bin:/bin"}
DISABLED_POPPLER_BACKENDS = (
    "ENABLE_BOOST", "ENABLE_CPP", "ENABLE_GLIB", "ENABLE_GOBJECT_INTROSPECTION", "ENABLE_QT5", "ENABLE_QT6",
    "ENABLE_BROTLI", "ENABLE_LIBOPENJPEG", "ENABLE_LIBJPEG", "ENABLE_LCMS", "ENABLE_LIBCURL", "ENABLE_LIBTIFF",
    "ENABLE_NSS3", "ENABLE_GPGME", "ENABLE_HARFBUZZ", "BUILD_MANUAL_TESTS", "BUILD_GTK_TESTS", "BUILD_QT5_TESTS",
    "BUILD_QT6_TESTS", "BUILD_CPP_TESTS",
)
DISABLED_FREETYPE_LIBRARIES = ("ZLIB", "BZIP2", "PNG", "HARFBUZZ", "BROTLI", "HVF")
DISABLED_LIBMAGIC_LIBRARIES = ("zlib", "bzlib", "xzlib", "zstdlib", "lzlib", "libseccomp")
LIBMAGIC_FUNCTION_OVERRIDES = ["ac_cv_func_pipe2=no"]
POPPLER_TOOLS = ("pdfinfo", "pdftoppm")


class NativesError(assemble.PackError):
    pass


def validate_lock(lock: dict) -> dict:
    if lock.get("format") != LOCK_FORMAT:
        raise NativesError(f"the natives lock is not {LOCK_FORMAT}")
    if not TARGET.fullmatch(str(lock.get("deployment_target", ""))):
        raise NativesError("the natives lock needs a deployment_target such as 14.0")
    if not str(lock.get("prefix", "")).startswith("/") or not str(lock.get("source_date_epoch", "")).isdigit():
        raise NativesError("the natives lock needs an absolute prefix and a numeric source_date_epoch")
    names = set()
    for entry in lock.get("sources") or []:
        name = entry.get("name", "")
        if not name or name in names:
            raise NativesError(f"source name {name!r} is missing or repeated")
        names.add(name)
        if entry.get("kind") not in SOURCE_KINDS or not entry.get("version") or not entry.get("top"):
            raise NativesError(f"{name} needs a kind, a version and a top folder")
        if not str(entry.get("url", "")).startswith("https://"):
            raise NativesError(f"{name} must be fetched over https")
        if not isinstance(entry.get("size"), int) or entry["size"] <= 0 or not DIGEST.fullmatch(str(entry.get("sha256"))):
            raise NativesError(f"{name} needs a positive size and a lowercase SHA-256")
        files = entry.get("license_files") or []
        if not files or any(Path(item).is_absolute() or ".." in Path(item).parts for item in files):
            raise NativesError(f"{name} needs relative license files")
    if not names or not lock.get("shipped"):
        raise NativesError("the natives lock names no sources or no shipped files")
    return lock


def load_lock(path: Path) -> dict:
    try:
        return validate_lock(json.loads(path.read_text()))
    except (OSError, ValueError) as error:
        raise NativesError(f"{path} is not a readable natives lock: {error}") from error


def unpack(entry: dict, archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True)
    try:
        with tarfile.open(archive) as bundle:
            bundle.extractall(destination, filter="data")
    except (tarfile.TarError, OSError) as error:
        raise NativesError(f"{archive.name} could not be unpacked: {error}") from error
    top = destination / entry["top"]
    if not top.is_dir():
        raise NativesError(f"{archive.name} does not unpack to {entry['top']}")
    return top


def run(command: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(part) for part in command], check=True, **kwargs)


def tool_versions(tools: Path) -> dict:
    def first_line(command: list) -> str:
        output = subprocess.run([str(part) for part in command], capture_output=True, text=True, check=True)
        return (output.stdout or output.stderr).strip().splitlines()[0]

    return {
        "cmake": first_line([tools / "cmake", "--version"]),
        "ninja": first_line([tools / "ninja", "--version"]),
        "clang": first_line(["/usr/bin/clang", "--version"]),
        "make": first_line(["/usr/bin/make", "--version"]),
        "sdk": first_line(["/usr/bin/xcrun", "--show-sdk-version"]),
    }


def link_tools(directory: Path) -> Path:
    directory.mkdir(parents=True)
    for name in BUILD_TOOLS:
        found = shutil.which(name)
        if found is None:
            raise NativesError(f"{name} is required to build the native tools")
        (directory / name).symlink_to(found)
    return directory


def build_environment(lock: dict, tools: Path, deps: Path, work: Path, sdk: str) -> dict:
    target = lock["deployment_target"]
    maps = f"-ffile-prefix-map={work}=/build -fdebug-prefix-map={work}=/build"
    flags = f"-arch arm64 -mmacosx-version-min={target} -isysroot {sdk} -O2 {maps} -Werror=unguarded-availability-new"
    return {
        "PATH": f"{tools}:{SYSTEM_PATH}",
        "HOME": str(work / "home"),
        "TMPDIR": str(work / "tmp"),
        "LANG": "C",
        "LC_ALL": "C",
        "SOURCE_DATE_EPOCH": lock["source_date_epoch"],
        "ZERO_AR_DATE": "1",
        "MACOSX_DEPLOYMENT_TARGET": target,
        "SDKROOT": sdk,
        "CC": "/usr/bin/clang",
        "CXX": "/usr/bin/clang++",
        "CFLAGS": flags,
        "CXXFLAGS": flags,
        "LDFLAGS": f"-arch arm64 -mmacosx-version-min={target} -isysroot {sdk}",
        "PKG_CONFIG_LIBDIR": str(deps / "lib" / "pkgconfig"),
    }


def cmake_options(lock: dict, deps: Path, prefix: str) -> list[str]:
    return [
        "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release", f"-DCMAKE_OSX_DEPLOYMENT_TARGET={lock['deployment_target']}",
        "-DCMAKE_OSX_ARCHITECTURES=arm64", f"-DCMAKE_IGNORE_PREFIX_PATH={HOST_PREFIXES}", "-DBUILD_SHARED_LIBS=OFF",
        "-DCMAKE_SKIP_RPATH=ON", f"-DCMAKE_PREFIX_PATH={deps}", f"-DCMAKE_INSTALL_PREFIX={prefix}",
    ]


def freetype_options() -> list[str]:
    return [f"-DFT_DISABLE_{name}=ON" for name in DISABLED_FREETYPE_LIBRARIES]


def poppler_options() -> list[str]:
    options = [f"-D{name}=OFF" for name in DISABLED_POPPLER_BACKENDS]
    return options + ["-DFONT_CONFIGURATION=generic", "-DENABLE_UTILS=ON", "-DCMAKE_DISABLE_FIND_PACKAGE_PNG=ON",
                      "-DCMAKE_DISABLE_FIND_PACKAGE_Cairo=ON", "-DCMAKE_DISABLE_FIND_PACKAGE_Boost=ON"]


def libmagic_options(lock: dict) -> list[str]:
    options = [f"--prefix={lock['prefix']}", "--enable-shared", "--disable-static", "--disable-dependency-tracking"]
    return options + [f"--disable-{name}" for name in DISABLED_LIBMAGIC_LIBRARIES] + LIBMAGIC_FUNCTION_OVERRIDES


def build_freetype(lock: dict, source: Path, work: Path, deps: Path, environment: dict) -> None:
    build = work / "build-freetype"
    run(["cmake", "-S", source, "-B", build, *cmake_options(lock, deps, str(deps)), *freetype_options()], env=environment)
    run(["cmake", "--build", build], env=environment)
    run(["cmake", "--install", build], env=environment)


def build_poppler(lock: dict, source: Path, work: Path, deps: Path, environment: dict) -> Path:
    build = work / "build-poppler"
    run(["cmake", "-S", source, "-B", build, *cmake_options(lock, deps, lock["prefix"]), *poppler_options()], env=environment)
    run(["cmake", "--build", build, "--target", *POPPLER_TOOLS], env=environment)
    return build / "utils"


def build_libmagic(lock: dict, source: Path, work: Path, environment: dict) -> Path:
    build, destination = work / "build-libmagic", work / "install-libmagic"
    build.mkdir()
    run([source / "configure", *libmagic_options(lock)], cwd=build, env=environment, stdout=subprocess.DEVNULL)
    run(["make", f"-j{os.cpu_count() or 2}"], cwd=build, env=environment, stdout=subprocess.DEVNULL)
    run(["make", "install", f"DESTDIR={destination}"], cwd=build, env=environment, stdout=subprocess.DEVNULL)
    return destination / lock["prefix"].lstrip("/")


def seal_binary(path: Path, environment: dict) -> None:
    run(["strip", "-x", path], env=environment)
    run(["codesign", "--force", "--sign", "-", path], env=environment, stderr=subprocess.DEVNULL)


def extract_ripgrep(entry: dict, archive: Path, scratch: Path, destination: Path) -> None:
    top = unpack(entry, archive, scratch)
    binary = top / "rg"
    if not binary.is_file():
        raise NativesError("the ripgrep release has no rg binary")
    shutil.copy2(binary, destination / "bin" / "rg")
    (destination / "bin" / "rg").chmod(0o755)


def copy_licenses(entry: dict, top: Path, out: Path) -> list[str]:
    target = out / LICENSE_ROOT / f"{entry['name']}-{entry['version']}"
    target.mkdir(parents=True)
    shipped = []
    for item in entry["license_files"]:
        source = top / item
        if not source.is_file() or source.stat().st_size == 0:
            raise NativesError(f"{entry['name']} {entry['version']} has no license file {item}")
        shutil.copy2(source, target / Path(item).name)
        shipped.append(f"{LICENSE_ROOT}/{target.name}/{Path(item).name}")
    return shipped


def file_digests(out: Path, shipped: list[str]) -> list[dict]:
    missing = [name for name in shipped if not (out / name).exists()]
    if missing:
        raise NativesError("the build did not produce " + ", ".join(missing))
    return [{"path": name, "sha256": pack_manifest.sha256_file((out / name).resolve())} for name in sorted(shipped)]


def write_receipt(out: Path, lock: dict, lock_sha: str, licenses: dict, tools: dict) -> None:
    receipt = {
        "format": RECEIPT_FORMAT,
        "lock_sha256": lock_sha,
        "deployment_target": lock["deployment_target"],
        "tools": tools,
        "sources": [
            {"name": entry["name"], "version": entry["version"], "kind": entry["kind"], "sha256": entry["sha256"],
             "license": entry["license"], "license_files": licenses[entry["name"]]}
            for entry in lock["sources"]
        ],
        "files": file_digests(out, lock["shipped"]),
    }
    (out / RECEIPT_PATH).write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")


def build(lock_path: Path, out: Path, cache: Path, work: Path) -> Path:
    lock = load_lock(lock_path)
    for stale in (out, work):
        shutil.rmtree(stale, ignore_errors=True)
    for name in ("home", "tmp", "src", "deps"):
        (work / name).mkdir(parents=True)
    archives = {entry["name"]: assemble.fetch_pinned(entry, cache) for entry in lock["sources"]}
    tools = link_tools(work / "tools")
    sdk = run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, text=True).stdout.strip()
    deps = work / "deps"
    environment = build_environment(lock, tools, deps, work, sdk)
    entries = {entry["name"]: entry for entry in lock["sources"]}
    tops = {name: unpack(entries[name], archives[name], work / "src" / name)
            for name in ("freetype", "poppler", "libmagic")}

    build_freetype(lock, tops["freetype"], work, deps, environment)
    utilities = build_poppler(lock, tops["poppler"], work, deps, environment)
    installed = build_libmagic(lock, tops["libmagic"], work, environment)

    for directory in ("bin", "lib", "share/magic"):
        (out / directory).mkdir(parents=True)
    for tool in POPPLER_TOOLS:
        shutil.copy2(utilities / tool, out / "bin" / tool)
        seal_binary(out / "bin" / tool, environment)
    library = out / "lib" / "libmagic.1.dylib"
    shutil.copy2((installed / "lib" / "libmagic.1.dylib").resolve(), library)
    run(["install_name_tool", "-id", "@loader_path/libmagic.1.dylib", library], env=environment)
    seal_binary(library, environment)
    (out / "lib" / "libmagic.dylib").symlink_to("libmagic.1.dylib")
    shutil.copy2(installed / "share" / "misc" / "magic.mgc", out / "share" / "magic" / "magic.mgc")
    extract_ripgrep(entries["ripgrep"], archives["ripgrep"], work / "src" / "ripgrep", out)

    licenses = {}
    for name, entry in entries.items():
        top = tops.get(name) or work / "src" / name / entry["top"]
        licenses[name] = copy_licenses(entry, top, out)
    shutil.copy2(lock_path, out / LOCK_COPY_PATH)
    write_receipt(out, lock, pack_manifest.sha256_file(lock_path), licenses, tool_versions(tools))
    assemble.scan_for_leaks(out, [work, lock_path.parent])
    return out


def read_receipt(natives: Path) -> dict:
    try:
        receipt = json.loads((natives / RECEIPT_PATH).read_text())
    except (OSError, ValueError) as error:
        raise NativesError(f"{natives} has no readable natives receipt: {error}") from error
    if receipt.get("format") != RECEIPT_FORMAT:
        raise NativesError(f"{natives} receipt is not {RECEIPT_FORMAT}")
    return receipt


def require_receipt_for(natives: Path, lock_path: Path) -> dict:
    receipt = read_receipt(natives)
    if receipt["lock_sha256"] != pack_manifest.sha256_file(lock_path):
        raise NativesError("the natives folder was built from a different sources lock")
    stale = [item["path"] for item in receipt["files"]
             if not (natives / item["path"]).exists()
             or pack_manifest.sha256_file((natives / item["path"]).resolve()) != item["sha256"]]
    if stale:
        raise NativesError("the natives folder changed after it was built: " + ", ".join(stale))
    return receipt


def summary(natives: Path) -> dict:
    receipt = read_receipt(natives)
    return {key: receipt[key] for key in ("lock_sha256", "deployment_target", "tools", "sources", "files")}


def license_problems(root: Path) -> list[str]:
    try:
        receipt = read_receipt(root)
        lock = json.loads((root / LOCK_COPY_PATH).read_text())
    except (NativesError, OSError, ValueError) as error:
        return [str(error)]
    problems = [f"shipped file {name} is missing" for name in lock.get("shipped", []) if not (root / name).exists()]
    for entry in receipt["sources"]:
        for name in entry["license_files"]:
            path = root / name
            if not path.is_file() or path.stat().st_size == 0:
                problems.append(f"license file {name} is missing or empty")
        if not entry["license_files"]:
            problems.append(f"{entry['name']} ships no license file")
    return problems


def pack_problems(root: Path) -> list[str]:
    problems, scanned = macho.pack_problems(root)
    print(f"{'PASS' if not problems and scanned else 'FAIL'} {scanned} Mach-O files: minos 14.0 or lower, "
          "no host or @rpath load commands")
    if not scanned:
        problems.append("the pack holds no Mach-O files")
    found = license_problems(root)
    print(f"{'PASS' if not found else 'FAIL'} native license files and source lock are in the pack")
    return problems[: verify.PROBLEM_LIMIT] + found


def run_probe(root: Path, python: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(python), "-I", "-B", str(PROBE), str(root)], env=PROBE_ENV, cwd=root.parent, capture_output=True,
        text=True, timeout=PROBE_TIMEOUT, check=False,
    )


def check_pack(source: Path) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="node-pack-natives-") as scratch:
        root, manifest = relocation_check.locate_pack(source, Path(scratch))
        failures = pack_problems(root)
        probe = run_probe(root, root / "python" / "bin" / "python3.11")
        print(probe.stdout, end="")
        if probe.returncode != 0:
            failures.append(f"probe exited {probe.returncode}: {probe.stderr.strip()[-600:]}")
        problems = verify.tree_problems(root, manifest)
        print(f"{'PASS' if not problems else 'FAIL'} tree still matches its manifest")
        return failures + problems[: verify.PROBLEM_LIMIT]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--check", type=Path, metavar="PACK", help="assembled output folder or unpacked pack root")
    commands.add_argument("--build", type=Path, metavar="OUT", help="build the natives folder into OUT")
    parser.add_argument("--lock", type=Path, default=HERE / "sources.lock.json")
    parser.add_argument("--work", type=Path, help="build scratch folder, required with --build")
    parser.add_argument("--cache", type=Path, help="source download cache, required with --build")
    arguments = parser.parse_args(argv)
    try:
        if arguments.build is not None:
            if arguments.work is None or arguments.cache is None:
                sys.exit("natives: --build needs --work and --cache")
            print(build(arguments.lock, arguments.build, arguments.cache, arguments.work))
            return
        failures = check_pack(arguments.check)
    except (assemble.PackError, relocation_check.RelocationError, verify.VerifyError, OSError,
            subprocess.SubprocessError) as error:
        sys.exit(f"natives: {error}")
    if failures:
        sys.exit("natives: " + "; ".join(failures))
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
