"""Feature-066 T032/FR-033 composer refusal-reason rendering pins.

The server was already honest on the wire; these pins hold the WEB client to
that honesty: every refusal reason in the server-owned ``VOICE_REASONS``
vocabulary renders as its own line (never the generic error fallback), and an
activation timeout while the browser permission prompt is pending reports a
permission-shaped reason instead of ``network_interrupted``.
"""

from __future__ import annotations

import re

from astralprojection.resources import static_path
from webrender.chrome.composer_model import VOICE_REASONS

CLIENT_JS = static_path("client.js").read_text(encoding="utf-8")

# ``ready`` is not a refusal; ``internal_error`` deliberately falls through to
# the generic error line because it has no more-specific honest wording.
_NO_DEDICATED_LINE = {"ready", "internal_error"}


def _reason_text_keys() -> set[str]:
    match = re.search(
        r"var VOICE_REASON_TEXT = Object\.freeze\(\{(.*?)\}\);",
        CLIENT_JS,
        re.DOTALL,
    )
    assert match, "client.js lost its VOICE_REASON_TEXT map"
    return set(re.findall(r"^\s{4}([a-z_0-9]+):", match.group(1), re.MULTILINE))


def test_every_server_refusal_reason_has_dedicated_composer_copy() -> None:
    keys = _reason_text_keys()
    missing = (set(VOICE_REASONS) - _NO_DEDICATED_LINE) - keys
    assert not missing, (
        "server VOICE_REASONS without composer copy (would render as the "
        f"generic error line): {sorted(missing)}"
    )


def test_reason_text_keys_stay_inside_the_server_vocabulary() -> None:
    keys = _reason_text_keys()
    unknown = keys - set(VOICE_REASONS)
    assert not unknown, (
        f"client.js invents reasons the server never sends: {sorted(unknown)}"
    )


def test_activation_timeout_reports_permission_shaped_reason() -> None:
    # The 30s activation timeout must branch on the pending permission prompt
    # (T032 defect b): permission_not_determined while the prompt is open,
    # network_interrupted otherwise.
    match = re.search(
        r"pending\.timeout = setTimeout\(function \(\) \{(.*?)\}, 30000\);",
        CLIENT_JS,
        re.DOTALL,
    )
    assert match, "client.js lost the activation timeout"
    body = match.group(1)
    assert "pending.awaiting_permission" in body
    assert '"permission_not_determined"' in body
    assert '"network_interrupted"' in body


def test_permission_wait_flag_brackets_microphone_acquisition() -> None:
    acquire = re.search(
        r"pending\.awaiting_permission = true;\s*\n\s*try \{\s*\n\s*"
        r"await acquireVoiceMicrophone\(\);",
        CLIENT_JS,
    )
    assert acquire, "microphone acquisition no longer sets awaiting_permission"
    assert CLIENT_JS.count("pending.awaiting_permission = false;") >= 2, (
        "awaiting_permission must clear on both the success and failure legs"
    )


class TestFirefoxDisclaimer:
    """Voice may not work in Firefox (WS refusal by extensions/proxies);
    the composer must say so — but only there, and only while voice is
    starting or failing, so the at-rest composer stays quiet (P11)."""

    def test_hint_exists_and_names_firefox(self) -> None:
        match = re.search(
            r'var VOICE_FIREFOX_HINT = ("[^"]*"\s*(?:\+\s*"[^"]*")*)', CLIENT_JS
        )
        assert match, "client.js lost the Firefox voice disclaimer"
        text = "".join(re.findall(r'"([^"]*)"', match.group(1)))
        assert "Firefox" in text
        assert "voice may not work" in text.lower()

    def test_detection_is_user_agent_gated(self) -> None:
        assert re.search(
            r"var VOICE_FIREFOX = /\\bFirefox\\//\.test\(navigator\.userAgent",
            CLIENT_JS,
        ), "Firefox detection lost"

    def test_hint_applies_only_to_starting_or_failing_states(self) -> None:
        match = re.search(
            r"var VOICE_FIREFOX_HINT_STATES = \{([^}]*)\}", CLIENT_JS
        )
        assert match, "client.js lost the Firefox hint state gate"
        states = set(re.findall(r"([a-z_]+):", match.group(1)))
        assert states == {"connecting", "reconnecting", "error", "unavailable"}
        # Never on a healthy or at-rest composer.
        assert "off" not in states and "listening" not in states

    def test_voice_message_appends_the_hint_once(self) -> None:
        body = re.search(
            r"function voiceMessage\(state, reason, message\) \{(.*?)\n  \}",
            CLIENT_JS,
            re.DOTALL,
        )
        assert body, "voiceMessage lost"
        assert "VOICE_FIREFOX && VOICE_FIREFOX_HINT_STATES[state]" in body.group(1)
        assert "indexOf(VOICE_FIREFOX_HINT) === -1" in body.group(1), (
            "the hint must not duplicate when a message already carries it"
        )
