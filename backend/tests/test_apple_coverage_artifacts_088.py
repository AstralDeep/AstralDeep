"""Real filesystem/Git fixtures for same-build Apple artifact closure."""

import json
from pathlib import Path
import plistlib
import subprocess
import sys
import zipfile

import pytest

from scripts import apple_coverage_artifacts as helper


def git(root, *args):
    return subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            *args,
        ],
        check=True,
        capture_output=True,
    ).stdout


def write(path, value=b"binary"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


@pytest.fixture(params=["ios", "macos"])
def built(tmp_path, request):
    repo = tmp_path / "repo"
    component = repo / helper.COMPONENT
    component.mkdir(parents=True)
    source = write(
        component / "apple-clients/AstralCore/Sources/Core.swift", b"let real = 1\n"
    )
    git(component, "init", "-q")
    git(component, "add", ".")
    git(component, "commit", "-qm", "fixture")
    git(repo, "init", "-q")
    git(repo, "add", helper.COMPONENT)
    git(repo, "commit", "-qm", "fixture")
    platform = request.param
    derived = repo / "build/dd"
    products = derived / "Build/Products"
    folder = "Debug-iphonesimulator" if platform == "ios" else "Debug"
    app = products / folder / "AstralDeep.app"
    inside = app / "Contents" if platform == "macos" else app
    plist = {
        "CFBundleIdentifier": "com.personalailabs.astraldeep",
        "CFBundleExecutable": "AstralDeep",
        "CFBundlePackageType": "APPL",
        "CFBundleSupportedPlatforms": [
            "iPhoneSimulator" if platform == "ios" else "MacOSX"
        ],
    }
    info = write(inside / "Info.plist", plistlib.dumps(plist))
    executable = write(
        inside / ("MacOS/AstralDeep" if platform == "macos" else "AstralDeep")
    )
    executable.chmod(0o755)
    plugin = "Contents/PlugIns/" if platform == "macos" else "PlugIns/"
    write(app / (plugin + "AstralAppTests.xctest/tests"))
    write(
        products
        / folder
        / "AstralAppUITests-Runner.app"
        / (plugin + "AstralAppUITests.xctest/tests")
    )
    runner = write(products / folder / "AstralAppUITests-Runner.app/runner")

    def path(value):
        return "__TESTROOT__/" + value.relative_to(products).as_posix()

    run = {
        "TestConfigurations": [
            {
                "TestTargets": [
                    {
                        "BlueprintName": "AstralAppTests",
                        "IsAppHostedTestBundle": True,
                        "TestHostBundleIdentifier": "com.personalailabs.astraldeep",
                        "TestHostPath": path(app),
                        "TestBundlePath": "__TESTHOST__/"
                        + plugin
                        + "AstralAppTests.xctest",
                    },
                    {
                        "BlueprintName": "AstralAppUITests",
                        "IsUITestBundle": True,
                        "TestHostBundleIdentifier": "com.personalailabs.astraldeep.uitests.xctrunner",
                        "TestHostPath": path(runner.parent),
                        "UITargetAppPath": path(app),
                        "TestBundlePath": "__TESTHOST__/"
                        + plugin
                        + "AstralAppUITests.xctest",
                    },
                ]
            }
        ]
    }
    run["CodeCoverageBuildableInfos"] = [
        {
            "Name": "AstralDeep.app",
            "IncludeInReport": True,
            "ProductPaths": [path(executable)],
        }
    ]
    xctestrun = write(products / "AstralApp.xctestrun", plistlib.dumps(run))
    core = repo / "build/core" if platform == "ios" else None
    if core:
        core_products = core / "Build/Products"
        write(
            core_products
            / "Debug-iphonesimulator/AstralCoreTests.xctest/AstralCoreTests"
        )
        write(core_products / "Debug-iphonesimulator/AstralCore.o")
        write(
            core_products
            / "Debug-iphonesimulator/PackageFrameworks/AstralCore_-ABC_PackageProduct.framework/AstralCore_-ABC_PackageProduct"
        )
        core_run = {
            "TestConfigurations": [
                {
                    "TestTargets": [
                        {
                            "BlueprintName": "AstralCoreTests",
                            "TestHostPath": "__PLATFORMS__/iPhoneSimulator.platform/Developer/Library/Xcode/Agents/xctest",
                            "TestBundlePath": "__TESTROOT__/Debug-iphonesimulator/AstralCoreTests.xctest",
                        }
                    ]
                }
            ],
            "CodeCoverageBuildableInfos": [
                {
                    "Name": "AstralCore",
                    "IncludeInReport": True,
                    "IsStatic": True,
                    "ProductPaths": [
                        "__TESTROOT__/Debug-iphonesimulator/PackageFrameworks/AstralCore_-ABC_PackageProduct.framework/AstralCore_-ABC_PackageProduct"
                    ],
                },
                {
                    "Name": "AstralCoreTests",
                    "IncludeInReport": True,
                    "ProductPaths": [
                        "__TESTROOT__/Debug-iphonesimulator/AstralCoreTests.xctest/AstralCoreTests"
                    ],
                },
            ],
        }
        write(core_products / "AstralCore.xctestrun", plistlib.dumps(core_run))
    output = repo / "build/evidence"
    return dict(
        repo=repo,
        derived=derived,
        core=core,
        platform=platform,
        output=output,
        source=source,
        info=info,
        app=app,
        runner=runner,
        xctestrun=xctestrun,
        executable=executable,
    )


def args(built, operation):
    result = [
        operation,
        "--repo",
        str(built["repo"]),
        "--derived-data",
        str(built["derived"]),
        "--platform",
        built["platform"],
        "--output",
        str(built["output"]),
    ]
    if built["core"]:
        result += ["--core-derived-data", str(built["core"])]
    return result


def test_shipping_target_not_sorted_runner_is_archived_and_verified(built):
    assert helper.main(args(built, "prepare")) == 0
    assert helper.main(args(built, "verify")) == 0
    assert helper.main(args(built, "validate")) == 0
    archive = built["output"] / built["platform"] / "AstralApp.app.zip"
    with zipfile.ZipFile(archive) as bundle:
        assert all(name.startswith("AstralDeep.app/") for name in bundle.namelist())
        assert not any("-Runner" in name for name in bundle.namelist())
    before = archive.read_bytes()
    assert helper.main(args(built, "prepare")) == 2
    assert archive.read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("CFBundleIdentifier", "com.attacker.runner"),
        ("CFBundleExecutable", "runner"),
        ("CFBundlePackageType", "BNDL"),
        ("CFBundleSupportedPlatforms", ["WatchSimulator"]),
    ],
)
def test_wrong_bundle_identity_refuses_before_any_archive(built, field, value):
    info = plistlib.loads(built["info"].read_bytes())
    info[field] = value
    built["info"].write_bytes(plistlib.dumps(info))
    assert helper.main(args(built, "prepare")) == 2
    assert not built["output"].exists()


