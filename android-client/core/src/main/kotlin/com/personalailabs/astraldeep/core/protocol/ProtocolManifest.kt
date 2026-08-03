package com.personalailabs.astraldeep.core.protocol

/**
 * Frame classification for the Android client (feature 044).
 *
 * Every server->client WS frame type in the committed UI-protocol manifest
 * (`backend/shared/ui_protocol.json`) is classified as either [HANDLED] (the
 * reducer consumes it) or [IGNORED] (a deliberate, logged drop recorded in the
 * parity matrix). There is no third state: `ProtocolManifestTest` asserts this
 * table covers the manifest exactly, so a new server frame type fails the build
 * until it is classified — never a silent drop (FR-001/FR-002/FR-023).
 */
object ProtocolManifest {
    const val HANDLED = "handled"
    const val IGNORED = "ignored"

    val classification: Map<String, String> =
        mapOf(
            // bootstrap
            // rote_config: natives are full-capability; profile unused
            "rote_config" to IGNORED,
            "chrome_menu" to HANDLED,
            // user_preferences: theme boot (044)
            "user_preferences" to HANDLED,
            // system_config: web dashboard payload; app uses agent_list
            "system_config" to IGNORED,
            "agent_list" to HANDLED,
            "agent_registered" to IGNORED,
            // Android authors/manages agents but never advertises a desktop
            // runtime host, so server-issued host control frames are explicit drops.
            "agent_host_inventory_reconciled" to IGNORED,
            "agent_host_registered" to IGNORED,
            "agent_host_registration_refused" to IGNORED,
            // auth
            "auth_required" to HANDLED,
            // canvas / SDUI
            "ui_render" to HANDLED,
            // ui_update: legacy frame; server no longer targets natives
            "ui_update" to IGNORED,
            "ui_upsert" to HANDLED,
            // ui_append: legacy frame
            "ui_append" to IGNORED,
            "ui_stream_data" to HANDLED,
            // chrome
            // chrome_render: web HTML region push; native gets chrome_surface
            "chrome_render" to IGNORED,
            "chrome_surface" to HANDLED,
            // chat lifecycle / progress
            "chat_status" to HANDLED,
            "chat_step" to HANDLED,
            "chat_created" to HANDLED,
            "chat_loaded" to HANDLED,
            // chat_deleted: cross-tab concern; app is single-window
            "chat_deleted" to IGNORED,
            "history_list" to HANDLED,
            "user_message_acked" to HANDLED,
            "task_started" to HANDLED,
            "task_completed" to HANDLED,
            "tool_progress" to HANDLED,
            "workspace_timeline_mode" to HANDLED,
            // Feature 060 canonical committed/status frames. Strict transport
            // decoding lands here; feature reducers consume the typed variants.
            "conversation_snapshot" to HANDLED,
            "conversation_commit_ready" to HANDLED,
            "operation_status" to HANDLED,
            "agent_lifecycle" to HANDLED,
            // Feature 065 server-owned conversational voice contract. Every
            // direct-client push is strictly decoded before controller use.
            "composer_state" to HANDLED,
            "voice_control_binding" to HANDLED,
            "voice_session_state" to HANDLED,
            "voice_turn_state" to HANDLED,
            "voice_submission_rejected" to HANDLED,
            "voice_transcript" to HANDLED,
            "voice_announcement_media" to HANDLED,
            // heartbeat: transport keepalive
            "heartbeat" to IGNORED,
            // streaming
            "stream_subscribed" to HANDLED,
            "stream_unsubscribed" to HANDLED,
            // stream_list: no app surface enumerates streams
            "stream_list" to IGNORED,
            "stream_data" to HANDLED,
            "stream_error" to HANDLED,
            // workspace component verbs (055 US3: identity-keyed canvas
            // reconcile + status surfaces; previously web-only acks)
            "component_saved" to HANDLED,
            "component_save_error" to HANDLED,
            "saved_components_list" to HANDLED,
            "component_deleted" to HANDLED,
            "combine_status" to HANDLED,
            "combine_error" to HANDLED,
            "components_combined" to HANDLED,
            "components_condensed" to HANDLED,
            // permissions (capability lives in the native Agents screen)
            "agent_permissions" to IGNORED,
            "agent_permissions_updated" to IGNORED,
            // llm (app uses the LLM settings surface round-trip)
            "llm_config_ack" to IGNORED,
            "llm_usage_report" to IGNORED,
            // audit (app fetches audit via REST)
            "audit_append" to IGNORED,
            // creation (draft cards carry state in-chat)
            "agent_creation_progress" to IGNORED,
            // BYO client agents (058): the tunnel/bundle/stop frames are the DESKTOP
            // HOST's transport — Android authors and manages agents but never hosts
            // one, so a phone must never act on them. agent_offline is the host-liveness
            // notice; the authoring surface derives that state server-side today.
            "agent_tunnel" to IGNORED,
            "agent_bundle_deliver" to IGNORED,
            "agent_stop" to IGNORED,
            "agent_offline" to IGNORED,
            // scheduler notifications + errors (044)
            "notification" to HANDLED,
            "error" to HANDLED,
        )

    fun isHandled(frameType: String): Boolean = classification[frameType] == HANDLED

    fun isClassified(frameType: String): Boolean = frameType in classification
}
