"""Feature 067 — source-level regression pins for the web voice client's hot paths.

``components/AstralProjection/backend/webrender/static/client.js`` has no build step and no JS test runner,
so its runtime obligations are pinned here as text assertions over the shipped
source, in the same style as ``test_client_js_contract.py``.

Three defects are pinned:

* **V13** — ``decodeVoicePacket`` re-encoded every packet just to measure a size
  ``payload.byteLength`` already carried, and built two codec objects per packet;
  ``retainFinalVoiceSubmission`` ran ``JSON.stringify`` + a UTF-8 encode on the
  final-transcript -> submission path purely for a retention-budget check.
* **V14** — the 561 KB LiveKit SDK loaded eagerly on every page load for every
  user. It is now lazy + idle-prefetched, and the prefetch is load-bearing: lazy
  alone would trade a page-load win for an activation-budget regression.
* **V16** — ``voiceMediaTimers`` was append-only and grew ~4 entries per spoken
  announcement for the life of a session.
"""

from __future__ import annotations

import json
import re

import pytest
from astralprojection.resources import static_path, template_path, vendor_path

CLIENT_JS = static_path("client.js")
SHELL_HTML = template_path("shell.html")
LIVEKIT_BUNDLE = vendor_path("livekit-client.umd.min.js")


def _js_function(src: str, name: str) -> str:
    """Extract ``function <name>(...) {...}`` by brace counting."""
    sig = f"function {name}("
    assert sig in src, f"client.js has no function {name}"
    start = src.index(sig)
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces extracting function {name}")


@pytest.fixture(scope="module")
def client_js() -> str:
    src = CLIENT_JS.read_text(encoding="utf-8")
    assert len(src) > 500, "wrong file?"
    return src


@pytest.fixture(scope="module")
def shell_html() -> str:
    return SHELL_HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# V13 — no redundant codec work on the per-packet / per-submission hot paths
# ---------------------------------------------------------------------------

def test_decode_voice_packet_bounds_bytes_without_a_second_encode(client_js):
    """The size gate reads ``byteLength`` and refuses BEFORE decoding."""
    fn = _js_function(client_js, "decodeVoicePacket")

    # the binary branch never re-encodes to measure what it already knows
    assert "payload.byteLength > maximum" in fn
    assert "new TextEncoder().encode(text)" not in fn
    # …and an oversized packet is rejected before it is decoded at all
    assert fn.index("payload.byteLength > maximum") < fn.index("VOICE_TEXT_DECODER.decode")

    # codecs are constructed once at module scope, not per packet
    assert "var VOICE_TEXT_DECODER = new TextDecoder();" in client_js
    assert "var VOICE_TEXT_ENCODER = new TextEncoder();" in client_js
    assert "new TextDecoder()" not in fn
    assert "new TextEncoder()" not in fn

    # the string branch still measures exactly, since chars != bytes there
    assert "VOICE_TEXT_ENCODER.encode(payload).length > maximum" in fn


def test_pending_submission_budget_is_estimated_not_serialized(client_js):
    """The retention budget must not sit behind a JSON round trip."""
    fn = _js_function(client_js, "retainFinalVoiceSubmission")

    assert "JSON.stringify" not in fn, (
        "serializing the submission to size it puts a full encode on the "
        "final-transcript -> submission path, inside the acknowledgement budget"
    )
    assert "new TextEncoder()" not in fn
    assert "copy.byte_length = 1024 + 6 * (" in fn
    # every variable-length field is bounded, not just `text`
    for field in ("copy.text.length", "copy.source_participant_identity.length",
                  "copy.detected_language.length"):
        assert field in fn, f"{field} is unbounded in the estimate"
    # the estimate still gates admission the same way
    assert "copy.byte_length > VOICE_MAX_PENDING_BYTES" in fn
    assert "voicePendingSubmissionBytes + copy.byte_length > VOICE_MAX_PENDING_BYTES" in fn


