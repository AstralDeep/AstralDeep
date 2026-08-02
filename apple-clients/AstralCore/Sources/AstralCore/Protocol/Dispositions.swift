// Feature 051 — per-client protocol dispositions (the parity matrix, as code).
//
// Every push frame type and component type in the committed manifest
// (backend/shared/ui_protocol.json) MUST have an explicit disposition here
// for each Apple client (FR-003/FR-004/FR-037). The drift-guard test fails
// whenever the manifest gains/loses a name that this table does not account
// for — the same contract as the Windows and Android guards (FR-038).
//
// `.ignored` is a DELIBERATE, documented channel decision (044 precedent:
// admin tools, HTML-only chrome, web-only media stay web-only).
import Foundation

public enum FrameDisposition: Equatable, Sendable {
    case handled
    case ignored(String)  // reason — documentation, not dead weight
}

public enum ComponentDisposition: Equatable, Sendable {
    case native
    case fallback(String)  // rendered via the readable-text fallback; reason
}

public struct ClientDispositions: Sendable {
    public let client: String
    public let frames: [String: FrameDisposition]
    public let components: [String: ComponentDisposition]
    public let voiceActions: Set<String>

    public var nativeComponentTypes: [String] {
        components.compactMap { key, value in
            if case .native = value { return key }
            return nil
        }.sorted()
    }

    // MARK: shared vocabulary baselines

    /// Frames every Apple client handles the same way (the watch's reduce is
    /// the common core; iOS/macOS add the full-screen surfaces below). These
    /// tables mirror the ACTUAL reduce switch cases — a disposition claiming
    /// `handled` for a frame the code lets fall through `default:` is a lie
    /// the parity matrix would inherit.
    private static let commonHandled: [String] = [
        "agent_lifecycle",
        "auth_required", "chat_created", "chat_deleted", "chat_loaded", "chat_status",
        "chat_step", "composer_state", "conversation_commit_ready", "conversation_snapshot", "error",
        "operation_status", "stream_error", "ui_append", "ui_render",
        "ui_stream_data", "ui_update", "ui_upsert",
        "user_message_acked",
        "voice_announcement_media", "voice_control_binding", "voice_session_state",
        "voice_submission_rejected", "voice_transcript", "voice_turn_state",
    ]

    /// Deliberately ignored on every Apple client (044 channel decisions —
    /// same dispositions as the Android ProtocolManifest).
    private static let commonIgnored: [String: String] = [
        "agent_permissions": "acks for web workspace verbs; natives re-discover",
        "agent_permissions_updated": "acks for web workspace verbs; natives re-discover",
        "agent_registered": "agent lifecycle acks have no native surface (matches Android)",
        "agent_host_inventory_reconciled": "host-only inventory result; author-only clients ignore",
        "agent_host_registered": "structured host acknowledgement; Apple targets are author-only until feature 059",
        "agent_host_registration_refused": "host-only registration refusal; author-only clients ignore",
        // Feature 058 BYO host frames — only a HOSTING desktop acts on these; the
        // Apple clients are author-only (macOS hosting is deferred to feature 059),
        // so they ignore them, exactly as the Android ProtocolManifest does.
        "agent_bundle_deliver":
            "BYO code delivery — only a hosting desktop receives it; author-only clients ignore (matches Android)",
        "agent_offline": "BYO host-liveness signal — no native host surface; author-only (matches Android)",
        "agent_stop": "BYO host frame — Apple clients never host a user agent (matches Android)",
        "agent_tunnel":
            "BYO agent frames — relayed only by a hosting desktop; author-only clients ignore (matches Android)",
        "audit_append": "admin audit surface is web-only (044); natives fetch audit via REST",
        "chrome_render": "raw-HTML chrome region is web-only; natives use chrome_surface",
        "heartbeat": "keepalive; the transport layer answers (matches Windows/Android)",
        "llm_config_ack": "natives use the LLM chrome-surface round trip (044)",
        "llm_usage_report": "no native usage surface (044)",
        "rote_config": "natives are full-capability; profile info unused (044)",
        "stream_list": "no native stream-browser surface (matches Windows/Android)",
        "stream_unsubscribed": "terminal state arrives via ui_stream_data done flag",
        "system_config": "dashboard data; natives use agent_list (044)",
    ]

