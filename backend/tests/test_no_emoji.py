"""Tests enforcing the no-emoji rule: scans backend/orchestrator, agents, shared and the
render layer (not tests) for emoji, checks backend/orchestrator/welcome.py's
examples, and proves the scanner catches a real one.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent

SCANNED_ROOTS = (
    BACKEND / "orchestrator",
    BACKEND / "agents",
    BACKEND / "shared",
    BACKEND / "personalization",
    BACKEND / "persistent_agents",
    BACKEND / "scheduler",
    BACKEND / "dreaming",
    BACKEND / "onboarding",
    BACKEND / "llm_config",
    BACKEND / "knowledge",
    BACKEND / "security_benchmark",
    REPO / "components" / "AstralProjection" / "backend" / "webrender",
)

SCANNED_SUFFIXES = (".py", ".js", ".css", ".html")

EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "☀-➿"
    "⬀-⯿"
    "️"
    "⃣"
    "]"
)

ALLOWED_MARKS = "✦✓✗★☆✕✎⬇"

SKIP_PARTS = ("__pycache__", "vendor", "node_modules", "tests", "test_data", "tmp")


def _scanned_files():
    for root in SCANNED_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix not in SCANNED_SUFFIXES or not path.is_file():
                continue
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            yield path


def _offenders(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    out = []
    for number, line in enumerate(text.split("\n"), 1):
        hits = [ch for ch in EMOJI.findall(line) if ch not in ALLOWED_MARKS]
        if hits:
            out.append((number, "".join(hits), line.strip()[:90]))
    return out


def test_no_emoji_in_anything_the_product_renders():
    found = []
    for path in _scanned_files():
        for number, hits, line in _offenders(path):
            found.append(f"{path.relative_to(REPO)}:{number}: {hits!r} in {line!r}")
    assert not found, (
        "emoji are out of this project — use a word, a drawn SVG icon, or "
        "nothing:\n  " + "\n  ".join(found)
    )


def test_the_curated_welcome_examples_are_plain_text():
    from orchestrator.welcome import WELCOME_EXAMPLES

    for title, caption, query in WELCOME_EXAMPLES:
        for field, value in (("title", title), ("caption", caption), ("query", query)):
            hits = [ch for ch in EMOJI.findall(value) if ch not in ALLOWED_MARKS]
            assert not hits, f"welcome example {title!r} has emoji in its {field}: {hits}"


def test_the_scan_would_actually_catch_one():
    assert [ch for ch in EMOJI.findall("ship it \U0001F680") if ch not in ALLOWED_MARKS]
    assert [ch for ch in EMOJI.findall("⚠️ careful") if ch not in ALLOWED_MARKS]
    assert not [ch for ch in EMOJI.findall("83.7°F — up 2°")
                if ch not in ALLOWED_MARKS]
    assert not [ch for ch in EMOJI.findall("✦ Workspace") if ch not in ALLOWED_MARKS]