@pytest.mark.parametrize(
    "change", ["unit_host", "ui_target", "unit_bundle", "missing", "duplicate"]
)
def test_test_host_binding_must_identify_exact_shipping_app(built, change):
    doc = plistlib.loads(built["xctestrun"].read_bytes())
    targets = doc["TestConfigurations"][0]["TestTargets"]
    if change == "unit_host":
        targets[0]["TestHostPath"] = targets[1]["TestHostPath"]
    elif change == "ui_target":
        targets[1]["UITargetAppPath"] = targets[1]["TestBundlePath"]
    elif change == "unit_bundle":
        targets[0]["TestBundlePath"] = "__TESTROOT__/../outside.xctest"
    elif change == "missing":
        targets.pop()
    else:
        targets.append(targets[0])
    built["xctestrun"].write_bytes(plistlib.dumps(doc))
    assert helper.main(args(built, "prepare")) == 2


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "app",
        "runner",
        "test_metadata",
        "new_binary",
        "archive",
        "manifest",
        "core",
    ],
)
def test_each_source_binary_and_evidence_drift_refuses(built, change):
    assert helper.main(args(built, "prepare")) == 0
    path = {
        "source": built["source"],
        "app": built["executable"],
        "runner": built["runner"],
        "test_metadata": built["xctestrun"],
        "new_binary": built["app"] / "new.bin",
        "archive": built["output"] / built["platform"] / "AstralApp.app.zip",
        "manifest": built["output"]
        / "coverage/raw"
        / f"apple-{built['platform']}-artifacts.json",
        "core": (built["core"] or built["derived"]) / "Build/Products/other.bin",
    }[change]
    write(path, b"changed")
    assert helper.main(args(built, "verify")) == 2


