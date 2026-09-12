"""Pure security tests for native compiler-domain metadata, never native hit evidence.

Synthetic Mach-O headers and zero-count LLVM regions exercise parsing only. Real
App/Core binary probes and original xccov arrays are retained separately; none of
these fixture rows or identities may be used to qualify native product coverage.
"""

from __future__ import annotations

from copy import deepcopy
import json
import struct

import pytest

from scripts import native_xccov_domain as domain


APP = domain.APP_ROOT + "Example.swift"
CORE = domain.CORE_ROOT + "AstralCore/Example.swift"
ROOT = "/qualified/source"
SOURCE = b"enum Example {\n static func value() -> Int { 7 }\n}\n"


def _macho(lane="ui", commands=None, **changes):
    if commands is None:
        commands = [struct.pack("<6I", 0x32, 24, 7, 0x110000, 0x1A0500, 0)]
    payload = b"".join(commands)
    fields = [
        0xFEEDFACF,
        0x0100000C,
        0,
        8 if lane == "core" else 6,
        len(commands),
        len(payload),
        0,
        0,
    ]
    for key, value in changes.items():
        fields[int(key.removeprefix("word"))] = value
    return struct.pack("<8I", *fields) + payload


def _mapping(paths=(APP,), *, prefix=""):
    filenames = [ROOT + "/" + prefix + path for path in paths]
    return {
        "type": "llvm.coverage.json.export",
        "version": "3.0.1",
        "data": [
            {
                "files": [{"filename": name, "segments": []} for name in filenames],
                "functions": [
                    {
                        "branches": [],
                        "count": 0,
                        "mcdc_records": [],
                        "name": "$sFixture" + domain.sha256(paths[index].encode())[:12],
                        "filenames": [name],
                        "regions": [[2, 2, 2, 39, 0, 0, 0, 0]],
                    }
                    for index, name in enumerate(filenames)
                ],
                "totals": {},
            }
        ],
    }


def _geometry(value=None, *, paths=(APP,), prefix="", lane="ui", root=ROOT):
    return domain.mapping_geometry(
        json.dumps(
            value if value is not None else _mapping(paths, prefix=prefix)
        ).encode(),
        archive_root=root,
        prefix=prefix,
        tracked={prefix + path for path in paths},
        lane=lane,
    )


def _identity(lane="ui", raw=None):
    return {
        "member": domain.BINARY_MEMBERS["core" if lane == "core" else "app"],
        "sha256": domain.sha256(raw if raw is not None else _macho(lane)),
        "artifact_member": f"coverage/native-binaries/apple-ios-{lane}.zip",
        "artifact_sha256": domain.sha256(b"unit-test archive identity only"),
    }


def _witness(lane="ui", *, paths=None, observed=None, prefix=""):
    paths = paths or ((CORE,) if lane == "core" else (APP,))
    return domain.make_domain(
        geometry=_geometry(paths=paths, prefix=prefix, lane=lane),
        source_bytes=lambda _path: SOURCE,
        binary=_identity(lane),
        observed_sources=[prefix + path for path in (observed or paths)],
        prefix=prefix,
        lane=lane,
    )


def _rows():
    return [
        {"line": 1, "isExecutable": False},
        {"line": 2, "isExecutable": True, "executionCount": 0},
    ]


