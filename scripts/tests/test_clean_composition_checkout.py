"""Network-disabled clean-checkout proof for the feature-074 composition."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPONENT_PATHS = (
    "components/AstralProjection",
    "components/AstralPlane",
    "components/AstralPrimitives",
    "components/LETS",
)


def _git(
    *arguments: str,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode:
        stderr = completed.stderr.strip()
        raise AssertionError(
            f"git {' '.join(arguments)} failed with exit {completed.returncode}: {stderr}"
        )
    return completed.stdout.rstrip()


def _network_disabled_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _gitlink_revision(component_path: str) -> str:
    output = _git(
        "ls-tree",
        "HEAD",
        "--",
        component_path,
        cwd=REPOSITORY_ROOT,
    )
    fields = output.split()
    assert fields[:2] == ["160000", "commit"]
    assert len(fields[2]) == 40
    return fields[2]


def _canonical_submodule_urls() -> dict[str, str]:
    output = _git(
        "config",
        "--file",
        ".gitmodules",
        "--get-regexp",
        r"^submodule\..*\.url$",
        cwd=REPOSITORY_ROOT,
    )
    urls: dict[str, str] = {}
    for line in output.splitlines():
        key, url = line.split(maxsplit=1)
        name = key.removeprefix("submodule.").removesuffix(".url")
        urls[name] = url
    assert len(urls) == len(COMPONENT_PATHS)
    return urls


def test_clean_checkout_initializes_exact_offline_composition(tmp_path: Path) -> None:
    environment = _network_disabled_environment()
    mirrors = tmp_path / "component-mirrors"
    mirrors.mkdir()

    urls = _canonical_submodule_urls()
    rewrites: dict[str, str] = {}
    for component_path in COMPONENT_PATHS:
        component_name = component_path.rsplit("/", 1)[1]
        component_root = REPOSITORY_ROOT / component_path
        revision = _gitlink_revision(component_path)
        assert _git("rev-parse", "HEAD", cwd=component_root) == revision

        mirror = mirrors / f"{component_name}.git"
        _git("init", "--bare", str(mirror), environment=environment)
        _git(
            f"--git-dir={mirror}",
            "fetch",
            "--depth=1",
            "--no-tags",
            str(component_root),
            f"{revision}:refs/heads/pin",
            environment=environment,
        )
        _git(
            f"--git-dir={mirror}",
            "symbolic-ref",
            "HEAD",
            "refs/heads/pin",
            environment=environment,
        )
        rewrites[urls[component_name]] = mirror.resolve().as_uri()

    checkout = tmp_path / "AstralDeep"
    _git(
        "clone",
        "--depth=1",
        "--no-local",
        "--no-checkout",
        str(REPOSITORY_ROOT),
        str(checkout),
        environment=environment,
    )
    source_revision = _git("rev-parse", "HEAD", cwd=REPOSITORY_ROOT)
    _git("checkout", "--detach", source_revision, cwd=checkout, environment=environment)

    # `git submodule update` starts child clone processes outside the parent
    # repository, so parent-local url.* config does not reach them. Command
    # configuration in the environment is inherited while remaining confined
    # to this test process and its children.
    environment["GIT_CONFIG_COUNT"] = str(len(rewrites))
    for index, (canonical_url, local_url) in enumerate(sorted(rewrites.items())):
        environment[f"GIT_CONFIG_KEY_{index}"] = f"url.{local_url}.insteadOf"
        environment[f"GIT_CONFIG_VALUE_{index}"] = canonical_url
    _git("submodule", "sync", "--recursive", cwd=checkout, environment=environment)
    _git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
        "--recursive",
        cwd=checkout,
        environment=environment,
    )

    submodule_status = _git(
        "submodule", "status", "--recursive", cwd=checkout, environment=environment
    ).splitlines()
    assert len(submodule_status) == len(COMPONENT_PATHS)
    assert all(line.startswith(" ") for line in submodule_status)

    verification = subprocess.run(
        [sys.executable, "scripts/verify_composition.py", "--root", str(checkout)],
        cwd=checkout,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert verification.returncode == 0, verification.stderr
    assert "four exact clean component pins" in verification.stdout
    assert _git("status", "--porcelain", cwd=checkout, environment=environment) == ""