@pytest.mark.parametrize("kind", ["source", "binary", "output"])
def test_external_or_dangling_symlink_is_never_followed(built, tmp_path, kind):
    outside = write(tmp_path / "outside", b"unrelated")
    if kind == "source":
        built["source"].unlink()
        built["source"].symlink_to(outside)
    elif kind == "binary":
        (built["app"] / "unsafe").symlink_to(outside)
    else:
        archive = built["output"] / built["platform"] / "AstralApp.app.zip"
        archive.parent.mkdir(parents=True)
        archive.symlink_to(tmp_path / "absent")
    assert helper.main(args(built, "prepare")) == 2
    assert outside.read_bytes() == b"unrelated"


def test_internal_link_preserved_and_independent_isolated_validation(built):
    (built["app"] / "current").symlink_to(built["executable"].relative_to(built["app"]))
    assert helper.main(args(built, "prepare")) == 0
    command = [
        sys.executable,
        "-I",
        str(Path(helper.__file__).resolve()),
        *args(built, "validate"),
    ]
    assert (
        subprocess.run(command, cwd=built["repo"], capture_output=True).returncode == 0
    )


def test_manifest_duplicate_keys_and_wrong_candidate_refuse(built):
    assert helper.main(args(built, "prepare")) == 0
    path = (
        built["output"] / "coverage/raw" / f"apple-{built['platform']}-artifacts.json"
    )
    raw = path.read_bytes()
    path.write_bytes(b'{"version":"forged",' + raw[1:])
    assert helper.main(args(built, "validate")) == 2
    doc = json.loads(raw)
    doc["candidate_sha"] = "f" * 40
    path.write_text(json.dumps(doc))
    assert helper.main(args(built, "validate")) == 2


@pytest.mark.parametrize("change", ["bytes", "mode", "staged", "untracked"])
def test_dirty_source_cannot_be_attested_as_head_even_before_prepare(
    built, change, capsys
):
    component = built["repo"] / helper.COMPONENT
    if change == "bytes":
        built["source"].write_text("PRIVATE DIRTY SOURCE")
    elif change == "mode":
        git(component, "config", "core.filemode", "false")
        built["source"].chmod(0o755)
    else:
        write(component / "apple-clients/Untracked.swift", b"PRIVATE DIRTY SOURCE")
        if change == "staged":
            git(component, "add", ".")
    assert helper.main(args(built, "prepare")) == 2
    assert not built["output"].exists()
    assert "PRIVATE" not in capsys.readouterr().err


def test_committed_internal_source_link_binds_target_bytes(built):
    component = built["repo"] / helper.COMPONENT
    link = built["source"].parent / "Linked.swift"
    link.symlink_to(built["source"].name)
    git(component, "add", ".")
    git(component, "commit", "-qm", "source link")
    git(built["repo"], "add", helper.COMPONENT)
    git(built["repo"], "commit", "-qm", "pin")
    assert helper.main(args(built, "prepare")) == 0
    built["source"].write_bytes(b"changed target")
    assert helper.main(args(built, "validate")) == 2