def _metadata(lane="ui"):
    # Field names and nesting match retained Xcode26.6 Core213/App198/UI22 JSON.
    device = {
        "deviceId": "fixture-device",
        "architecture": "arm64",
        "platform": "iOS Simulator",
    }
    identifier = (
        "ReleaseEvidenceUITests/testReleaseEvidenceProducesPlatformReport()"
        if lane == "staging"
        else "ExampleTests/testExample()"
    )
    bundle = {"core": "AstralCoreTests", "unit": "AstralAppTests"}.get(
        lane, "AstralAppUITests"
    )
    summary = {
        "result": "Passed",
        "totalTestCount": 1,
        "passedTests": 1,
        "failedTests": 0,
        "skippedTests": 0,
        "expectedFailures": 0,
        "devicesAndConfigurations": [{"device": deepcopy(device)}],
    }
    tests = {
        "devices": [device],
        "testNodes": [
            {
                "name": "AstralCore" if lane == "core" else "AstralApp",
                "nodeType": "Test Plan",
                "result": "Passed",
                "children": [
                    {
                        "name": bundle,
                        "result": "Passed",
                        "nodeType": "Unit test bundle",
                        "children": [
                            {
                                "name": "ExampleTests",
                                "nodeType": "Test Suite",
                                "children": [
                                    {
                                        "nodeType": "Test Case",
                                        "nodeIdentifier": identifier,
                                        "result": "Passed",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    return summary, tests


def _case(tests):
    return tests["testNodes"][0]["children"][0]["children"][0]["children"][0]


def _set(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


@pytest.mark.parametrize("lane", domain.LANES)
def test_qualified_thin_macho_and_native_lane_metadata(lane):
    domain.ios_macho(_macho(lane), lane)
    domain.test_identity(*_metadata(lane), lane)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\0" * 31,
        _macho(word0=0xCAFEBABE),
        _macho(word1=0x01000007),
        _macho(word2=2),
        _macho(word3=2),
        _macho(word4=0),
        _macho(word4=4097),
        _macho(word5=0),
        _macho(word5=10000),
        _macho(word7=1),
        _macho(commands=[struct.pack("<6I", 0x32, 24, 2, 0x110000, 0x1A0500, 0)]),
        _macho(commands=[struct.pack("<6I", 0x32, 24, 1, 0x110000, 0x1A0500, 0)]),
        _macho(commands=[struct.pack("<6I", 0x32, 24, 7, 0, 0x1A0500, 0)]),
        _macho(commands=[struct.pack("<6I", 0x32, 24, 7, 0x110000, 0, 0)]),
        _macho(commands=[struct.pack("<6I", 0x32, 24, 7, 0x110000, 0x1A0500, 1)]),
        _macho(commands=[struct.pack("<2I", 0x32, 8)]),
        _macho(commands=[struct.pack("<2I", 0x32, 7)]),
        _macho(commands=[struct.pack("<2I", 0x32, 32)]),
        _macho(commands=[struct.pack("<2I", 0x24, 8)]),
        _macho(commands=[struct.pack("<6I", 0x32, 24, 7, 0x110000, 0x1A0500, 0)] * 2),
    ],
)
def test_wrong_architecture_platform_kind_or_load_command_refuses(raw):
    with pytest.raises(domain.DomainError):
        domain.ios_macho(raw, "ui")


def test_core_bundle_kind_cannot_be_relabelled_as_app_dylib():
    with pytest.raises(domain.DomainError):
        domain.ios_macho(_macho("core"), "ui")
    with pytest.raises(domain.DomainError):
        domain.ios_macho(_macho("ui"), "core")


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff",
        b"{",
        b'{"same":1,"same":2}',
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b'{"value":1e400}',
    ],
)
def test_malformed_duplicate_or_nonfinite_json_refuses(raw):
    with pytest.raises(domain.DomainError):
        domain.strict_json(raw)


def test_strict_json_preserves_finite_summary_numbers_and_utf8():
    value = {"percent": 12.5, "filename": "λ.swift"}
    assert domain.strict_json(domain.canonical(value)) == value


def test_mapping_byte_limit_is_enforced_before_parse(monkeypatch):
    monkeypatch.setattr(domain, "MAX_MAPPING_BYTES", 1)
    with pytest.raises(domain.DomainError):
        domain.strict_json(b"{}")


@pytest.mark.parametrize(
    "path",
    [
        "/" + APP,
        "../" + APP,
        APP.replace("/", "\\"),
        APP.replace("Example", "../Example"),
        APP.replace("Example", "./Example"),
        APP.replace("Example", "/Example"),
        APP.replace(".swift", ".txt"),
        "apple-clients/AstralWatch/Watch.swift",
        APP + "\n",
        domain.APP_ROOT + "\ud800.swift",
        True,
        None,
    ],
)
def test_source_path_has_one_safe_maintained_spelling(path):
    with pytest.raises(domain.DomainError):
        domain.source_path(path, "")


@pytest.mark.parametrize("prefix", domain.SOURCE_PREFIXES)
def test_exact_source_bytes_and_prefix_are_kept_without_lexical_tail_guesses(prefix):
    path = prefix + APP
    assert domain.source_path(path, prefix) == path
    geometry = _geometry(prefix=prefix)
    witness = _witness(prefix=prefix)
    assert geometry[path]["native_last_line"] == 2
    assert witness["sources"][path] == {
        **geometry[path],
        "source_sha256": domain.sha256(SOURCE),
        "physical_lines": 3,
    }
    assert domain.source_facts(b"last line without newline")["physical_lines"] == 1
    with pytest.raises(domain.DomainError):
        domain.source_facts(b"")


@pytest.mark.parametrize(
    "root", ["relative", "/bad/", "/bad/../source", "/bad//source", "/bad\\source"]
)
def test_mapping_root_aliases_refuse(root):
    with pytest.raises(domain.DomainError):
        _geometry(root=root)


@pytest.mark.parametrize(
    "path,replacement",
    [
        (("version",), "2.0.1"),
        (("type",), "other"),
        (("data",), []),
        (("data", 0, "functions"), []),
        (("data", 0, "files"), []),
        (("data", 0, "functions", 0, "count"), True),
        (("data", 0, "functions", 0, "count"), 1),
        (("data", 0, "functions", 0, "name"), "\ud800"),
        (("data", 0, "functions", 0, "regions"), []),
        (("data", 0, "functions", 0, "regions", 0), [1, 1]),
        (("data", 0, "functions", 0, "regions", 0, 0), True),
        (("data", 0, "functions", 0, "regions", 0, 0), 0),
        (("data", 0, "functions", 0, "regions", 0, 2), 1000001),
        (("data", 0, "functions", 0, "regions", 0, 3), 0),
        (("data", 0, "functions", 0, "regions", 0, 4), 1),
        (("data", 0, "functions", 0, "regions", 0, 5), 1),
        (("data", 0, "functions", 0, "regions", 0, 6), 1),
        (("data", 0, "functions", 0, "regions", 0, 7), 4),
        (("data", 0, "files", 0, "segments"), {}),
    ],
)
def test_malformed_or_unsupported_mapping_records_refuse(path, replacement):
    value = _mapping()
    _set(value, path, replacement)
    with pytest.raises(domain.DomainError):
        _geometry(value)


def test_mapping_rejects_duplicate_file_function_and_undeclared_filename():
    for field in ("files", "functions"):
        value = _mapping()
        value["data"][0][field].append(deepcopy(value["data"][0][field][0]))
        with pytest.raises(domain.DomainError):
            _geometry(value)
    value = _mapping()
    value["data"][0]["functions"][0]["filenames"] = ["/unlisted/other.swift"]
    with pytest.raises(domain.DomainError):
        _geometry(value)


@pytest.mark.parametrize(
    "alias", ["/alternate/" + APP, APP, ROOT + "/components/AstralProjection/" + APP]
)
def test_alternate_checkout_and_composed_root_cannot_hide_as_external(alias):
    value = _mapping()
    value["data"][0]["files"][0]["filename"] = alias
    value["data"][0]["functions"][0]["filenames"] = [alias]
    with pytest.raises(domain.DomainError):
        _geometry(value)


def test_external_mapping_is_ignored_but_not_allowed_to_expand_owned_code():
    value = _mapping()
    external = "/sdk/external.swift"
    value["data"][0]["files"].append({"filename": external, "segments": []})
    func = deepcopy(value["data"][0]["functions"][0])
    func.update(name="sdk-function", filenames=[external])
    value["data"][0]["functions"].append(func)
    assert set(_geometry(value)) == {APP}
    own = value["data"][0]["functions"][0]
    own["filenames"].append(external)
    own["regions"][0][6:] = [1, 1]
    with pytest.raises(domain.DomainError):
        _geometry(value)


def test_all_region_kinds_and_per_function_file_ids_contribute_geometry():
    value = _mapping((APP, CORE))
    function = value["data"][0]["functions"][0]
    function["filenames"].append(ROOT + "/" + CORE)
    function["regions"] += [
        [2, 1, 2, 2, 0, 0, 1, 1],
        [2, 1, 2, 3, 0, 0, 0, 2],
        [2, 1, 2, 4, 0, 1, 0, 3],
    ]
    result = _geometry(value, paths=(APP, CORE))
    reordered = deepcopy(value)
    reordered["data"][0]["functions"].reverse()
    reordered["data"][0]["functions"][1]["regions"].reverse()
    assert result == _geometry(reordered, paths=(APP, CORE))
    changed = deepcopy(value)
    changed["data"][0]["functions"][0]["regions"][-1][3] = 5
    assert _geometry(changed, paths=(APP, CORE))[CORE] != result[CORE]
    with pytest.raises(domain.DomainError):
        _geometry(value, paths=(APP, CORE), lane="core")


def test_geometry_cardinality_bounds_apply_before_witness(monkeypatch):
    monkeypatch.setattr(domain, "MAX_FILES", 1)
    with pytest.raises(domain.DomainError):
        _geometry(paths=(APP, CORE))
    monkeypatch.setattr(domain, "MAX_REGIONS", 1)
    value = _mapping()
    value["data"][0]["functions"][0]["regions"] *= 2
    with pytest.raises(domain.DomainError):
        _geometry(value)


def test_app_unit_can_observe_app_only_while_retaining_complete_core_mapping():
    witness = _witness("unit", paths=(APP, CORE), observed=(APP,))
    coverage = {APP: _rows()}
    report = domain.native_report(coverage, {"unit": witness})
    assert report["coverage"] is coverage
    assert set(report["domains"]["unit"]["sources"]) == {APP, CORE}
    assert report["domains"]["unit"]["observed_sources"] == [APP]
    assert domain.parse_native_report(report) == report


def test_domain_union_preserves_original_rows_and_rejects_conflicting_sources():
    core = _witness("core")
    unit = _witness("unit", paths=(APP, CORE), observed=(APP,))
    ui = _witness("ui", paths=(APP, CORE))
    rows = {APP: _rows(), CORE: _rows()}
    domains = {"core": core, "unit": unit, "ui": ui}
    assert domain.native_report(rows, domains)["coverage"] is rows
    bad = deepcopy(domains)
    bad["ui"]["sources"][CORE]["geometry_sha256"] = "a" * 64
    with pytest.raises(domain.DomainError):
        domain.native_report(rows, bad)


@pytest.mark.parametrize(
    "change",
    [
        lambda w: w.update(format="future"),
        lambda w: w.update(platform="macos"),
        lambda w: w.update(architecture="x86_64"),
        lambda w: w.update(lane="unknown"),
        lambda w: w.update(source_prefix="other/"),
        lambda w: w.update(xcode_build="unknown"),
        lambda w: w["binary"].update(member="Products/AstralDeep"),
        lambda w: w["binary"].update(artifact_member="../artifact.zip"),
        lambda w: w["binary"].update(sha256="not-a-digest"),
        lambda w: w["sources"][APP].update(physical_lines=True),
        lambda w: w["sources"][APP].update(native_last_line=4),
        lambda w: w["sources"][APP].update(geometry_sha256="UPPER"),
        lambda w: w.update(observed_sources=[]),
        lambda w: w.update(observed_sources=[{}]),
        lambda w: w.update(observed_sources=[APP, APP]),
        lambda w: w.update(observed_sources=[CORE]),
        lambda w: w.update(extra="unexpected"),
    ],
)
def test_forged_malformed_or_inconsistent_witness_refuses(change):
    witness = _witness()
    change(witness)
    with pytest.raises(domain.DomainError):
        domain.validate_domain(witness)


@pytest.mark.parametrize(
    "change",
    [
        lambda rows: rows[APP].pop(),
        lambda rows: rows[APP].append({"line": 3}),
        lambda rows: rows[APP][0].update(line=True),
        lambda rows: rows[APP][1].update(line=1),
        lambda rows: rows.update({CORE: _rows()}),
        lambda rows: rows.clear(),
    ],
)
def test_missing_internal_truncated_or_invented_prefix_refuses(change):
    rows = {APP: _rows()}
    change(rows)
    with pytest.raises(domain.DomainError):
        domain.native_report(rows, {"ui": _witness()})


def test_envelope_lane_prefix_and_field_mismatch_refuse():
    rows = {APP: _rows()}
    with pytest.raises(domain.DomainError):
        domain.native_report(rows, {"unit": _witness("ui")})
    report = domain.native_report(rows, {"ui": _witness()})
    report["extra"] = "untrusted"
    with pytest.raises(domain.DomainError):
        domain.parse_native_report(report)
    with pytest.raises(domain.DomainError):
        domain.native_report(
            rows,
            {
                "ui": _witness(),
                "core": _witness("core", prefix="components/AstralProjection/"),
            },
        )


@pytest.mark.parametrize(
    "side,path,value",
    [
        ("summary", ("result",), "Failed"),
        ("summary", ("totalTestCount",), True),
        ("summary", ("passedTests",), 0),
        ("summary", ("skippedTests",), 1),
        ("summary", ("devicesAndConfigurations",), []),
        ("summary", ("devicesAndConfigurations", 0, "device", "platform"), "macOS"),
        (
            "summary",
            ("devicesAndConfigurations", 0, "device", "architecture"),
            "x86_64",
        ),
        ("tests", ("devices", 0, "deviceId"), "other-device"),
        ("tests", ("testNodes", 0, "name"), "OtherPlan"),
        ("tests", ("testNodes", 0, "children", 0, "name"), "OtherTests"),
    ],
)
def test_test_metadata_denies_wrong_result_device_architecture_or_lane(
    side, path, value
):
    summary, tests = _metadata()
    _set(summary if side == "summary" else tests, path, value)
    with pytest.raises(domain.DomainError):
        domain.test_identity(summary, tests, "ui")


def test_test_metadata_requires_unique_passed_leaf_identifiers_and_staging_separation():
    for value in (False, "Failed"):
        summary, tests = _metadata()
        case = _case(tests)
        case["nodeIdentifier" if value is False else "result"] = value
        with pytest.raises(domain.DomainError):
            domain.test_identity(summary, tests, "ui")
    summary, tests = _metadata()
    suite = tests["testNodes"][0]["children"][0]["children"][0]
    suite["children"].append(deepcopy(_case(tests)))
    summary.update(totalTestCount=2, passedTests=2)
    with pytest.raises(domain.DomainError):
        domain.test_identity(summary, tests, "ui")
    with pytest.raises(domain.DomainError):
        domain.test_identity(*_metadata("staging"), "ui")
    with pytest.raises(domain.DomainError):
        domain.test_identity(*_metadata("ui"), "staging")


def test_collector_uses_fixed_geometry_command_and_requires_pinned_toolchain(tmp_path):
    raw = _macho()
    binary = tmp_path / "AstralDeep.debug.dylib"
    binary.write_bytes(raw)
    calls = []

    def run(command, bound):
        calls.append((command, bound))
        if command == ["/usr/bin/xcodebuild", "-version"]:
            return b"Xcode 26.6\nBuild version 17F113\n"
        if command == ["/usr/bin/xcrun", "llvm-cov", "--version"]:
            return b"Apple LLVM version 21.0.0\n  Optimized build.\n"
        assert command == [
            "/usr/bin/xcrun",
            "llvm-cov",
            "export",
            "--empty-profile",
            "--arch=arm64",
            str(binary),
        ]
        return domain.canonical(_mapping())

    arguments = dict(
        binary_path=binary,
        binary_bytes=raw,
        binary_identity=_identity(raw=raw),
        archive_root=ROOT,
        prefix="",
        lane="ui",
        tracked={APP},
        source_bytes=lambda _path: SOURCE,
        run=run,
        summary=_metadata()[0],
        tests=_metadata()[1],
    )
    witness = domain.collect_domain(**arguments)
    assert witness["sources"][APP]["native_last_line"] == 2
    assert len(calls) == 3
    assert calls[-1][1] == domain.MAX_MAPPING_BYTES
    for reply in (b"Xcode 26.5\nBuild version 17F113", b"other"):
        with pytest.raises(domain.DomainError):
            domain.collect_domain(
                **{**arguments, "run": lambda _command, _bound, r=reply: r}
            )
    bad_identity = {**_identity(raw=raw), "sha256": "f" * 64}
    with pytest.raises(domain.DomainError):
        domain.collect_domain(**{**arguments, "binary_identity": bad_identity})


def test_deep_json_has_stable_data_free_refusal():
    with pytest.raises(domain.DomainError, match="^native_xccov_domain_invalid$"):
        domain.strict_json(b"[" * 4096 + b"0" + b"]" * 4096)


@pytest.mark.parametrize("mode", ["wrong-bytes", "symlink", "replaced-during-tool"])
def test_collector_rejects_path_identity_mismatch_and_midread_replacement(
    tmp_path, mode
):
    raw = _macho()
    binary = tmp_path / "AstralDeep.debug.dylib"
    binary.write_bytes(raw if mode != "wrong-bytes" else _macho(word6=1))
    if mode == "symlink":
        target = tmp_path / "other.dylib"
        target.write_bytes(raw)
        binary.unlink()
        binary.symlink_to(target)

    def run(command, _bound):
        if command == ["/usr/bin/xcodebuild", "-version"]:
            return b"Xcode 26.6\nBuild version 17F113\n"
        if command == ["/usr/bin/xcrun", "llvm-cov", "--version"]:
            return b"Apple LLVM version 21.0.0\n"
        replacement = tmp_path / "replacement.dylib"
        replacement.write_bytes(raw)
        replacement.replace(binary)
        return domain.canonical(_mapping())

    with pytest.raises(domain.DomainError):
        domain.collect_domain(
            binary_path=binary,
            binary_bytes=raw,
            binary_identity=_identity(raw=raw),
            archive_root=ROOT,
            prefix="",
            lane="ui",
            tracked={APP},
            source_bytes=lambda _path: SOURCE,
            run=run,
            summary=_metadata()[0],
            tests=_metadata()[1],
        )


# Complete23-region CanvasExportPolicy geometry from Apple LLVM21 actual iOS
# Core binary c28d39e8..., plus real metadata-only Mach-O load-command prefixes.
# Empty-profile counts stay zero and are never interpreted as executed coverage.
_ACTUAL_CORE_FUNCTIONS = [
    {
        "branches": [],
        "count": 0,
        "filenames": [
            "/qualified/source/apple-clients/AstralCore/Sources/AstralCore/API/CanvasExportPolicy.swift"
        ],
        "mcdc_records": [],
        "name": "$s10AstralCore18CanvasExportPolicyO14boundedSession4likeSo12NSURLSessionCAGSg_tFZ",
        "regions": [[13, 79, 24, 6, 0, 0, 0, 0]],
    },
    {
        "branches": [],
        "count": 0,
        "filenames": [
            "/qualified/source/apple-clients/AstralCore/Sources/AstralCore/API/CanvasExportPolicy.swift"
        ],
        "mcdc_records": [],
        "name": "$s10AstralCore18CanvasExportPolicyO14boundedSession4likeSo12NSURLSessionCAGSg_tFZ0F0L__7ceilingS2dSg_SdtF",
        "regions": [
            [17, 70, 20, 10, 0, 0, 0, 0],
            [18, 70, 18, 88, 0, 0, 0, 0],
            [18, 88, 19, 42, 0, 0, 0, 0],
        ],
    },
    {
        "branches": [],
        "count": 0,
        "filenames": [
            "/qualified/source/apple-clients/AstralCore/Sources/AstralCore/API/CanvasExportPolicy.swift"
        ],
        "mcdc_records": [],
        "name": "$s10AstralCore18CanvasExportPolicyO15validateRequestyAA9JSONValueO10Foundation4DataVKFZ",
        "regions": [
            [26, 67, 39, 6, 0, 0, 0, 0],
            [37, 14, 37, 46, 0, 0, 0, 0],
            [37, 46, 38, 21, 0, 0, 0, 0],
        ],
    },
    {
        "branches": [],
        "count": 0,
        "filenames": [
            "/qualified/source/apple-clients/AstralCore/Sources/AstralCore/API/CanvasExportPolicy.swift"
        ],
        "mcdc_records": [],
        "name": "$s10AstralCore18CanvasExportPolicyO16validateResponse_7capturey10Foundation4DataV_AA9JSONValueOtKFZ",
        "regions": [
            [41, 75, 51, 6, 0, 0, 0, 0],
            [50, 14, 50, 54, 0, 0, 0, 0],
            [50, 54, 51, 6, 0, 0, 0, 0],
        ],
    },
    {
        "branches": [],
        "count": 0,
        "filenames": [
            "/qualified/source/apple-clients/AstralCore/Sources/AstralCore/API/CanvasExportPolicy.swift"
        ],
        "mcdc_records": [],
        "name": "$s10AstralCore18CanvasExportPolicyO8response_7sessionSi_10Foundation4DataVSSSgtAF10URLRequestV_So12NSURLSessionCtYaKFZ",
        "regions": [
            [53, 107, 69, 6, 0, 0, 0, 0],
            [56, 108, 68, 99, 0, 0, 0, 0],
            [57, 60, 57, 98, 0, 0, 0, 0],
            [57, 98, 68, 99, 0, 0, 0, 0],
            [58, 73, 60, 10, 0, 0, 0, 0],
            [60, 10, 68, 99, 0, 0, 0, 0],
            [62, 12, 68, 99, 0, 0, 0, 0],
            [62, 37, 66, 10, 0, 0, 0, 0],
            [63, 41, 66, 10, 0, 0, 0, 0],
            [64, 56, 64, 101, 0, 0, 0, 0],
            [64, 101, 66, 10, 0, 0, 0, 0],
            [66, 10, 68, 99, 0, 0, 0, 0],
            [67, 37, 68, 99, 0, 0, 0, 0],
        ],
    },
]

_ACTUAL_LOAD_COMMAND_PREFIXES = {
    "core": "cffaedfe0c00000100000000080000002e00000048190000850000000000000019000000180800005f5f5445585400000000000000000000000000000000000000c0250000000000000000000000000000c0250000000000050000000500000019000000000000005f5f74657874000000000000000000005f5f5445585400000000000000000000e019000000000000ecac210000000000e0190000020000000000000000000000000400800000000000000000000000005f5f73747562730000000000000000005f5f5445585400000000000000000000ccc62100000000004c1d000000000000ccc6210002000000000000000000000008040080000000000c000000000000005f5f6f626a635f7374756273000000005f5f544558540000000000000000000020e4210000000000600900000000000020e42100050000000000000000000000000400800000000000000000000000005f5f696e69745f6f66667365747300005f5f544558540000000000000000000080ed210000000000040000000000000080ed2100020000000000000000000000160000000000000000000000000000005f5f6f626a635f6d6574686c697374005f5f544558540000000000000000000088ed210000000000141300000000000088ed2100030000000000000000000000000000000000000000000000000000005f5f6f626a635f636c6173736e616d655f5f5445585400000000000000000000a0002200000000009a0f000000000000a0002200040000000000000000000000020000000000000000000000000000005f5f636f6e73740000000000000000005f5f54455854000000000000000000004010220000000000e86600000000000040102200040000000000000000000000000000000000000000000000000000005f5f636f6e7374675f737769667474005f5f54455854000000000000000000002877220000000000ec2700000000000028772200020000000000000000000000000000000000000000000000000000005f5f7377696674355f747970657265665f5f5445585400000000000000000000149f220000000000c31a000000000000149f2200010000000000000000000000000000000000000000000000000000005f5f7377696674355f6669656c646d645f5f5445585400000000000000000000d8b9220000000000b032000000000000d8b92200020000000000000000000000000000000000000000000000000000005f5f63737472696e67000000000000005f5f544558540000000000000000000090ec220000000000e2d500000000000090ec2200040000000000000000000000020000000000000000000000000000005f5f7377696674355f747970657300005f5f544558540000000000000000000074c2230000000000980300000000000074c22300020000000000000000000000000000000000000000000000000000005f5f6f626a635f6d6574686e616d65005f5f544558540000000000000000000010c6230000000000993c00000000000010c62300040000000000000000000000020000000000000000000000000000005f5f6f626a635f6d65746874797065005f5f5445585400000000000000000000b0022400000000001406000000000000b0022400040000000000000000000000020000000000000000000000000000005f5f7377696674355f636170747572655f5f5445585400000000000000000000c408240000000000600a000000000000c4082400020000000000000000000000000000000000000000000000000000005f5f7377696674355f7265666c7374725f5f54455854000000000000000000003013240000000000ec1c00000000000030132400040000000000000000000000000000000000000000000000000000005f5f7377696674355f70726f746f00005f5f54455854000000000000000000001c30240000000000b8030000000000001c302400020000000000000000000000000000000000000000000000000000005f5f73776966745f61735f656e7472795f5f5445585400000000000000000000d4332400000000009802000000000000d4332400020000000000000000000000000000000000000000000000000000005f5f73776966745f61735f72657400005f5f54455854000000000000000000006c36240000000000b0020000000000006c362400020000000000000000000000000000000000000000000000000000005f5f7377696674355f6275696c74696e5f5f54455854000000000000000000001c39240000000000a4010000000000001c392400020000000000000000000000000000000000000000000000000000005f5f7377696674355f6173736f6374795f5f5445585400000000000000000000c03a240000000000f804000000000000c03a2400020000000000000000000000000000000000000000000000000000005f5f7377696674355f6d70656e756d005f5f5445585400000000000000000000b83f2400000000005000000000000000b83f2400020000000000000000000000000000000000000000000000000000005f5f7377696674355f70726f746f73005f5f54455854000000000000000000000840240000000000040000000000000008402400020000000000000000000000000000000000000000000000000000005f5f756e77696e645f696e666f0000005f5f54455854000000000000000000000c40240000000000a0700000000000000c402400020000000000000000000000000000000000000000000000000000005f5f65685f6672616d650000000000005f5f5445585400000000000000000000b0b024000000000088f4000000000000b0b024000300000000000000000000000b00006800000000000000000000000019000000d80100005f5f444154415f434f4e53540000000000c0250000000000008000000000000000c02500000000000080000000000000030000000300000005000000100000005f5f676f7400000000000000000000005f5f444154415f434f4e53540000000000c0250000000000c81c00000000000000c02500030000000000000000000000060000007102000000000000000000005f5f636f6e73740000000000000000005f5f444154415f434f4e535400000000c8dc250000000000c85d000000000000c8dc2500030000000000000000000000000000000000000000000000000000005f5f6f626a635f636c6173736c6973745f5f444154415f434f4e535400000000903a2600000000004802000000000000903a2600030000000000000000000000000000100000000000000000000000005f5f6f626a635f70726f746f6c6973745f5f444154415f434f4e535400000000d83c2600000000006000000000000000d83c2600030000000000000000000000000000000000000000000000000000005f5f6f626a635f696d616765696e666f5f5f444154415f434f4e535400000000383d2600000000000800000000000000383d26000200000000000000000000000000000000000000000000000000000019000000f80400005f5f4441544100000000000000000000004026000000000000400500000000000040260000000000008004000000000003000000030000000f000000000000005f5f6f626a635f636f6e7374000000005f5f44415441000000000000000000000040260000000000a83a00000000000000402600030000000000000000000000000000000000000000000000000000005f5f6f626a635f73656c7265667300005f5f4441544100000000000000000000a87a260000000000780a000000000000a87a2600030000000000000000000000050000100000000000000000000000005f5f6f626a635f70726f746f726566735f5f444154410000000000000000000020852600000000003000000000000000208526000300000000000000000000000b0000100000000000000000000000005f5f6f626a635f636c617373726566735f5f44415441000000000000000000005085260000000000900000000000000050852600030000000000000000000000000000100000000000000000000000005f5f6f626a635f6461746100000000005f5f4441544100000000000000000000e085260000000000b015000000000000e0852600030000000000000000000000000000000000000000000000000000005f5f64617461000000000000000000005f5f4441544100000000000000000000909b260000000000dc5d000000000000909b2600040000000000000000000000000000000000000000000000000000005f5f6c6c766d5f7072665f636e7473005f5f44415441000000000000000000000000270000000000b076000000000000000027000e0000000000000000000000000000000000000000000000000000005f5f6c6c766d5f7072665f64617461005f5f444154410000000000000000000000802700000000000078020000000000008027000e0000000000000000000000000000000000000000000000000000005f5f6c6c766d5f7072665f6e616d65735f5f444154410000000000000000000000f8290000000000978f00000000000000f82900000000000000000000000000000000000000000000000000000000005f5f6c6c766d5f7072665f62697473005f5f444154410000000000000000000000c02a0000000000000000000000000000c02a000e0000000000000000000000000000000000000000000000000000005f5f6c6c766d5f7072665f766e6473005f5f444154410000000000000000000000c02a0000000000000000000000000000c02a00000000000000000000000000000000000000000000000000000000005f5f6c6c766d5f7072665f766e7300005f5f444154410000000000000000000000c02a0000000000000000000000000000c02a00000000000000000000000000000000000000000000000000000000005f5f6c6c766d5f7072665f76746162005f5f444154410000000000000000000000c02a0000000000000000000000000000c02a00000000000000000000000000000000000000000000000000000000005f5f636f6d6d6f6e00000000000000005f5f444154410000000000000000000000c02a0000000000280200000000000000000000030000000000000000000000010000000000000000000000000000005f5f62737300000000000000000000005f5f444154410000000000000000000030c22a0000000000e899000000000000000000000400000000000000000000000100000000000000000000000000000019000000e80000005f5f4c4c564d5f434f5600000000000000802b0000000000000002000000000000c02a00000000000000020000000000030000000300000002000000000000005f5f6c6c766d5f636f7666756e0000005f5f4c4c564d5f434f5600000000000000802b00000000001ec801000000000000c02a00030000000000000000000000000000000000000000000000000000005f5f6c6c766d5f636f766d61700000005f5f4c4c564d5f434f5600000000000020482d0000000000a01b00000000000020882c000300000000000000000000000000000000000000000000000000000019000000480000005f5f4c494e4b4544495400000000000000802d0000000000000036000000000000c02c000000000010d335000000000001000000010000000000000000000000340000801000000000c02c0030aa00003300008010000000306a2d0000a201000200000018000000783c2f0093160100d0bd4000c05c21000b0000005000000000000000ff020100ff0201009e0f00009d120100f6030000000000000000000000000000000000000000000000000000a8a540000a060000000000000000000000000000000000001b0000001800000013f1f8312a2d3cdbbf2f4c136e7968e13200000020000000070000000000110000051a0001000000030000000000f3042a0000001000000000000000000000000c00000038000000180000000200000000004861000000004072706174682f5843546573742e6672616d65776f726b2f58435465737400000c00000040000000180000000200000000004861000001004072706174682f6c69625843546573745377696674537570706f72742e64796c69620000000000000c0000005800000018000000020000000405a21300002c012f53797374656d2f4c6962726172792f4672616d65776f726b732f466f756e646174696f6e2e6672616d65776f726b2f466f756e646174696f6e0000000000000c0000003800000018000000020000000000e400000001002f7573722f6c69622f6c69626f626a632e412e64796c696200000000000000000c00000038000000180000000200000000004c05000001002f7573722f6c69622f6c696253797374656d2e422e64796c69620000000000000c0000006000000018000000020000000405a213000096002f53797374656d2f4c6962726172792f4672616d65776f726b732f436f7265466f756e646174696f6e2e6672616d65776f726b2f436f7265466f756e646174696f6e0000000000000c00000050000000180000000200000000046d03000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f436f7265546578742e6672616d65776f726b2f436f72655465787400000c00000058000000180000000200000000000100000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f43727970746f4b69742e6672616d65776f726b2f43727970746f4b697400000000000000000c000000500000001800000002000000017ab416000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f4e6574776f726b2e6672616d65776f726b2f4e6574776f726b000000000c0000005000000018000000020000004378cdf1000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f53656375726974792e6672616d65776f726b2f53656375726974790000180000805000000018000000020000000505a623000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f55494b69742e6672616d65776f726b2f55494b697400000000000000000c00000040000000180000000200000000000000000000002f7573722f6c69622f73776966742f6c69627377696674436f72652e64796c6962000000000000001800008048000000180000000200000000647800000001002f7573722f6c69622f73776966742f6c69627377696674436f7265466f756e646174696f6e2e64796c696200000000001800008040000000180000000200000000020200000001002f7573722f6c69622f73776966742f6c69627377696674436f7265496d6167652e64796c696200001800008040000000180000000200000008787901000001002f7573722f6c69622f73776966742f6c6962737769667444617277696e2e64796c696200000000000c00000040000000180000000200000020640606000001002f7573722f6c69622f73776966742f6c6962737769667444697370617463682e64796c69620000001800008040000000180000000200000000027501000001002f7573722f6c69622f73776966742f6c696273776966744d6574616c2e64796c69620000000000001800008040000000180000000200000000000a00000001002f7573722f6c69622f73776966742f6c696273776966744f534c6f672e64796c69620000000000000c0000004000000018000000020000000007b703000001002f7573722f6c69622f73776966742f6c696273776966744f626a656374697665432e64796c6962001800008040000000180000000200000000000500000001002f7573722f6c69622f73776966742f6c6962737769667451756172747a436f72652e64796c6962001800008040000000180000000200000000000100000001002f7573722f6c69622f73776966742f6c696273776966745370617469616c2e64796c6962000000001800008050000000180000000200000001056f03000001002f7573722f6c69622f73776966742f6c69627377696674556e69666f726d547970654964656e746966696572732e64796c696200000000001800008040000000180000000200000002788000000001002f7573722f6c69622f73776966742f6c696273776966745850432e64796c696200000000000000000c00000048000000180000000200000000000000000000002f7573722f6c69622f73776966742f6c696273776966745f436f6e63757272656e63792e64796c6962000000000000001800008038000000180000000200000000003a04000001002f7573722f6c69622f73776966742f6c696273776966746f732e64796c6962001800008040000000180000000200000000001700000001002f7573722f6c69622f73776966742f6c6962737769667473696d642e64796c6962000000000000000c00000050000000180000000200000003050700000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f537769667455492e6672616d65776f726b2f53776966745549000000001c000080200000000c0000002f7573722f6c69622f73776966740000000000001c000080600000000c0000002f746d702f61737472616c2d3038382d636f72652d696f732f4275696c642f50726f64756374732f44656275672d6970686f6e6573696d756c61746f722f5061636b6167654672616d65776f726b7300000000001c000080280000000c000000406c6f616465725f706174682f4672616d65776f726b7300000000001c000080280000000c000000406c6f616465725f706174682f2e2e2f4672616d65776f726b7300002600000010000000300c2f00483000002900000010000000783c2f00000000001d00000010000000901a620080780000",
    "ui": "cffaedfe0c00000100000000060000005200000058260000850011000000000019000000b80800005f5f5445585400000000000000000000000000000000000000c0cf0000000000000000000000000000c0cf000000000005000000050000001b000000000000005f5f74657874000000000000000000005f5f54455854000000000000000000008027000000000000e8efba000000000080270000020000000000000000000000000400800000000000000000000000005f5f73747562730000000000000000005f5f54455854000000000000000000006817bb0000000000e44e0000000000006817bb0002000000000000000000000008040080000000000c000000000000005f5f6f626a635f7374756273000000005f5f54455854000000000000000000006066bb0000000000a0590000000000006066bb00050000000000000000000000000400800000000000000000000000005f5f696e69745f6f66667365747300005f5f544558540000000000000000000000c0bb0000000000080000000000000000c0bb00020000000000000000000000160000000000000000000000000000005f5f6f626a635f6d6574686c697374005f5f544558540000000000000000000008c0bb00000000002c5000000000000008c0bb00030000000000000000000000000000000000000000000000000000005f5f636f6e73740000000000000000005f5f54455854000000000000000000004010bc000000000060e10300000000004010bc00040000000000000000000000000000000000000000000000000000005f5f63737472696e67000000000000005f5f5445585400000000000000000000a0f1bf00000000005edf010000000000a0f1bf00040000000000000000000000020000000000000000000000000000005f5f7377696674355f747970657265665f5f544558540000000000000000000000d1c10000000000403f03000000000000d1c100040000000000000000000000000000000000000000000000000000005f5f7377696674355f636170747572655f5f54455854000000000000000000004010c5000000000038690000000000004010c500020000000000000000000000000000000000000000000000000000005f5f7377696674355f7265666c7374725f5f54455854000000000000000000008079c50000000000ccd60000000000008079c500040000000000000000000000000000000000000000000000000000005f5f7377696674355f6173736f6374795f5f54455854000000000000000000004c50c60000000000a8310000000000004c50c600020000000000000000000000000000000000000000000000000000005f5f636f6e7374675f737769667474005f5f5445585400000000000000000000f481c600000000006421010000000000f481c600020000000000000000000000000000000000000000000000000000005f5f7377696674355f6669656c646d645f5f544558540000000000000000000058a3c70000000000fc2301000000000058a3c700020000000000000000000000000000000000000000000000000000005f5f6f626a635f636c6173736e616d655f5f544558540000000000000000000060c7c80000000000e73300000000000060c7c800040000000000000000000000020000000000000000000000000000005f5f6f626a635f6d6574686e616d65005f5f544558540000000000000000000050fbc8000000000055fd00000000000050fbc800040000000000000000000000020000000000000000000000000000005f5f7377696674355f6275696c74696e5f5f5445585400000000000000000000a8f8c90000000000c409000000000000a8f8c900020000000000000000000000000000000000000000000000000000005f5f7377696674355f70726f746f00005f5f54455854000000000000000000006c02ca0000000000f02c0000000000006c02ca00020000000000000000000000000000000000000000000000000000005f5f7377696674355f747970657300005f5f54455854000000000000000000005c2fca000000000068150000000000005c2fca00020000000000000000000000000000000000000000000000000000005f5f73776966745f61735f656e7472795f5f5445585400000000000000000000c444ca00000000002811000000000000c444ca00020000000000000000000000000000000000000000000000000000005f5f73776966745f61735f72657400005f5f5445585400000000000000000000ec55ca00000000004c13000000000000ec55ca00020000000000000000000000000000000000000000000000000000005f5f6f626a635f6d65746874797065005f5f54455854000000000000000000004069ca000000000007490000000000004069ca00040000000000000000000000020000000000000000000000000000005f5f7377696674355f6d70656e756d005f5f544558540000000000000000000048b2ca0000000000e40300000000000048b2ca00020000000000000000000000000000000000000000000000000000005f5f7377696674355f70726f746f73005f5f54455854000000000000000000002cb6ca0000000000e8000000000000002cb6ca00020000000000000000000000000000000000000000000000000000005f5f7377696674355f656e74727900005f5f544558540000000000000000000014b7ca0000000000080000000000000014b7ca00020000000000000000000000000000000000000000000000000000005f5f6763635f6578636570745f7461625f5f54455854000000000000000000001cb7ca00000000003c000000000000001cb7ca00020000000000000000000000000000000000000000000000000000005f5f756e77696e645f696e666f0000005f5f544558540000000000000000000058b7ca000000000058e901000000000058b7ca00020000000000000000000000000000000000000000000000000000005f5f65685f6672616d650000000000005f5f5445585400000000000000000000b0a0cc0000000000b4ee020000000000b0a0cc000300000000000000000000000b00006800000000000000000000000019000000d80100005f5f444154415f434f4e53540000000000c0cf0000000000000004000000000000c0cf00000000000000040000000000030000000300000005000000100000005f5f676f7400000000000000000000005f5f444154415f434f4e53540000000000c0cf0000000000e05c00000000000000c0cf00030000000000000000000000060000009306000000000000000000005f5f636f6e73740000000000000000005f5f444154415f434f4e535400000000e01cd00000000000586d030000000000e01cd000030000000000000000000000000000000000000000000000000000005f5f6f626a635f636c6173736c6973745f5f444154415f434f4e535400000000388ad300000000007007000000000000388ad300030000000000000000000000000000100000000000000000000000005f5f6f626a635f70726f746f6c6973745f5f444154415f434f4e535400000000a891d30000000000b801000000000000a891d300030000000000000000000000000000000000000000000000000000005f5f6f626a635f696d616765696e666f5f5f444154415f434f4e5354000000006093d3000000000008000000000000006093d3000200000000000000000000000000000000000000000000000000000019000000e80500005f5f444154410000000000000000000000c0d3000000000000001d000000000000c0d3000000000000c0170000000000030000000300000012000000000000005f5f6f626a635f636f6e7374000000005f5f444154410000000000000000000000c0d30000000000085b01000000000000c0d300030000000000000000000000000000000000000000000000000000005f5f6f626a635f73656c7265667300005f5f4441544100000000000000000000081bd50000000000c02a000000000000081bd500030000000000000000000000050000100000000000000000000000005f5f6f626a635f70726f746f726566735f5f4441544100000000000000000000c845d500000000007801000000000000c845d5000300000000000000000000000b0000100000000000000000000000005f5f6f626a635f636c617373726566735f5f44415441000000000000000000004047d50000000000a8030000000000004047d500030000000000000000000000000000100000000000000000000000005f5f6f626a635f6461746100000000005f5f4441544100000000000000000000e84ad500000000005884000000000000e84ad500030000000000000000000000000000000000000000000000000000005f5f64617461000000000000000000005f5f444154410000000000000000000040cfd50000000000a6c301000000000040cfd500040000000000000000000000000000000000000000000000000000005f5f6c6c766d5f7072665f636e7473005f5f444154410000000000000000000000c0d70000000000b87303000000000000c0d7000e0000000000000000000000000000000000000000000000000000005f5f6c6c766d5f7072665f64617461005f5f44415441000000000000000000000040db000000000040b80c00000000000040db000e0000000000000000000000000000000000000000000000000000005f5f6c6c766d5f7072665f6e616d65735f5f444154410000000000000000000040f8e70000000000515603000000000040f8e700000000000000000000000000000000000000000000000000000000005f5f6f626a635f737475626c697374005f5f4441544100000000000000000000984eeb00000000001800000000000000984eeb00030000000000000000000000000000000000000000000000000000005f5f735f6173796e635f686f6f6b00005f5f4441544100000000000000000000b04eeb00000000009001000000000000b04eeb00030000000000000000000000000000000000000000000000000000005f5f737769667435365f686f6f6b73005f5f44415441000000000000000000004050eb0000000000b0000000000000004050eb00030000000000000000000000000000000000000000000000000000005f5f6c6c766d5f7072665f62697473005f5f44415441000000000000000000000080eb000000000000000000000000000080eb000e0000000000000000000000000000000000000000000000000000005f5f6c6c766d5f7072665f766e6473005f5f44415441000000000000000000000080eb000000000000000000000000000080eb00000000000000000000000000000000000000000000000000000000005f5f6c6c766d5f7072665f766e7300005f5f44415441000000000000000000000080eb000000000000000000000000000080eb00000000000000000000000000000000000000000000000000000000005f5f6c6c766d5f7072665f76746162005f5f44415441000000000000000000000080eb000000000000000000000000000080eb00000000000000000000000000000000000000000000000000000000005f5f62737300000000000000000000005f5f44415441000000000000000000000080eb000000000040fa04000000000000000000040000000000000000000000010000000000000000000000000000005f5f636f6d6d6f6e00000000000000005f5f4441544100000000000000000000407af000000000000841000000000000000000000300000000000000000000000100000000000000000000000000000019000000e80000005f5f4c4c564d5f434f5600000000000000c0f0000000000000400c00000000000080eb000000000000400c0000000000030000000300000002000000000000005f5f6c6c766d5f636f7666756e0000005f5f4c4c564d5f434f5600000000000000c0f00000000000e0430b00000000000080eb00030000000000000000000000000000000000000000000000000000005f5f6c6c766d5f636f766d61700000005f5f4c4c564d5f434f56000000000000e003fc000000000028fb000000000000e0c3f6000300000000000000000000000000000000000000000000000000000019000000480000005f5f4c494e4b454449540000000000000000fd0000000000004060010000000000c0f70000000000d01d600100000000010000000100000000000000000000000d00000038000000180000000100000000000000000000004072706174682f41737472616c446565702e64656275672e64796c6962000000340000801000000000c0f700d82102003300008010000000d8e1f90068d20c000200000018000000e8f4070141ff0600b83178019039de000b0000005000000000000000d77f0600d77f0600ba73000091f30600b00b0000000000000000000000000000000000000000000000000000f8e877012f120000000000000000000000000000000000001b00000018000000f9e6b0ce87c536668eb5ae09924f65da3200000020000000070000000000110000051a0001000000030000000000f3042a0000001000000000000000000000000c00000048000000180000000200000000000000000000004072706174682f4c6976654b69745765625254432e6672616d65776f726b2f4c6976654b6974576562525443000000000c00000050000000180000000200000000000000000000004072706174682f527573744c6976654b6974556e694646492e6672616d65776f726b2f527573744c6976654b6974556e69464649000000000c0000005800000018000000020000000405a21300002c012f53797374656d2f4c6962726172792f4672616d65776f726b732f466f756e646174696f6e2e6672616d65776f726b2f466f756e646174696f6e0000000000000c0000003800000018000000020000000000e400000001002f7573722f6c69622f6c69626f626a632e412e64796c696200000000000000000c00000038000000180000000200000000004c05000001002f7573722f6c69622f6c696253797374656d2e422e64796c69620000000000000c00000050000000180000000200000000000100000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f415646417564696f2e6672616d65776f726b2f415646417564696f00000c00000058000000180000000200000000000200000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f4156466f756e646174696f6e2e6672616d65776f726b2f4156466f756e646174696f6e00000c00000050000000180000000200000000000100000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f41564b69742e6672616d65776f726b2f41564b697400000000000000000c00000058000000180000000200000000000400000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f416363656c65726174652e6672616d65776f726b2f416363656c65726174650000000000000c00000070000000180000000200000005027002000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f41757468656e7469636174696f6e53657276696365732e6672616d65776f726b2f41757468656e7469636174696f6e53657276696365730000000000000c0000005000000018000000020000000000cf0b000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f436f6d62696e652e6672616d65776f726b2f436f6d62696e65000000000c0000006000000018000000020000000405a213000096002f53797374656d2f4c6962726172792f4672616d65776f726b732f436f7265466f756e646174696f6e2e6672616d65776f726b2f436f7265466f756e646174696f6e0000000000000c0000005800000018000000020000000105ad07000040002f53797374656d2f4c6962726172792f4672616d65776f726b732f436f726547726170686963732e6672616d65776f726b2f436f7265477261706869637300000c00000058000000180000000200000000000700000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f436f7265496d6167652e6672616d65776f726b2f436f7265496d61676500000000000000000c0000005800000018000000020000000108f80c000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f436f72654d656469612e6672616d65776f726b2f436f72654d6564696100000000000000000c0000005800000018000000020000000000ca04000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f436f726553657276696365732e6672616d65776f726b2f436f7265536572766963657300000c00000050000000180000000200000000046d03000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f436f7265546578742e6672616d65776f726b2f436f72655465787400000c00000060000000180000000200000000000100000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f436f72655472616e7366657261626c652e6672616d65776f726b2f436f72655472616e7366657261626c6500000c0000005800000018000000020000000004de02000201002f53797374656d2f4c6962726172792f4672616d65776f726b732f436f7265566964656f2e6672616d65776f726b2f436f7265566964656f00000000000000000c00000058000000180000000200000000000100000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f43727970746f4b69742e6672616d65776f726b2f43727970746f4b697400000000000000000c0000007000000018000000020000001a281700000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f446576656c6f706572546f6f6c73537570706f72742e6672616d65776f726b2f446576656c6f706572546f6f6c73537570706f727400000000000000000c00000050000000180000000200000000000100000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f496d616765494f2e6672616d65776f726b2f496d616765494f000000000c00000050000000180000000200000000027501000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f4d6574616c2e6672616d65776f726b2f4d6574616c00000000000000000c0000005000000018000000020000000007ad00000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f4d6574616c4b69742e6672616d65776f726b2f4d6574616c4b697400000c000000500000001800000002000000017ab416000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f4e6574776f726b2e6672616d65776f726b2f4e6574776f726b000000000c00000050000000180000000200000066005403000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f50686f746f7355492e6672616d65776f726b2f50686f746f73554900000c000000580000001800000002000000040eab04000201002f53797374656d2f4c6962726172792f4672616d65776f726b732f51756172747a436f72652e6672616d65776f726b2f51756172747a436f72650000000000000c00000058000000180000000200000000000100000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f5265706c61794b69742e6672616d65776f726b2f5265706c61794b697400000000000000000c0000005000000018000000020000004378cdf1000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f53656375726974792e6672616d65776f726b2f536563757269747900000c00000050000000180000000200000003050700000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f537769667455492e6672616d65776f726b2f53776966745549000000000c0000005000000018000000020000000505a623000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f55494b69742e6672616d65776f726b2f55494b697400000000000000000c00000050000000180000000200000004050900000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f566973696f6e2e6672616d65776f726b2f566973696f6e0000000000000c0000006800000018000000020000000000de00000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f5761746368436f6e6e65637469766974792e6672616d65776f726b2f5761746368436f6e6e656374697669747900000000000000000c00000050000000180000000200000005027002000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f5765624b69742e6672616d65776f726b2f5765624b69740000000000000c00000068000000180000000200000066005403000001002f53797374656d2f4c6962726172792f4672616d65776f726b732f5f50686f746f7355495f537769667455492e6672616d65776f726b2f5f50686f746f7355495f5377696674554900000000000000000c000000300000001800000002000000002b3408000001002f7573722f6c69622f6c6962632b2b2e312e64796c6962001800008048000000180000000200000001077409000001002f7573722f6c69622f73776966742f6c696273776966744156466f756e646174696f6e2e64796c6962000000000000001800008040000000180000000200000002644d00000001002f7573722f6c69622f73776966742f6c69627377696674416363656c65726174652e64796c6962000c00000040000000180000000200000000000000000000002f7573722f6c69622f73776966742f6c69627377696674436f72652e64796c6962000000000000000c00000040000000180000000200000000ff9b01000001002f7573722f6c69622f73776966742f6c69627377696674436f7265417564696f2e64796c696200000c00000048000000180000000200000000647800000001002f7573722f6c69622f73776966742f6c69627377696674436f7265466f756e646174696f6e2e64796c696200000000001800008040000000180000000200000000020200000001002f7573722f6c69622f73776966742f6c69627377696674436f7265496d6167652e64796c696200001800008048000000180000000200000000003500000001002f7573722f6c69622f73776966742f6c69627377696674436f72654c6f636174696f6e2e64796c6962000000000000001800008040000000180000000200000000000600000001002f7573722f6c69622f73776966742f6c69627377696674436f72654d4944492e64796c69620000000c0000004000000018000000020000000108f80c000001002f7573722f6c69622f73776966742f6c69627377696674436f72654d656469612e64796c696200000c00000040000000180000000200000008787901000001002f7573722f6c69622f73776966742f6c6962737769667444617277696e2e64796c696200000000000c00000040000000180000000200000020640606000001002f7573722f6c69622f73776966742f6c6962737769667444697370617463682e64796c69620000001800008040000000180000000200000000027501000001002f7573722f6c69622f73776966742f6c696273776966744d6574616c2e64796c69620000000000001800008040000000180000000200000000030100000001002f7573722f6c69622f73776966742f6c696273776966744d6574616c4b69742e64796c69620000001800008040000000180000000200000000000200000001002f7573722f6c69622f73776966742f6c696273776966744d6f64656c494f2e64796c6962000000001800008040000000180000000200000000000a00000001002f7573722f6c69622f73776966742f6c696273776966744f534c6f672e64796c69620000000000000c0000004000000018000000020000000007b703000001002f7573722f6c69622f73776966742f6c696273776966744f626a656374697665432e64796c6962000c00000048000000180000000200000000000000000000002f7573722f6c69622f73776966742f6c696273776966744f62736572766174696f6e2e64796c696200000000000000001800008040000000180000000200000000000500000001002f7573722f6c69622f73776966742f6c6962737769667451756172747a436f72652e64796c6962001800008040000000180000000200000000000100000001002f7573722f6c69622f73776966742f6c696273776966745370617469616c2e64796c6962000000001800008048000000180000000200000000000000000000002f7573722f6c69622f73776966742f6c6962737769667453796e6368726f6e697a6174696f6e2e64796c6962000000000c00000050000000180000000200000001056f03000001002f7573722f6c69622f73776966742f6c69627377696674556e69666f726d547970654964656e746966696572732e64796c696200000000001800008040000000180000000200000002788000000001002f7573722f6c69622f73776966742f6c696273776966745850432e64796c696200000000000000000c00000048000000180000000200000000000000000000002f7573722f6c69622f73776966742f6c696273776966745f436f6e63757272656e63792e64796c6962000000000000000c00000038000000180000000200000000003a04000001002f7573722f6c69622f73776966742f6c696273776966746f732e64796c6962001800008040000000180000000200000000001700000001002f7573722f6c69622f73776966742f6c6962737769667473696d642e64796c6962000000000000000c00000048000000180000000200000000000100000001002f7573722f6c69622f73776966742f6c69627377696674436f726547726170686963732e64796c6962000000000000000c00000040000000180000000200000005027002000001002f7573722f6c69622f73776966742f6c696273776966745765624b69742e64796c696200000000001c000080200000000c0000002f7573722f6c69622f73776966740000000000001c000080600000000c0000002f746d702f61737472616c2d3038382d636170747572652d696f732f4275696c642f50726f64756374732f44656275672d6970686f6e6573696d756c61746f722f5061636b6167654672616d65776f726b7300001c000080280000000c0000004065786563757461626c655f706174682f4672616d65776f726b7300260000001000000040b40601a84001002900000010000000e8f40701000000001d00000010000000506b560280720100",
}


def test_actual_retained_native_header_metadata_and_core_region_golden():
    for lane, raw in _ACTUAL_LOAD_COMMAND_PREFIXES.items():
        domain.ios_macho(bytes.fromhex(raw), lane)
    path = "apple-clients/AstralCore/Sources/AstralCore/API/CanvasExportPolicy.swift"
    value = _mapping((path,))
    value["data"][0]["functions"] = deepcopy(_ACTUAL_CORE_FUNCTIONS)
    assert _geometry(value, paths=(path,), lane="core")[path] == {
        "native_last_line": 69,
        "geometry_sha256": "16a1333d776af34fbd9ce55c87fe73188398f2d6a73e4168be3d7b2f8c50e6bb",
    }
