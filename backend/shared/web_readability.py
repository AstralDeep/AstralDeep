"""Shared HTML-to-readable-text heuristics for fetched pages: skips
navigation/boilerplate chrome and drops unbroken junk-token blobs. Used by
agents/summarizer and agents/web_research mcp_tools.py to clean fetched page content.
"""

import re
from typing import List, Sequence, Tuple

VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

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

_SKIP_ROLES = frozenset({
    "navigation", "menu", "menubar", "banner", "search", "contentinfo", "complementary",
})

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

_JUNK_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=_-]{120,}$")


def should_skip_attrs(attrs: Sequence[Tuple[str, object]]) -> bool:
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
    b = block.strip()
    if not b or " " in b or "\n" in b:
        return False
    return bool(_JUNK_TOKEN_RE.match(b))


def clean_page_text(text: str) -> str:
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
    clean = str(url or "").strip()
    return f"Source: [{clean}]({clean})"