@pytest.mark.parametrize(
    "change",
    [
        "unit_host",
        "ui_target",
        "unknown_token",
        "traversal",
        "uninstrumented",
        "core_host",
        "wrong_coverage_binary",
        "wrong_host_identifier",
        "wrong_host_kind",
    ],
)
def test_protected_validation_rechecks_bindings_even_with_recomputed_archive_digests(
    built, change
):
    assert helper.main(args(built, "prepare")) == 0
    kind = (
        "core_products"
        if change == "core_host" and built["platform"] == "ios"
        else "products"
    )
    manifest = (
        built["output"] / "coverage/raw" / f"apple-{built['platform']}-artifacts.json"
    )
    state = json.loads(manifest.read_bytes())
    archive = manifest.parent / f"apple-{built['platform']}-{kind}.zip"
    with zipfile.ZipFile(archive) as bundle:
        entries = [(info, bundle.read(info)) for info in bundle.infolist()]
    rewritten = []
    for info, raw in entries:
        if info.filename.endswith(".xctestrun"):
            doc = plistlib.loads(raw)
            targets = doc["TestConfigurations"][0]["TestTargets"]
            if kind == "core_products":
                targets[0]["TestHostPath"] = (
                    "__PLATFORMS__/MacOSX.platform/Developer/Library/Xcode/Agents/xctest"
                )
            elif change == "unit_host":
                targets[0]["TestHostPath"] = targets[1]["TestHostPath"]
            elif change in {"ui_target", "core_host"}:
                targets[1]["UITargetAppPath"] = targets[1]["TestHostPath"]
            elif change == "unknown_token":
                targets[0]["TestBundlePath"] = (
                    "__UNKNOWN__/PlugIns/AstralAppTests.xctest"
                )
            elif change == "traversal":
                targets[0]["TestBundlePath"] = "__TESTHOST__/../AstralAppTests.xctest"
            elif change == "wrong_coverage_binary":
                doc["CodeCoverageBuildableInfos"][0]["ProductPaths"] = [
                    targets[1]["TestHostPath"] + "/runner"
                ]
            elif change == "wrong_host_identifier":
                targets[0]["TestHostBundleIdentifier"] = targets[1][
                    "TestHostBundleIdentifier"
                ]
            elif change == "wrong_host_kind":
                targets[0]["IsAppHostedTestBundle"] = False
            else:
                doc["CodeCoverageBuildableInfos"][0]["IncludeInReport"] = False
            raw = plistlib.dumps(doc)
            record = state[kind][info.filename.removeprefix("Products/")]
            record.update(sha256=helper.digest(raw), size=len(raw))
        rewritten.append((info, raw))
    with zipfile.ZipFile(archive, "w") as bundle:
        for info, raw in rewritten:
            bundle.writestr(info, raw)
    state["build_archives"][kind] = helper.digest(archive.read_bytes())
    manifest.write_text(json.dumps(state))
    assert helper.main(args(built, "validate")) == 2


def test_output_parent_symlink_and_binary_budget_fail_closed(
    built, tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    outside.mkdir()
    built["output"].symlink_to(outside)
    assert helper.main(args(built, "prepare")) == 2
    assert not list(outside.iterdir())
    built["output"].unlink()
    monkeypatch.setattr(helper, "MAX_FILES", 1)
    assert helper.main(args(built, "prepare")) == 2


def test_archive_reader_rejects_path_duplicate_mode_and_expansion(
    built, tmp_path, monkeypatch
):
    path = tmp_path / "malicious.zip"
    for name, mode, raw in [
        ("Other/file", 0o100644, b"a"),
        ("Products/../escape", 0o100644, b"a"),
        ("Products/link", 0o120777, b"../../escape"),
        ("Products/pipe", 0o10644, b"a"),
    ]:
        with zipfile.ZipFile(path, "w") as bundle:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            bundle.writestr(info, raw)
        with pytest.raises(helper.ArtifactError):
            helper.archive_records(path, "Products")
    monkeypatch.setattr(helper, "MAX_BYTES", 0)
    with pytest.raises(helper.ArtifactError):
        helper.archive_records(path, "Products")


def test_tested_resource_cannot_be_omitted_from_rehashed_shipping_archive(built):
    resource = write(built["app"] / "Resources/visible.html", b"tested resource")
    assert helper.main(args(built, "prepare")) == 0
    manifest = (
        built["output"] / "coverage/raw" / f"apple-{built['platform']}-artifacts.json"
    )
    state = json.loads(manifest.read_bytes())
    del state["app"][resource.relative_to(built["app"]).as_posix()]
    archive = built["output"] / built["platform"] / "AstralApp.app.zip"
    archive.unlink()
    state["archive_sha256"] = helper.archive_tree(built["app"], state["app"], archive)
    manifest.write_text(json.dumps(state))
    assert helper.main(args(built, "validate")) == 2


@pytest.mark.parametrize("change", ["symlink", "not_executable"])
def test_shipping_executable_must_be_regular_and_executable(built, change):
    executable = built["executable"]
    if change == "symlink":
        target = write(executable.parent / "other-executable")
        target.chmod(0o755)
        executable.unlink()
        executable.symlink_to(target.name)
    else:
        executable.chmod(0o644)
    assert helper.main(args(built, "prepare")) == 2


@pytest.mark.parametrize(
    "entries",
    [
        [("a", b"missing", True)],
        [("a", b"b", True), ("b", b"missing", True)],
        [("a", b"b", True), ("b", b"a", True)],
        [("dir/a", b"../b/../../escape", True), ("b", b"dir", True)],
        [("a//b", b"data", False)],
        [("a/./b", b"data", False)],
        [("a", b"data", False), ("a/b", b"data", False)],
        [("a", b"real", True), ("real/b", b"data", False), ("a/b", b"data", False)],
    ],
)
def test_archive_refuses_noncanonical_or_unresolvable_links(tmp_path, entries):
    archive = tmp_path / "invalid.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, raw, link in entries:
            info = zipfile.ZipInfo("Products/" + name)
            info.external_attr = (0o120777 if link else 0o100644) << 16
            bundle.writestr(info, raw)
    with pytest.raises(helper.ArtifactError):
        helper.archive_records(archive, "Products")


def test_archive_resolves_real_framework_directory_links_and_chains(tmp_path):
    archive = tmp_path / "framework.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, raw, mode in [
            ("Versions/A/real", b"binary", 0o100755),
            ("Versions/Current", b"A", 0o120777),
            ("real", b"Versions/Current/real", 0o120777),
        ]:
            info = zipfile.ZipInfo("Products/" + name)
            info.external_attr = mode << 16
            bundle.writestr(info, raw)
    assert set(helper.archive_records(archive, "Products")) == {
        "Versions/A/real",
        "Versions/Current",
        "real",
    }


