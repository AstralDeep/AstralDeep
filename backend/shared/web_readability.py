"""Shared heuristics for turning fetched HTML into readable, chrome-free text.

The web_research and summarizer agents both extract readable text from fetched
pages with stdlib ``html.parser``. This module centralizes the parts that keep
that text clean: skipping navigation/boilerplate elements by their class/id/role
(not just by semantic tag), dropping known boilerplate lines (gov banners, skip
links, cookie notices), and dropping unbroken junk blobs (base64/serialized
state) that carry no meaning and overflow the UI. Stdlib only — no new deps.
"""

import re
from typing import List, Sequence, Tuple

#: Void elements never have a matching end tag, so a skipped subtree must not
#: increment its depth counter on them (or the counter would never unwind).
VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

#: Bare ``menu`` is ambiguous: content lists use it too. Require an explicit
#: navigation role or a clearly qualified site-navigation class/id instead.
_SKIP_ATTR_RE = re.compile(
    r"(?:^|[\s_-])(?:nav|navbar|navigation|(?:site|main|global|mobile)[_-]menu|"
    r"megamenu|header|masthead|"
    r"footer|sidebar|sidenav|breadcrumb|cookie|consent|gdpr|banner|usa-banner|"
    r"skiplink|skip-link|skip-to|skipnav|social|share|sharing|subscribe|"
    r"newsletter|toolbar|sitesearch|search-form|pagination|pager|"
    r"backtotop|back-to-top|langselect|language-selector|utility-nav|"
    r"site-nav|main-nav|top-nav|global-nav)(?:$|[\s_-])",
    re.IGNORECASE,
)

#: Explicit ARIA navigation/menu roles and landmarks denote page chrome.
_SKIP_ROLES = frozenset({
    "navigation", "menu", "menubar", "banner", "search", "contentinfo", "complementary",
})

#: Whole lines that are pure boilerplate — gov banners, skip links, cookie/legal
#: chrome — matched after any leading markdown markers are stripped.
_BOILERPLATE_RE = re.compile(
    r"^(?:skip to (?:main )?content"
    r"|an official website of the .*government"
    r"|official websites use \.gov"
    r"|a \.gov website belongs to"
    r"|secure \.gov websites use https"
    r"|a lock \("
    r"|https?:// means you"
    r"|(?:we|this website|this site) uses? cookies"
    r"|accept (?:all )?cookies"
    r"|(?:cookie|privacy) (?:preferences|settings)"
    r"|manage cookies"
    r"|copyright ©"
    r"|© \d{4}"
    r"|all rights reserved)",
    re.IGNORECASE,
)

#: A base64/base64url/hex-ish token: the shape of serialized state, hashes, and
#: inline data blobs. URLs are excluded because they contain ``:`` / ``.`` / ``?``.
_JUNK_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=_-]{120,}$")


def should_skip_attrs(attrs: Sequence[Tuple[str, object]]) -> bool:
    """True when an element's class / id / role marks it as navigation/chrome."""
    for name, value in attrs:
        val = "" if value is None else str(value)
        if name in ("class", "id") and val and _SKIP_ATTR_RE.search(val):
            return True
        if name == "role" and val.lower() in _SKIP_ROLES:
            return True
        if name == "aria-hidden" and val.lower() == "true":
            return True
    return False


def _is_junk_block(block: str) -> bool:
    """True for a block that is a single very long unbroken non-prose token."""
    b = block.strip()
    if not b or " " in b or "\n" in b:
        return False
    return bool(_JUNK_TOKEN_RE.match(b))


def clean_page_text(text: str) -> str:
    """Drop boilerplate lines and unbroken junk-token blobs from extracted text.

    Blocks are the ``\\n\\n``-separated units the extractors emit. A block is
    dropped when, after stripping any leading markdown markers, it matches a
    boilerplate pattern, or when it is a lone base64/serialized-state token.
    """
    kept: List[str] = []
    for block in text.split("\n\n"):
        b = block.strip()
        if not b:
            continue
        probe = b.lstrip("#-• \t").strip()
        if _BOILERPLATE_RE.match(probe):
            continue
        if _is_junk_block(b):
            continue
        kept.append(b)
    return "\n\n".join(kept)


def source_markdown(url: str) -> str:
    """A markdown source-attribution line for auditability of fetched content."""
    clean = str(url or "").strip()
    return f"Source: [{clean}]({clean})"
