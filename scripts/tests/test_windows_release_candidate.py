"""Checks that Windows environment manifests include the locally built presentation packages.
The composed release lock retains their exact versions alongside their wheel hashes.
"""

from pathlib import Path

import pytest

from scripts import windows_release_candidate as candidate


def test_composed_lock_includes_both_local_versions(tmp_path: Path, monkeypatch):
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "public==1.0\n"
        "# local-presentation: astralprims==0.13.0\n"
        "astralprims @ ${ASTRAL_PRIMITIVES_WHEEL_URI}\n"
        "# local-presentation: astral-rote==0.1.0\n"
        "astral-rote @ ${ASTRAL_ROTE_WHEEL_URI}\n"
    )
    expected = {"public": "1.0", "astralprims": "0.13.0", "astral-rote": "0.1.0"}
    assert candidate.locked_packages(lock) == expected
    monkeypatch.setattr(candidate, "installed_versions", lambda names: expected)
    assert candidate.environment_manifest(lock)["packages"] == [
        {"name": name, "version": expected[name]} for name in sorted(expected)
    ]
    monkeypatch.setattr(candidate, "installed_versions", lambda names: {**expected, "astral-rote": "0.0.1"})
    with pytest.raises(candidate.CandidateManifestError, match="versions differ"):
        candidate.environment_manifest(lock)


@pytest.mark.parametrize("name", ["astralprims", "astral-rote"])
def test_composed_lock_rejects_duplicate_local_versions(tmp_path: Path, name):
    lock = tmp_path / "requirements.lock"
    lock.write_text(f"{name}==0.1.0\n# local-presentation: {name}==0.1.0\n")
    with pytest.raises(candidate.CandidateManifestError, match="repeats package"):
        candidate.locked_packages(lock)