@pytest.mark.parametrize(
    "change", [None, "file_bytes", "file_mode", "directory_bytes", "extra_file"]
)
def test_committed_external_scope_file_and_directory_links_have_exact_closure(
    built, change
):
    component = built["repo"] / helper.COMPONENT
    vendor = write(component / "backend/static/library.js", b"public library")
    fixture = write(component / "fixtures/voice/input.json", b"public fixture")
    (component / "apple-clients/library.js").symlink_to("../backend/static/library.js")
    (component / "apple-clients/fixture").symlink_to("../fixtures/voice")
    git(component, "add", ".")
    git(component, "commit", "-qm", "exact linked resources")
    git(built["repo"], "add", helper.COMPONENT)
    git(built["repo"], "commit", "-qm", "pin")
    sources = helper.source_closure(built["repo"])["sources"]
    assert sources[helper.COMPONENT + "/backend/static/library.js"][
        "sha256"
    ] == helper.digest(vendor.read_bytes())
    assert helper.COMPONENT + "/fixtures/voice/input.json" in sources
    if change == "file_bytes":
        vendor.write_bytes(b"private changed bytes")
    elif change == "file_mode":
        vendor.chmod(0o755)
    elif change == "directory_bytes":
        fixture.write_bytes(b"private changed bytes")
    elif change == "extra_file":
        write(fixture.parent / "untracked.json")
    assert helper.main(args(built, "prepare")) == (0 if change is None else 2)


def observation(lane):
    """Minimal Xcode 26 summary/tests shape witnessed from real retained results."""
    plan = "AstralCore" if lane == "core" else "AstralApp"
    target = {"core": "AstralCoreTests", "unit": "AstralAppTests"}.get(
        lane, "AstralAppUITests"
    )
    prefix = f"test://com.apple.xcode/{plan}/{target}"
    methods = {"ExampleTests": ["testExample()"]}
    if lane == "ui":
        methods = {name: ["testExample()"] for name in sorted(helper.UI_SUITES)}
        methods["ConversationContinuityUITests"] = [
            helper.CONTINUITY_CASE.split("/")[1]
        ]
    elif lane == "staging":
        methods = {"ReleaseEvidenceUITests": [helper.STAGING_CASE.split("/")[1]]}
    suites = [
        {
            "name": name,
            "nodeType": "Test Suite",
            "result": "Passed",
            "nodeIdentifierURL": f"{prefix}/{name}",
            "children": [
                {
                    "name": method,
                    "nodeType": "Test Case",
                    "result": "Passed",
                    "nodeIdentifier": f"{name}/{method}",
                    "nodeIdentifierURL": f"{prefix}/{name}/{method[:-2]}",
                }
                for method in tests
            ],
        }
        for name, tests in methods.items()
    ]
    count = sum(len(tests) for tests in methods.values())
    summary = dict(
        result="Passed",
        totalTestCount=count,
        passedTests=count,
        failedTests=0,
        skippedTests=0,
        expectedFailures=0,
        testFailures=[],
    )
    tests = {
        "devices": [{"platform": "iOS Simulator"}],
        "testPlanConfigurations": [{"configurationId": "1"}],
        "testNodes": [
            {
                "name": plan,
                "nodeType": "Test Plan",
                "result": "Passed",
                "children": [
                    {
                        "name": target,
                        "nodeType": "Unit test bundle"
                        if lane in {"core", "unit"}
                        else "UI test bundle",
                        "result": "Passed",
                        "nodeIdentifierURL": prefix,
                        "children": suites,
                    }
                ],
            }
        ],
    }
    return summary, tests


