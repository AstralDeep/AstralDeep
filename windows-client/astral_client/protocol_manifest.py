"""Frame classification for the desktop client (feature 044).

Every server->client WS frame type in the committed UI-protocol manifest
(``backend/shared/ui_protocol.json``) is classified here as either

* ``"handled"`` — the client routes it in ``MainWindow._on_message``; or
* ``"ignored"`` — a deliberate, logged drop (the frame carries nothing the
  desktop presents natively; the parity matrix records why).

There is no third state. ``tests/test_protocol_manifest.py`` asserts this table
covers the manifest exactly, so a new server frame type fails the build until it
is classified — never a silent drop (FR-001/FR-002/FR-023).
"""

from __future__ import annotations

HANDLED = "handled"
IGNORED = "ignored"

#: ui_event actions handled entirely in-app (never sent for a server
#: ``chrome_surface`` re-render), so a surface's load-timeout bound must NOT be
#: armed for them. Committed mirror of ``backend/shared/ui_protocol.json``
#: ``client_local_actions`` — a packaged build has no repo tree to probe, so
#: the value lives here and ``tests/test_protocol_manifest.py`` asserts the two
#: stay in sync.
CLIENT_LOCAL_ACTIONS: frozenset[str] = frozenset({"attach_existing"})

CLASSIFICATION: dict[str, str] = {
    # bootstrap
    "rote_config": IGNORED,           # natives are full-capability; profile unused
    "chrome_menu": HANDLED,
    "user_preferences": HANDLED,      # theme boot (044)
    "system_config": IGNORED,         # web dashboard payload; desktop uses agent_list
    "agent_list": HANDLED,
    # 058: the registration ack is the ONLY signal that a BYO agent's child
    # process was accepted — a refusal is total silence (no NAK frame exists),
    # so the host times out on its absence. Not ignorable any more.
    "agent_registered": HANDLED,
    "agent_host_inventory_reconciled": HANDLED,
    "agent_host_registered": HANDLED,  # binds the server-issued host session
    "agent_host_registration_refused": HANDLED,
    # BYO client-side agents (058): this desktop IS the host. The four frames
    # below are HANDLED, never ignored — an ignored agent frame would silently
    # drop the user's own agent's traffic and look like a hang.
    "agent_bundle_deliver": HANDLED,  # write the bundle + spawn the child
    "agent_tunnel": HANDLED,          # inbound agent frame -> the child's stdin
    "agent_stop": HANDLED,            # terminate the child, drop routing
    "agent_offline": HANDLED,         # server dropped routing for one of ours
    # auth
    "auth_required": HANDLED,
    # canvas / SDUI
    "ui_render": HANDLED,
    "ui_update": IGNORED,             # legacy frame; server no longer targets natives
    "ui_upsert": HANDLED,
    "ui_append": IGNORED,             # legacy frame
    "ui_stream_data": HANDLED,
    # chrome
    "chrome_render": HANDLED,         # web HTML region push -> status notice only
    "chrome_surface": HANDLED,
    # chat lifecycle / progress
    "chat_status": HANDLED,
    "chat_step": HANDLED,
    "chat_created": HANDLED,
    "chat_loaded": HANDLED,
    "chat_deleted": IGNORED,          # cross-tab concern; desktop is single-window
    "history_list": HANDLED,
    "user_message_acked": HANDLED,
    "task_started": HANDLED,
    "task_completed": HANDLED,
    "tool_progress": HANDLED,
    "workspace_timeline_mode": HANDLED,
    # 060 canonical committed/status frames; reducers land in the matching
    # continuity and operation tasks, while transport validation is immediate.
    "conversation_commit_ready": HANDLED,
    "conversation_snapshot": HANDLED,
    "operation_status": HANDLED,
    "agent_lifecycle": HANDLED,
    # 065 server-owned conversational voice. Control frames are reduced by
    # MainWindow/VoiceController, while transcript and announcement manifests
    # are strictly reduced by VoiceController on the authenticated RTC channel.
    "composer_state": HANDLED,
    "voice_control_binding": HANDLED,
    "voice_session_state": HANDLED,
    "voice_turn_state": HANDLED,
    "voice_submission_rejected": HANDLED,
    "voice_transcript": HANDLED,
    "voice_announcement_media": HANDLED,
    "heartbeat": IGNORED,             # transport keepalive
    # streaming
    "stream_subscribed": HANDLED,
    "stream_unsubscribed": HANDLED,
    "stream_list": IGNORED,           # no desktop surface enumerates streams
    "stream_data": HANDLED,
    "stream_error": HANDLED,
    # workspace component verbs (055 US3: identity-keyed canvas reconcile +
    # status surfaces; previously web-only acks)
    "component_saved": HANDLED,
    "component_save_error": HANDLED,
    "saved_components_list": HANDLED,
    "component_deleted": HANDLED,
    "combine_status": HANDLED,
    "combine_error": HANDLED,
    "components_combined": HANDLED,
    "components_condensed": HANDLED,
    # permissions (capability lives in the native Agents dialog via agent_list)
    "agent_permissions": IGNORED,
    "agent_permissions_updated": IGNORED,
    # llm (desktop uses the LLM settings surface round-trip)
    "llm_config_ack": IGNORED,
    "llm_usage_report": IGNORED,
    # audit (desktop fetches audit via REST)
    "audit_append": IGNORED,
    # creation (draft cards carry state in-chat)
    "agent_creation_progress": IGNORED,
    # scheduler notifications + errors (044)
    "notification": HANDLED,
    "error": HANDLED,
}


def is_handled(frame_type: str) -> bool:
    return CLASSIFICATION.get(frame_type) == HANDLED


def is_classified(frame_type: str) -> bool:
    return frame_type in CLASSIFICATION
