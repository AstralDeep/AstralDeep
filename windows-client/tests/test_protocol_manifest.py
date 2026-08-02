"""Feature 044 — desktop protocol-coverage drift guard.

The committed manifest ``backend/shared/ui_protocol.json`` is the single source
of the server->client frame vocabulary. The desktop classification table must
cover it exactly: no unclassified frame, no stale entry. A new server frame
type therefore fails this suite until the desktop deliberately classifies it.
"""

import json
from pathlib import Path

from astral_client.protocol_manifest import (
    CLASSIFICATION,
    CLIENT_LOCAL_ACTIONS,
    HANDLED,
    IGNORED,
    is_classified,
    is_handled,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "backend" / "shared" / "ui_protocol.json"
ADMISSION_REFUSAL_CODES = [
    "capacity_exceeded",
    "registration_required",
    "registration_timeout",
    "idempotency_conflict",
    "connection_closing",
    "service_draining",
    "invalid_input",
    "registration_queue_full",
    "operation_failed",
]


def _manifest_push_types() -> set[str]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {entry["name"] for entry in data["push_types"]}


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_classification_covers_manifest_exactly():
    push = _manifest_push_types()
    classified = set(CLASSIFICATION)
    missing = sorted(push - classified)
    stale = sorted(classified - push)
    assert not missing, f"server frame types the desktop has not classified: {missing}"
    assert not stale, f"desktop classifies frame types the server no longer sends: {stale}"


def test_client_local_actions_matches_manifest():
    """The committed CLIENT_LOCAL_ACTIONS constant (a packaged build has no repo
    tree to probe at import time) must mirror the manifest's
    ``client_local_actions`` exactly."""
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert CLIENT_LOCAL_ACTIONS == frozenset(data["client_local_actions"])


def test_classification_values_are_valid():
    assert set(CLASSIFICATION.values()) <= {HANDLED, IGNORED}


def test_core_loop_frames_are_handled():
    for frame in (
        "ui_render", "ui_upsert", "chat_status", "error", "auth_required",
        "chrome_menu", "chrome_surface", "user_message_acked", "chat_step",
        "tool_progress", "task_started", "task_completed", "notification",
        "user_preferences", "workspace_timeline_mode", "conversation_snapshot",
        "operation_status", "agent_lifecycle", "agent_host_inventory_reconciled",
        "agent_host_registered", "agent_host_registration_refused",
    ):
        assert is_handled(frame), f"{frame} must be handled per the parity matrix"


def test_voice_frames_are_explicitly_handled_and_unknowns_remain_unclassified():
    voice_frames = {
        "composer_state",
        "voice_announcement_media",
        "voice_control_binding",
        "voice_session_state",
        "voice_submission_rejected",
        "voice_transcript",
        "voice_turn_state",
    }

    assert all(is_handled(frame) for frame in voice_frames)
    assert not is_classified("voice_unknown")


def test_manifest_declares_structured_host_registration():
    registrations = [
        entry
        for entry in _manifest()["additive_fields"]
        if entry.get("field") == "agent_host"
        and entry.get("carried_on") == ["register_ui"]
    ]
    assert len(registrations) == 1
    assert set(registrations[0]["shape"]) == {
        "host_id",
        "supported_runtime_contract_versions",
        "runtime_lock_sha256",
        "platform",
        "client_version",
    }


def test_manifest_declares_exact_admission_refusal_contract():
    assert _manifest()["frame_contracts"]["admission_refusal"] == {
        "type": "error",
        "exact_fields": [
            "type",
            "submission_id",
            "accepted",
            "code",
            "message",
            "retryable",
            "retry_after_ms",
        ],
        "submission_id": "canonical_lowercase_uuid4",
        "accepted": False,
        "additional_fields": False,
        "codes": ADMISSION_REFUSAL_CODES,
    }