@pytest.mark.parametrize("lane", ["core", "unit", "ui", "staging"])
def test_observation_requires_successful_expected_target_and_suite(lane):
    summary, tests = observation(lane)
    assert len(helper.observation_cases(summary, tests, lane)) == summary["passedTests"]


@pytest.mark.parametrize("destination", ["unit", "staging", "core"])
def test_relabelled_ui_result_cannot_supply_other_lane(destination):
    summary, tests = observation("ui")
    with pytest.raises(helper.ArtifactError):
        helper.observation_cases(summary, tests, destination)


@pytest.mark.parametrize(
    "field",
    [
        "totalTestCount",
        "passedTests",
        "failedTests",
        "skippedTests",
        "expectedFailures",
        "testFailures",
        "result",
    ],
)
@pytest.mark.parametrize("value", [None, True, -1, 1, "Passed"])
def test_summary_requires_exact_nonempty_success_counters(field, value):
    summary, tests = observation("ui")
    summary[field] = value
    if field == "result" and value == "Passed":
        return
    with pytest.raises(helper.ArtifactError):
        helper.observation_cases(summary, tests, "ui")


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "wrong_platform",
        "missing_configuration",
        "wrong_plan",
        "wrong_target",
        "wrong_bundle_kind",
        "wrong_target_url",
        "missing_suite",
        "missing_continuity",
        "wrong_suite_url",
        "duplicate_suite",
        "empty_suite",
        "duplicate_case",
        "wrong_case_url",
        "wrong_case_identifier",
        "unexpected_child",
        "unknown_node",
        "unsafe_name",
        "count_mismatch",
        "empty_tree",
        "extra_target",
    ],
)
def test_test_tree_refuses_missing_wrong_or_ambiguous_observations(change):
    summary, tests = observation("ui")
    plan = tests["testNodes"][0]
    bundle = plan["children"][0]
    suites = bundle["children"]
    case = suites[0]["children"][0]
    if change == "empty":
        summary.update(totalTestCount=0, passedTests=0)
    elif change == "wrong_platform":
        tests["devices"][0]["platform"] = "macOS"
    elif change == "missing_configuration":
        tests["testPlanConfigurations"] = []
    elif change == "wrong_plan":
        plan["name"] = "Other"
    elif change == "wrong_target":
        bundle["name"] = "Other"
    elif change == "wrong_bundle_kind":
        bundle["nodeType"] = "Unit test bundle"
    elif change == "wrong_target_url":
        bundle["nodeIdentifierURL"] = "test://com.apple.xcode/Other/AstralAppUITests"
    elif change == "missing_suite":
        suites.pop()
        summary.update(totalTestCount=5, passedTests=5)
    elif change == "missing_continuity":
        other = suites[1]["children"][0]
        other["name"] = "testOther()"
        other["nodeIdentifier"] = "ConversationContinuityUITests/testOther()"
        other["nodeIdentifierURL"] = (
            other["nodeIdentifierURL"].rsplit("/", 1)[0] + "/testOther"
        )
    elif change == "wrong_suite_url":
        suites[0]["nodeIdentifierURL"] = "test://other"
    elif change == "duplicate_suite":
        suites.append(suites[0])
    elif change == "empty_suite":
        suites[0]["children"] = []
    elif change == "duplicate_case":
        suites[0]["children"].append(case)
    elif change == "wrong_case_url":
        case["nodeIdentifierURL"] = "test://other"
    elif change == "wrong_case_identifier":
        case["nodeIdentifier"] = "Other/testExample()"
    elif change == "unexpected_child":
        case["children"] = [{"nodeType": "Test Case Run", "result": "Passed"}]
    elif change == "unknown_node":
        case["nodeType"] = "Future Case"
    elif change == "unsafe_name":
        suites[0]["name"] = "../Other"
    elif change == "count_mismatch":
        summary.update(totalTestCount=7, passedTests=7)
    elif change == "empty_tree":
        tests["testNodes"] = []
    elif change == "extra_target":
        plan["children"].append(bundle)
    with pytest.raises(helper.ArtifactError):
        helper.observation_cases(summary, tests, "ui")


