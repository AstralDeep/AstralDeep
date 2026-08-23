"""Feature 033 (capability C-S2) — taint / provenance data-flow control.

Covers the trust lattice (effective trust = min over ancestors), source/sink
classification, the value-level flow policy, and the TaintTracker including
multi-hop laundering survival.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import taint  # noqa: E402
from orchestrator.taint import (  # noqa: E402
    INTERNAL, TRUSTED, UNTRUSTED, TaintTracker, check_flow, classify_source,
    combine, is_sink,
)


# ───────────────────────── flag ──────────────────────────────────────────────

def test_taint_default_off(monkeypatch):
    monkeypatch.delenv("FF_TAINT_TRACKING", raising=False)
    assert taint.taint_enabled() is False


@pytest.mark.parametrize("v", ["true", "1", "yes", "on"])
def test_taint_on_values(monkeypatch, v):
    monkeypatch.setenv("FF_TAINT_TRACKING", v)
    assert taint.taint_enabled() is True


# ───────────────────────── lattice ───────────────────────────────────────────

def test_combine_is_minimum():
    assert combine([TRUSTED, INTERNAL, UNTRUSTED]) == UNTRUSTED
    assert combine([TRUSTED, INTERNAL]) == INTERNAL


def test_combine_empty_is_trusted():
    assert combine([]) == TRUSTED          # a constant has no ancestors
    assert combine([None, None]) == TRUSTED


# ───────────────────────── source / sink classification ──────────────────────

@pytest.mark.parametrize("agent,tool,trust", [
    ("web-research-1", "web_search", UNTRUSTED),
    ("any", "fetch_page", UNTRUSTED),
    ("summarizer-1", "anything", UNTRUSTED),
    ("ml-services-1", "classify_submit_dataset", INTERNAL),
    (None, "read_text", INTERNAL),
])
def test_classify_source(agent, tool, trust):
    assert classify_source(agent, tool) == trust


@pytest.mark.parametrize("tool,sink", [
    ("send_email", True), ("send_message", True), ("post_update", True),
    ("transfer_funds", True), ("delete_user", True), ("write_file", True),
    ("execute_command", True), ("upload_file", True), ("webhook", True),
    ("http_post", True),
    # Read-only GET fetchers are NOT sinks (destination validated by
    # shared.external_http; their output stays UNTRUSTED as a source).
    ("fetch_page", False), ("web_search", False), ("summarize_url", False),
    ("read_text", False), ("classify_submit_dataset", False), ("search", False),
])
def test_is_sink(tool, sink):
    assert is_sink(None, tool) is sink


def test_read_only_fetchers_remain_untrusted_sources():
    """Dropping fetch_page/web_search from the sink list must not bleach
    their OUTPUT: they still introduce untrusted data."""
    for tool in ("fetch_page", "web_search", "summarize_url"):
        assert classify_source(None, tool) == UNTRUSTED


def test_search_then_fetch_is_not_denied():
    """The user-facing regression: web_search emits each result URL as a
    plain string leaf; fetch_page(<that url>) in the same chat must NOT be a
    sink check at all."""
    t = TaintTracker()
    url = "https://example.org/article"
    t.record_output([{"type": "text", "content": url}],
                    classify_source("web-research-1", "web_search"), TRUSTED)
    assert t.trust_of(url) == UNTRUSTED            # still tainted as data
    assert is_sink("web-research-1", "fetch_page") is False
    # ...but the same URL into a genuine sink is still refused.
    assert check_flow(t.effective_trust_of_args({"body": url})) == "deny"


# ───────────────────────── user-intent exemption ─────────────────────────────

def test_user_supplied_is_verbatim_substring():
    assert taint.user_supplied("https://a.example/x", "read https://a.example/x please")
    assert taint.user_supplied("  https://a.example/x ", "read https://a.example/x")
    assert not taint.user_supplied("https://a.example/x", "read https://A.example/x")
    assert not taint.user_supplied("https://a.example/x", "")
    assert not taint.user_supplied("https://a.example/x", None)
    assert not taint.user_supplied("   ", "anything")


def test_value_typed_by_user_is_trusted_for_the_check():
    t = TaintTracker()
    url = "https://example.org/article"
    t.mark(url, UNTRUSTED)
    # Without the user's text the tainted URL is untrusted.
    assert t.effective_trust_of_args({"to": url}) == UNTRUSTED
    # The user typed it verbatim this turn ⇒ user-supplied for this check.
    assert t.effective_trust_of_args({"to": url}, user_text=f"send {url} to bob") == TRUSTED
    # The exemption is per-value: a second tainted leaf the user did NOT type
    # still drags the call down.
    t.mark("leaked-secret", UNTRUSTED)
    assert t.effective_trust_of_args(
        {"to": url, "body": "leaked-secret"}, user_text=f"send {url}") == UNTRUSTED


def test_user_exemption_does_not_persist_in_the_store():
    t = TaintTracker()
    t.mark("evil", UNTRUSTED)
    t.effective_trust_of_args({"x": "evil"}, user_text="evil")
    assert t.trust_of("evil") == UNTRUSTED   # the stored label is untouched


# ───────────────────────── flow policy ───────────────────────────────────────

def test_check_flow():
    assert check_flow(UNTRUSTED) == "deny"
    assert check_flow(INTERNAL) == "escalate"
    assert check_flow(TRUSTED) == "allow"


# ───────────────────────── tracker basics ────────────────────────────────────

def test_unknown_value_is_trusted():
    assert TaintTracker().trust_of("never seen") == TRUSTED


def test_mark_and_trust_of():
    t = TaintTracker()
    t.mark("evil", UNTRUSTED)
    assert t.trust_of("evil") == UNTRUSTED
    # mark only ever lowers (min)
    t.mark("evil", TRUSTED)
    assert t.trust_of("evil") == UNTRUSTED


def test_fingerprint_is_stable_and_skips_empty():
    assert TaintTracker.fingerprint("  hi ") == TaintTracker.fingerprint("hi")
    assert TaintTracker.fingerprint("   ") == ""


def test_effective_trust_skips_system_keys():
    t = TaintTracker()
    t.mark("tainted", UNTRUSTED)
    # the tainted value lives under a system `_`-prefixed key ⇒ ignored
    assert t.effective_trust_of_args({"_credentials": "tainted", "q": "clean"}) == TRUSTED
    # but a real arg carrying it is caught
    assert t.effective_trust_of_args({"body": "tainted"}) == UNTRUSTED


def test_record_output_only_stores_nontrusted():
    t = TaintTracker()
    t.record_output([{"content": "clean"}], TRUSTED, TRUSTED)
    assert t.known() == []  # trusted outputs aren't remembered


# ───────────────────────── multi-hop laundering survival ─────────────────────

def test_laundering_through_an_internal_tool_survives():
    t = TaintTracker()

    # Hop 1: an untrusted web source emits data.
    src_a = classify_source("web-research-1", "web_search")
    out_a = t.record_output([{"type": "text", "content": "leak-xyz"}],
                            src_a, t.effective_trust_of_args({"q": "hello"}))
    assert out_a == UNTRUSTED
    assert t.trust_of("leak-xyz") == UNTRUSTED

    # Hop 2: an INTERNAL tool consumes that data and "launders" it into new text.
    src_b = classify_source("util-1", "format_text")   # internal
    inp_b = t.effective_trust_of_args({"text": "leak-xyz"})
    assert inp_b == UNTRUSTED
    out_b = t.record_output([{"content": "laundered-output"}], src_b, inp_b)
    assert out_b == UNTRUSTED                            # min(internal, untrusted)
    assert t.trust_of("laundered-output") == UNTRUSTED   # taint survived the hop

    # Sink: sending the laundered output is denied.
    assert is_sink(None, "send_email")
    assert check_flow(t.effective_trust_of_args({"body": "laundered-output"})) == "deny"


def test_clean_call_into_sink_is_allowed():
    t = TaintTracker()
    t.mark("leak-xyz", UNTRUSTED)
    # a sink call carrying only constants / user intent passes
    assert check_flow(t.effective_trust_of_args({"body": "hello there"})) == "allow"
