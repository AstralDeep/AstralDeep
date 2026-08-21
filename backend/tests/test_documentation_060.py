"""Tracked documentation, link, and apply/recreate contracts for feature 060."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path, PurePosixPath
import runpy
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_doc_links.py"
GUIDE = REPO_ROOT / "docs" / "byo-client-agents.md"

if not (
    (REPO_ROOT / "scripts").is_dir() and (REPO_ROOT / "docs").is_dir()
):  # repo root absent inside the product image
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("doc_links_060", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for name, body in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    return repo


def test_byo_guide_is_explicitly_unignored_and_reachable_from_existing_docs() -> None:
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    guide = GUIDE.read_text(encoding="utf-8")

    assert "docs/*" in ignore
    assert "!docs/byo-client-agents.md" in ignore
    assert _git(REPO_ROOT, "check-ignore", "-q", "--no-index", str(GUIDE), check=False).returncode == 1
    for relative in (
        "CLAUDE.md",
        "components/AstralProjection/apple-clients/README.md",
        "docs/production-deployment.md",
    ):
        assert "byo-client-agents.md" in (REPO_ROOT / relative).read_text(
            encoding="utf-8"
        )
    for heading in (
        "## Enable and verify the effective setting",
        "## Hosting modes",
        "## Lifecycle shown to users",
        "## Recovery and failover",
        "## Runtime compatibility",
        "## Rollback and disablement",
    ):
        assert heading in guide


def test_apply_target_recreates_and_prints_only_the_normalized_flag() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    guide = GUIDE.read_text(encoding="utf-8")
    body = makefile.split("apply-config:", 1)[1].split("\n\n", 1)[0]

    assert "docker compose up -d --force-recreate astraldeep" in body
    assert "docker compose exec -T astraldeep" in body
    assert "Effective FF_BYO_AGENTS=" in body
    assert "docker compose restart" not in body
    assert "printenv" not in body.lower()
    assert "cat .env" not in body.lower()
    assert "docker inspect" not in body.lower()
    assert "make apply-config" in guide
    assert "Do not use `make restart`" in guide


def test_link_validator_is_stdlib_only_and_documents_public_functions() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
    imports: set[str] = set()
    public_functions: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            public_functions[node.name] = node
    imports.discard("__future__")

    assert imports <= sys.stdlib_module_names
    expected = {
        "git_tracked_files",
        "git_candidate_files",
        "tracked_markdown_files",
        "maintained_markdown_files",
        "extract_markdown_targets",
        "markdown_anchors",
        "validate_markdown_links",
        "build_parser",
        "main",
    }
    assert expected <= set(public_functions)
    assert all(ast.get_docstring(public_functions[name]) for name in expected)


def test_nul_safe_git_inventory_and_requested_path_selection(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "README.md": "[Guide](docs/operator guide.md)\n",
            "docs/operator guide.md": "# Operator guide\n",
            "ignored.md": "ignored\n",
            ".gitignore": "ignored.md\n",
        },
    )
    (repo / "candidate.md").write_text("candidate\n", encoding="utf-8")

    assert validator.git_tracked_files(repo) == (
        PurePosixPath(".gitignore"),
        PurePosixPath("README.md"),
        PurePosixPath("docs/operator guide.md"),
    )
    assert PurePosixPath("candidate.md") in validator.git_candidate_files(repo)
    assert PurePosixPath("ignored.md") not in validator.git_candidate_files(repo)
    (repo / "README.md").unlink()
    assert PurePosixPath("README.md") not in validator.git_candidate_files(repo)
    assert validator.tracked_markdown_files(repo, ("docs",)) == (
        PurePosixPath("docs/operator guide.md"),
    )


def test_extractor_skips_fences_and_collects_inline_image_and_reference_links() -> None:
    text = """[one](target.md)\n![two](image.png)\n[ref]: <other.md>\n```\n[ignored](missing.md)\n```\n"""
    assert validator.extract_markdown_targets(text) == (
        (1, "target.md"),
        (2, "image.png"),
        (3, "other.md"),
    )


def test_valid_files_directories_root_fallback_and_anchors_pass(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "README.md": (
                "[local](docs/guide.md#recovery) [directory](docs/) "
                "[external](https://example.invalid/x) [route](/readyz)\n"
            ),
            "docs/guide.md": (
                "# Guide\n## Recovery\n## Recovery\n<a id=\"explicit\"></a>\n"
                "[root fallback](backend/source.py)\n"
            ),
            "backend/source.py": "value = 1\n",
        },
    )
    tracked = validator.git_tracked_files(repo)
    sources = validator.tracked_markdown_files(repo)

    assert validator.markdown_anchors(
        (repo / "docs" / "guide.md").read_text(encoding="utf-8")
    ) == frozenset({"guide", "recovery", "recovery-1", "explicit"})
    assert validator.validate_markdown_links(repo, sources, tracked) == ()


def test_invalid_target_classes_are_reported_without_crashing(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "README.md": (
                "[missing](missing.md) [anchor](guide.md#absent) "
                "[escape](../outside.md) [percent](bad%2.md) "
                "[scheme](file:///tmp/nope) [empty]()\n"
            ),
            "guide.md": "# Present\n",
            "empty/.keep": "tracked\n",
        },
    )
    tracked = validator.git_tracked_files(repo)
    issues = validator.validate_markdown_links(
        repo, (PurePosixPath("README.md"),), tracked
    )

    assert {issue.reason for issue in issues} == {
        "anchor does not exist",
        "empty target",
        "invalid percent escape",
        "target does not exist",
        "target escapes repository",
        "unsupported URI scheme",
    }
    assert all(issue.render().startswith("README.md:1:") for issue in issues)


def test_untracked_target_and_unreadable_source_fail_clean_checkout_validation(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path, {"README.md": "[new](new.md)\n"})
    (repo / "new.md").write_text("# New\n", encoding="utf-8")
    tracked = validator.git_tracked_files(repo)

    issues = validator.validate_markdown_links(
        repo, (PurePosixPath("README.md"), PurePosixPath("gone.md")), tracked
    )
    assert any(issue.reason == "target is not tracked" for issue in issues)
    assert any("No such file or directory" in issue.reason for issue in issues)


def test_existing_directory_without_tracked_contents_is_rejected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, {"README.md": "[empty](empty/)\n"})
    (repo / "empty").mkdir()
    issues = validator.validate_markdown_links(
        repo,
        (PurePosixPath("README.md"),),
        validator.git_tracked_files(repo),
    )

    assert [issue.reason for issue in issues] == [
        "target directory has no tracked files"
    ]


@pytest.mark.parametrize(
    ("function_name", "returncode", "stdout", "stderr", "message"),
    [
        ("git_tracked_files", 0, b"\xff", b"", "non-UTF-8"),
        ("git_candidate_files", 0, b"\xff", b"", "non-UTF-8"),
        ("git_candidate_files", 1, b"", b"candidate failed", "candidate failed"),
        ("git_candidate_files", 1, b"", b"", "candidate inventory failed"),
    ],
)
def test_git_inventory_errors_are_stable(
    monkeypatch,
    function_name: str,
    returncode: int,
    stdout: bytes,
    stderr: bytes,
    message: str,
) -> None:
    monkeypatch.setattr(
        validator.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )

    with pytest.raises(validator.GitInventoryError, match=message):
        getattr(validator, function_name)(REPO_ROOT)


def test_cli_default_scope_all_scope_and_exit_codes(tmp_path: Path, capsys) -> None:
    repo = _make_repo(
        tmp_path,
        {
            "README.md": "[guide](guide.md)\n",
            "guide.md": "# Guide\n",
            "specs/001-history/spec.md": "[retired](missing.ts)\n",
        },
    )

    assert validator.main(["--repo-root", str(repo)]) == 0
    assert "passed: 2 Markdown file(s)" in capsys.readouterr().out
    assert validator.main(["--repo-root", str(repo), "--all"]) == 1
    assert "target does not exist" in capsys.readouterr().err
    assert validator.main(["--repo-root", str(repo), "not-tracked.md"]) == 2
    assert "not tracked" in capsys.readouterr().err
    assert validator.main(["--repo-root", str(tmp_path / "not-a-repo")]) == 2
    assert "could not run" in capsys.readouterr().err


def test_cli_default_scope_checks_nonignored_candidate_markdown(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path, {"README.md": "# Ready\n"})
    (repo / "candidate.md").write_text("[missing](not-here.md)\n", encoding="utf-8")

    assert validator.main(["--repo-root", str(repo)]) == 1
    assert "candidate.md:1: target does not exist" in capsys.readouterr().err


def test_script_entrypoint_executes_the_same_successful_cli(tmp_path: Path, monkeypatch) -> None:
    repo = _make_repo(tmp_path, {"README.md": "# Ready\n"})
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--repo-root", str(repo)],
    )

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert raised.value.code == 0


def test_current_maintained_documentation_targets_resolve() -> None:
    candidates = validator.git_candidate_files(REPO_ROOT)
    sources = validator.maintained_markdown_files(candidates)

    selected = {path.as_posix() for path in sources}
    assert GUIDE.relative_to(REPO_ROOT).as_posix() in selected
    assert validator.validate_markdown_links(REPO_ROOT, sources, candidates) == ()


def test_ci_executes_doc_validation_under_tooling_python_coverage() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "backend/tests/test_documentation_060.py" in workflow
    assert "python scripts/check_doc_links.py" in workflow
