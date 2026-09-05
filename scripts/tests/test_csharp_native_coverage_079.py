"""Real Microsoft.CodeCoverage 18.9.0 producer and strict malformed denials."""

from __future__ import annotations

import copy
import gzip
import hashlib
import importlib.util
from pathlib import Path
import sys
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name("fixtures") / "csharp-native-079.cobertura.xml.gz"
SPEC = importlib.util.spec_from_file_location(
    "changed_coverage_native_079", ROOT / "scripts/check_changed_coverage.py"
)
assert SPEC is not None and SPEC.loader is not None
collector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


def _root() -> ET.Element:
    return ET.fromstring(gzip.decompress(FIXTURE.read_bytes()))


def _parse(root: ET.Element, tmp_path: Path):
    report = tmp_path / "native.cobertura.xml"
    ET.ElementTree(root).write(report, encoding="utf-8", xml_declaration=True)
    return collector.parse_coverage_report(report, "windows_csharp")


def _rate(node: ET.Element) -> None:
    lines = node.findall("./lines/line")
    node.set("line-rate", str(sum(int(v.get("hits", "0")) > 0 for v in lines) / len(lines)))


def _retotal(root: ET.Element) -> None:
    for package in root.findall("./packages/package"):
        lines = package.findall("./classes/class/lines/line")
        package.set("line-rate", str(sum(int(v.get("hits", "0")) > 0 for v in lines) / len(lines)))
    lines = root.findall("./packages/package/classes/class/lines/line")
    covered = sum(int(v.get("hits", "0")) > 0 for v in lines)
    root.set("lines-valid", str(len(lines)))
    root.set("lines-covered", str(covered))
    root.set("line-rate", str(covered / len(lines)))


def test_exact_native_recording_retains_hash_and_unique_line_semantics(tmp_path: Path) -> None:
    raw = gzip.decompress(FIXTURE.read_bytes())
    assert hashlib.sha256(raw).hexdigest() == (
        "bc22f9143115b99d1e26afa8f5ccec403bb3c848ab2f990eda1594c477ca5616"
    )
    report = tmp_path / "original.cobertura.xml"
    report.write_bytes(raw)
    data = collector.parse_coverage_report(report, "windows_csharp")
    assert len(data.executable) == 524
    assert len(data.covered) == 470
    assert all(path.endswith(".cs") for path in data.files)
    root = _root()
    assert int(root.get("lines-valid", "0")) == 531
    assert len(root.findall("./packages/package/classes/class/methods/method/lines/line")) == 531


@pytest.mark.parametrize(
    ("xpath", "attribute", "value"),
    [
        (".", "lines-valid", None),
        (".", "lines-covered", None),
        (".", "line-rate", None),
        (".", "lines-valid", "1000"),
        (".", "lines-covered", "1000"),
        (".", "line-rate", "NaN"),
        (".", "line-rate", "0.5"),
        ("./packages/package", "line-rate", "0.5"),
        (".//class", "filename", None),
        (".//class", "name", None),
        (".//class", "line-rate", None),
        (".//class", "line-rate", "0.5"),
        (".//method", "name", None),
        (".//method", "signature", None),
        (".//method", "line-rate", None),
        (".//method", "line-rate", "0.5"),
        (".//class/lines/line", "number", "0"),
        (".//class/lines/line", "hits", "-1"),
        (".//class/lines/line", "hits", None),
        (".//method/lines/line", "number", "0"),
        (".//method/lines/line", "hits", "invalid"),
    ],
)
def test_native_missing_or_inconsistent_counts_are_denied(
    tmp_path: Path, xpath: str, attribute: str, value: str | None
) -> None:
    root = _root()
    node = root.find(xpath)
    assert node is not None
    if value is None:
        node.attrib.pop(attribute)
    else:
        node.set(attribute, value)
    with pytest.raises(collector.CoveragePolicyError):
        _parse(root, tmp_path)


@pytest.mark.parametrize(
    "xpath",
    [".//class/lines", ".//method/lines", ".//class/methods", ".//class", ".//method"],
)
def test_duplicate_native_manifests_are_denied(tmp_path: Path, xpath: str) -> None:
    root = _root()
    original = root.find(xpath)
    assert original is not None
    parent = next(node for node in root.iter() if original in list(node))
    parent.append(copy.deepcopy(original))
    with pytest.raises(collector.CoveragePolicyError):
        _parse(root, tmp_path)


