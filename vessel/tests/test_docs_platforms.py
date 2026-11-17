# Tests that the platform page and the quickstart no longer ask a user install to run Docker.

from __future__ import annotations

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"
BANNED = ("delve", "tapestry", "game-changer", "unlock", "testament", "robust", "leverage", "seamless", "cutting-edge",
          "world-class", "actually", "embark", "invaluable", "relentless", "groundbreaking", "endeavour")


def user_install_part(name: str) -> str:
    text = (DOCS / name).read_text()
    return text.split("## Container mode", 1)[0]


def test_platforms_does_not_require_docker_for_a_user_install():
    part = user_install_part("security/platforms.md")
    assert "docker" not in part.lower()
    assert "Seatbelt" in part and "bubblewrap" in part
    assert "## Container mode" in (DOCS / "security/platforms.md").read_text()


def test_the_quickstart_user_path_needs_no_docker_and_names_the_ubuntu_rule_and_hwe_kernel():
    assert "docker" not in user_install_part("get-started/quickstart.md").lower()
    platforms = (DOCS / "security/platforms.md").read_text()
    assert "kernel.apparmor_restrict_unprivileged_userns" in platforms and "HWE" in platforms


def test_the_new_pages_follow_the_writing_rules():
    for name in ("security/platforms.md", "get-started/quickstart.md", "run/migration.md", "troubleshooting.md"):
        text = (DOCS / name).read_text()
        prose = re.sub(r"```.*?```", "", text, flags=re.S)
        assert "—" not in text, name
        assert not any(word in text.lower() for word in BANNED), name
        if name == "security/platforms.md":
            assert ";" not in prose
