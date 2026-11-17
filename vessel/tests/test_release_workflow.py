"""Protects the release workflow's source-pinned Kernel and publication gates."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text()


def test_release_restores_qualified_kernel_artifacts_for_both_architectures():
    assert "kernel-binding:" in WORKFLOW
    assert "platform: linux/amd64" in WORKFLOW
    assert "platform: linux/arm64" in WORKFLOW
    assert "python scripts/build_kernel_binding.py fetch" in WORKFLOW
    assert "--run-id 43680163653" in WORKFLOW
    assert "--expected-digest" in WORKFLOW
    assert "provider_artifact_id: 14148318283" in WORKFLOW
    assert "provider_artifact_id: 14148388081" in WORKFLOW
    assert "name: kernel-artifacts-linux-${{ matrix.architecture }}" in WORKFLOW
    assert "needs: [check, kernel-binding]" in WORKFLOW
    producer = WORKFLOW.split("  kernel-binding:\n", 1)[1].split("\n  images:\n", 1)[0]
    assert "runs-on: ${{ matrix.runner }}" in producer
    assert "runner: ubuntu-24.04" in producer
    assert "runner: ubuntu-24.04-arm" in producer
    assert "docker/setup-qemu-action" not in producer
    assert "- uses: actions/upload-artifact@" in producer
    assert "        if: always()" in producer
    assert "retention-days: 14" in producer
    images = WORKFLOW.split("  images:\n", 1)[1].split("\n  build:\n", 1)[0]
    assert "needs: [check, kernel-binding]" in images
    assert "if: always()" not in images
    assert "docker/setup-qemu-action" in images
    assert "kernel-artifacts=${{ runner.temp }}/kernel-artifacts" in WORKFLOW
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "from=kernel-artifacts" in dockerfile
    assert "python /opt/vessel-build/install_kernel.py /kernel-artifacts" in dockerfile


def test_release_keeps_public_image_availability_before_oidc_pypi_publish():
    assert "push: ${{ github.event_name == 'push' }}" in WORKFLOW
    assert "needs: [check, images, build]" in WORKFLOW
    assert "environment: pypi" in WORKFLOW
    assert "id-token: write" in WORKFLOW
    assert 'https://ghcr.io/v2/astraldeep/$name/manifests/$VERSION' in WORKFLOW
    assert "pypa/gh-action-pypi-publish" in WORKFLOW


def test_every_release_workflow_job_stays_within_the_thirty_minute_limit():
    timeouts = [int(value) for value in re.findall(r"^\s+timeout-minutes:\s+(\d+)\s*$", WORKFLOW, re.MULTILINE)]
    assert timeouts
    assert max(timeouts) <= 30
