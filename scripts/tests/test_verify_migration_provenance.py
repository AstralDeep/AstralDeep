"""Adversarial tests for the feature-074 migration provenance verifier."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "verify_migration_provenance.py"
SCHEMA = REPOSITORY_ROOT / "contracts" / "extraction-provenance.schema.json"

SPEC = importlib.util.spec_from_file_location("verify_migration_provenance_074", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
provenance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = provenance
SPEC.loader.exec_module(provenance)

DEEP_URL = "https://github.com/AstralDeep/AstralDeep.git"
PLANE_URL = "https://github.com/AstralDeep/AstralPlane.git"
COMPONENT_PATH = "components/AstralPlane"
FEATURE_BRANCH = "codex/074-extract-data-plane"


def _run(arguments: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _git(repo: Path, *arguments: str) -> str:
    return _run(["git", "-C", os.fspath(repo), *arguments])


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")


def _write_json(path: Path, document: object) -> None:
    _write(path, json.dumps(document, sort_keys=True, indent=2) + "\n")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _init(repo: Path, branch: str, repository: str) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet", f"--initial-branch={branch}")
    _git(repo, "config", "user.name", "Migration Provenance Test")
    _git(repo, "config", "user.email", "provenance@example.invalid")
    _git(repo, "remote", "add", "origin", repository)


@dataclass
class Checkout:
    root: Path
    component: Path
    source_commit: str
    source_blob: str
    baseline: str
    target: str
    manifest_path: Path
    manifest: dict[str, Any]
    remotes: dict[str, Any]

    @property
    def component_spec(self) -> Any:
        return provenance.ComponentSpec(
            "astral-plane",
            COMPONENT_PATH,
            PLANE_URL,
            "provenance/extraction.json",
        )

    def probe(self, repository: str) -> Any:
        return self.remotes[repository]


@pytest.fixture
def checkout(tmp_path: Path) -> Checkout:
    root = tmp_path / "AstralDeep"
    _init(root, "main", DEEP_URL)
    source_file = root / "backend" / "shared" / "database.py"
    _write(source_file, "SCHEMA_REVISION = '066.001'\n")
    source_commit = _commit(root, "Create immutable extraction source")
    source_tree = _git(root, "show", "-s", "--format=%T", source_commit)
    source_blob = _git(root, "rev-parse", f"{source_commit}:backend/shared/database.py")
    source_bytes = len(source_file.read_bytes())

    component = root / COMPONENT_PATH
    _init(component, "master", PLANE_URL)
    _write(component / "README.md", "legacy plane\n")
    baseline = _commit(component, "Legacy Plane baseline")
    _git(component, "branch", "main", baseline)
    _git(component, "switch", "--quiet", "-c", FEATURE_BRANCH)

    manifest: dict[str, Any] = {
        "format": "astral.extraction-provenance/v1",
        "digestAlgorithm": "sha256",
        "source": {
            "repository": DEEP_URL,
            "commit": source_commit,
            "tree": source_tree,
        },
        "destination": {
            "repository": PLANE_URL,
            "branch": FEATURE_BRANCH,
            "legacyBaseline": {
                "sourceRef": "refs/heads/master",
                "commit": baseline,
                "observedAt": "2026-08-13T23:06:36.714643Z",
            },
        },
        "selectionRoots": ["backend/shared/database.py"],
        "entries": [
            {
                "sourcePath": "backend/shared/database.py",
                "destinationPath": "src/astralplane/_legacy_import/database.py",
                "mode": "100644",
                "blob": source_blob,
                "bytes": source_bytes,
            }
        ],
    }
    manifest["manifestSha256"] = provenance.compute_manifest_sha256(manifest)
    manifest_path = component / "provenance" / "extraction.json"
    _write_json(manifest_path, manifest)
    _write(component / "src" / "astralplane" / "__init__.py", "CONTRACT = 'v1'\n")
    _git(component, "add", "-A")
    _git(
        component,
        "commit",
        "--quiet",
        "-m",
        "Extract AstralPlane",
        "-m",
        f"Source-Repository: {DEEP_URL}\n"
        f"Source-Commit: {source_commit}\n"
        f"Source-Manifest-SHA256: {manifest['manifestSha256']}",
    )
    target = _git(component, "rev-parse", "HEAD")

    composition = {
        "format": "astral.composition/v1",
        "components": {
            "astral-plane": {
                "repository": PLANE_URL,
                "path": COMPONENT_PATH,
                "commit": target,
                "contract_version": "astralplane.contract/v1",
            }
        },
    }
    _write_json(root / "config" / "astral-composition.json", composition)
    _write(
        root / ".gitmodules",
        "[submodule \"AstralPlane\"]\n"
        f"\tpath = {COMPONENT_PATH}\n"
        f"\turl = {PLANE_URL}\n",
    )
    _git(
        root,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{target},{COMPONENT_PATH}",
    )

    remotes = {
        DEEP_URL: provenance.RemoteState(
            DEEP_URL,
            "refs/heads/main",
            {"HEAD": source_commit, "refs/heads/main": source_commit},
        ),
        PLANE_URL: provenance.RemoteState(
            PLANE_URL,
            "refs/heads/main",
            {
                "HEAD": baseline,
                "refs/heads/main": baseline,
                "refs/heads/master": baseline,
                f"refs/heads/{FEATURE_BRANCH}": target,
            },
        ),
    }
    return Checkout(
        root,
        component,
        source_commit,
        source_blob,
        baseline,
        target,
        manifest_path,
        manifest,
        remotes,
    )


def _verify(checkout: Checkout, probe: Any | None = None) -> Any:
    return provenance.verify_migration_provenance(
        checkout.root,
        schema_path=SCHEMA,
        components=(checkout.component_spec,),
        remote_probe=probe or checkout.probe,
    )


def _codes(report: Any, repository: str | None = None) -> set[str]:
    return {
        item.code
        for item in report.diagnostics
        if repository is None or item.repository == repository
    }


def _rewrite_manifest(checkout: Checkout, mutate: Any, *, redigest: bool) -> None:
    document = json.loads(checkout.manifest_path.read_text(encoding="utf-8"))
    mutate(document)
    if redigest:
        document["manifestSha256"] = provenance.compute_manifest_sha256(document)
    _write_json(checkout.manifest_path, document)
    checkout.target = _commit(checkout.component, "Mutate manifest fixture")
    _repin(checkout, checkout.target)
    checkout.remotes[PLANE_URL].refs[
        f"refs/heads/{FEATURE_BRANCH}"
    ] = checkout.target


def _commit_manifest_document(checkout: Checkout, document: dict[str, Any]) -> None:
    document["manifestSha256"] = provenance.compute_manifest_sha256(document)
    _write_json(checkout.manifest_path, document)
    checkout.target = _commit(checkout.component, "Replace manifest fixture")
    _repin(checkout, checkout.target)
    checkout.remotes[PLANE_URL].refs[
        f"refs/heads/{FEATURE_BRANCH}"
    ] = checkout.target


def _repin(checkout: Checkout, commit: str) -> None:
    composition_path = checkout.root / "config" / "astral-composition.json"
    composition = json.loads(composition_path.read_text(encoding="utf-8"))
    composition["components"]["astral-plane"]["commit"] = commit
    _write_json(composition_path, composition)
    _git(
        checkout.root,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{commit},{COMPONENT_PATH}",
    )


def _schema_manifest() -> dict[str, Any]:
    return {
        "format": "astral.extraction-provenance/v1",
        "digestAlgorithm": "sha256",
        "source": {
            "repository": DEEP_URL,
            "commit": "1" * 40,
            "tree": "2" * 40,
        },
        "destination": {
            "repository": PLANE_URL,
            "branch": FEATURE_BRANCH,
            "legacyBaseline": {
                "sourceRef": "refs/heads/master",
                "commit": "3" * 40,
                "observedAt": "2026-08-13T23:06:36Z",
            },
        },
        "selectionRoots": ["backend/shared/database.py"],
        "entries": [
            {
                "sourcePath": "backend/shared/database.py",
                "destinationPath": "src/astralplane/database.py",
                "mode": "100644",
                "blob": "4" * 40,
                "bytes": 1,
            }
        ],
        "manifestSha256": "5" * 64,
    }


def test_exact_canonical_descendant_and_source_blob_proof_passes(
    checkout: Checkout,
) -> None:
    report = _verify(checkout)

    assert report.ok, report.to_dict()
    assert report.verified_repositories == ("astraldeep", "astral-plane")


def test_wrong_case_url_is_not_accepted_as_canonical(checkout: Checkout) -> None:
    gitmodules = checkout.root / ".gitmodules"
    gitmodules.write_text(
        gitmodules.read_text(encoding="utf-8").replace("AstralPlane.git", "astralplane.git"),
        encoding="utf-8",
    )

    report = _verify(checkout)

    assert "E_CANONICAL_URL" in _codes(report, "astral-plane")


def test_redirect_only_remote_identity_fails_closed(checkout: Checkout) -> None:
    def redirect_only(repository: str) -> Any:
        if repository == PLANE_URL:
            raise provenance.RemoteProbeError(
                "fixture requires a redirect", redirect_only=True
            )
        return checkout.remotes[repository]

    report = _verify(checkout, redirect_only)

    assert "E_REDIRECT_DEPENDENCE" in _codes(report, "astral-plane")


def test_orphan_replacement_is_not_normal_ancestry(checkout: Checkout) -> None:
    tree = _git(checkout.component, "rev-parse", f"{checkout.target}^{{tree}}")
    orphan = _git(checkout.component, "commit-tree", tree, "-m", "orphan fixture")
    _repin(checkout, orphan)
    checkout.remotes[PLANE_URL].refs[f"refs/heads/{FEATURE_BRANCH}"] = orphan

    report = _verify(checkout)

    assert "E_NOT_DESCENDANT" in _codes(report, "astral-plane")


def test_remote_force_divergence_is_rejected(checkout: Checkout) -> None:
    checkout.remotes[PLANE_URL].refs[f"refs/heads/{FEATURE_BRANCH}"] = checkout.baseline

    report = _verify(checkout)

    assert "E_REMOTE_FEATURE_DIVERGED" in _codes(report, "astral-plane")


def test_missing_source_blob_object_is_rejected(checkout: Checkout) -> None:
    object_path = (
        checkout.root
        / ".git"
        / "objects"
        / checkout.source_blob[:2]
        / checkout.source_blob[2:]
    )
    assert object_path.is_file()
    object_path.chmod(stat.S_IWRITE)
    object_path.unlink()

    report = _verify(checkout)

    assert "E_SOURCE_BLOB_MISSING" in _codes(report, "astral-plane")


def test_manifest_tamper_is_rejected_before_git_claims(checkout: Checkout) -> None:
    _rewrite_manifest(
        checkout,
        lambda document: document["destination"].update(branch="codex/tampered"),
        redigest=False,
    )

    report = _verify(checkout)

    assert "E_MANIFEST_DIGEST" in _codes(report, "astral-plane")


def test_schema_tamper_is_rejected_even_with_a_recomputed_digest(
    checkout: Checkout,
) -> None:
    _rewrite_manifest(
        checkout,
        lambda document: document.update(unreviewed=True),
        redigest=True,
    )

    report = _verify(checkout)

    assert "E_MANIFEST_SCHEMA" in _codes(report, "astral-plane")


def test_changed_legacy_master_stops_verification(checkout: Checkout) -> None:
    checkout.remotes[PLANE_URL].refs["refs/heads/master"] = checkout.target

    report = _verify(checkout)

    assert "E_MASTER_CHANGED" in _codes(report, "astral-plane")


@pytest.mark.parametrize(
    "reference",
    (
        "refs/heads/archive/074-pre-migration",
        "refs/tags/074-migration-backup",
    ),
)
def test_feature_074_archive_refs_are_prohibited(
    checkout: Checkout, reference: str
) -> None:
    checkout.remotes[PLANE_URL].refs[reference] = checkout.baseline

    report = _verify(checkout)

    assert "E_ARCHIVE_REF" in _codes(report, "astral-plane")


def test_exact_gitlink_must_equal_composition_pin(checkout: Checkout) -> None:
    composition_path = checkout.root / "config" / "astral-composition.json"
    composition = json.loads(composition_path.read_text(encoding="utf-8"))
    composition["components"]["astral-plane"]["commit"] = checkout.baseline
    _write_json(composition_path, composition)

    report = _verify(checkout)

    assert "E_GITLINK" in _codes(report, "astral-plane")


def test_remote_parser_preserves_annotated_tag_peel_and_default() -> None:
    payload = (
        b"ref: refs/heads/main\tHEAD\n"
        + b"1" * 40
        + b"\tHEAD\n"
        + b"2" * 40
        + b"\trefs/heads/main\n"
        + b"3" * 40
        + b"\trefs/tags/v1.0.10\n"
        + b"4" * 40
        + b"\trefs/tags/v1.0.10^{}\n"
    )

    state = provenance._parse_ls_remote(DEEP_URL, payload)

    assert state.head == "refs/heads/main"
    assert state.refs["refs/tags/v1.0.10^{}"] == "4" * 40


def test_report_is_deterministic_and_does_not_expose_remote_errors(
    checkout: Checkout,
) -> None:
    def failing_probe(repository: str) -> Any:
        del repository
        raise RuntimeError("https://token:secret@example.invalid/private")

    first = _verify(checkout, failing_probe).to_dict()
    second = _verify(checkout, failing_probe).to_dict()

    assert first == second
    serialized = json.dumps(first, sort_keys=True)
    assert "secret" not in serialized
    assert "RuntimeError" in serialized


def test_fixture_mutations_do_not_change_committed_manifest(
    checkout: Checkout,
) -> None:
    original = copy.deepcopy(checkout.manifest)
    changed = copy.deepcopy(original)
    changed["entries"][0]["bytes"] += 1

    assert provenance.compute_manifest_sha256(changed) != original["manifestSha256"]


def test_source_and_remote_failure_diagnostics_are_specific(
    checkout: Checkout,
) -> None:
    original = copy.deepcopy(checkout.manifest)
    scenarios: tuple[tuple[Any, str], ...] = (
        (
            lambda value: value["source"].update(commit="f" * 40),
            "E_SOURCE_COMMIT_MISSING",
        ),
        (
            lambda value: value["source"].update(tree="f" * 40),
            "E_SOURCE_TREE_MISMATCH",
        ),
        (
            lambda value: (
                value["entries"][0].update(sourcePath="backend/missing.py"),
                value.update(selectionRoots=["backend/missing.py"]),
            ),
            "E_SOURCE_PATH_MISSING",
        ),
        (
            lambda value: value["entries"][0].update(bytes=999),
            "E_SOURCE_BLOB_MISMATCH",
        ),
        (
            lambda value: value["source"].update(
                repository="https://github.com/AstralDeep/LETS.git"
            ),
            "E_CANONICAL_URL",
        ),
        (
            lambda value: value["destination"].update(
                repository="https://github.com/AstralDeep/AstralProjection.git"
            ),
            "E_CANONICAL_URL",
        ),
    )
    for mutate, expected in scenarios:
        document = copy.deepcopy(original)
        mutate(document)
        _commit_manifest_document(checkout, document)
        assert expected in _codes(_verify(checkout), "astral-plane")

    _commit_manifest_document(checkout, copy.deepcopy(original))
    del checkout.remotes[PLANE_URL].refs["refs/heads/master"]
    del checkout.remotes[PLANE_URL].refs[f"refs/heads/{FEATURE_BRANCH}"]
    codes = _codes(_verify(checkout), "astral-plane")
    assert {"E_MASTER_MISSING", "E_REMOTE_FEATURE_MISSING"} <= codes


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.update(format="wrong"),
        lambda value: value.update(unreviewed=True),
        lambda value: value["source"].update(repository="https://example.invalid/repo"),
        lambda value: value["destination"]["legacyBaseline"].update(
            observedAt="not-a-date"
        ),
        lambda value: value.update(selectionRoots=[]),
        lambda value: value.update(selectionRoots=["a", "a"]),
        lambda value: value["entries"][0].update(bytes=-1),
        lambda value: value["entries"][0].update(sourcePath="../escape"),
        lambda value: value.pop("digestAlgorithm"),
    ),
)
def test_schema_validator_rejects_each_used_assertion_vocabulary(
    mutation: Any,
) -> None:
    document = _schema_manifest()
    mutation(document)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    assert provenance.validate_manifest_schema(document, schema)


def test_schema_contract_itself_must_be_canonical() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    manifest = _schema_manifest()

    with pytest.raises(provenance.VerificationError, match="root"):
        provenance.validate_manifest_schema(manifest, [])

    wrong_draft = copy.deepcopy(schema)
    wrong_draft["$schema"] = "draft-07"
    with pytest.raises(provenance.VerificationError, match="Draft 2020-12"):
        provenance.validate_manifest_schema(manifest, wrong_draft)

    wrong_id = copy.deepcopy(schema)
    wrong_id["$id"] = "https://example.invalid/schema"
    with pytest.raises(provenance.VerificationError, match="non-canonical"):
        provenance.validate_manifest_schema(manifest, wrong_id)


@pytest.mark.parametrize(
    ("instance", "schema", "message"),
    (
        ("x", {"enum": ["y"]}, "enumeration"),
        ("x", {"type": "integer"}, "wrong JSON type"),
        ("x", {"type": []}, "type"),
        ({}, {"type": "object", "required": [1]}, "required"),
        ({}, {"type": "object", "properties": [], "required": []}, "object schema"),
        ([1, 2], {"type": "array", "maxItems": 1}, "maxItems"),
        ("x", {"type": "string", "minLength": 2}, "minLength"),
        ("xx", {"type": "string", "maxLength": 1}, "maxLength"),
        ("x", {"type": "string", "pattern": "["}, "pattern"),
        (2, {"type": "integer", "maximum": 1}, "maximum"),
    ),
)
def test_schema_engine_fails_closed_on_malformed_or_violating_nodes(
    instance: object, schema: object, message: str
) -> None:
    root = schema if isinstance(schema, dict) else {}

    if message in {"type", "required", "object schema", "pattern"}:
        with pytest.raises(provenance.VerificationError, match=message):
            provenance._schema_errors(instance, schema, root)
    else:
        errors = provenance._schema_errors(instance, schema, root)
        assert any(message in error for error in errors)


def test_schema_reference_must_be_local_and_resolvable() -> None:
    with pytest.raises(provenance.VerificationError, match="non-local"):
        provenance._resolve_schema_ref({}, "https://example.invalid/schema")
    with pytest.raises(provenance.VerificationError, match="cannot be resolved"):
        provenance._resolve_schema_ref({}, "#/$defs/missing")


def test_strict_json_rejects_duplicate_nonfinite_and_non_utf8() -> None:
    with pytest.raises(provenance.VerificationError, match="duplicate"):
        provenance._parse_json_bytes(b'{"x":1,"x":2}', label="fixture")
    with pytest.raises(provenance.VerificationError, match="non-finite"):
        provenance._parse_json_bytes(b'{"x":NaN}', label="fixture")
    with pytest.raises(provenance.VerificationError, match="strict JSON"):
        provenance._parse_json_bytes(b"\xff", label="fixture")
    with pytest.raises(provenance.VerificationError, match="canonical JSON"):
        provenance._canonical_json_bytes({"not-json": {1, 2}})


def test_json_file_symlink_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    link = tmp_path / "link.json"
    _write(target, "{}\n")
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("test host cannot create a file symlink")

    with pytest.raises(provenance.VerificationError, match="symbolic link"):
        provenance._read_json(link)


def test_direct_remote_probe_distinguishes_redirect_from_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"ref: refs/heads/main\tHEAD\n" + b"1" * 40 + b"\trefs/heads/main\n"
    responses = [SimpleNamespace(returncode=1, stdout=b"", stderr=b"denied")]
    responses.append(SimpleNamespace(returncode=0, stdout=payload, stderr=b""))
    monkeypatch.setattr(provenance, "_run_git", lambda *args, **kwargs: responses.pop(0))

    with pytest.raises(provenance.RemoteProbeError) as redirected:
        provenance.probe_direct_remote(DEEP_URL)

    assert redirected.value.redirect_only is True

    responses.extend(
        [
            SimpleNamespace(returncode=1, stdout=b"", stderr=b"denied"),
            SimpleNamespace(returncode=1, stdout=b"", stderr=b"denied"),
        ]
    )
    with pytest.raises(provenance.RemoteProbeError) as unavailable:
        provenance.probe_direct_remote(DEEP_URL)
    assert unavailable.value.redirect_only is False


def test_direct_remote_probe_accepts_no_redirect_advertisement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"ref: refs/heads/main\tHEAD\n" + b"1" * 40 + b"\trefs/heads/main\n"
    monkeypatch.setattr(
        provenance,
        "_run_git",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=payload, stderr=b""
        ),
    )

    state = provenance.probe_direct_remote(DEEP_URL)

    assert state.repository == DEEP_URL


@pytest.mark.parametrize(
    "payload",
    (
        b"malformed\n",
        b"ref: refs/heads/main\tNOT_HEAD\n",
        b"not-a-sha\trefs/heads/main\n",
        b"1" * 40 + b"\trefs/heads/main\n",
    ),
)
def test_remote_parser_rejects_malformed_or_headless_advertisements(
    payload: bytes,
) -> None:
    with pytest.raises(provenance.RemoteProbeError):
        provenance._parse_ls_remote(DEEP_URL, payload)


def test_git_execution_and_decoding_failures_are_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_run(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise OSError("fixture")

    monkeypatch.setattr(provenance.subprocess, "run", fail_run)
    with pytest.raises(provenance.VerificationError, match="execute"):
        provenance._run_git(None, ("version",))

    monkeypatch.setattr(
        provenance,
        "_run_git",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=b"", stderr=b""),
    )
    with pytest.raises(provenance.VerificationError, match="unavailable"):
        provenance._git_bytes(tmp_path, "rev-parse", "HEAD")

    monkeypatch.setattr(provenance, "_git_bytes", lambda *args: b"\xff")
    with pytest.raises(provenance.VerificationError, match="UTF-8"):
        provenance._git_text(tmp_path, "rev-parse", "HEAD")


def test_gitmodule_parser_rejects_invalid_and_duplicate_paths(tmp_path: Path) -> None:
    path = tmp_path / ".gitmodules"
    _write(path, "[wrong \"section\"]\npath = a\nurl = b\n")
    with pytest.raises(provenance.VerificationError, match="invalid"):
        provenance._parse_gitmodules(path)

    _write(
        path,
        "[submodule \"one\"]\npath = duplicate\nurl = a\n"
        "[submodule \"two\"]\npath = duplicate\nurl = b\n",
    )
    with pytest.raises(provenance.VerificationError, match="duplicate"):
        provenance._parse_gitmodules(path)


def test_source_inventory_and_batch_results_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(provenance, "_git_bytes", lambda *args: b"malformed\0")
    with pytest.raises(provenance.VerificationError, match="malformed"):
        provenance._source_tree_inventory(tmp_path, "1" * 40)

    monkeypatch.setattr(
        provenance,
        "_run_git",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=b"1" * 40 + b" missing\n", stderr=b""
        ),
    )
    assert provenance._source_blob_sizes(tmp_path, ("1" * 40,)) == {
        "1" * 40: None
    }


def test_invalid_deep_root_returns_a_report_without_remote_access(
    tmp_path: Path,
) -> None:
    touched = False

    def probe(repository: str) -> Any:
        nonlocal touched
        touched = True
        raise AssertionError(repository)

    report = provenance.verify_migration_provenance(
        tmp_path / "missing", remote_probe=probe
    )

    assert _codes(report) == {"E_DEEP_REPOSITORY"}
    assert touched is False


def test_cli_writes_the_same_deterministic_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    report = provenance.VerificationReport(("astraldeep",), ())
    monkeypatch.setattr(provenance, "verify_migration_provenance", lambda *a, **k: report)
    output = tmp_path / "evidence" / "report.json"

    exit_code = provenance.main(["--deep-repo", os.fspath(tmp_path), "--output", os.fspath(output)])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == report.to_dict()
    assert json.loads(output.read_text(encoding="utf-8")) == report.to_dict()