    private static func frames(
        extraHandled: [String],
        extraIgnored: [String: String]
    ) -> [String: FrameDisposition] {
        var table: [String: FrameDisposition] = [:]
        for name in commonHandled { table[name] = .handled }
        for (name, reason) in commonIgnored { table[name] = .ignored(reason) }
        for name in extraHandled { table[name] = .handled }
        for (name, reason) in extraIgnored { table[name] = .ignored(reason) }
        return table
    }

    // MARK: iOS (twin of Android — 041/044 dispositions)

    public static let ios = ClientDispositions(
        client: "ios",
        frames: frames(
            extraHandled: [
                "agent_list", "chrome_menu", "chrome_surface",
                "combine_error", "combine_status", "component_deleted",
                "component_save_error", "component_saved",
                "components_combined", "components_condensed", "history_list",
                "notification", "saved_components_list", "stream_data",
                "stream_subscribed", "task_completed", "task_started",
                "tool_progress", "user_preferences", "workspace_timeline_mode",
            ],
            extraIgnored: [
                "agent_creation_progress": "agentic-creation drafting UX is web-only for now (matches Android)"
            ]),
        components: fullComponentSet(fallbacks: [
            "audio": "web-only media, server degrade ladder (044 channel decision)",
            "generative": "web-only media (044 channel decision)",
        ]),
        voiceActions: Set(allVoiceControlActions))

    // MARK: macOS (twin of Windows — 044 dispositions)

    public static let macos = ClientDispositions(
        client: "macos",
        frames: ios.frames,  // identical frame surface to iOS by design
        components: fullComponentSet(fallbacks: [
            "audio": "web-only media, server degrade ladder (044 channel decision)",
            "generative": "web-only media (044 channel decision)",
        ]),
        voiceActions: Set(allVoiceControlActions))

    // MARK: watch (server pre-degrades via the `watch` ROTE profile)

    public static let watch = ClientDispositions(
        client: "watch",
        frames: frames(
            // 055 background-task continuity: a completion notification
            // reaches the wrist as a brief status line + spoken rendition.
            extraHandled: ["notification"],
            extraIgnored: [
                "agent_creation_progress": "no drafting UX on the wrist",
                "agent_list": "agent management happens on phone/desktop/web",
                "chrome_menu": "no chrome surfaces on the wrist",
                "chrome_surface": "no chrome surfaces on the wrist",
                "combine_error": "workspace verbs are larger-screen affordances (055 carve-out)",
                "combine_status": "workspace verbs are larger-screen affordances (055 carve-out)",
                "component_deleted": "workspace verbs are larger-screen affordances (055 carve-out)",
                "component_save_error": "workspace verbs are larger-screen affordances (055 carve-out)",
                "component_saved": "workspace verbs are larger-screen affordances (055 carve-out)",
                "components_combined": "workspace verbs are larger-screen affordances (055 carve-out)",
                "components_condensed": "workspace verbs are larger-screen affordances (055 carve-out)",
                "history_list": "recents come from REST (bounded list)",
                "saved_components_list": "workspace verbs are larger-screen affordances (055 carve-out)",
                "stream_data": "no live-stream nodes on the wrist",
                "stream_subscribed": "no live-stream nodes on the wrist",
                "task_completed": "async detachment is a larger-screen affordance",
                "task_started": "async detachment is a larger-screen affordance",
                "tool_progress": "chat_status text is the wrist progress channel",
                "user_preferences": "the wrist is system-styled (no live theming)",
                "workspace_timeline_mode": "timeline is a larger-screen surface",
            ]),
        components: watchComponentSet(),
        voiceActions: Set(allVoiceControlActions))

