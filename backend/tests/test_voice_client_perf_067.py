"""Source-level regression pins for AstralProjection's static client.js voice hot paths
(no build step or JS test runner): packet-size gating without re-encoding, a
lazy/idle-prefetched LiveKit SDK, and self-pruning media timers.
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


def test_decode_voice_packet_bounds_bytes_without_a_second_encode(client_js):
    fn = _js_function(client_js, "decodeVoicePacket")

    assert "payload.byteLength > maximum" in fn
    assert "new TextEncoder().encode(text)" not in fn
    assert fn.index("payload.byteLength > maximum") < fn.index("VOICE_TEXT_DECODER.decode")

    assert "var VOICE_TEXT_DECODER = new TextDecoder();" in client_js
    assert "var VOICE_TEXT_ENCODER = new TextEncoder();" in client_js
    assert "new TextDecoder()" not in fn
    assert "new TextEncoder()" not in fn

    assert "VOICE_TEXT_ENCODER.encode(payload).length > maximum" in fn


def test_pending_submission_budget_is_estimated_not_serialized(client_js):
    fn = _js_function(client_js, "retainFinalVoiceSubmission")

    assert "JSON.stringify" not in fn, (
        "serializing the submission to size it puts a full encode on the "
        "final-transcript -> submission path, inside the acknowledgement budget"
    )
    assert "new TextEncoder()" not in fn
    assert "copy.byte_length = 1024 + 6 * (" in fn
    for field in ("copy.text.length", "copy.source_participant_identity.length",
                  "copy.detected_language.length"):
        assert field in fn, f"{field} is unbounded in the estimate"
    assert "copy.byte_length > VOICE_MAX_PENDING_BYTES" in fn
    assert "voicePendingSubmissionBytes + copy.byte_length > VOICE_MAX_PENDING_BYTES" in fn


def test_submission_byte_estimate_is_an_upper_bound(client_js):
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
        "a" * 8000,
        "\u3053\u3093\u306b\u3061\u306f" * 1000,
        "\U0001f600" * 2000,
        "\x01\x02\x03" * 2000,
        '"quoted"\\back\n' * 500,
    ]
    # JS length counts UTF-16 units, not Python code points
    def utf16_len(value: str) -> int:
        return sum(2 if ord(ch) > 0xFFFF else 1 for ch in value)

    for text in samples:
        copy = dict(base, text=text)
        estimate = fixed + factor * (
            utf16_len(copy["text"])
            + utf16_len(copy["source_participant_identity"])
            + utf16_len(copy["detected_language"])
        )
        actual = len(
            json.dumps(copy, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        assert estimate >= actual, (
            f"estimate {estimate} under-counts actual {actual} for a "
            f"{len(text)}-char transcript — under-estimating is unsafe"
        )

    realistic = fixed + factor * (
        len("Please summarize the quarterly report for me.")
        + len(base["source_participant_identity"])
        + len(base["detected_language"])
    )
    assert realistic * 4 <= 48 * 1024


def test_livekit_bundle_is_not_loaded_from_the_shell(shell_html):
    assert len(LIVEKIT_BUNDLE.read_bytes()) > 500_000, "sanity: the bundle is still large"
    assert not re.search(
        r"<script[^>]+src=\"[^\"]*livekit", shell_html, re.IGNORECASE
    ), "the eager LiveKit <script> tag is back in the shell"


def test_shell_injects_the_versioned_livekit_url(shell_html):
    assert (
        'window.__ASTRAL_LIVEKIT_URL__ = "/static/vendor/livekit-client.umd.min.js'
        '?v=%%ASTRAL_V:vendor/livekit-client.umd.min.js%%"'
    ) in shell_html
    match = re.search(r"<script[^>]*>([^<]*__ASTRAL_TOKEN__[^<]*)</script>", shell_html)
    assert match is not None
    assert "__ASTRAL_LIVEKIT_URL__" in match.group(1)


def test_livekit_loader_mirrors_the_plotly_lazy_pattern(client_js):
    fn = _js_function(client_js, "ensureLiveKitSdk")
    assert "window.__ASTRAL_LIVEKIT_URL__" in fn
    assert 'document.createElement("script")' in fn
    assert "document.head.appendChild(s)" in fn
    assert "s.onload" in fn
    assert "s.onerror" in fn
    assert fn.count("flush") >= 3
    assert "livekitLoading = false" in fn


def test_livekit_is_idle_prefetched_alongside_plotly(client_js):
    fn = _js_function(client_js, "idlePrefetchVendorBundles")
    assert "ensureLiveKitSdk(null)" in fn
    assert "ensurePlotly(null)" in fn
    assert "window.requestIdleCallback(idlePrefetchVendorBundles" in client_js
    assert "setTimeout(idlePrefetchVendorBundles, 2500)" in client_js


def test_every_livekit_sdk_entry_point_is_gated_by_the_loader(client_js):
    router = _js_function(client_js, "routeVoiceBackendActivation")
    assert 'voiceSpeechBackend === "client_local"' in router
    assert "beginClientLocalActivation(kind, record.body)" in router
    assert "beginRemoteVoiceActivation(kind)" in router
    activation = _js_function(client_js, "beginRemoteVoiceActivation")
    assert "if (!livekitSdkReady() && sdkRetried !== true)" in activation
    assert "ensureLiveKitSdk(function () { beginRemoteVoiceActivation(kind, true); })" in activation
    assert activation.index("ensureLiveKitSdk") < activation.index("createVoiceRoomFromGesture()")

    recovery = _js_function(client_js, "performVoiceRecovery")
    assert "if (!livekitSdkReady()) {" in recovery
    assert "await new Promise(function (resolve) { ensureLiveKitSdk(resolve); });" in recovery
    assert recovery.index("ensureLiveKitSdk") < recovery.index("createVoiceRoom(false)")
    tail = recovery[recovery.index("ensureLiveKitSdk"):]
    assert "voiceRecovery !== recovery || recovery.epoch !== epoch" in tail


def test_no_livekit_sdk_read_escapes_the_gated_functions(client_js):
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

    clear = _js_function(client_js, "clearVoiceMediaTimers")
    assert "voiceMediaTimers.forEach(function (timer) { clearTimeout(timer); });" in clear
    assert "voiceMediaTimers.clear();" in clear


def test_finish_voice_track_prunes_both_playout_timers(client_js):
    fn = _js_function(client_js, "finishVoiceTrack")
    for timer in ("active.timeout", "active.tailTimer"):
        assert f"clearTimeout({timer});" in fn
        assert f"voiceMediaTimers.delete({timer});" in fn
        assert fn.index(f"clearTimeout({timer});") < fn.index(
            f"voiceMediaTimers.delete({timer});"
        )


def test_self_expiring_timers_remove_their_own_id(client_js):
    for name in ("expiry", "orphanSweep", "bindWatchdog"):
        assert f"voiceMediaTimers.delete({name});" in client_js
        assert f"voiceMediaTimers.add({name});" in client_js
        body = client_js[client_js.index(f"var {name} = setTimeout("):]
        assert body.index(f"voiceMediaTimers.delete({name});") < body.index(
            f"voiceMediaTimers.add({name});"
        )
