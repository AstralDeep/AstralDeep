"""No emoji anywhere a person reads — a standing project rule, enforced here.

Emoji were scattered through the product: the welcome examples carried one in
front of every title, the recent-chats list drew a different pictograph per
agent, warnings opened with a triangle, the attachment chip led with a
paperclip. They set a tone the console does not want, they render differently
on every platform, and several of them were carrying meaning that the text
beside them already carried.

So: no emoji. A word, a drawn SVG icon, or nothing.

**Scope.** This scans the source the product renders from — the orchestrator,
the agents, the shared layer and the render layer. It deliberately does NOT
scan tests: a handful of them feed emoji IN as input, on purpose, to prove
that a name, a note or a skill description survives arbitrary Unicode from a
user. A person may still type an emoji at us and everything must keep working;
what is out is emoji we author.

**What counts.** The pictographic ranges (Miscellaneous Symbols and
Pictographs, Emoticons, Transport, Supplemental Symbols, the older
dingbat-range emoji like the warning sign and the check mark, and the emoji
presentation selector U+FE0F). Typographic marks are NOT emoji and stay
allowed: the em dash, the degree sign, arrows, box drawing, the four-pointed
star U+2726 used as the dialog's icon mark, and the mathematical symbols the
agents emit.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent

#: Directories whose source is rendered to a person. Tests are excluded by the
#: per-path rule below, not here, so a new test directory needs no edit.
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

#: Emoji, not symbols. Each range is pictographic or is an emoji mechanism;
#: none of them is a typographic mark the product legitimately prints.
EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # pictographs, emoticons, transport, symbols, extended-A
    "☀-➿"           # misc symbols + dingbats (see ALLOWED_MARKS below)
    "⬀-⯿"           # misc symbols and arrows
    "️"                  # emoji presentation selector
    "⃣"                  # combining enclosing keycap
    "]"
)

#: Dingbat-range characters that are typography, not emoji, and are used as
#: such. Anything added here needs a reason on its own line.
#:   U+2726 four-pointed star — the chrome dialog's icon mark and the canvas
#:           empty-state glyph, both text marks beside real headings.
#:   U+2713 / U+2717 check and ballot — step state in a rendered stepper.
#:   U+2605 / U+2606 star — the saved-components marker on a history row.
#:   U+2715 multiplication X — the close-button mark, and the way the close
#:           button is referred to throughout the chrome comments.
#:   U+270E pencil, U+2B07 down arrow — two of the four marks in the
#:           server-owned component-action vocabulary
#:           (``webrender/chrome/component_model.py``), alongside U+27F2 and
#:           U+2197 which fall outside these ranges anyway. That table is one
#:           coherent set of text marks every client renders; spelling two of
#:           the four differently would be arbitrary. If the set is ever
#:           redrawn as SVG, redraw all four and delete these two entries.
ALLOWED_MARKS = "✦✓✗★☆✕✎⬇"

#: Third-party bundles we neither author nor render text from.
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
    """The catalog that seeds every client's first screen, checked by name.

    The general scan above would catch these too. This one names them because
    they are the examples a first-time user reads, and they are the place the
    emoji kept coming back.
    """
    from orchestrator.welcome import WELCOME_EXAMPLES

    for title, caption, query in WELCOME_EXAMPLES:
        for field, value in (("title", title), ("caption", caption), ("query", query)):
            hits = [ch for ch in EMOJI.findall(value) if ch not in ALLOWED_MARKS]
            assert not hits, f"welcome example {title!r} has emoji in its {field}: {hits}"


def test_the_scan_would_actually_catch_one():
    """The guard has to fail on a real emoji, or it guards nothing."""
    assert [ch for ch in EMOJI.findall("ship it \U0001F680") if ch not in ALLOWED_MARKS]
    assert [ch for ch in EMOJI.findall("⚠️ careful") if ch not in ALLOWED_MARKS]
    # …and has to leave typography alone.
    assert not [ch for ch in EMOJI.findall("83.7°F — up 2°")
                if ch not in ALLOWED_MARKS]
    assert not [ch for ch in EMOJI.findall("✦ Workspace") if ch not in ALLOWED_MARKS]
