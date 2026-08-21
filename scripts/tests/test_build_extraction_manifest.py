"""Tests for deterministic feature-074 extraction provenance."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "scripts" / "migration" / "build_extraction_manifest.py"
SPEC = importlib.util.spec_from_file_location("build_extraction_manifest", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
manifest_tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manifest_tool
SPEC.loader.exec_module(manifest_tool)

SOURCE_REPOSITORY = "https://github.com/AstralDeep/AstralDeep.git"
DESTINATION_REPOSITORY = "https://github.com/AstralDeep/AstralProjection.git"
OBSERVED_AT = "2026-08-13T23:06:36.714643Z"


def _run(
    arguments: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
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


@pytest.fixture
def source_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Extraction Manifest Test")
    _git(repo, "config", "user.email", "manifest-test@example.invalid")

    files = {
        ".gitignore": "*.cache\n",
        "backend/rote/adapter.py": "#!/usr/bin/env python3\nprint('adapter')\n",
        "backend/webrender/alpha.txt": "alpha\n",
        "backend/webrender/zeta.txt": "zeta\n",
        "backend/shared/ui_protocol.json": '{"version": 1}\n',
    }
    for relative_path, content in files.items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "--", *files)
    _git(repo, "update-index", "--chmod=+x", "backend/rote/adapter.py")
    _git(repo, "commit", "-m", "Create extraction source fixture")

    # Both paths are beneath a selected root, but neither is in the commit.
    (repo / "backend/webrender/generated.cache").write_text(
        "ignored\n", encoding="utf-8"
    )
    (repo / "backend/webrender/untracked.txt").write_text(
        "untracked\n", encoding="utf-8"
    )
    return repo, _git(repo, "rev-parse", "HEAD")


def _baseline() -> object:
    return manifest_tool.LegacyBaseline(
        repository=DESTINATION_REPOSITORY,
        source_ref="refs/heads/master",
        commit="1" * 40,
        observed_at=OBSERVED_AT,
    )


def _build(
    repo: Path,
    revision: str,
    selections: list[object],
) -> dict[str, object]:
    return manifest_tool.build_extraction_manifest(
        source_repo=repo,
        source_repository=SOURCE_REPOSITORY,
        source_revision=revision,
        expected_source_revision=revision,
        destination_repository=DESTINATION_REPOSITORY,
        legacy_baseline=_baseline(),
        selected_branch="codex/074-extract-projection",
        selections=selections,
    )


def _cli_arguments(repo: Path, revision: str, *, output: str = "-") -> list[str]:
    return [
        "--source-repo",
        os.fspath(repo),
        "--source-repository",
        SOURCE_REPOSITORY,
        "--source-revision",
        revision,
        "--expected-source-revision",
        revision,
        "--destination-repository",
        DESTINATION_REPOSITORY,
        "--legacy-source-ref",
        "refs/heads/master",
        "--legacy-commit",
        "1" * 40,
        "--legacy-observed-at",
        OBSERVED_AT,
        "--selected-branch",
        "codex/074-extract-projection",
        "--select",
        "backend/webrender",
        "backend/webrender",
        "--output",
        output,
    ]


def test_canonical_ordering_is_independent_of_selection_order(
    source_repo: tuple[Path, str],
) -> None:
    repo, revision = source_repo
    selections = [
        manifest_tool.Selection("backend/webrender", "backend/webrender"),
        manifest_tool.Selection("backend/rote/adapter.py", "backend/rote/adapter.py"),
    ]

    first = _build(repo, revision, selections)
    second = _build(repo, revision, list(reversed(selections)))

    assert first == second
    assert first["selectionRoots"] == [
        "backend/rote/adapter.py",
        "backend/webrender",
    ]
    assert [entry["sourcePath"] for entry in first["entries"]] == [
        "backend/rote/adapter.py",
        "backend/webrender/alpha.txt",
        "backend/webrender/zeta.txt",
    ]
    assert manifest_tool.manifest_document_bytes(first) == (
        json.dumps(first, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def test_entries_capture_git_tree_blob_ids_modes_sizes_and_tree(
    source_repo: tuple[Path, str],
) -> None:
    repo, revision = source_repo
    manifest = _build(
        repo,
        revision,
        [manifest_tool.Selection("backend", "extracted")],
    )
    entries = {entry["sourcePath"]: entry for entry in manifest["entries"]}

    executable = entries["backend/rote/adapter.py"]
    assert executable == {
        "sourcePath": "backend/rote/adapter.py",
        "destinationPath": "extracted/rote/adapter.py",
        "mode": "100755",
        "blob": _git(repo, "rev-parse", f"{revision}:backend/rote/adapter.py"),
        "bytes": int(
            _git(repo, "cat-file", "-s", f"{revision}:backend/rote/adapter.py")
        ),
    }
    assert manifest["source"] == {
        "repository": SOURCE_REPOSITORY,
        "commit": revision,
        "tree": _git(repo, "rev-parse", f"{revision}^{{tree}}"),
    }


def test_manifest_digest_is_stable_and_excludes_only_itself(
    source_repo: tuple[Path, str],
) -> None:
    repo, revision = source_repo
    manifest = _build(
        repo,
        revision,
        [manifest_tool.Selection("backend/webrender", "backend/webrender")],
    )

    digest = manifest["manifestSha256"]
    assert digest == manifest_tool.compute_manifest_sha256(manifest)
    with_different_digest = copy.deepcopy(manifest)
    with_different_digest["manifestSha256"] = "f" * 64
    assert manifest_tool.compute_manifest_sha256(with_different_digest) == digest

    changed_provenance = copy.deepcopy(manifest)
    changed_provenance["destination"]["branch"] = "codex/changed"
    assert manifest_tool.compute_manifest_sha256(changed_provenance) != digest


def test_ignored_and_untracked_worktree_files_are_omitted(
    source_repo: tuple[Path, str],
) -> None:
    repo, revision = source_repo
    manifest = _build(
        repo,
        revision,
        [manifest_tool.Selection("backend/webrender", "backend/webrender")],
    )
    source_paths = {entry["sourcePath"] for entry in manifest["entries"]}

    assert "backend/webrender/generated.cache" not in source_paths
    assert "backend/webrender/untracked.txt" not in source_paths
    assert source_paths == {
        "backend/webrender/alpha.txt",
        "backend/webrender/zeta.txt",
    }


def test_source_revision_mismatch_refuses_before_repository_git_access(
    tmp_path: Path,
) -> None:
    nonexistent_repo = tmp_path / "does-not-exist"

    with pytest.raises(manifest_tool.ManifestError, match="source revision mismatch"):
        manifest_tool.build_extraction_manifest(
            source_repo=nonexistent_repo,
            source_repository=SOURCE_REPOSITORY,
            source_revision="a" * 40,
            expected_source_revision="b" * 40,
            destination_repository=DESTINATION_REPOSITORY,
            legacy_baseline=_baseline(),
            selected_branch="codex/074-extract-projection",
            selections=[manifest_tool.Selection("backend", "backend")],
        )


@pytest.mark.parametrize(
    ("source_path", "destination_path"),
    [
        ("../backend", "backend"),
        ("backend", "../backend"),
        ("backend", "C:/migration/backend"),
        ("backend", ".git/hooks"),
        ("backend", "output//backend"),
    ],
)
def test_unsafe_or_non_normalized_selection_paths_are_refused(
    source_repo: tuple[Path, str],
    source_path: str,
    destination_path: str,
) -> None:
    repo, revision = source_repo

    with pytest.raises(manifest_tool.ManifestError):
        _build(
            repo,
            revision,
            [manifest_tool.Selection(source_path, destination_path)],
        )


def test_overlapping_selections_and_casefold_destination_collisions_are_refused(
    source_repo: tuple[Path, str],
) -> None:
    repo, revision = source_repo

    with pytest.raises(manifest_tool.ManifestError, match="overlapping selections"):
        _build(
            repo,
            revision,
            [
                manifest_tool.Selection("backend", "all"),
                manifest_tool.Selection("backend/webrender", "web"),
            ],
        )

    with pytest.raises(manifest_tool.ManifestError, match="destination collision"):
        _build(
            repo,
            revision,
            [
                manifest_tool.Selection(
                    "backend/webrender/alpha.txt", "Client/File.txt"
                ),
                manifest_tool.Selection(
                    "backend/webrender/zeta.txt", "client/file.TXT"
                ),
            ],
        )


def test_cli_writes_stdout_and_an_atomic_output_file(
    source_repo: tuple[Path, str], tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    repo, revision = source_repo

    assert manifest_tool.main(_cli_arguments(repo, revision)) == 0
    stdout_manifest = json.loads(capfd.readouterr().out)

    output = tmp_path / "extraction.json"
    assert (
        manifest_tool.main(_cli_arguments(repo, revision, output=os.fspath(output)))
        == 0
    )
    file_manifest = json.loads(output.read_text(encoding="utf-8"))

    assert file_manifest == stdout_manifest
    assert output.read_bytes() == manifest_tool.manifest_document_bytes(file_manifest)
    assert not list(tmp_path.glob(".extraction.json.*.tmp"))


def test_cli_reports_manifest_errors_without_a_traceback(
    source_repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, revision = source_repo
    arguments = _cli_arguments(repo, revision)
    arguments[arguments.index("--expected-source-revision") + 1] = "2" * 40

    assert manifest_tool.main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "build_extraction_manifest: source revision mismatch" in captured.err


@pytest.mark.parametrize(
    "value",
    [
        "",
        "e\N{COMBINING ACUTE ACCENT}.txt",
        "a" * 4097,
        "/absolute",
        "trailing/",
        r"windows\path",
        "dot/./path",
        "device/CON.txt",
        "trailing-dot./file",
        "unsafe/file?.txt",
    ],
)
def test_path_normalization_rejects_nonportable_values(value: str) -> None:
    with pytest.raises(manifest_tool.ManifestError):
        manifest_tool._normalized_repo_path(value, field="test path")


@pytest.mark.parametrize("value", ["", "-option", "refs/heads/main"])
def test_branch_normalization_rejects_ambiguous_names(value: str) -> None:
    with pytest.raises(manifest_tool.ManifestError):
        manifest_tool._normalized_branch(value)


@pytest.mark.parametrize(
    "value",
    [
        "main",
        "refs/heads/" + "a" * 256,
        "refs/heads/bad name",
        "refs/heads/.hidden",
        "refs/heads/release.lock",
    ],
)
def test_ref_normalization_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(manifest_tool.ManifestError):
        manifest_tool._normalized_ref(value, field="test ref")


@pytest.mark.parametrize(
    "value",
    ["2026-08-13T23:06:36+00:00", "2026-02-30T00:00:00Z"],
)
def test_observed_at_requires_a_real_canonical_utc_timestamp(value: str) -> None:
    with pytest.raises(manifest_tool.ManifestError):
        manifest_tool._normalized_observed_at(value)


def test_full_commit_and_repository_identifiers_are_strict() -> None:
    with pytest.raises(manifest_tool.ManifestError, match="full lowercase"):
        manifest_tool._require_full_commit("A" * 40, field="test commit")
    with pytest.raises(manifest_tool.ManifestError, match="exact canonical"):
        manifest_tool._canonical_repository(
            "https://github.com/astraldeep/AstralDeep.git", field="test repository"
        )


def test_git_failures_are_wrapped_as_manifest_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def raise_oserror(*_args: object, **_kwargs: object) -> object:
        raise OSError("fixture execution failure")

    monkeypatch.setattr(manifest_tool.subprocess, "run", raise_oserror)
    with pytest.raises(manifest_tool.ManifestError, match="could not execute git"):
        manifest_tool._git(tmp_path, ("status",))

    monkeypatch.setattr(
        manifest_tool.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=7, stdout=b"", stderr=b"fixture git failure\n"
        ),
    )
    with pytest.raises(manifest_tool.ManifestError, match="exit code 7"):
        manifest_tool._git(tmp_path, ("status",))


def test_exact_repository_root_refuses_missing_file_and_nested_paths(
    source_repo: tuple[Path, str], tmp_path: Path
) -> None:
    repo, _revision = source_repo
    with pytest.raises(manifest_tool.ManifestError, match="does not resolve"):
        manifest_tool._exact_repository_root(tmp_path / "missing")

    regular_file = tmp_path / "regular.txt"
    regular_file.write_text("fixture\n", encoding="utf-8")
    with pytest.raises(manifest_tool.ManifestError, match="not a directory"):
        manifest_tool._exact_repository_root(regular_file)

    with pytest.raises(manifest_tool.ManifestError, match="exact-root mismatch"):
        manifest_tool._exact_repository_root(repo / "backend")


def test_source_revision_must_name_a_commit(source_repo: tuple[Path, str]) -> None:
    repo, revision = source_repo
    blob = _git(repo, "rev-parse", f"{revision}:backend/webrender/alpha.txt")

    with pytest.raises(manifest_tool.ManifestError, match="not a commit"):
        manifest_tool.inventory_tracked_blobs(
            repo,
            source_revision=blob,
            expected_source_revision=blob,
        )


def test_source_revision_verification_refuses_resolution_or_tree_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    revision = "a" * 40
    responses = iter([b"commit\n", b"b" * 40 + b"\n"])
    monkeypatch.setattr(manifest_tool, "_git", lambda *_args: next(responses))
    with pytest.raises(manifest_tool.ManifestError, match="Git resolved"):
        manifest_tool._verify_source_revision(tmp_path, revision)

    responses = iter([b"commit\n", revision.encode() + b"\n", b"bad-tree\n"])
    monkeypatch.setattr(manifest_tool, "_git", lambda *_args: next(responses))
    with pytest.raises(manifest_tool.ManifestError, match="invalid tree"):
        manifest_tool._verify_source_revision(tmp_path, revision)


@pytest.mark.parametrize(
    ("raw_tree", "message"),
    [
        (b"malformed\0", "malformed blob metadata"),
        (
            b"100664 blob " + b"a" * 40 + b" 1\tfile.txt\0",
            "unsupported Git mode",
        ),
        (b"100644 blob bad 1\tfile.txt\0", "invalid identity or size"),
        (
            b"100644 blob "
            + b"a" * 40
            + b" 1\tfile.txt\0"
            + b"100644 blob "
            + b"b" * 40
            + b" 1\tfile.txt\0",
            "repeats tracked path",
        ),
    ],
)
def test_malformed_tree_inventory_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw_tree: bytes,
    message: str,
) -> None:
    revision = "a" * 40
    monkeypatch.setattr(manifest_tool, "_exact_repository_root", lambda _path: tmp_path)
    monkeypatch.setattr(
        manifest_tool, "_verify_source_revision", lambda _repo, _revision: "b" * 40
    )
    monkeypatch.setattr(manifest_tool, "_git", lambda *_args: raw_tree)

    with pytest.raises(manifest_tool.ManifestError, match=message):
        manifest_tool.inventory_tracked_blobs(
            tmp_path,
            source_revision=revision,
            expected_source_revision=revision,
        )


def test_empty_duplicate_and_missing_selections_are_refused(
    source_repo: tuple[Path, str],
) -> None:
    repo, revision = source_repo
    with pytest.raises(manifest_tool.ManifestError, match="at least one"):
        _build(repo, revision, [])
    duplicate = manifest_tool.Selection("backend", "backend")
    with pytest.raises(manifest_tool.ManifestError, match="duplicate"):
        _build(repo, revision, [duplicate, duplicate])
    with pytest.raises(manifest_tool.ManifestError, match="does not name"):
        _build(
            repo,
            revision,
            [manifest_tool.Selection("missing", "missing")],
        )


def test_invalid_manifest_values_are_not_serialized() -> None:
    invalid = {"unsupported": {"set"}}
    with pytest.raises(manifest_tool.ManifestError, match="cannot be canonicalized"):
        manifest_tool.canonical_manifest_bytes(invalid)
    with pytest.raises(manifest_tool.ManifestError, match="cannot be serialized"):
        manifest_tool.manifest_document_bytes(invalid)


def test_atomic_writer_refuses_unsafe_output_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {"format": manifest_tool.FORMAT}
    with pytest.raises(manifest_tool.ManifestError, match="parent does not resolve"):
        manifest_tool.write_manifest(tmp_path / "missing" / "manifest.json", manifest)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(manifest_tool.ManifestError, match="not a regular file"):
        manifest_tool.write_manifest(directory, manifest)

    link = tmp_path / "manifest-link.json"
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    try:
        link.symlink_to(target)
    except OSError:
        pass
    else:
        with pytest.raises(manifest_tool.ManifestError, match="symlink"):
            manifest_tool.write_manifest(link, manifest)

    monkeypatch.setattr(
        manifest_tool.tempfile,
        "NamedTemporaryFile",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("fixture write failure")),
    )
    with pytest.raises(manifest_tool.ManifestError, match="could not write"):
        manifest_tool.write_manifest(tmp_path / "failure.json", manifest)
