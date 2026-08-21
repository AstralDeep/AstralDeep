"""Tests for the feature-074 tracked-source ownership verifier."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "scripts" / "verify_component_ownership.py"
SPEC = importlib.util.spec_from_file_location("verify_component_ownership", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
ownership_tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ownership_tool
SPEC.loader.exec_module(ownership_tool)


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _git(repo: Path, *arguments: str) -> str:
    completed = _run(["git", "-C", os.fspath(repo), *arguments])
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _git_bytes(repo: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", os.fspath(repo), *arguments],
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return completed.stdout


def _commit_files(repo: Path, files: dict[str, str], message: str) -> None:
    for relative_path, content in files.items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "--", *files)
    _git(repo, "commit", "-m", message)


def _init_repository(path: Path, files: dict[str, str]) -> Path:
    path.mkdir()
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.name", "Ownership Verifier Test")
    _git(path, "config", "user.email", "ownership-test@example.invalid")
    _commit_files(path, files, "Create ownership fixture")
    return path


def _map_document() -> dict[str, Any]:
    return {
        "format": "astral.component-ownership/v1",
        "repositories": {
            "deep": {
                "canonicalUrl": "https://github.com/AstralDeep/AstralDeep.git",
                "compositionPath": ".",
                "role": "composition host",
                "mutableRoots": ["adapters/", "legacy/"],
            },
            "projection": {
                "canonicalUrl": ("https://github.com/AstralDeep/AstralProjection.git"),
                "compositionPath": "components/AstralProjection",
                "role": "presentation owner",
                "mutableRoots": ["src/"],
            },
        },
        "ownershipDomains": [
            {
                "id": "host-adapters",
                "owner": "deep",
                "ownedPaths": ["adapters/**"],
                "forbiddenCopies": [],
                "scanExactDuplicates": True,
            },
            {
                "id": "presentation",
                "owner": "projection",
                "ownedPaths": ["src/render/**"],
                "forbiddenCopies": [
                    {"repository": "deep", "paths": ["legacy/render/**"]}
                ],
                "scanExactDuplicates": True,
            },
        ],
        "compatibilityAdapters": [
            {
                "repository": "deep",
                "path": "adapters/host.py",
                "upstreamOwner": "projection",
                "rule": "Semantic host callback only; never copied renderer code.",
            }
        ],
        "generatedCopies": [],
        "ignoredPathPolicy": "Only tracked Git blobs are ownership inputs.",
        "duplicatePolicy": "Every managed source has exactly one owner.",
    }


def _write_map(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def component_repositories(tmp_path: Path) -> dict[str, Path]:
    return {
        "deep": _init_repository(
            tmp_path / "deep",
            {
                ".gitignore": "*.ignored\n",
                "adapters/host.py": "def render_host(value):\n    return value\n",
            },
        ),
        "projection": _init_repository(
            tmp_path / "projection",
            {
                "src/render/schema.json": '{"version": 1}\n',
                "src/render/view.py": "def render(value):\n    return str(value)\n",
            },
        ),
    }


def _load_map(tmp_path: Path, document: dict[str, Any] | None = None) -> object:
    return ownership_tool.load_ownership_map(
        _write_map(tmp_path / "ownership.json", document or _map_document())
    )


def _audit(ownership_map: object, repositories: dict[str, Path]) -> dict[str, Any]:
    return ownership_tool.audit_component_ownership(
        ownership_map,
        repository_roots=repositories,
    )


def _codes(report: dict[str, Any]) -> set[str]:
    return {violation["code"] for violation in report["violations"]}


def _generated_copy(
    *,
    source_path: str = "src/render/schema.json",
    destination_path: str = "adapters/generated_schema.py",
    digest: str = "0" * 64,
) -> dict[str, str]:
    return {
        "sourceRepository": "projection",
        "sourcePath": source_path,
        "destinationRepository": "deep",
        "destinationPath": destination_path,
        "sha256": digest,
    }


def _tracked_digest(repo: Path, path: str) -> str:
    object_id = _git(repo, "rev-parse", f"HEAD:{path}")
    return hashlib.sha256(_git_bytes(repo, "cat-file", "blob", object_id)).hexdigest()


def test_owner_map_is_strict_and_references_existing_repositories(
    tmp_path: Path,
) -> None:
    parsed = _load_map(tmp_path)

    assert sorted(parsed.repositories) == ["deep", "projection"]
    assert [domain.identifier for domain in parsed.domains] == [
        "host-adapters",
        "presentation",
    ]

    malformed = _map_document()
    malformed["ownershipDomains"][0]["owner"] = "missing"
    with pytest.raises(
        ownership_tool.OwnershipError,
        match="names missing repository 'missing'",
    ):
        _load_map(tmp_path, malformed)


def test_owner_map_file_encoding_size_and_json_are_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ownership.json"
    invalid_payloads = [
        (b"", "size must be"),
        (b"\xff", "not UTF-8"),
        (b"{not-json", "invalid JSON"),
        (b'{"format": 1, "format": 2}', "duplicate key"),
        (b'{"value": NaN}', "non-finite number"),
        (b"x" * (ownership_tool.MAX_MAP_BYTES + 1), "size must be"),
    ]
    for payload, message in invalid_payloads:
        path.write_bytes(payload)
        with pytest.raises(ownership_tool.OwnershipError, match=message):
            ownership_tool.load_ownership_map(path)

    with pytest.raises(ownership_tool.OwnershipError, match="unreadable"):
        ownership_tool.load_ownership_map(tmp_path / "missing.json")


def test_owner_map_rejects_wrong_container_and_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "ownership.json"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ownership_tool.OwnershipError, match="must be an object"):
        ownership_tool.load_ownership_map(path)

    document = _map_document()
    document["unexpected"] = True
    with pytest.raises(ownership_tool.OwnershipError, match="keys differ"):
        _load_map(tmp_path, document)

    document = _map_document()
    document["repositories"]["deep"]["mutableRoots"] = "adapters/"
    with pytest.raises(ownership_tool.OwnershipError, match="must be an array"):
        _load_map(tmp_path, document)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (" role", "non-empty trimmed string"),
        ("e\u0301/path", "Unicode NFC"),
        ("/absolute", "normalized relative POSIX path"),
        ("a//b", "unsafe path segment"),
        ("a/../b", "unsafe path segment"),
        ("a/.git/b", "reserved path segment"),
        ("a/CON.txt", "reserved path segment"),
        ("a/trailing.", "non-portable path segment"),
        ("a/name:bad", "unsafe character"),
    ],
)
def test_path_and_string_validation_rejects_nonportable_values(
    value: str,
    message: str,
) -> None:
    with pytest.raises(ownership_tool.OwnershipError, match=message):
        if value == " role":
            ownership_tool._nonempty_string(value, field="fixture")
        else:
            ownership_tool._normalized_path(value, field="fixture")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("root", "must end in"),
        ("/root/**", "relative POSIX glob"),
        ("root/[ab]", "unsupported glob syntax"),
        ("root/../**", "unsafe path segment"),
    ],
)
def test_root_and_glob_validation_is_fail_closed(
    value: str,
    message: str,
) -> None:
    with pytest.raises(ownership_tool.OwnershipError, match=message):
        if value == "root":
            ownership_tool._normalized_root(value, field="fixture")
        else:
            ownership_tool._normalized_pattern(value, field="fixture")


def test_path_pattern_supports_bounded_star_and_recursive_globs() -> None:
    single = ownership_tool.PathPattern.compile("src/*.py", field="fixture")
    recursive = ownership_tool.PathPattern.compile("src/**/view.py", field="fixture")
    suffix = ownership_tool.PathPattern.compile("src/**", field="fixture")

    assert single.matches("src/view.py")
    assert not single.matches("src/nested/view.py")
    assert recursive.matches("src/view.py")
    assert recursive.matches("src/nested/view.py")
    assert suffix.matches("src/nested/view.py")


def test_owner_map_repository_invariants_are_enforced(tmp_path: Path) -> None:
    cases: list[tuple[dict[str, Any], str]] = []

    document = _map_document()
    document["format"] = "astral.component-ownership/v0"
    cases.append((document, "format must be"))

    document = _map_document()
    document["repositories"] = {}
    cases.append((document, "must not be empty"))

    document = _map_document()
    document["repositories"]["Deep"] = document["repositories"].pop("deep")
    cases.append((document, "invalid repository identifier"))

    document = _map_document()
    document["repositories"]["deep"]["canonicalUrl"] = (
        "git@github.com:AstralDeep/AstralDeep.git"
    )
    cases.append((document, "canonical AstralDeep HTTPS Git URL"))

    document = _map_document()
    document["repositories"]["deep"]["mutableRoots"] = []
    cases.append((document, "mutableRoots must not be empty"))

    document = _map_document()
    document["repositories"]["deep"]["mutableRoots"] = ["legacy/", "LEGACY/"]
    cases.append((document, "mutableRoots collide"))

    document = _map_document()
    document["repositories"]["projection"]["compositionPath"] = "."
    cases.append((document, "composition paths collide"))

    document = _map_document()
    document["repositories"]["projection"]["canonicalUrl"] = document["repositories"][
        "deep"
    ]["canonicalUrl"]
    cases.append((document, "canonical URLs collide"))

    document = _map_document()
    document["repositories"]["deep"]["compositionPath"] = "components/AstralDeep"
    cases.append((document, "exactly one repository"))

    for index, (document, message) in enumerate(cases):
        path = tmp_path / f"repository-invariant-{index}.json"
        with pytest.raises(ownership_tool.OwnershipError, match=message):
            ownership_tool.load_ownership_map(_write_map(path, document))


def test_owner_map_domain_invariants_are_enforced(tmp_path: Path) -> None:
    cases: list[tuple[dict[str, Any], str]] = []

    document = _map_document()
    document["ownershipDomains"] = []
    cases.append((document, "ownershipDomains must not be empty"))

    document = _map_document()
    document["ownershipDomains"][0]["id"] = "Not-Normal"
    cases.append((document, "id is not normalized"))

    document = _map_document()
    document["ownershipDomains"][0]["ownedPaths"] = []
    cases.append((document, "ownedPaths must not be empty"))

    document = _map_document()
    document["ownershipDomains"][0]["ownedPaths"] = ["adapters/**", "ADAPTERS/**"]
    cases.append((document, "ownedPaths collide"))

    document = _map_document()
    document["ownershipDomains"][0]["scanExactDuplicates"] = "yes"
    cases.append((document, "scanExactDuplicates must be a boolean"))

    document = _map_document()
    document["ownershipDomains"][1]["scanExactDuplicates"] = False
    cases.append((document, "cannot be false when forbiddenCopies are declared"))

    document = _map_document()
    document["ownershipDomains"][1]["forbiddenCopies"][0]["repository"] = "missing"
    cases.append((document, "names missing repository"))

    document = _map_document()
    document["ownershipDomains"][1]["forbiddenCopies"][0]["repository"] = "projection"
    cases.append((document, "cannot forbid paths in its owner"))

    document = _map_document()
    document["ownershipDomains"][1]["forbiddenCopies"][0]["paths"] = []
    cases.append((document, "paths must not be empty"))

    document = _map_document()
    document["ownershipDomains"][1]["forbiddenCopies"][0]["paths"] = [
        "legacy/**",
        "LEGACY/**",
    ]
    cases.append((document, "paths collide"))

    document = _map_document()
    document["ownershipDomains"][1]["forbiddenCopies"].append(
        {"repository": "deep", "paths": ["adapters/**"]}
    )
    cases.append((document, "repeats repositories"))

    document = _map_document()
    duplicate = copy.deepcopy(document["ownershipDomains"][0])
    document["ownershipDomains"].append(duplicate)
    cases.append((document, "domain IDs collide"))

    for index, (document, message) in enumerate(cases):
        path = tmp_path / f"domain-invariant-{index}.json"
        with pytest.raises(ownership_tool.OwnershipError, match=message):
            ownership_tool.load_ownership_map(_write_map(path, document))


def test_owner_map_adapter_and_generated_copy_invariants_are_enforced(
    tmp_path: Path,
) -> None:
    cases: list[tuple[dict[str, Any], str]] = []

    document = _map_document()
    document["compatibilityAdapters"][0]["upstreamOwner"] = "missing"
    cases.append((document, "references a missing repository"))

    document = _map_document()
    document["compatibilityAdapters"][0]["upstreamOwner"] = "deep"
    cases.append((document, "upstreamOwner must differ"))

    document = _map_document()
    document["compatibilityAdapters"].append(
        copy.deepcopy(document["compatibilityAdapters"][0])
    )
    cases.append((document, "adapter paths collide"))

    document = _map_document()
    generated = _generated_copy()
    generated["sourceRepository"] = "missing"
    document["generatedCopies"] = [generated]
    cases.append((document, "references a missing repository"))

    document = _map_document()
    generated = _generated_copy()
    generated["destinationRepository"] = "projection"
    document["generatedCopies"] = [generated]
    cases.append((document, "source and destination must differ"))

    document = _map_document()
    document["generatedCopies"] = [_generated_copy(digest="A" * 64)]
    cases.append((document, "lowercase 64-hex"))

    document = _map_document()
    document["generatedCopies"] = [_generated_copy(), _generated_copy()]
    cases.append((document, "generated-copy destinations collide"))

    for index, (document, message) in enumerate(cases):
        path = tmp_path / f"adapter-generated-invariant-{index}.json"
        with pytest.raises(ownership_tool.OwnershipError, match=message):
            ownership_tool.load_ownership_map(_write_map(path, document))


def test_audit_passes_one_owner_and_ignores_worktree_only_files(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    deep = component_repositories["deep"]
    ignored = deep / "legacy/render/view.ignored"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("def render(value):\n    return str(value)\n", encoding="utf-8")
    (deep / "legacy/render/untracked.py").write_text(
        "def render(value):\n    return str(value)\n", encoding="utf-8"
    )

    report = _audit(_load_map(tmp_path), component_repositories)

    assert report["ok"] is True
    assert report["managedPathCount"] == 3
    assert report["verifiedGeneratedCopyCount"] == 0
    assert report["violations"] == []
    assert [item["id"] for item in report["repositories"]] == [
        "deep",
        "projection",
    ]


def test_unmanaged_exact_duplicate_is_reported_from_tracked_blobs(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    source = (component_repositories["projection"] / "src/render/view.py").read_text(
        encoding="utf-8"
    )
    _commit_files(
        component_repositories["deep"],
        {"legacy/render/view.py": source},
        "Copy renderer without an ownership exception",
    )

    report = _audit(_load_map(tmp_path), component_repositories)

    assert report["ok"] is False
    assert {"forbidden_copy", "unmanaged_exact_duplicate"} <= _codes(report)
    duplicate = next(
        item
        for item in report["violations"]
        if item["code"] == "unmanaged_exact_duplicate"
    )
    assert duplicate == {
        "code": "unmanaged_exact_duplicate",
        "repository": "deep",
        "path": "legacy/render/view.py",
        "domain": "presentation",
        "detail": "tracked bytes duplicate owner path src/render/view.py",
    }


def test_zero_domain_mutable_path_is_reported_as_unowned(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    _commit_files(
        component_repositories["deep"],
        {"legacy/misc/rogue.py": "def rogue():\n    return 'unowned'\n"},
        "Add mutable source outside every ownership domain",
    )

    report = _audit(_load_map(tmp_path), component_repositories)

    assert report["ok"] is False
    assert report["violations"] == [
        {
            "code": "unowned_mutable_path",
            "repository": "deep",
            "path": "legacy/misc/rogue.py",
            "detail": "tracked mutable path matches no ownership domain",
        }
    ]


def test_renamed_owner_blob_outside_forbidden_globs_is_still_reported(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    source = (component_repositories["projection"] / "src/render/view.py").read_text(
        encoding="utf-8"
    )
    _commit_files(
        component_repositories["deep"],
        {"adapters/renamed_projection_code.py": source},
        "Hide copied Projection bytes beneath a Deep-owned name",
    )
    document = _map_document()
    document["ownershipDomains"][1]["forbiddenCopies"] = []
    ownership_map = _load_map(tmp_path, document)

    report = _audit(ownership_map, component_repositories)
    reordered = _audit(
        ownership_map,
        {
            "projection": component_repositories["projection"],
            "deep": component_repositories["deep"],
        },
    )

    assert report["ok"] is False
    assert ownership_tool.report_bytes(report) == ownership_tool.report_bytes(reordered)
    assert "forbidden_copy" not in _codes(report)
    duplicates = [
        violation
        for violation in report["violations"]
        if violation["code"] == "unmanaged_exact_duplicate"
    ]
    assert {
        "code": "unmanaged_exact_duplicate",
        "repository": "deep",
        "path": "adapters/renamed_projection_code.py",
        "domain": "presentation",
        "detail": "tracked bytes duplicate owner path src/render/view.py",
    } in duplicates


def test_same_repository_blob_copy_is_not_a_cross_owner_duplicate(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    source = (component_repositories["projection"] / "src/render/view.py").read_text(
        encoding="utf-8"
    )
    _commit_files(
        component_repositories["projection"],
        {"src/render/view_alias.py": source},
        "Add an owner-local source alias",
    )

    report = _audit(_load_map(tmp_path), component_repositories)

    assert report["ok"] is True
    assert "unmanaged_exact_duplicate" not in _codes(report)


def test_empty_cross_repository_blob_is_not_source_duplication(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    _commit_files(
        component_repositories["projection"],
        {"src/render/empty.py": ""},
        "Add an empty presentation package marker",
    )
    _commit_files(
        component_repositories["deep"],
        {"adapters/empty.py": ""},
        "Add an empty host package marker",
    )

    report = _audit(_load_map(tmp_path), component_repositories)

    assert report["ok"] is True
    assert "unmanaged_exact_duplicate" not in _codes(report)


def test_domain_can_exclude_repository_support_from_exact_duplicate_scan(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    source = (component_repositories["projection"] / "src/render/view.py").read_text(
        encoding="utf-8"
    )
    _commit_files(
        component_repositories["deep"],
        {"adapters/reused_fixture.py": source},
        "Reuse bytes in an independently owned support domain",
    )
    document = _map_document()
    document["ownershipDomains"][0]["scanExactDuplicates"] = False
    document["ownershipDomains"][1]["scanExactDuplicates"] = False
    document["ownershipDomains"][1]["forbiddenCopies"] = []

    report = _audit(_load_map(tmp_path, document), component_repositories)

    assert report["ok"] is True
    assert "unmanaged_exact_duplicate" not in _codes(report)


def test_cross_repository_copy_of_non_domain_blob_is_not_reported(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    content = "fixture metadata outside mutable ownership\n"
    _commit_files(
        component_repositories["projection"],
        {"src/misc/NOTICE.fixture": content},
        "Add non-domain repository metadata",
    )
    _commit_files(
        component_repositories["deep"],
        {"legacy/misc/notice_fixture.py": content},
        "Reuse non-domain fixture bytes",
    )

    report = _audit(_load_map(tmp_path), component_repositories)

    assert report["ok"] is False
    assert "unmanaged_exact_duplicate" not in _codes(report)
    assert [
        (violation["repository"], violation["path"])
        for violation in report["violations"]
    ] == [
        ("deep", "legacy/misc/notice_fixture.py"),
        ("projection", "src/misc/NOTICE.fixture"),
    ]


def test_generated_copy_requires_owner_source_and_matching_digest(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    source_path = component_repositories["projection"] / "src/render/schema.json"
    source = source_path.read_text(encoding="utf-8")
    _commit_files(
        component_repositories["deep"],
        {"adapters/generated_schema.py": source},
        "Add declared generated contract copy",
    )
    document = _map_document()
    source_object = _git(
        component_repositories["projection"],
        "rev-parse",
        "HEAD:src/render/schema.json",
    )
    document["generatedCopies"] = [
        {
            "sourceRepository": "projection",
            "sourcePath": "src/render/schema.json",
            "destinationRepository": "deep",
            "destinationPath": "adapters/generated_schema.py",
            "sha256": hashlib.sha256(
                _git_bytes(
                    component_repositories["projection"],
                    "cat-file",
                    "blob",
                    source_object,
                )
            ).hexdigest(),
        }
    ]

    report = _audit(_load_map(tmp_path, document), component_repositories)

    assert report["ok"] is True
    assert report["verifiedGeneratedCopyCount"] == 1
    assert report["violations"] == []

    _commit_files(
        component_repositories["deep"],
        {"adapters/generated_schema.py": '{"version": 2}\n'},
        "Tamper with generated contract copy",
    )
    tampered = _audit(_load_map(tmp_path, document), component_repositories)
    assert tampered["ok"] is False
    assert "generated_destination_digest_mismatch" in _codes(tampered)
    assert tampered["verifiedGeneratedCopyCount"] == 0


def test_generated_copy_missing_source_fails_closed(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    document = _map_document()
    document["generatedCopies"] = [
        _generated_copy(source_path="src/render/missing.json")
    ]

    report = _audit(_load_map(tmp_path, document), component_repositories)

    assert "generated_source_missing" in _codes(report)
    assert report["verifiedGeneratedCopyCount"] == 0


def test_generated_copy_source_must_have_exactly_one_owner(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    content = '{"fixture": true}\n'
    _commit_files(
        component_repositories["projection"],
        {"src/misc/source.json": content},
        "Add source outside an ownership domain",
    )
    _commit_files(
        component_repositories["deep"],
        {"adapters/generated_schema.py": content},
        "Add destination for unowned generated source",
    )
    document = _map_document()
    document["generatedCopies"] = [
        _generated_copy(
            source_path="src/misc/source.json",
            digest=_tracked_digest(
                component_repositories["projection"], "src/misc/source.json"
            ),
        )
    ]

    report = _audit(_load_map(tmp_path, document), component_repositories)

    assert "generated_source_not_owned" in _codes(report)
    assert report["verifiedGeneratedCopyCount"] == 0


def test_generated_copy_destination_must_exist(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    document = _map_document()
    document["generatedCopies"] = [
        _generated_copy(
            digest=_tracked_digest(
                component_repositories["projection"], "src/render/schema.json"
            )
        )
    ]

    report = _audit(_load_map(tmp_path, document), component_repositories)

    assert "generated_destination_missing" in _codes(report)
    assert report["verifiedGeneratedCopyCount"] == 0


def test_generated_copy_rejects_nonregular_git_modes(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    projection = component_repositories["projection"]
    deep = component_repositories["deep"]
    content = (projection / "src/render/schema.json").read_text(encoding="utf-8")
    destination = "adapters/generated_schema.py"
    _commit_files(deep, {destination: content}, "Add generated schema")
    destination_object = _git(deep, "rev-parse", f"HEAD:{destination}")
    _git(
        deep,
        "update-index",
        "--cacheinfo",
        f"120000,{destination_object},{destination}",
    )
    _git(deep, "commit", "-m", "Record generated schema as a symbolic link")
    document = _map_document()
    document["generatedCopies"] = [
        _generated_copy(digest=_tracked_digest(projection, "src/render/schema.json"))
    ]

    report = _audit(_load_map(tmp_path, document), component_repositories)

    assert "generated_copy_not_regular" in _codes(report)


def test_generated_copy_requires_identical_git_modes(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    projection = component_repositories["projection"]
    deep = component_repositories["deep"]
    content = (projection / "src/render/schema.json").read_text(encoding="utf-8")
    destination = "adapters/generated_schema.py"
    _commit_files(deep, {destination: content}, "Add generated schema")
    _git(deep, "update-index", "--chmod=+x", "--", destination)
    _git(deep, "commit", "-m", "Make generated schema executable")
    document = _map_document()
    document["generatedCopies"] = [
        _generated_copy(digest=_tracked_digest(projection, "src/render/schema.json"))
    ]

    report = _audit(_load_map(tmp_path, document), component_repositories)

    assert "generated_mode_mismatch" in _codes(report)


def test_generated_copy_requires_declared_source_digest(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    projection = component_repositories["projection"]
    content = (projection / "src/render/schema.json").read_text(encoding="utf-8")
    _commit_files(
        component_repositories["deep"],
        {"adapters/generated_schema.py": content},
        "Add generated schema with an incorrect declaration",
    )
    document = _map_document()
    document["generatedCopies"] = [_generated_copy(digest="0" * 64)]

    report = _audit(_load_map(tmp_path, document), component_repositories)

    assert "generated_source_digest_mismatch" in _codes(report)


def test_valid_generated_copy_can_be_outside_owned_paths_and_forbidden(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    projection = component_repositories["projection"]
    content = (projection / "src/render/schema.json").read_text(encoding="utf-8")
    destination = "legacy/render/generated-schema.json"
    _commit_files(
        component_repositories["deep"],
        {destination: content},
        "Add immutable generated renderer contract",
    )
    document = _map_document()
    document["generatedCopies"] = [
        _generated_copy(
            destination_path=destination,
            digest=_tracked_digest(projection, "src/render/schema.json"),
        )
    ]

    report = _audit(_load_map(tmp_path, document), component_repositories)

    assert report["ok"] is True
    assert report["verifiedGeneratedCopyCount"] == 1
    assert report["violations"] == []


def test_valid_generated_copy_can_match_a_nonowner_domain_without_duplication(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    projection = component_repositories["projection"]
    content = (projection / "src/render/schema.json").read_text(encoding="utf-8")
    destination = "legacy/render/generated-schema.json"
    _commit_files(
        component_repositories["deep"],
        {destination: content},
        "Add a declared derivative under the legacy renderer path",
    )
    document = _map_document()
    document["ownershipDomains"][1]["ownedPaths"].append("legacy/render/**")
    document["generatedCopies"] = [
        _generated_copy(
            destination_path=destination,
            digest=_tracked_digest(projection, "src/render/schema.json"),
        )
    ]

    report = _audit(_load_map(tmp_path, document), component_repositories)

    assert report["ok"] is True
    assert report["verifiedGeneratedCopyCount"] == 1
    assert report["violations"] == []


def test_owned_paths_are_owner_relative_and_forbidden_copy_is_reported(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    _commit_files(
        component_repositories["deep"],
        {"legacy/render/foreign.py": "FOREIGN = True\n"},
        "Add uniquely named renderer source in the host",
    )
    document = _map_document()
    document["ownershipDomains"][1]["ownedPaths"].append("legacy/render/**")

    report = _audit(_load_map(tmp_path, document), component_repositories)

    assert {"unowned_mutable_path", "forbidden_copy"} <= _codes(report)
    assert "wrong_repository_owner" not in _codes(report)


def test_nonmutable_exact_duplicate_is_not_a_source_owner_violation(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    source = (component_repositories["projection"] / "src/render/view.py").read_text(
        encoding="utf-8"
    )
    _commit_files(
        component_repositories["deep"],
        {"NOTICE.fixture": source},
        "Add immutable fixture outside mutable roots",
    )

    report = _audit(_load_map(tmp_path), component_repositories)

    assert report["ok"] is True
    assert "unmanaged_exact_duplicate" not in _codes(report)


def test_compatibility_adapter_must_exist_and_tree_form_is_supported(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    missing_document = _map_document()
    missing_document["compatibilityAdapters"][0]["path"] = "adapters/missing.py"
    missing = _audit(_load_map(tmp_path, missing_document), component_repositories)
    assert "compatibility_adapter_missing" in _codes(missing)

    tree_document = _map_document()
    tree_document["compatibilityAdapters"][0]["path"] = "adapters/"
    tree = _audit(_load_map(tmp_path, tree_document), component_repositories)
    assert tree["ok"] is True


def test_missing_owner_fails_even_when_no_forbidden_copy_exists(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    projection = component_repositories["projection"]
    _git(projection, "rm", "-r", "--", "src/render")
    _git(projection, "commit", "-m", "Remove presentation owner source")

    report = _audit(_load_map(tmp_path), component_repositories)

    assert report["ok"] is False
    assert report["violations"] == [
        {
            "code": "missing_owner",
            "repository": "projection",
            "path": "src/render/**",
            "domain": "presentation",
            "detail": "owner repository has no tracked path in this domain",
        }
    ]


def test_path_matching_multiple_domains_has_no_ambiguous_owner(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    document = _map_document()
    overlapping = copy.deepcopy(document["ownershipDomains"][1])
    overlapping["id"] = "presentation-shadow"
    document["ownershipDomains"].append(overlapping)

    report = _audit(_load_map(tmp_path, document), component_repositories)

    assert report["ok"] is False
    assert "ambiguous_owner" in _codes(report)
    assert any(
        violation["path"] == "src/render/view.py"
        for violation in report["violations"]
        if violation["code"] == "ambiguous_owner"
    )


def test_output_is_deterministic_and_nested_repository_roots_are_refused(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    ownership_map = _load_map(tmp_path)
    first = _audit(ownership_map, component_repositories)
    second = _audit(
        ownership_map,
        {
            "projection": component_repositories["projection"],
            "deep": component_repositories["deep"],
        },
    )

    assert ownership_tool.report_bytes(first) == ownership_tool.report_bytes(second)
    with pytest.raises(ownership_tool.OwnershipError, match="exact-root mismatch"):
        ownership_tool.audit_component_ownership(
            ownership_map,
            repository_roots={
                "deep": component_repositories["deep"] / "adapters",
                "projection": component_repositories["projection"],
            },
        )


def test_audit_rejects_repository_and_revision_key_drift(
    tmp_path: Path,
    component_repositories: dict[str, Path],
) -> None:
    ownership_map = _load_map(tmp_path)
    with pytest.raises(ownership_tool.OwnershipError, match="roots differ"):
        ownership_tool.audit_component_ownership(
            ownership_map,
            repository_roots={"deep": component_repositories["deep"]},
        )
    with pytest.raises(ownership_tool.OwnershipError, match="unknown repositories"):
        ownership_tool.audit_component_ownership(
            ownership_map,
            repository_roots=component_repositories,
            revisions={"missing": "HEAD"},
        )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (["deep"], "REPOSITORY=VALUE syntax"),
        (["Deep=HEAD"], "invalid assignment"),
        (["deep="], "invalid assignment"),
        (["deep=HEAD", "deep=main"], "repeats repository"),
    ],
)
def test_assignment_parser_rejects_ambiguous_values(
    values: list[str],
    message: str,
) -> None:
    with pytest.raises(ownership_tool.OwnershipError, match=message):
        ownership_tool._parse_assignments(values, field="revision")


def test_default_roots_are_derived_from_composition_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ownership_map = _load_map(tmp_path)
    root = tmp_path / "composition"
    root.mkdir()
    monkeypatch.setattr(ownership_tool, "exact_git_root", lambda _value: root)

    roots = ownership_tool._default_roots(ownership_map, root)

    assert roots == {
        "deep": root,
        "projection": root / "components" / "AstralProjection",
    }


def test_cli_returns_machine_readable_success_violation_and_error(
    tmp_path: Path,
    component_repositories: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    map_path = _write_map(tmp_path / "ownership.json", _map_document())
    roots = [
        item
        for identifier, root in component_repositories.items()
        for item in ("--repository-root", f"{identifier}={root}")
    ]
    common = ["--ownership-map", str(map_path), *roots]

    assert ownership_tool.main([*common, "--revision", "deep=HEAD"]) == 0
    success = json.loads(capsys.readouterr().out)
    assert success["ok"] is True

    _commit_files(
        component_repositories["deep"],
        {"legacy/misc/unowned.py": "UNOWNED = True\n"},
        "Add unowned source for CLI failure",
    )
    assert ownership_tool.main(common) == 1
    violation = json.loads(capsys.readouterr().out)
    assert violation["ok"] is False
    assert "unowned_mutable_path" in _codes(violation)

    assert ownership_tool.main([*common, "--revision", "bad-assignment"]) == 2
    failure = capsys.readouterr()
    assert failure.out == ""
    assert json.loads(failure.err)["error"].startswith("revision must use")


def test_cli_uses_composition_derived_roots_by_default(
    tmp_path: Path,
    component_repositories: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    map_path = _write_map(tmp_path / "ownership.json", _map_document())
    monkeypatch.setattr(
        ownership_tool,
        "_default_roots",
        lambda _ownership, _root: component_repositories,
    )

    result = ownership_tool.main(
        [
            "--ownership-map",
            str(map_path),
            "--composition-root",
            str(tmp_path),
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_script_entrypoint_propagates_main_exit_code(
    tmp_path: Path,
    component_repositories: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    map_path = _write_map(tmp_path / "ownership.json", _map_document())
    arguments = [
        os.fspath(MODULE_PATH),
        "--ownership-map",
        str(map_path),
        *[
            item
            for identifier, root in component_repositories.items()
            for item in ("--repository-root", f"{identifier}={root}")
        ],
    ]
    monkeypatch.setattr(sys, "argv", arguments)

    with pytest.raises(SystemExit) as exited:
        runpy.run_path(os.fspath(MODULE_PATH), run_name="__main__")

    assert exited.value.code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_git_wrapper_translates_process_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_os_error(*_args: object, **_kwargs: object) -> object:
        raise OSError("git executable unavailable")

    monkeypatch.setattr(ownership_tool.subprocess, "run", raise_os_error)
    with pytest.raises(ownership_tool.OwnershipError, match="could not execute git"):
        ownership_tool._git(tmp_path, ("status",))

    def fail_without_stderr(*_args: object, **_kwargs: object) -> object:
        return subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"")

    monkeypatch.setattr(ownership_tool.subprocess, "run", fail_without_stderr)
    with pytest.raises(ownership_tool.OwnershipError, match="no error output"):
        ownership_tool._git(tmp_path, ("status",))


def test_repository_root_inspection_errors_are_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ownership_tool.OwnershipError, match="cannot be inspected"):
        ownership_tool._is_reparse_point(missing)

    with monkeypatch.context() as patch:
        patch.setattr(ownership_tool, "_is_reparse_point", lambda _path: True)
        with pytest.raises(ownership_tool.OwnershipError, match="reparse point"):
            ownership_tool.exact_git_root(tmp_path)

    with monkeypatch.context() as patch:
        patch.setattr(ownership_tool, "_is_reparse_point", lambda _path: False)
        with pytest.raises(ownership_tool.OwnershipError, match="does not resolve"):
            ownership_tool.exact_git_root(missing)

    file_path = tmp_path / "file"
    file_path.write_text("not a repository\n", encoding="utf-8")
    with pytest.raises(ownership_tool.OwnershipError, match="not a directory"):
        ownership_tool.exact_git_root(file_path)

    repository = tmp_path / "repository"
    repository.mkdir()
    nonexistent_root = tmp_path / "vanished-git-root"
    with monkeypatch.context() as patch:
        patch.setattr(
            ownership_tool,
            "_git",
            lambda _root, _arguments: os.fsencode(nonexistent_root) + b"\n",
        )
        with pytest.raises(ownership_tool.OwnershipError, match="does not resolve"):
            ownership_tool.exact_git_root(repository)


def test_inventory_rejects_invalid_object_identity_and_tree_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    responses = {
        "commit": b"a" * 40 + b"\n",
        "tree": b"b" * 40 + b"\n",
        "records": b"",
    }

    def fake_git(_root: Path, arguments: tuple[str, ...]) -> bytes:
        if arguments[0] == "ls-tree":
            return responses["records"]
        if arguments[-1].endswith("^{tree}"):
            return responses["tree"]
        return responses["commit"]

    monkeypatch.setattr(ownership_tool, "exact_git_root", lambda _root: repository)
    monkeypatch.setattr(ownership_tool, "_git", fake_git)

    responses["commit"] = b"not-an-object\n"
    with pytest.raises(ownership_tool.OwnershipError, match="invalid commit ID"):
        ownership_tool.inventory_repository("fixture", repository)

    responses["commit"] = b"a" * 40 + b"\n"
    responses["tree"] = b"not-a-tree\n"
    with pytest.raises(ownership_tool.OwnershipError, match="invalid tree ID"):
        ownership_tool.inventory_repository("fixture", repository)

    responses["tree"] = b"b" * 40 + b"\n"
    responses["records"] = b"invalid-record\0"
    with pytest.raises(ownership_tool.OwnershipError, match="invalid Git tree record"):
        ownership_tool.inventory_repository("fixture", repository)

    record = b"100644 blob " + b"c" * 40 + b" 1\tfile.py\0"
    responses["records"] = record + record
    with pytest.raises(ownership_tool.OwnershipError, match="repeats tracked path"):
        ownership_tool.inventory_repository("fixture", repository)

    responses["records"] = b"100644 blob " + b"z" * 40 + b" 1\tfile.py\0"
    with pytest.raises(ownership_tool.OwnershipError, match="invalid object ID"):
        ownership_tool.inventory_repository("fixture", repository)

    responses["records"] = b"160000 commit " + b"c" * 40 + b" -\tsubmodule\0"
    inventory = ownership_tool.inventory_repository("fixture", repository)
    assert inventory.blobs == {}
