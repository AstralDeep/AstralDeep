"""Feature-066 T032/FR-033 composer refusal-reason rendering pins.

The server was already honest on the wire; these pins hold the WEB client to
that honesty: every refusal reason in the server-owned ``VOICE_REASONS``
vocabulary renders as its own line (never the generic error fallback), and an
activation timeout while the browser permission prompt is pending reports a
permission-shaped reason instead of ``network_interrupted``.
"""

from __future__ import annotations

import re
from pathlib import Path

from webrender.chrome.composer_model import VOICE_REASONS

CLIENT_JS = (
    Path(__file__).resolve().parents[1] / "webrender" / "static" / "client.js"
).read_text(encoding="utf-8")

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
