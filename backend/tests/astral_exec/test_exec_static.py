"""Static guards for the astral_exec package: standard library imports only, no Docker, a committed fold table equal to the contract table and a vector copy equal to the contract vectors.
These run on every backend test pass, so a drift in the contract or an import slipping in fails here first.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import random
import subprocess
import sys

import astral_exec
from astral_exec import Policy, PolicyError, fold, fold_table
from exec_vectors import VECTOR_FILE

PACKAGE = pathlib.Path(astral_exec.__file__).resolve().parent
REPO = pathlib.Path(__file__).resolve().parents[3]
CONTRACTS = REPO / "specs" / "106-sign-in-free-clients" / "contracts"


def _modules():
    return sorted(PACKAGE.glob("*.py"))


def test_every_import_is_standard_library_or_the_package_itself():
    offenders = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                if root not in sys.stdlib_module_names and root != "astral_exec":
                    offenders.append((path.name, root))
    assert offenders == []


def test_the_package_imports_in_a_bare_interpreter_with_no_site_packages():
    code = "import sys; sys.path.insert(0, sys.argv[1]); import astral_exec; print(astral_exec.__name__)"
    done = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(PACKAGE.parent)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "astral_exec"


def test_no_product_module_names_docker_or_a_container_runtime():
    for path in _modules():
        if path.name == "fold_table.py":
            continue
        text = path.read_text(encoding="utf-8").lower()
        assert "docker" not in text and "podman" not in text, path.name


def test_the_fold_table_equals_the_contract_table():
    contract = json.loads((CONTRACTS / "reference" / "path_fold.json").read_text())
    assert fold_table.TABLE == contract
    assert fold_table.TABLE["unicode"] == "16.0.0"


def test_the_fold_never_consults_runtime_unicode_data():
    text = (PACKAGE / "fold.py").read_text(encoding="utf-8")
    assert "unicodedata" not in text
    assert "casefold" not in text and ".lower(" not in text


def _reference():
    spec = importlib.util.spec_from_file_location(
        "exec_policy_reference", CONTRACTS / "reference" / "exec_policy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fold_equals_the_reference_on_awkward_and_random_strings():
    reference = _reference()
    awkward = [
        "/Opt/Stra\u00dfe",
        "/opt/STRASSE",
        "/x/\u1f80",
        "/x/\u03b1\u0345\u0301",
        "/x/\u03b1\u0301\u0345",
        "/x/\ufb00",
        "/x/\uac01",
        "/x/\u1100\u1161\u11a8",
        "/x/\u00e9",
        "/x/e\u0301",
    ]
    rng = random.Random(1160)
    pool = [chr(int(k, 16)) for k in fold_table.TABLE["fold"]]
    pool += [chr(int(k, 16)) for k in fold_table.TABLE["decompose"]]
    pool += [chr(int(k, 16)) for k in fold_table.TABLE["ccc"]]
    pool += list("abcXYZ/.-_ 09")
    for _ in range(400):
        awkward.append(
            "/" + "".join(rng.choice(pool) for _ in range(rng.randint(1, 8)))
        )
    for text in awkward:
        assert fold.fold(text, "darwin") == reference._fold(text, "darwin"), text
        assert fold.fold(text, "linux") == text
        assert fold.assigned(text[-1]) == reference._assigned(text[-1])
    assert fold.fold("/x/\u00e9", "darwin") == fold.fold("/x/e\u0301", "darwin")
    assert fold.fold("/x/\uac01", "darwin") == fold.fold(
        "/x/\u1100\u1161\u11a8", "darwin"
    )


def test_validation_agrees_with_the_reference_on_mutated_policies():
    reference = _reference()
    rng = random.Random(106)
    base = json.loads(
        json.dumps(
            {
                "v": 1,
                "reads": ["/opt/a", "/opt/b"],
                "workspace": "/private/tmp/ws",
                "hosts": [{"host": "a.example.com", "port": 443}],
                "limits": {
                    "memory_bytes": 536870912,
                    "processes": 2,
                    "open_files": 256,
                    "file_bytes": 4096,
                },
                "time": {"wall_ms": 1000},
            }
        )
    )
    junk = [
        None,
        True,
        -1,
        0,
        1,
        2**63,
        "",
        "x",
        "/",
        "/a/../b",
        "/opt/B",
        "\u00df",
        [],
        {},
        99999999999,
    ]
    paths = [
        ["reads", 0],
        ["reads", 1],
        ["workspace"],
        ["hosts", 0, "host"],
        ["hosts", 0, "port"],
        ["limits", "memory_bytes"],
        ["limits", "processes"],
        ["time", "wall_ms"],
        ["v"],
        ["reads"],
        ["hosts"],
    ]
    for _ in range(600):
        doc = json.loads(json.dumps(base))
        for _ in range(rng.randint(1, 3)):
            path = rng.choice(paths)
            node = doc
            try:
                for key in path[:-1]:
                    node = node[key]
                if rng.random() < 0.15 and isinstance(node, dict) and path[-1] in node:
                    del node[path[-1]]
                else:
                    node[path[-1]] = rng.choice(junk)
            except (IndexError, KeyError, TypeError):
                continue
        if rng.random() < 0.1:
            doc[rng.choice(["listen", "extra"])] = 1
        for platform in ("linux", "darwin"):
            try:
                want = reference.parse(json.loads(json.dumps(doc)), platform)
                want_code = "ok"
            except reference.PolicyError as error:
                want_code = error.code
            try:
                Policy.parse(doc, platform)
                got = "ok"
            except PolicyError as error:
                got = error.code
            assert got == want_code, (doc, platform)
            if want_code == "ok":
                assert Policy.parse(doc, platform).digest() == reference.digest(want)


def test_the_vector_copy_equals_the_contract_vectors():
    assert (
        VECTOR_FILE.read_bytes()
        == (CONTRACTS / "vectors" / "exec-policy-v1.json").read_bytes()
    )


def test_each_module_opens_with_a_short_header_naming_no_feature():
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        header = ast.get_docstring(tree)
        assert header, path.name
        sentences = [s for s in header.replace("\n", " ").split(". ") if s.strip()]
        assert len(sentences) <= 3, path.name
        lowered = header.lower()
        assert (
            "spec 106" not in lowered
            and "task " not in lowered
            and "phase " not in lowered
        )