def test_submission_byte_estimate_is_an_upper_bound(client_js):
    """Re-derive the shipped arithmetic and prove it never under-counts.

    An under-estimate would let the pending-submission budget be exceeded; an
    over-estimate merely refuses earlier, which is the safe direction. JSON
    escaping costs at most 6 UTF-8 bytes per UTF-16 code unit (a control
    character becomes ``\\uXXXX``), which is where the factor comes from.
    """
    match = re.search(
        r"copy\.byte_length = (\d+) \+ (\d+) \* \(", _js_function(client_js, "retainFinalVoiceSubmission")
    )
    assert match, "the byte estimate changed shape — re-derive this bound"
    fixed, factor = int(match.group(1)), int(match.group(2))

    base = {
        "session_id": "00000000-0000-4000-8000-000000000001",
        "generation": 3,
        "media_grant_revision": 2,
        "turn_id": "00000000-0000-4000-8000-000000000002",
        "client_turn_id": "00000000-0000-4000-8000-000000000003",
        "submission_id": "00000000-0000-4000-8000-000000000004",
        "request_generation": "00000000-0000-4000-8000-000000000005",
        "chat_id": "00000000-0000-4000-8000-000000000006",
        "chat_context_revision": 9,
        "source_participant_identity": "voice-worker-00000000-0000-4000-8000-000000000007",
        "detected_language": "en-US",
        "text_digest_sha256": "a" * 64,
        "transcript_proof": "b" * 64,
        "proof_expires_at": "2026-08-05T00:00:00Z",
        "text": "",
        "timer": None,
    }
    samples = [
        "",
        "Please summarize the quarterly report for me.",
        "a" * 8000,                     # the validated maximum transcript length
        "\u3053\u3093\u306b\u3061\u306f" * 1000,  # 3-byte UTF-8
        "\U0001f600" * 2000,            # surrogate pairs
        "\x01\x02\x03" * 2000,          # worst case: every char becomes \uXXXX
        '"quoted"\\back\n' * 500,       # escaped punctuation
    ]
    # JS String#length counts UTF-16 code units, which is what the shipped
    # expression multiplies; Python len() counts code points, so widen
    # astral-plane characters to match.
    def utf16_len(value: str) -> int:
        return sum(2 if ord(ch) > 0xFFFF else 1 for ch in value)

    for text in samples:
        copy = dict(base, text=text)
        estimate = fixed + factor * (
            utf16_len(copy["text"])
            + utf16_len(copy["source_participant_identity"])
            + utf16_len(copy["detected_language"])
        )
        # the exact quantity the pre-fix code measured: JSON.stringify + UTF-8
        # encode (ensure_ascii=False so Python matches JS's non-escaping of
        # printable non-ASCII)
        actual = len(
            json.dumps(copy, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        assert estimate >= actual, (
            f"estimate {estimate} under-counts actual {actual} for a "
            f"{len(text)}-char transcript — under-estimating is unsafe"
        )

    # …and it must not over-refuse ordinary speech: four realistic pending
    # submissions still have to fit the 48 KiB budget.
    realistic = fixed + factor * (
        len("Please summarize the quarterly report for me.")
        + len(base["source_participant_identity"])
        + len(base["detected_language"])
    )
    assert realistic * 4 <= 48 * 1024


# ---------------------------------------------------------------------------
# V14 — the LiveKit SDK is lazy AND idle-prefetched
# ---------------------------------------------------------------------------

def test_livekit_bundle_is_not_loaded_from_the_shell(shell_html):
    """No page load may pay for the SDK before the user asks for voice."""
    assert len(LIVEKIT_BUNDLE.read_bytes()) > 500_000, "sanity: the bundle is still large"
    assert not re.search(
        r"<script[^>]+src=\"[^\"]*livekit", shell_html, re.IGNORECASE
    ), "the eager LiveKit <script> tag is back in the shell"


def test_shell_injects_the_versioned_livekit_url(shell_html):
    """The loader cannot read the template, so the version stamp comes server-side."""
    assert (
        'window.__ASTRAL_LIVEKIT_URL__ = "/static/vendor/livekit-client.umd.min.js'
        '?v=%%ASTRAL_V:vendor/livekit-client.umd.min.js%%"'
    ) in shell_html
    # it rides the same inline bootstrap script as the other injected globals,
    # so it exists before client.js runs
    match = re.search(r"<script[^>]*>([^<]*__ASTRAL_TOKEN__[^<]*)</script>", shell_html)
    assert match is not None
    assert "__ASTRAL_LIVEKIT_URL__" in match.group(1)


def test_livekit_loader_mirrors_the_plotly_lazy_pattern(client_js):
    fn = _js_function(client_js, "ensureLiveKitSdk")
    assert "window.__ASTRAL_LIVEKIT_URL__" in fn
    assert 'document.createElement("script")' in fn
    assert "document.head.appendChild(s)" in fn
    assert "s.onload" in fn
    # a failed load must settle its waiters (they report media_unavailable)
    # rather than leaving voice activation hanging forever
    assert "s.onerror" in fn
    assert fn.count("flush") >= 3
    assert "livekitLoading = false" in fn


def test_livekit_is_idle_prefetched_alongside_plotly(client_js):
    """Lazy-only would move the cost from page load onto the activation budget."""
    fn = _js_function(client_js, "idlePrefetchVendorBundles")
    assert "ensureLiveKitSdk(null)" in fn
    assert "ensurePlotly(null)" in fn
    assert "window.requestIdleCallback(idlePrefetchVendorBundles" in client_js
    assert "setTimeout(idlePrefetchVendorBundles, 2500)" in client_js


def test_every_livekit_sdk_entry_point_is_gated_by_the_loader(client_js):
    """``createVoiceRoom`` is the only door to ``window.LivekitClient``; both of
    its callers must ensure the SDK is resident first."""
    activation = _js_function(client_js, "beginVoiceActivation")
    assert "if (!livekitSdkReady() && sdkRetried !== true)" in activation
    assert "ensureLiveKitSdk(function () { beginVoiceActivation(kind, true); })" in activation
    # the gate runs before the room is built
    assert activation.index("ensureLiveKitSdk") < activation.index("createVoiceRoomFromGesture()")

    recovery = _js_function(client_js, "performVoiceRecovery")
    assert "if (!livekitSdkReady()) {" in recovery
    assert "await new Promise(function (resolve) { ensureLiveKitSdk(resolve); });" in recovery
    assert recovery.index("ensureLiveKitSdk") < recovery.index("createVoiceRoom(false)")
    # recovery re-checks currency after the await, like every other suspension
    tail = recovery[recovery.index("ensureLiveKitSdk"):]
    assert "voiceRecovery !== recovery || recovery.epoch !== epoch" in tail


def test_no_livekit_sdk_read_escapes_the_gated_functions(client_js):
    """Every ``window.LivekitClient`` read must live in a function that is only
    reachable after ``ensureLiveKitSdk`` has run.

    ``createVoiceRoom`` is the single door: it builds the Room and wires the
    handlers, so ``roomEventName``/``configureVoiceSdkLogging`` (its callees),
    ``joinVoiceMedia`` (post-connect) and the two track consumers (room event
    handlers) are all downstream of it. A read anywhere else would run before
    the bundle exists and silently break voice.
    """
    gated = {
        name: _js_function(client_js, name)
        for name in (
            "livekitSdkReady",
            "roomEventName",
            "configureVoiceSdkLogging",
            "createVoiceRoom",
            "joinVoiceMedia",
            "consumeVoiceAudioTrack",
            "consumeVoicePublishedTrack",
        )
    }
    spans = []
    for body in gated.values():
        start = client_js.index(body)
        spans.append((start, start + len(body)))

    stray = []
    for match in re.finditer(r"window\.LivekitClient", client_js):
        # comments are prose, not executable reads
        line = client_js[client_js.rfind("\n", 0, match.start()) + 1 : match.start()]
        if line.lstrip().startswith("//"):
            continue
        if not any(lo <= match.start() < hi for lo, hi in spans):
            lineno = client_js.count("\n", 0, match.start()) + 1
            stray.append(lineno)
    assert not stray, (
        f"client.js:{stray} reads window.LivekitClient outside the loader-gated "
        "functions — it would run before the lazily injected bundle exists"
    )


# ---------------------------------------------------------------------------
# V16 — voiceMediaTimers is bounded
# ---------------------------------------------------------------------------

def test_voice_media_timers_is_a_set_that_is_pruned(client_js):
    assert "var voiceMediaTimers = new Set();" in client_js
    assert "voiceMediaTimers.push(" not in client_js, (
        "an append-only array grows ~4 entries per spoken announcement"
    )

    adds = client_js.count("voiceMediaTimers.add(")
    deletes = client_js.count("voiceMediaTimers.delete(")
    assert adds == 5, f"expected the five known timer sites, found {adds}"
    assert deletes == adds, (
        f"{adds} timers are registered but only {deletes} are pruned — every "
        "add needs a matching delete where the timer definitively fires or is cleared"
    )

    # teardown still clears whatever is genuinely outstanding
    clear = _js_function(client_js, "clearVoiceMediaTimers")
    assert "voiceMediaTimers.forEach(function (timer) { clearTimeout(timer); });" in clear
    assert "voiceMediaTimers.clear();" in clear


def test_finish_voice_track_prunes_both_playout_timers(client_js):
    """The two per-playout timers are cleared here, so they must be pruned here."""
    fn = _js_function(client_js, "finishVoiceTrack")
    for timer in ("active.timeout", "active.tailTimer"):
        assert f"clearTimeout({timer});" in fn
        assert f"voiceMediaTimers.delete({timer});" in fn
        assert fn.index(f"clearTimeout({timer});") < fn.index(
            f"voiceMediaTimers.delete({timer});"
        )


def test_self_expiring_timers_remove_their_own_id(client_js):
    """The three fire-and-forget timers drop themselves as their callback runs."""
    for name in ("expiry", "orphanSweep", "bindWatchdog"):
        assert f"voiceMediaTimers.delete({name});" in client_js
        assert f"voiceMediaTimers.add({name});" in client_js
        # the self-delete is the first statement, so an early return still prunes
        body = client_js[client_js.index(f"var {name} = setTimeout("):]
        assert body.index(f"voiceMediaTimers.delete({name});") < body.index(
            f"voiceMediaTimers.add({name});"
        )