@pytest.mark.parametrize("level", ["plan", "bundle", "suite", "case"])
@pytest.mark.parametrize(
    "status", [None, "Failed", "Skipped", "Expected Failure", "unknown"]
)
def test_failed_skipped_or_missing_status_is_never_a_pass(level, status):
    summary, tests = observation("unit")
    plan = tests["testNodes"][0]
    bundle = plan["children"][0]
    suite = bundle["children"][0]
    node = {
        "plan": plan,
        "bundle": bundle,
        "suite": suite,
        "case": suite["children"][0],
    }[level]
    node["result"] = status
    with pytest.raises(helper.ArtifactError):
        helper.observation_cases(summary, tests, "unit")


def raw_observations(built, monkeypatch):
    data = {}
    for lane in ("core", "unit", "ui", "staging"):
        path = built["output"] / "coverage/raw" / f"apple-ios-{lane}.xcresult"
        write(path / "Info.plist", lane.encode())
        summary, tests = observation(lane)
        data[lane] = {
            "summary": summary,
            "tests": tests,
            "metadata": {
                "rootId": {"hash": f"0~{lane}"},
                "externalLocations": [],
            },
        }

    def query(result, operation, *, deadline):
        assert deadline > helper.time.monotonic()
        lane = result.name.removeprefix("apple-ios-").removesuffix(".xcresult")
        return data[lane][operation]

    monkeypatch.setattr(helper, "xcresult_json", query)
    return data


def test_final_observation_operation_is_separate_from_build_preparation(
    built, monkeypatch
):
    assert helper.main(args(built, "prepare")) == 0
    assert helper.main(args(built, "verify")) == 0
    assert helper.main(args(built, "validate-observations")) == 2
    raw_observations(built, monkeypatch)
    assert helper.main(args(built, "validate-observations")) == (
        0 if built["platform"] == "ios" else 2
    )


@pytest.mark.parametrize(
    "change",
    [
        "same_root",
        "same_bytes",
        "external",
        "missing_root",
        "unsafe_root",
        "symlink",
        "changed",
    ],
)
def test_raw_lane_identity_and_stability_refuse_substitution(
    built, monkeypatch, change
):
    if built["platform"] != "ios":
        return
    assert helper.main(args(built, "prepare")) == 0
    data = raw_observations(built, monkeypatch)
    path = built["output"] / "coverage/raw/apple-ios-unit.xcresult/Info.plist"
    if change == "same_root":
        data["unit"]["metadata"]["rootId"] = data["core"]["metadata"]["rootId"]
    elif change == "same_bytes":
        path.write_bytes(b"core")
    elif change == "external":
        data["unit"]["metadata"]["externalLocations"] = ["file:///private/source"]
    elif change == "missing_root":
        data["unit"]["metadata"]["rootId"] = None
    elif change == "unsafe_root":
        data["unit"]["metadata"]["rootId"] = {"hash": "../other"}
    elif change == "symlink":
        path.symlink_to("other") if not path.exists() else path.rename(
            path.with_name("other")
        )
        path.symlink_to("other")
    elif change == "changed":
        old = helper.xcresult_json

        def mutate(result, operation, *, deadline):
            value = old(result, operation, deadline=deadline)
            if result.name == "apple-ios-unit.xcresult" and operation == "tests":
                path.write_bytes(b"changed")
            return value

        monkeypatch.setattr(helper, "xcresult_json", mutate)
    assert helper.main(args(built, "validate-observations")) == 2