@pytest.mark.parametrize("xpath", [".//class/lines", ".//method/lines", ".//class/methods"])
def test_missing_native_line_witness_is_denied(tmp_path: Path, xpath: str) -> None:
    root = _root()
    original = root.find(xpath)
    assert original is not None
    parent = next(node for node in root.iter() if original in list(node))
    parent.remove(original)
    with pytest.raises(collector.CoveragePolicyError):
        _parse(root, tmp_path)


def test_consistent_rates_cannot_hide_method_class_disagreement(tmp_path: Path) -> None:
    root = _root()
    method = root.find(".//method")
    assert method is not None
    line = method.find("./lines/line")
    assert line is not None
    line.set("hits", "1")
    _rate(method)
    with pytest.raises(collector.CoveragePolicyError, match="method.*class|class.*method"):
        _parse(root, tmp_path)


def test_cross_class_conflicting_hits_are_denied_even_with_correct_totals(tmp_path: Path) -> None:
    root = _root()
    cls = next(node for node in root.iter("class") if "DisplayClass4_0" in node.get("name", ""))
    for method in cls.findall("./methods/method"):
        for line in method.findall("./lines/line"):
            line.set("hits", "1")
        _rate(method)
    for line in cls.findall("./lines/line"):
        line.set("hits", "1")
    _rate(cls)
    _retotal(root)
    with pytest.raises(collector.CoveragePolicyError, match="conflicting|contradictory"):
        _parse(root, tmp_path)


def test_cross_class_source_aliases_are_denied(tmp_path: Path) -> None:
    root = _root()
    cls = next(node for node in root.iter("class") if "DisplayClass4_0" in node.get("name", ""))
    cls.set("filename", "windows-client/asr-helper/Program.cs")
    with pytest.raises(collector.CoveragePolicyError, match="alias|manifest"):
        _parse(root, tmp_path)


def test_python_duplicate_rule_is_unchanged(tmp_path: Path) -> None:
    report = tmp_path / "native.cobertura.xml"
    report.write_bytes(gzip.decompress(FIXTURE.read_bytes()))
    with pytest.raises(collector.CoveragePolicyError, match="duplicate Cobertura"):
        collector.parse_coverage_report(report, "projection_python")


@pytest.mark.parametrize("xpath", ["./packages", "./packages/package/classes", ".//class/lines", ".//class/methods"])
def test_unknown_native_manifest_members_are_denied(tmp_path: Path, xpath: str) -> None:
    root = _root()
    parent = root.find(xpath)
    assert parent is not None
    parent.append(ET.Element("unknown"))
    with pytest.raises(collector.CoveragePolicyError):
        _parse(root, tmp_path)


@pytest.mark.parametrize("tag", ["class", "line"])
def test_unbound_native_observations_are_denied(tmp_path: Path, tag: str) -> None:
    root = _root()
    extra = root.find(f".//{tag}")
    assert extra is not None
    ET.SubElement(root, "unbound").append(copy.deepcopy(extra))
    with pytest.raises(collector.CoveragePolicyError, match="unbound"):
        _parse(root, tmp_path)


def test_repeated_method_observation_requires_identical_hits(tmp_path: Path) -> None:
    root = _root()
    container = root.find(".//class/methods")
    assert container is not None
    method = copy.deepcopy(container[0])
    method.set("name", "DifferentGeneratedMethod")
    container.append(method)
    assert len(_parse(root, tmp_path).executable) == 524
    line = method.find("./lines/line")
    assert line is not None
    line.set("hits", "1")
    _rate(method)
    with pytest.raises(collector.CoveragePolicyError, match="methods have conflicting"):
        _parse(root, tmp_path)


@pytest.mark.parametrize("xpath", [".//class/lines", ".//method/lines"])
def test_repeated_direct_line_is_denied(tmp_path: Path, xpath: str) -> None:
    root = _root()
    container = root.find(xpath)
    assert container is not None
    container.append(copy.deepcopy(container[0]))
    with pytest.raises(collector.CoveragePolicyError, match="duplicate"):
        _parse(root, tmp_path)
