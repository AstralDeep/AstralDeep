"""Protects the Vessel release workflow's tag routing, source-built Kernel artifacts, closed environment and OIDC publication.
It reads .github/workflows/release-vessel.yml and vessel/Dockerfile and sits beside test_release_tag_prefixes.py."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / ".github" / "workflows" / "release-vessel.yml").read_text(encoding="utf-8")
FLOW = yaml.safe_load(TEXT)
PUSH_ONLY = "(github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main') && (github.event_name == 'push')"


def test_only_a_vessel_tag_or_a_manual_dry_run_starts_it():
    triggers = FLOW.get("on", FLOW.get(True))
    assert triggers["push"] == {"tags": ["vessel-v*"]}
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert FLOW["permissions"] == {"contents": "read"}


def test_kernel_artifacts_are_built_from_the_pinned_source_for_both_architectures():
    assert FLOW["jobs"]["kernel-binding-amd64"]["runs-on"] == "ubuntu-24.04"
    assert FLOW["jobs"]["kernel-binding-arm64"]["runs-on"] == "ubuntu-24.04-arm"
    assert "--platform linux/amd64" in TEXT and "--platform linux/arm64" in TEXT
    assert "scripts/build_kernel_binding.py produce" in TEXT
    assert "--run-id" not in TEXT


def test_images_carry_astral_exec_and_publishing_needs_the_closed_environment():
    dockerfile = (ROOT / "vessel" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY --from=astral-exec" in dockerfile and "confine" not in dockerfile
    assert "astral-exec=backend/astral_exec" in TEXT
    dry, publish = FLOW["jobs"]["images"], FLOW["jobs"]["publish-images"]
    assert "environment" not in dry and "permissions" not in dry
    assert "push: false" in yaml.safe_dump(dry["steps"]).replace("'", "")
    assert publish["environment"] == "release-vessel" and publish["permissions"]["packages"] == "write"
    assert publish["if"] == PUSH_ONLY and FLOW["jobs"]["pypi"]["if"] == PUSH_ONLY and dry["if"] == "github.event_name == 'workflow_dispatch'"
    for job in (publish, FLOW["jobs"]["pypi"]):
        assert "RELEASE_VESSEL_READY" in yaml.safe_dump(job["steps"][0])
        assert job["steps"][0]["name"] == "Require the owner-configured release-vessel environment"


def test_pypi_uses_oidc_only_after_public_images_and_stores_no_token():
    job = FLOW["jobs"]["pypi"]
    assert job["needs"] == ["check", "publish-images", "build"]
    assert job["environment"] == "release-vessel" and job["permissions"] == {"id-token": "write"}
    assert "pypa/gh-action-pypi-publish@" in TEXT
    assert "password:" not in yaml.safe_dump(job["steps"][-1])
    assert not re.search(r"secrets\.(?!GITHUB_TOKEN)", TEXT)
    assert "https://ghcr.io/v2/astraldeep/$name/manifests/$VERSION" in TEXT


def test_the_tag_must_match_the_package_version_and_every_job_stays_within_thirty_minutes():
    check = yaml.safe_dump(FLOW["jobs"]["check"])
    assert "vessel/pyproject.toml" in check and 'vessel-v$version' in check
    assert max(int(value) for value in re.findall(r"timeout-minutes: (\d+)", TEXT)) <= 30
    assert all(re.search(r"@[0-9a-f]{40}", step["uses"]) for job in FLOW["jobs"].values()
               for step in job["steps"] if "uses" in step)
