#!/usr/bin/env python3
"""Validates retained Apple build artifacts and raw Xcode test observations for release
evidence; this is diagnostic collection only, never authorization, and is exercised
by test_apple_coverage_artifacts_088.py.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import posixpath
import re
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

VERSION = "astral.apple-coverage-artifacts/v1"
COMPONENT = "components/AstralProjection"
MAX_FILES = 20000
MAX_BYTES = 512 * 1024 * 1024
MAX_TEST_NODES = 50000
UI_SUITES = frozenset(
    {
        "Accessibility060UITests",
        "ConversationContinuityUITests",
        "LLMFirstLoginUITests",
        "VoiceConversationUITests",
        "WorkspaceActionsUITests",
        "WorkspacePresentationUITests",
    }
)
CONTINUITY_CASE = (
    "ConversationContinuityUITests/"
    "testDeterministicProcessRelaunchRestoresSemanticConversationTwentyTimes()"
)
STAGING_CASE = "ReleaseEvidenceUITests/testReleaseEvidenceProducesPlatformReport()"


class ArtifactError(ValueError):
    pass


def require(value):
    if not value:
        raise ArtifactError("apple_coverage_artifact_invalid")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    require(path.is_file() and not path.is_symlink())
    require(0 <= path.stat().st_size <= MAX_BYTES)
    raw = path.read_bytes()
    require(len(raw) <= MAX_BYTES)
    return raw


def document(path):
    return json_document(read(path))


def json_document(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value)
            value[key] = item
        return value

    return json.loads(
        raw,
        object_pairs_hook=unique,
        parse_constant=lambda _value: require(False),
    )


def git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, timeout=30
    )
    require(len(result.stdout) <= MAX_BYTES)
    return result.stdout


def source_closure(repo):
    component = repo / COMPONENT
    candidate = git(repo, "rev-parse", "HEAD").decode().strip()
    projection = git(component, "rev-parse", "HEAD").decode().strip()
    require(git(repo, "rev-parse", f"HEAD:{COMPONENT}").decode().strip() == projection)
    inventory = git(
        component,
        "ls-files",
        "-z",
        "--",
        "apple-clients",
        "contracts",
        "scripts/build_native_chart.py",
        "scripts/build_native_export.py",
    )
    require(inventory.endswith(b"\0"))
    paths = inventory.decode().rstrip("\0").split("\0")
    require(paths and len(paths) <= MAX_FILES and len(paths) == len(set(paths)))
    scope = (
        "apple-clients",
        "contracts",
        "scripts/build_native_chart.py",
        "scripts/build_native_export.py",
    )
    git(component, "diff", "--exit-code", "HEAD", "--", *scope)
    require(
        not git(component, "ls-files", "--others", "--exclude-standard", "--", *scope)
    )
    committed = {}
    for entry in git(component, "ls-tree", "-rz", "--full-tree", "HEAD").split(b"\0"):
        if entry:
            metadata, name = entry.decode().split("\t", 1)
            mode, kind, _object = metadata.split()
            if kind == "blob" and mode in {"100644", "100755", "120000"}:
                committed[name] = mode
    require(
        set(paths)
        == {
            name
            for name in committed
            if any(name == prefix or name.startswith(prefix + "/") for prefix in scope)
        }
    )
    pending = list(paths)
    selected = set(paths)
    while pending:
        name = pending.pop()
        path = component / name
        if committed[name] != "120000":
            continue
        require(path.is_symlink())
        link = os.readlink(path)
        require(link.encode() == git(component, "show", f"HEAD:{name}"))
        require(not os.path.isabs(link))
        target = Path(os.path.normpath(path.parent / link))
        require(target.is_relative_to(component))
        parent = target.parent
        while parent != component:
            require(not parent.is_symlink())
            parent = parent.parent
        target_name = target.relative_to(component).as_posix()
        if target.is_dir() and not target.is_symlink():
            children = {key for key in committed if key.startswith(target_name + "/")}
            require(children)
            require(
                set(tree(target))
                == {key.removeprefix(target_name + "/") for key in children}
            )
        else:
            require(target_name in committed)
            children = {target_name}
        for child in children - selected:
            selected.add(child)
            pending.append(child)
        require(len(selected) <= MAX_FILES)
    sources = {}
    for name in sorted(selected):
        path = component / name
        require(path.resolve().is_relative_to(component.resolve()))
        actual_mode = (
            "120000"
            if path.is_symlink()
            else ("100755" if path.stat().st_mode & stat.S_IXUSR else "100644")
        )
        require(actual_mode == committed[name])
        blob = git(component, "show", f"HEAD:{name}")
        require(
            blob == (os.readlink(path).encode() if path.is_symlink() else read(path))
        )
        require(path.exists())
        sources[f"{COMPONENT}/{name}"] = {
            "sha256": digest(blob),
            "link": os.readlink(path) if path.is_symlink() else None,
            "git_mode": actual_mode,
        }
    return {
        "candidate_sha": candidate,
        "projection_sha": projection,
        "sources": sources,
    }


def tree(root):
    require(root.is_dir() and not root.is_symlink())
    entries = {}
    total = 0
    for path in sorted(root.rglob("*")):
        require(len(entries) < MAX_FILES)
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        require(stat.S_ISREG(mode) or stat.S_ISLNK(mode))
        require(path.resolve().is_relative_to(root.resolve()) and path.exists())
        raw = os.readlink(path).encode() if path.is_symlink() else read(path)
        total += len(raw)
        require(total <= MAX_BYTES)
        entries[path.relative_to(root).as_posix()] = {
            "mode": mode,
            "sha256": digest(raw),
            "size": len(raw),
        }
    require(entries)
    return entries


def app_info(info, platform):
    require(isinstance(info, dict))
    require(info.get("CFBundleIdentifier") == "com.personalailabs.astraldeep")
    require(info.get("CFBundleExecutable") == "AstralDeep")
    require(info.get("CFBundlePackageType") == "APPL")
    require(
        info.get("CFBundleSupportedPlatforms")
        == ["iPhoneSimulator" if platform == "ios" else "MacOSX"]
    )


def shipping_app(products, platform):
    folder = "Debug-iphonesimulator" if platform == "ios" else "Debug"
    app = products / folder / "AstralDeep.app"
    inside = "Contents/" if platform == "macos" else ""
    app_info(plistlib.loads(read(app / (inside + "Info.plist"))), platform)
    executable = app / (
        inside + ("MacOS/" if platform == "macos" else "") + "AstralDeep"
    )
    require(
        stat.S_ISREG(executable.lstat().st_mode)
        and executable.stat().st_mode & stat.S_IXUSR
    )
    return app


def test_binding(records, read_product, platform, *, core=False):
    folder = "Debug-iphonesimulator" if platform == "ios" else "Debug"
    app = folder + "/AstralDeep.app"
    plugins = "/Contents/PlugIns/" if platform == "macos" else "/PlugIns/"
    expected = {"AstralCoreTests"} if core else {"AstralAppTests", "AstralAppUITests"}
    targets = []
    coverage = []
    runs = [name for name in records if "/" not in name and name.endswith(".xctestrun")]
    require(runs)
    for name in runs:
        doc = plistlib.loads(read_product(name))
        require(isinstance(doc, dict))
        configurations = doc.get("TestConfigurations")
        require(isinstance(configurations, list) and configurations)
        for config in configurations:
            require(
                isinstance(config, dict) and isinstance(config.get("TestTargets"), list)
            )
            targets.extend(config["TestTargets"])
        values = doc.get("CodeCoverageBuildableInfos")
        require(isinstance(values, list) and values)
        coverage.extend(values)
    require(all(isinstance(target, dict) for target in targets))
    require(
        len(targets) == len(expected)
        and {target.get("BlueprintName") for target in targets} == expected
    )
    selected = {target["BlueprintName"]: target for target in targets}

    def resolve(value, host=None, *, exists=True):
        require(isinstance(value, str))
        if value.startswith("__TESTROOT__/"):
            name = value.removeprefix("__TESTROOT__/")
        else:
            require(host is not None and value.startswith("__TESTHOST__/"))
            name = host + "/" + value.removeprefix("__TESTHOST__/")
        require(
            str(PurePosixPath(name)) == name
            and ".." not in PurePosixPath(name).parts
            and "\\" not in name
            and "__" not in name
            and not name.startswith("/")
        )
        if exists:
            require(
                name in records or any(key.startswith(name + "/") for key in records)
            )
        return name

    if core:
        target = selected["AstralCoreTests"]
        require(platform == "ios")
        require(
            target.get("IsAppHostedTestBundle", False) is False
            and target.get("IsUITestBundle", False) is False
        )
        require(
            target.get("TestHostPath")
            == "__PLATFORMS__/iPhoneSimulator.platform/Developer/Library/Xcode/Agents/xctest"
        )
        require(
            resolve(target.get("TestBundlePath")) == folder + "/AstralCoreTests.xctest"
        )
        coverage_name = "AstralCore"
    else:
        unit, ui = selected["AstralAppTests"], selected["AstralAppUITests"]
        require(
            unit.get("IsAppHostedTestBundle") is True
            and unit.get("TestHostBundleIdentifier") == "com.personalailabs.astraldeep"
        )
        require(
            ui.get("IsUITestBundle") is True
            and ui.get("TestHostBundleIdentifier")
            == "com.personalailabs.astraldeep.uitests.xctrunner"
        )
        unit_host = resolve(unit.get("TestHostPath"))
        ui_host = resolve(ui.get("TestHostPath"))
        require(unit_host == app and ui_host == folder + "/AstralAppUITests-Runner.app")
        require(resolve(ui.get("UITargetAppPath")) == app)
        require(
            resolve(unit.get("TestBundlePath"), unit_host)
            == app + plugins + "AstralAppTests.xctest"
        )
        require(
            resolve(ui.get("TestBundlePath"), ui_host)
            == ui_host + plugins + "AstralAppUITests.xctest"
        )
        coverage_name = "AstralDeep.app"
    # Can't fix an uninstrumented build with a test-time flag
    matches = [
        entry
        for entry in coverage
        if isinstance(entry, dict) and entry.get("Name") == coverage_name
    ]
    require(len(matches) == 1 and matches[0].get("IncludeInReport") is True)
    paths = matches[0].get("ProductPaths")
    require(isinstance(paths, list) and len(paths) == 1)
    binary = resolve(paths[0], exists=not core)
    if core:
        require(
            re.fullmatch(
                re.escape(folder)
                + r"/PackageFrameworks/(AstralCore_[A-Za-z0-9-]+_PackageProduct)\.framework/\1",
                binary,
            )
        )
        require(matches[0].get("IsStatic") is True)
        test_binary = folder + "/AstralCoreTests.xctest/AstralCoreTests"
        for name in (folder + "/AstralCore.o", test_binary):
            require(name in records and stat.S_ISREG(records[name]["mode"]))
        test_coverage = [
            entry
            for entry in coverage
            if isinstance(entry, dict) and entry.get("Name") == "AstralCoreTests"
        ]
        require(
            len(test_coverage) == 1 and test_coverage[0].get("IncludeInReport") is True
        )
        require(test_coverage[0].get("ProductPaths") == ["__TESTROOT__/" + test_binary])
    else:
        require(binary in records and stat.S_ISREG(records[binary]["mode"]))
        require(
            binary
            == app + ("/Contents/MacOS/" if platform == "macos" else "/") + "AstralDeep"
        )


def snapshot(repo, derived, core, platform):
    require(platform in {"ios", "macos"})
    products = derived / "Build/Products"
    within(repo, products)
    if core is not None:
        within(repo, core / "Build/Products")
    app = shipping_app(products, platform)
    products_tree = tree(products)
    test_binding(products_tree, lambda name: read(products / name), platform)
    require((core is not None) == (platform == "ios"))
    core_tree = tree(core / "Build/Products") if core is not None else None
    if core is not None:
        test_binding(
            core_tree,
            lambda name: read(core / "Build/Products" / name),
            platform,
            core=True,
        )
    result = {
        "version": VERSION,
        "platform": platform,
        **source_closure(repo),
        "products": products_tree,
        "app": tree(app),
        "core_products": core_tree,
    }
    return result, app


def write_new(path, raw):
    require(not path.exists() and not path.is_symlink())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)


def within(repo, path):
    require(path.resolve().is_relative_to(repo.resolve()))
    current = path.absolute()
    while current != repo.absolute():
        require(not current.is_symlink() and current != current.parent)
        current = current.parent


def archive_tree(root, entries, archive):
    require(not archive.exists() and not archive.is_symlink())
    archive.parent.mkdir(parents=True, exist_ok=True)
    with (
        archive.open("xb") as stream,
        zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as bundle,
    ):
        for name, entry in entries.items():
            path = root / name
            info = zipfile.ZipInfo(root.name + "/" + name)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = entry["mode"] << 16
            raw = os.readlink(path).encode() if path.is_symlink() else read(path)
            require(digest(raw) == entry["sha256"])
            bundle.writestr(info, raw)
    return digest(read(archive))


def archive_records(archive, root):
    records = {}
    links = {}
    total = 0
    with zipfile.ZipFile(archive) as bundle:
        require(0 < len(bundle.infolist()) <= MAX_FILES)
        for item in bundle.infolist():
            name = item.filename
            require(
                name.startswith(root + "/")
                and str(PurePosixPath(name)) == name
                and ".." not in PurePosixPath(name).parts
                and "\\" not in name
            )
            key = name.removeprefix(root + "/")
            require(key and key not in records and not item.is_dir())
            total += item.file_size
            require(total <= MAX_BYTES)
            raw = bundle.read(item)
            mode = item.external_attr >> 16
            require(stat.S_ISREG(mode) or stat.S_ISLNK(mode))
            if stat.S_ISLNK(mode):
                target = raw.decode()
                require(not target.startswith("/") and "\\" not in target)
                destination = posixpath.normpath(
                    posixpath.join(posixpath.dirname(key), target)
                )
                require(destination != ".." and not destination.startswith("../"))
                links[key] = target
            records[key] = {"mode": mode, "sha256": digest(raw), "size": len(raw)}
    # A file/symlink can't be another entry's physical parent
    for key in records:
        require(
            all(
                parent.as_posix() not in records
                for parent in PurePosixPath(key).parents
            )
        )
    for key in links:
        current, seen = key, set()
        while True:
            require(current not in seen)
            seen.add(current)
            pieces = current.split("/")
            prefix = next(
                (
                    "/".join(pieces[:index])
                    for index in range(1, len(pieces) + 1)
                    if "/".join(pieces[:index]) in links
                ),
                None,
            )
            if prefix is None:
                require(
                    current in records
                    or any(name.startswith(current + "/") for name in records)
                )
                break
            current = posixpath.normpath(
                posixpath.join(
                    posixpath.dirname(prefix),
                    links[prefix],
                    current[len(prefix) :].lstrip("/"),
                )
            )
            require(
                current != ".."
                and not current.startswith("../")
                and not current.startswith("/")
            )
    return records


def prepare(repo, derived, core, platform, output):
    for path in (output, derived, *([core] if core else [])):
        within(repo, path)
    state, app = snapshot(repo, derived, core, platform)
    archive = output / platform / "AstralApp.app.zip"
    manifest = output / "coverage/raw" / f"apple-{platform}-artifacts.json"
    require(
        not archive.exists()
        and not archive.is_symlink()
        and not manifest.exists()
        and not manifest.is_symlink()
    )
    state["archive_sha256"] = archive_tree(app, state["app"], archive)
    state["build_archives"] = {}
    for kind, directory in (("products", derived), ("core_products", core)):
        if directory is not None:
            target = output / "coverage/raw" / f"apple-{platform}-{kind}.zip"
            state["build_archives"][kind] = archive_tree(
                directory / "Build/Products", state[kind], target
            )
    require(
        snapshot(repo, derived, core, platform)[0]
        == {
            key: value
            for key, value in state.items()
            if key not in {"archive_sha256", "build_archives"}
        }
    )
    write_new(manifest, json.dumps(state, sort_keys=True).encode())
    return state


def validate(repo, platform, output):
    within(repo, output)
    manifest = output / "coverage/raw" / f"apple-{platform}-artifacts.json"
    state = document(manifest)
    require(
        set(state)
        == {
            "version",
            "platform",
            "candidate_sha",
            "projection_sha",
            "sources",
            "products",
            "app",
            "core_products",
            "archive_sha256",
            "build_archives",
        }
    )
    require(state["version"] == VERSION and state["platform"] == platform)
    require(isinstance(state["products"], dict) and state["products"])
    require(isinstance(state["app"], dict) and state["app"])
    require(
        (isinstance(state["core_products"], dict) and bool(state["core_products"]))
        if platform == "ios"
        else state["core_products"] is None
    )
    prefix = (
        "Debug-iphonesimulator/" if platform == "ios" else "Debug/"
    ) + "AstralDeep.app/"
    require(
        {
            key.removeprefix(prefix): value
            for key, value in state["products"].items()
            if key.startswith(prefix)
        }
        == state["app"]
    )
    require(
        {key: state[key] for key in ("candidate_sha", "projection_sha", "sources")}
        == source_closure(repo)
    )
    archive = output / platform / "AstralApp.app.zip"
    require(digest(read(archive)) == state["archive_sha256"])
    require(
        isinstance(state["build_archives"], dict)
        and set(state["build_archives"])
        == ({"products", "core_products"} if platform == "ios" else {"products"})
    )
    for kind, expected in state["build_archives"].items():
        target = output / "coverage/raw" / f"apple-{platform}-{kind}.zip"
        require(digest(read(target)) == expected)
        records = archive_records(target, "Products")
        require(records == state[kind])
        with zipfile.ZipFile(target) as bundle:
            test_binding(
                records,
                lambda name: bundle.read("Products/" + name),
                platform,
                core=kind == "core_products",
            )
    records = archive_records(archive, "AstralDeep.app")
    with zipfile.ZipFile(archive) as bundle:
        inside = "Contents/" if platform == "macos" else ""
        app_info(
            plistlib.loads(bundle.read("AstralDeep.app/" + inside + "Info.plist")),
            platform,
        )
        executable = inside + ("MacOS/" if platform == "macos" else "") + "AstralDeep"
        require(
            executable in records
            and stat.S_ISREG(records[executable]["mode"])
            and records[executable]["mode"] & stat.S_IXUSR
        )
    require(records == state["app"])
    return state


@contextmanager
def result_query_copy(result, *, deadline):
    require(time.monotonic() < deadline)
    before = tree(result)
    require(all(stat.S_ISREG(entry["mode"]) for entry in before.values()))
    with tempfile.TemporaryDirectory(prefix="apple-result-query-") as directory:
        copied = Path(directory).resolve() / result.name
        copied.mkdir(mode=0o700)
        for name, entry in before.items():
            require(time.monotonic() < deadline)
            source = result / name
            within(result, source)
            raw = read(source)
            require(
                digest(raw) == entry["sha256"] and len(raw) == entry["size"]
                and source.lstat().st_mode == entry["mode"]
            )
            destination = copied / name
            write_new(destination, raw)
            destination.chmod(stat.S_IMODE(entry["mode"]))
        require(tree(copied) == before)
        yield copied
        require(time.monotonic() < deadline and tree(result) == before)


def xcresult_json(result, operation, *, deadline):
    path = Path(__file__).resolve().with_name("export_xccov_line_coverage.py")
    require(path.is_file() and not path.is_symlink())
    spec = importlib.util.spec_from_file_location("_apple_observation_io", path)
    require(spec is not None and spec.loader is not None)
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    require(operation in {"metadata", "summary", "tests"})
    command = ["/usr/bin/xcrun", "xcresulttool"]
    if operation == "metadata":
        command += ["metadata", "get"]
    else:
        command += [
            "get",
            "test-results",
            operation,
            "--schema-version",
            "0.1.0",
            "--compact",
        ]
    with result_query_copy(result, deadline=deadline) as copied:
        command += ["--path", str(copied)]
        return json_document(
            policy._bounded_command(
                command,
                cwd=copied.parent,
                max_stdout_bytes=16 * 1024 * 1024,
                export_deadline=min(deadline, time.monotonic() + 30),
            )
        )


def observation_cases(summary, tests, lane):
    require(lane in {"core", "unit", "ui", "staging"})
    require(isinstance(summary, dict) and isinstance(tests, dict))
    require(summary.get("result") == "Passed" and summary.get("testFailures") == [])
    for key in ("failedTests", "skippedTests", "expectedFailures"):
        require(type(summary.get(key)) is int and summary[key] == 0)
    total = summary.get("totalTestCount")
    require(type(total) is int and 0 < total <= MAX_TEST_NODES)
    require(type(summary.get("passedTests")) is int and summary["passedTests"] == total)
    devices = tests.get("devices")
    require(isinstance(devices, list) and len(devices) == 1)
    require(
        isinstance(devices[0], dict) and devices[0].get("platform") == "iOS Simulator"
    )
    configs = tests.get("testPlanConfigurations")
    require(isinstance(configs, list) and len(configs) == 1)
    require(
        isinstance(configs[0], dict)
        and isinstance(configs[0].get("configurationId"), str)
    )
    require(bool(configs[0]["configurationId"]))
    plan_name = "AstralCore" if lane == "core" else "AstralApp"
    target = {"core": "AstralCoreTests", "unit": "AstralAppTests"}.get(
        lane, "AstralAppUITests"
    )
    bundle_type = "Unit test bundle" if lane in {"core", "unit"} else "UI test bundle"
    nodes = tests.get("testNodes")
    require(isinstance(nodes, list) and len(nodes) == 1)
    plan = nodes[0]
    require(isinstance(plan, dict) and plan.get("nodeType") == "Test Plan")
    require(plan.get("name") == plan_name and plan.get("result") == "Passed")
    bundles = plan.get("children")
    require(isinstance(bundles, list) and len(bundles) == 1)
    bundle = bundles[0]
    require(isinstance(bundle, dict) and bundle.get("nodeType") == bundle_type)
    prefix = f"test://com.apple.xcode/{plan_name}/{target}"
    require(bundle.get("name") == target and bundle.get("result") == "Passed")
    require(bundle.get("nodeIdentifierURL") == prefix)
    suites = bundle.get("children")
    require(isinstance(suites, list) and 0 < len(suites) <= MAX_TEST_NODES)
    names, cases = set(), set()
    for suite in suites:
        require(isinstance(suite, dict) and suite.get("nodeType") == "Test Suite")
        name = suite.get("name")
        require(
            isinstance(name, str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,255}", name)
        )
        require(name not in names and suite.get("result") == "Passed")
        require(suite.get("nodeIdentifierURL") == f"{prefix}/{name}")
        names.add(name)
        children = suite.get("children")
        require(isinstance(children, list) and 0 < len(children) <= MAX_TEST_NODES)
        for case in children:
            require(isinstance(case, dict) and case.get("nodeType") == "Test Case")
            require(case.get("result") == "Passed")
            require("children" not in case or case["children"] == [])
            method = case.get("name")
            require(
                isinstance(method, str)
                and re.fullmatch(r"test[A-Za-z0-9_]+\(\)", method)
            )
            identifier = f"{name}/{method}"
            require(
                case.get("nodeIdentifier") == identifier and identifier not in cases
            )
            require(case.get("nodeIdentifierURL") == f"{prefix}/{name}/{method[:-2]}")
            cases.add(identifier)
            require(len(cases) <= MAX_TEST_NODES)
    require(len(cases) == total)
    if lane == "ui":
        require(names == UI_SUITES and CONTINUITY_CASE in cases)
    elif lane == "staging":
        require(cases == {STAGING_CASE})
    return sorted(cases)


def validate_observations(repo, platform, output):
    require(platform == "ios")
    validate(repo, platform, output)
    roots, digests, observations, snapshots = set(), set(), {}, {}
    deadline = time.monotonic() + 120
    for lane in ("core", "unit", "ui", "staging"):
        result = output / "coverage/raw" / f"apple-ios-{lane}.xcresult"
        within(repo, result)
        before = tree(result)
        snapshots[result] = before
        require(all(stat.S_ISREG(entry["mode"]) for entry in before.values()))
        fingerprint = digest(json.dumps(before, sort_keys=True).encode())
        require(fingerprint not in digests)
        digests.add(fingerprint)
        metadata = xcresult_json(result, "metadata", deadline=deadline)
        require(isinstance(metadata, dict) and metadata.get("externalLocations") == [])
        root = metadata.get("rootId")
        require(isinstance(root, dict) and set(root) == {"hash"})
        identity = root["hash"]
        require(
            isinstance(identity, str)
            and re.fullmatch(r"[0-9]+~[A-Za-z0-9_=-]{1,256}", identity)
        )
        require(identity not in roots)
        roots.add(identity)
        cases = observation_cases(
            xcresult_json(result, "summary", deadline=deadline),
            xcresult_json(result, "tests", deadline=deadline),
            lane,
        )
        require(tree(result) == before)
        observations[lane] = {
            "root_id": identity,
            "sha256": fingerprint,
            "cases": cases,
        }
    require(all(tree(path) == before for path, before in snapshots.items()))
    return observations


def _native_policy(name):
    require(name in {"native_xccov_domain", "export_xccov_line_coverage", "merge_xccov_line_coverage"})
    path = Path(__file__).resolve().with_name(name + ".py")
    require(path.is_file() and not path.is_symlink())
    spec = importlib.util.spec_from_file_location("_apple_native_" + name, path)
    require(spec is not None and spec.loader is not None)
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    return policy


# Candidate cannot choose or supply the coverage domain
def native_domain(repo, output, lane, archive_root):
    require(lane in {"core", "unit", "ui", "staging"})
    state = validate(repo, "ios", output)
    policy = _native_policy("native_xccov_domain")
    exporter = _native_policy("export_xccov_line_coverage")
    kind = "core_products" if lane == "core" else "products"
    member = policy.BINARY_MEMBERS["core" if lane == "core" else "app"]
    name = member.removeprefix("Products/")
    require(name in state[kind] and stat.S_ISREG(state[kind][name]["mode"]))
    archive_member = f"coverage/raw/apple-ios-{kind}.zip"
    archive = output / archive_member
    with zipfile.ZipFile(archive) as bundle:
        raw = bundle.read(member)
    require(digest(raw) == state[kind][name]["sha256"])
    result = output / "coverage/raw" / f"apple-ios-{lane}.xcresult"
    within(repo, result)
    require(result.is_dir() and not result.is_symlink())
    deadline = time.monotonic() + 15 * 60
    summary = xcresult_json(result, "summary", deadline=deadline)
    tests = xcresult_json(result, "tests", deadline=deadline)
    observation_cases(summary, tests, lane)
    prefix = COMPONENT + "/"
    tracked = {
        path for path in state["sources"]
        if path.endswith(".swift")
        and path.removeprefix(prefix).startswith((policy.CORE_ROOT, policy.APP_ROOT))
    }

    def source_bytes(path):
        require(path in state["sources"])
        raw_source = exporter._read_source_bytes(repo, path, export_deadline=deadline)
        require(digest(raw_source) == state["sources"][path]["sha256"])
        return raw_source

    with tempfile.TemporaryDirectory(prefix="apple-native-domain-", dir=output.parent) as directory:
        binary = Path(directory) / "mapping-binary"
        write_new(binary, raw)
        domain = policy.collect_domain(
            binary_path=binary, binary_bytes=raw,
            binary_identity={
                "member": member, "sha256": digest(raw),
                "artifact_member": archive_member,
                "artifact_sha256": state["build_archives"][kind],
            },
            archive_root=archive_root, prefix=prefix, lane=lane,
            tracked=tracked, source_bytes=source_bytes,
            run=lambda command, bound: exporter._bounded_command(
                command, cwd=repo, max_stdout_bytes=bound, export_deadline=deadline,
            ),
            summary=summary, tests=tests,
        )
    require(validate(repo, "ios", output) == state)
    return domain


def verify_native_domains(repo, output, report, archive_root):
    within(repo, report)
    policy = _native_policy("native_xccov_domain")
    actual = policy.parse_native_report(document(report))
    require(set(actual["domains"]) == {"core", "unit", "ui", "staging"})
    exporter = _native_policy("export_xccov_line_coverage")
    merger = _native_policy("merge_xccov_line_coverage")
    with tempfile.TemporaryDirectory(prefix="apple-native-recheck-", dir=output.parent) as directory:
        inputs = {}
        for lane in ("core", "unit", "ui", "staging"):
            destination = Path(directory) / (lane + ".json")
            exporter.export_xccov(
                repo=repo, xcresult=output / "coverage/raw" / f"apple-ios-{lane}.xcresult",
                output=destination, platform="ios", archive_repo_root=archive_root,
                native_domain=native_domain(repo, output, lane, archive_root),
            )
            inputs[lane] = destination
        regenerated = merger.merge_xccov_reports(
            repo=repo, inputs=inputs, output=Path(directory) / "union.json",
            platform="ios", profile="release",
        )
        require(actual == regenerated)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=("prepare", "verify", "validate", "validate-observations", "native-domain", "verify-native-domains")
    )
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--platform", choices=("ios", "macos"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--derived-data", type=Path)
    parser.add_argument("--core-derived-data", type=Path)
    parser.add_argument("--lane", choices=("core", "unit", "ui", "staging"))
    parser.add_argument("--archive-repo-root")
    parser.add_argument("--domain-output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == "prepare":
            require(args.derived_data is not None)
            prepare(
                args.repo,
                args.derived_data,
                args.core_derived_data,
                args.platform,
                args.output,
            )
        elif args.operation == "native-domain":
            require(args.platform == "ios" and args.lane is not None and args.domain_output is not None)
            within(args.repo, args.domain_output)
            value = native_domain(args.repo, args.output, args.lane, args.archive_repo_root)
            write_new(args.domain_output, json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        elif args.operation == "verify-native-domains":
            require(args.platform == "ios" and args.report is not None)
            verify_native_domains(args.repo, args.output, args.report, args.archive_repo_root)
        elif args.operation == "validate-observations":
            validate_observations(args.repo, args.platform, args.output)
        else:
            state = validate(args.repo, args.platform, args.output)
            if args.operation == "verify":
                require(args.derived_data is not None)
                actual, _app = snapshot(
                    args.repo, args.derived_data, args.core_derived_data, args.platform
                )
                require(
                    actual
                    == {
                        key: value
                        for key, value in state.items()
                        if key not in {"archive_sha256", "build_archives"}
                    }
                )
    except (
        ArtifactError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
    ):
        print("apple_coverage_artifact_invalid", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