    // MARK: component tables

    /// The full 35-type vocabulary. iOS/macOS render everything natively
    /// except the listed fallbacks (renderer subset grows during US1/US2 —
    /// a type flips to .native only when its renderer lands and the parity
    /// row is verified).
    private static func fullComponentSet(fallbacks: [String: String]) -> [String: ComponentDisposition] {
        var table: [String: ComponentDisposition] = [:]
        for name in allComponentTypes { table[name] = .native }
        for (name, reason) in fallbacks { table[name] = .fallback(reason) }
        return table
    }

    /// Watch: the server has already degraded the payload (watch ROTE
    /// profile); the client natively renders the compact set the profile can
    /// emit and text-falls-back for anything else (FR-032/033).
    private static func watchComponentSet() -> [String: ComponentDisposition] {
        let native: Set<String> = [
            "alert", "badge", "card", "container", "divider", "keyvalue",
            "list", "metric", "progress", "text",
        ]
        var table: [String: ComponentDisposition] = [:]
        for name in allComponentTypes {
            if native.contains(name) {
                table[name] = .native
            } else {
                table[name] = .fallback("outside the watch profile; server degrades or client text-falls-back")
            }
        }
        return table
    }

    /// Mirror of the committed manifest vocabulary; the drift-guard test
    /// asserts this list — and every per-client table — matches
    /// backend/shared/ui_protocol.json exactly.
    public static let allComponentTypes: [String] = [
        "alert", "audio", "badge", "bar_chart", "button", "card",
        "chat_history", "code", "collapsible", "color_picker", "container",
        "divider", "download_card", "file_download", "file_upload",
        "generative", "grid", "hero", "image", "input", "keyvalue",
        "line_chart", "list", "metric", "param_picker", "pie_chart",
        "plotly_chart", "progress", "rating", "skeleton", "table", "tabs",
        "text", "theme_apply", "timeline",
    ]

    public static let allPushTypes: [String] = [
        "agent_bundle_deliver", "agent_creation_progress", "agent_host_inventory_reconciled",
        "agent_host_registered", "agent_host_registration_refused", "agent_lifecycle",
        "agent_list", "agent_offline", "agent_permissions",
        "agent_permissions_updated", "agent_registered", "agent_stop",
        "agent_tunnel", "audit_append",
        "auth_required", "chat_created", "chat_deleted", "chat_loaded",
        "chat_status", "chat_step", "chrome_menu", "chrome_render",
        "chrome_surface", "combine_error", "combine_status",
        "component_deleted", "component_save_error", "component_saved",
        "components_combined", "components_condensed", "composer_state", "conversation_commit_ready",
        "conversation_snapshot",
        "error", "heartbeat", "history_list", "llm_config_ack",
        "llm_usage_report", "notification", "operation_status",
        "rote_config", "saved_components_list", "stream_data", "stream_error",
        "stream_list", "stream_subscribed", "stream_unsubscribed",
        "system_config", "task_completed", "task_started", "tool_progress",
        "ui_append", "ui_render", "ui_stream_data", "ui_update", "ui_upsert",
        "user_message_acked", "user_preferences", "voice_announcement_media",
        "voice_control_binding", "voice_session_state", "voice_submission_rejected",
        "voice_transcript", "voice_turn_state", "workspace_timeline_mode",
    ]

    /// Feature 065 server-owned composer actions. Each Apple surface handles
    /// this exact vocabulary; controls that are not currently applicable are
    /// omitted or disabled by `composer_state`, never renamed by the client.
    public static let allVoiceControlActions: [String] = [
        "voice_microphone_set", "voice_sensitive_recap_request", "voice_session_end",
        "voice_session_start", "voice_session_takeover", "voice_speech_mute_set",
        "voice_speech_stop", "voice_visible_chat_update",
    ]
}