@pytest.mark.parametrize("change", ["other_case", "extra_case", "wrong_suite"])
def test_staging_requires_the_real_platform_report_case(change):
    summary, tests = observation("staging")
    suite = tests["testNodes"][0]["children"][0]["children"][0]
    case = suite["children"][0]
    if change == "wrong_suite":
        suite["name"] = "WorkspacePresentationUITests"
    else:
        other = dict(case)
        other["name"] = "testOther()"
        other["nodeIdentifier"] = "ReleaseEvidenceUITests/testOther()"
        other["nodeIdentifierURL"] = (
            case["nodeIdentifierURL"].rsplit("/", 1)[0] + "/testOther"
        )
        suite["children"] = [other] if change == "other_case" else [case, other]
        summary.update(
            totalTestCount=len(suite["children"]), passedTests=len(suite["children"])
        )
    with pytest.raises(helper.ArtifactError):
        helper.observation_cases(summary, tests, "staging")


@pytest.mark.parametrize("operation", ["metadata", "summary", "tests"])
@pytest.mark.parametrize(
    "raw", [b'{"ok":true}', b'{"ok":true,"ok":false}', b'{"ok":NaN}']
)
def test_tool_reader_has_fixed_protected_import_command_and_bounded_output(
    tmp_path, monkeypatch, operation, raw
):
    from types import SimpleNamespace

    calls = []
    specs = []

    def bounded(command, **kwargs):
        calls.append((command, kwargs))
        return raw

    def spec(name, path):
        specs.append(path)
        return SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))

    monkeypatch.setattr(helper.importlib.util, "spec_from_file_location", spec)
    monkeypatch.setattr(
        helper.importlib.util,
        "module_from_spec",
        lambda spec: SimpleNamespace(_bounded_command=bounded),
    )
    result = tmp_path / "raw.xcresult"
    if raw == b'{"ok":true}':
        assert helper.xcresult_json(
            result, operation, deadline=helper.time.monotonic() + 5
        ) == {"ok": True}
    else:
        with pytest.raises(helper.ArtifactError):
            helper.xcresult_json(
                result, operation, deadline=helper.time.monotonic() + 5
            )
    assert specs == [
        Path(helper.__file__).resolve().with_name("export_xccov_line_coverage.py")
    ]
    command, options = calls[0]
    assert command[:2] == ["/usr/bin/xcrun", "xcresulttool"]
    assert command[-2:] == ["--path", str(result)]
    assert command[2:-2] == (
        ["metadata", "get"]
        if operation == "metadata"
        else [
            "get",
            "test-results",
            operation,
            "--schema-version",
            "0.1.0",
            "--compact",
        ]
    )
    assert options["cwd"] == tmp_path
    assert options["max_stdout_bytes"] == 16 * 1024 * 1024
    assert (
        helper.time.monotonic()
        < options["export_deadline"]
        <= helper.time.monotonic() + 5
    )


def test_tool_failure_is_closed_without_source_or_staging_output(
    built, monkeypatch, capsys
):
    if built["platform"] != "ios":
        return
    assert helper.main(args(built, "prepare")) == 0
    raw_observations(built, monkeypatch)

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(
            1, "xcresulttool", output="PRIVATE staging output"
        )

    monkeypatch.setattr(helper, "xcresult_json", fail)
    assert helper.main(args(built, "validate-observations")) == 2
    assert capsys.readouterr().err == "apple_coverage_artifact_invalid\n"


def test_observation_query_cannot_change_an_earlier_lane(built, monkeypatch):
    if built["platform"] != "ios":
        return
    assert helper.main(args(built, "prepare")) == 0
    raw_observations(built, monkeypatch)
    old = helper.xcresult_json
    path = built["output"] / "coverage/raw/apple-ios-core.xcresult/Info.plist"

    def mutate(result, operation, *, deadline):
        value = old(result, operation, deadline=deadline)
        if result.name == "apple-ios-staging.xcresult" and operation == "tests":
            path.write_bytes(b"changed earlier lane")
        return value

    monkeypatch.setattr(helper, "xcresult_json", mutate)
    assert helper.main(args(built, "validate-observations")) == 2
