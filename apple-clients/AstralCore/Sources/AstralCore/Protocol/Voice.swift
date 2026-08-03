// Feature 065 — strict conversational-voice wire models shared by every Apple client.
//
// Audio is never represented here.  This file validates only the content-free
// control/manifest envelopes and builds the ordinary chat_message used for a
// final transcript.  Bearers and transcript proofs remain memory-only and are
// deliberately redacted from textual descriptions.
import Foundation

public let voiceTranscriptTopic = "astraldeep.voice.transcript.v1"
public let voiceAnnouncementTopic = "astraldeep.voice.announcement.v1"

public enum VoiceContractLimits {
    public static let transcriptPacketBytes = 12 * 1024
    public static let announcementPacketBytes = 4 * 1024
    public static let playoutPacketBytes = 2 * 1024
    public static let pendingFinalCount = 4
    public static let pendingFinalBytes = 48 * 1024
    public static let quantumSamples = 96_000
    public static let resultOpeningSamples = 36_000
    public static let resultAggregateSamples = 720_000
}

private let voiceUUID4Pattern =
    "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
private let voiceOpaquePattern = "^[A-Za-z0-9._:-]+$"
private let voiceLanguagePattern = "^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$"
private let voiceSHA256Pattern = "^[0-9a-f]{64}$"

private func voiceMatches(_ value: String, _ pattern: String) -> Bool {
    value.range(of: pattern, options: .regularExpression) != nil
}

private func voiceUUID4(_ value: JSONValue?) -> String? {
    guard let value = value?.stringValue, voiceMatches(value, voiceUUID4Pattern) else { return nil }
    return value
}

private func voiceOpaque(_ value: JSONValue?) -> String? {
    guard let value = value?.stringValue, value.count <= 128,
        voiceMatches(value, voiceOpaquePattern)
    else { return nil }
    return value
}

private func voiceTimestamp(_ value: JSONValue?) -> String? {
    guard let value = value?.stringValue else { return nil }
    return voiceTimestampDate(value) == nil ? nil : value
}

private func voiceTimestampDate(_ value: String) -> Date? {
    let plain = ISO8601DateFormatter()
    if let parsed = plain.date(from: value) { return parsed }
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return fractional.date(from: value)
}

/// 2^53 − 1, the largest exactly-representable JSON/JS integer — clamped to
/// this platform's `Int` so the arm64_32 watch (32-bit `Int`) compiles. A wire
/// value above `Int.max` cannot be represented there anyway, so the tighter
/// bound refuses exactly the values integer extraction would refuse.
private let maximumSafeWireInteger = Int(clamping: 9_007_199_254_740_991 as Int64)

private func voiceInteger(
    _ value: JSONValue?, minimum: Int = 0, maximum: Int = maximumSafeWireInteger
) -> Int? {
    guard let number = value?.numberValue, number.isFinite, number.rounded() == number,
        number >= Double(minimum), number <= Double(maximum)
    else { return nil }
    return Int(number)
}

private func voiceExact(
    _ object: [String: JSONValue], required: Set<String>, optional: Set<String> = []
) -> Bool {
    let keys = Set(object.keys)
    return required.isSubset(of: keys) && keys.isSubset(of: required.union(optional))
}

private func voiceNullableUUID(
    _ object: [String: JSONValue], _ key: String
) -> (valid: Bool, value: String?) {
    guard let raw = object[key] else { return (false, nil) }
    if raw == .null { return (true, nil) }
    guard let value = voiceUUID4(raw) else { return (false, nil) }
    return (true, value)
}

private func voiceNullablePositiveInteger(
    _ object: [String: JSONValue], _ key: String
) -> (valid: Bool, value: Int?) {
    guard let raw = object[key] else { return (false, nil) }
    if raw == .null { return (true, nil) }
    guard let value = voiceInteger(raw, minimum: 1) else { return (false, nil) }
    return (true, value)
}

private func voiceNullableTimestamp(
    _ object: [String: JSONValue], _ key: String
) -> (valid: Bool, value: String?) {
    guard let raw = object[key] else { return (false, nil) }
    if raw == .null { return (true, nil) }
    guard let value = voiceTimestamp(raw) else { return (false, nil) }
    return (true, value)
}

private func voiceOptionalString(
    _ object: [String: JSONValue], _ key: String, maximum: Int
) -> (valid: Bool, value: String?) {
    guard let raw = object[key] else { return (true, nil) }
    guard let value = raw.stringValue, value.count <= maximum else { return (false, nil) }
    return (true, value)
}

private func voiceLanguage(_ value: JSONValue?) -> String? {
    guard let value = value?.stringValue, voiceMatches(value, voiceLanguagePattern) else { return nil }
    return value
}

public enum VoiceState: String, Sendable, CaseIterable {
    case off, unavailable, connecting, greeting, listening
    case speechDetected = "speech_detected"
    case transcribing, acknowledging, processing
    case waitingOnUser = "waiting_on_user"
    case speakingProgress = "speaking_progress"
    case speakingResult = "speaking_result"
    case muted, suspended, reconnecting, error, ended
}

public enum VoiceReason: String, Sendable, CaseIterable {
    case ready
    case featureDisabled = "feature_disabled"
    case authenticationRequired = "authentication_required"
    case permissionNotDetermined = "permission_not_determined"
    case permissionDenied = "permission_denied"
    case permissionRestricted = "permission_restricted"
    case noMicrophone = "no_microphone"
    case noAudioOutput = "no_audio_output"
    case mediaUnavailable = "media_unavailable"
    case workerUnavailable = "worker_unavailable"
    case asrUnavailable = "asr_unavailable"
    case ttsUnavailable = "tts_unavailable"
    case voiceUnavailable = "voice_unavailable"
    case outputLanguageUnsupported = "output_language_unsupported"
    case capacityExhausted = "capacity_exhausted"
    case takeoverRequired = "takeover_required"
    case idleExpired = "idle_expired"
    case backgrounded
    case audioInterrupted = "audio_interrupted"
    case chatContextUnavailable = "chat_context_unavailable"
    case authExpired = "auth_expired"
    case networkInterrupted = "network_interrupted"
    case mediaError = "media_error"
    case speechError = "speech_error"
    case staleGeneration = "stale_generation"
    case endedByUser = "ended_by_user"
    case internalError = "internal_error"
}

public enum VoiceControlAction: String, Sendable, CaseIterable {
    case start = "voice_session_start"
    case takeover = "voice_session_takeover"
    case end = "voice_session_end"
    case microphone = "voice_microphone_set"
    case stopSpeech = "voice_speech_stop"
    case muteSpeech = "voice_speech_mute_set"
    case visibleChat = "voice_visible_chat_update"
    case sensitiveRecap = "voice_sensitive_recap_request"
}

public enum VoiceTransport: String, Sendable {
    case liveKit = "livekit"
    case watchPCMWebSocket = "watch_pcm_websocket"
}

public enum VoiceSpeechOutcome: String, Sendable, CaseIterable {
    case sourceFinished = "source_finished"
    case failed
    case suppressed
}

public struct VoiceControl: Sendable, Equatable, Identifiable {
    public let key: String
    public let action: VoiceControlAction
    public let label: String
    public let icon: String
    public let visible: Bool
    public let enabled: Bool
    public let pressed: Bool
    public let busy: Bool
    public var id: String { key }

    fileprivate init?(json: JSONValue) {
        guard let object = json.objectValue,
            voiceExact(
                object,
                required: ["key", "action", "label", "icon", "visible", "enabled", "pressed", "busy"]),
            let key = voiceOpaque(object["key"]),
            let actionValue = object["action"]?.stringValue,
            let action = VoiceControlAction(rawValue: actionValue),
            let label = object["label"]?.stringValue, !label.isEmpty, label.count <= 80,
            let icon = object["icon"]?.stringValue, !icon.isEmpty, icon.count <= 64,
            let visible = object["visible"]?.boolValue,
            let enabled = object["enabled"]?.boolValue,
            let pressed = object["pressed"]?.boolValue,
            let busy = object["busy"]?.boolValue
        else { return nil }
        self.key = key
        self.action = action
        self.label = label
        self.icon = icon
        self.visible = visible
        self.enabled = enabled
        self.pressed = pressed
        self.busy = busy
    }
}

public struct VoiceOwnerDevice: Sendable, Equatable {
    public let deviceId: String
    public let deviceKind: String
    public let deviceLabel: String?
    public let generation: Int

    fileprivate init?(json: JSONValue) {
        guard let object = json.objectValue,
            voiceExact(
                object, required: ["device_id", "device_kind", "generation"],
                optional: ["device_label"]),
            let deviceId = voiceUUID4(object["device_id"]),
            let deviceKind = object["device_kind"]?.stringValue,
            ["web", "windows", "android", "ios", "macos", "watchos"].contains(deviceKind),
            let generation = voiceInteger(object["generation"], minimum: 1),
            voiceOptionalString(object, "device_label", maximum: 80).valid
        else { return nil }
        self.deviceId = deviceId
        self.deviceKind = deviceKind
        self.deviceLabel = voiceOptionalString(object, "device_label", maximum: 80).value
        self.generation = generation
    }
}

public struct VoiceComposerModel: Sendable, Equatable {
    public let available: Bool
    public let state: VoiceState
    public let speechMuted: Bool
    public let microphoneEnabled: Bool
    public let foregroundActive: Bool
    public let reason: VoiceReason
    public let outputLocale: String
    public let message: String?
    public let chatContextRevision: Int?
    public let appliedChatContextRevision: Int?
    public let chatContextSynced: Bool
    public let sessionId: String?
    public let generation: Int?
    public let mediaGrantRevision: Int?
    public let visibleChatId: String?
    public let foregroundTurnId: String?
    public let ownerDevice: VoiceOwnerDevice?
    public let idleExpiresAt: String?
    public let controls: [VoiceControl]

    fileprivate init?(json: JSONValue) {
        guard let object = json.objectValue else { return nil }
        let required: Set<String> = [
            "available", "state", "speech_muted", "microphone_enabled", "foreground_active",
            "reason", "output_locale", "chat_context_revision", "applied_chat_context_revision",
            "chat_context_synced", "controls",
        ]
        let optional: Set<String> = [
            "message", "session_id", "generation", "media_grant_revision", "visible_chat_id",
            "foreground_turn_id", "owner_device", "idle_expires_at",
        ]
        guard voiceExact(object, required: required, optional: optional),
            let available = object["available"]?.boolValue,
            let rawState = object["state"]?.stringValue, let state = VoiceState(rawValue: rawState),
            let speechMuted = object["speech_muted"]?.boolValue,
            let microphoneEnabled = object["microphone_enabled"]?.boolValue,
            let foregroundActive = object["foreground_active"]?.boolValue,
            let rawReason = object["reason"]?.stringValue, let reason = VoiceReason(rawValue: rawReason),
            object["output_locale"]?.stringValue == "en-US",
            let context = OptionalPositiveInteger.parse(object, "chat_context_revision"),
            let applied = OptionalPositiveInteger.parse(object, "applied_chat_context_revision"),
            let contextSynced = object["chat_context_synced"]?.boolValue,
            voiceOptionalString(object, "message", maximum: 240).valid,
            let session = OptionalUUID.optionalParse(object, "session_id"),
            let generation = OptionalPositiveInteger.optionalParse(object, "generation"),
            let grantRevision = OptionalPositiveInteger.optionalParse(object, "media_grant_revision"),
            let visibleChat = OptionalUUID.optionalParse(object, "visible_chat_id"),
            let foregroundTurn = OptionalUUID.optionalParse(object, "foreground_turn_id"),
            let idleExpiry = OptionalTimestamp.optionalParse(object, "idle_expires_at"),
            let owner = OptionalOwner.optionalParse(object, "owner_device"),
            let controlsJSON = object["controls"]?.arrayValue,
            !controlsJSON.isEmpty, controlsJSON.count <= 12
        else { return nil }
        let controls = controlsJSON.compactMap(VoiceControl.init(json:))
        guard controls.count == controlsJSON.count else { return nil }
        let inactive: Set<VoiceState> = [
            .off, .unavailable, .suspended, .reconnecting, .error, .ended,
        ]
        let active: Set<VoiceState> = [
            .connecting, .greeting, .listening, .speechDetected, .transcribing,
            .acknowledging, .processing, .waitingOnUser, .speakingProgress,
            .speakingResult, .muted, .reconnecting, .error,
        ]
        guard foregroundActive ? active.contains(state) : (!microphoneEnabled && inactive.contains(state))
        else { return nil }

        self.available = available
        self.state = state
        self.speechMuted = speechMuted
        self.microphoneEnabled = microphoneEnabled
        self.foregroundActive = foregroundActive
        self.reason = reason
        self.outputLocale = "en-US"
        self.message = voiceOptionalString(object, "message", maximum: 240).value
        self.chatContextRevision = context
        self.appliedChatContextRevision = applied
        self.chatContextSynced = contextSynced
        self.sessionId = session
        self.generation = generation
        self.mediaGrantRevision = grantRevision
        self.visibleChatId = visibleChat
        self.foregroundTurnId = foregroundTurn
        self.ownerDevice = owner
        self.idleExpiresAt = idleExpiry
        self.controls = controls
    }
}

// Tiny wrappers keep explicit-null distinct from an absent optional key.
private enum OptionalUUID {
    static func parse(_ object: [String: JSONValue], _ key: String) -> String?? {
        let parsed = voiceNullableUUID(object, key)
        return parsed.valid ? .some(parsed.value) : nil
    }
    static func optionalParse(_ object: [String: JSONValue], _ key: String) -> String?? {
        guard object[key] != nil else { return .some(nil) }
        return parse(object, key)
    }
}

private enum OptionalPositiveInteger {
    static func parse(_ object: [String: JSONValue], _ key: String) -> Int?? {
        let parsed = voiceNullablePositiveInteger(object, key)
        return parsed.valid ? .some(parsed.value) : nil
    }
    static func optionalParse(_ object: [String: JSONValue], _ key: String) -> Int?? {
        guard object[key] != nil else { return .some(nil) }
        return parse(object, key)
    }
}

private enum OptionalTimestamp {
    static func optionalParse(_ object: [String: JSONValue], _ key: String) -> String?? {
        guard object[key] != nil else { return .some(nil) }
        let parsed = voiceNullableTimestamp(object, key)
        return parsed.valid ? .some(parsed.value) : nil
    }
}

private enum OptionalOwner {
    static func optionalParse(_ object: [String: JSONValue], _ key: String) -> VoiceOwnerDevice?? {
        guard let raw = object[key] else { return .some(nil) }
        if raw == .null { return .some(nil) }
        guard let owner = VoiceOwnerDevice(json: raw) else { return nil }
        return .some(owner)
    }
}

public struct VoiceComposerState: Sendable, Equatable {
    public let revision: Int
    public let connectionGeneration: String
    public let voice: VoiceComposerModel

    public init?(frame: InboundFrame) {
        guard frame.name == "composer_state", let object = frame.payload.objectValue,
            voiceExact(
                object,
                required: ["type", "schema_version", "revision", "connection_generation", "voice"]),
            object["type"]?.stringValue == "composer_state",
            object["schema_version"]?.stringValue == "1",
            let revision = voiceInteger(object["revision"]),
            let connection = voiceUUID4(object["connection_generation"]),
            let voice = object["voice"].flatMap(VoiceComposerModel.init(json:))
        else { return nil }
        self.revision = revision
        self.connectionGeneration = connection
        self.voice = voice
    }
}

public struct VoiceControlBinding: Sendable, Equatable, CustomStringConvertible {
    public let deviceId: String
    public let connectionGeneration: String
    public let bindingId: String
    public let binding: String
    public let expiresAt: String

    public var description: String {
        "VoiceControlBinding(deviceId=\(deviceId), connectionGeneration=\(connectionGeneration), bindingId=\(bindingId), binding=[REDACTED], expiresAt=\(expiresAt))"
    }

    public init?(frame: InboundFrame) {
        guard frame.name == "voice_control_binding", let object = frame.payload.objectValue,
            voiceExact(
                object,
                required: [
                    "type", "schema_version", "device_id", "connection_generation", "binding_id",
                    "binding", "expires_at",
                ]),
            object["schema_version"]?.stringValue == "1",
            let device = voiceUUID4(object["device_id"]),
            let connection = voiceUUID4(object["connection_generation"]),
            let bindingId = voiceUUID4(object["binding_id"]),
            let binding = object["binding"]?.stringValue, (32...512).contains(binding.count),
            let expiresAt = voiceTimestamp(object["expires_at"])
        else { return nil }
        self.deviceId = device
        self.connectionGeneration = connection
        self.bindingId = bindingId
        self.binding = binding
        self.expiresAt = expiresAt
    }
}

public struct VoiceSessionState: Sendable, Equatable {
    public let sessionId: String
    public let connectionGeneration: String
    public let generation: Int
    public let mediaGrantRevision: Int
    public let visibleChatId: String
    public let chatContextRevision: Int
    public let appliedChatContextRevision: Int?
    public let chatContextSynced: Bool
    public let state: VoiceState
    public let speechMuted: Bool
    public let microphoneEnabled: Bool
    public let foregroundActive: Bool
    public let reason: VoiceReason
    public let message: String?
    public let occurredAt: String

    public init?(frame: InboundFrame) {
        guard frame.name == "voice_session_state", let object = frame.payload.objectValue else { return nil }
        let required: Set<String> = [
            "type", "schema_version", "session_id", "connection_generation", "generation",
            "media_grant_revision", "visible_chat_id", "chat_context_revision",
            "applied_chat_context_revision", "chat_context_synced", "state", "speech_muted",
            "microphone_enabled", "foreground_active", "reason", "occurred_at",
        ]
        guard voiceExact(object, required: required, optional: ["message"]),
            object["schema_version"]?.stringValue == "1",
            let session = voiceUUID4(object["session_id"]),
            let connection = voiceUUID4(object["connection_generation"]),
            let generation = voiceInteger(object["generation"], minimum: 1),
            let grantRevision = voiceInteger(object["media_grant_revision"], minimum: 1),
            let visibleChat = voiceUUID4(object["visible_chat_id"]),
            let contextRevision = voiceInteger(object["chat_context_revision"], minimum: 1),
            let applied = OptionalPositiveInteger.parse(object, "applied_chat_context_revision"),
            let contextSynced = object["chat_context_synced"]?.boolValue,
            let stateValue = object["state"]?.stringValue, let state = VoiceState(rawValue: stateValue),
            let muted = object["speech_muted"]?.boolValue,
            let microphone = object["microphone_enabled"]?.boolValue,
            let foreground = object["foreground_active"]?.boolValue,
            let reasonValue = object["reason"]?.stringValue, let reason = VoiceReason(rawValue: reasonValue),
            voiceOptionalString(object, "message", maximum: 240).valid,
            let occurred = voiceTimestamp(object["occurred_at"])
        else { return nil }
        let inactive: Set<VoiceState> = [.suspended, .reconnecting, .error, .ended]
        let active: Set<VoiceState> = [
            .connecting, .greeting, .listening, .speechDetected, .transcribing,
            .acknowledging, .processing, .waitingOnUser, .speakingProgress,
            .speakingResult, .muted, .reconnecting, .error,
        ]
        guard foreground ? active.contains(state) : (!microphone && inactive.contains(state)) else { return nil }
        self.sessionId = session
        self.connectionGeneration = connection
        self.generation = generation
        self.mediaGrantRevision = grantRevision
        self.visibleChatId = visibleChat
        self.chatContextRevision = contextRevision
        self.appliedChatContextRevision = applied
        self.chatContextSynced = contextSynced
        self.state = state
        self.speechMuted = muted
        self.microphoneEnabled = microphone
        self.foregroundActive = foreground
        self.reason = reason
        self.message = voiceOptionalString(object, "message", maximum: 240).value
        self.occurredAt = occurred
    }
}

public struct VoiceTurnState: Sendable, Equatable {
    public let sessionId: String
    public let connectionGeneration: String
    public let generation: Int
    public let mediaGrantRevision: Int
    public let turnId: String
    public let clientTurnId: String
    public let submissionId: String
    public let requestGeneration: String
    public let chatId: String
    public let chatContextRevision: Int
    public let detectedLanguage: String?
    public let spokenOutputPolicy: String
    public let outputReason: String
    public let state: String
    public let foreground: Bool
    public let sensitiveResultPending: Bool
    public let sequence: Int
    public let speechOutcome: VoiceSpeechOutcome?
    public let resultId: String?
    public let message: String?
    public let occurredAt: String

    public init?(frame: InboundFrame) {
        guard frame.name == "voice_turn_state", let object = frame.payload.objectValue else { return nil }
        let required: Set<String> = [
            "type", "schema_version", "session_id", "connection_generation", "generation",
            "media_grant_revision", "turn_id", "client_turn_id", "submission_id",
            "request_generation", "chat_id", "chat_context_revision", "detected_language",
            "spoken_output_policy", "output_reason", "state", "foreground",
            "sensitive_result_pending", "sequence", "occurred_at",
        ]
        guard
            voiceExact(
                object, required: required,
                optional: ["result_id", "message", "speech_outcome"]),
            object["schema_version"]?.stringValue == "1",
            let session = voiceUUID4(object["session_id"]),
            let connection = voiceUUID4(object["connection_generation"]),
            let generation = voiceInteger(object["generation"], minimum: 1),
            let grant = voiceInteger(object["media_grant_revision"], minimum: 1),
            let turn = voiceUUID4(object["turn_id"]),
            let clientTurn = voiceUUID4(object["client_turn_id"]),
            let submission = voiceUUID4(object["submission_id"]),
            let request = voiceUUID4(object["request_generation"]),
            let chat = voiceUUID4(object["chat_id"]),
            let context = voiceInteger(object["chat_context_revision"], minimum: 1),
            let language = VoiceNullableLanguage.parse(object["detected_language"]),
            let policy = object["spoken_output_policy"]?.stringValue,
            ["pending", "full_recap", "english_lifecycle_only"].contains(policy),
            let outputReason = object["output_reason"]?.stringValue,
            ["language_pending", "ready", "output_language_unsupported"].contains(outputReason),
            let state = object["state"]?.stringValue,
            [
                "recognizing", "submitting", "accepted", "processing", "waiting_on_user",
                "succeeded", "failed", "refused", "cancelled", "abandoned",
            ].contains(state),
            let foreground = object["foreground"]?.boolValue,
            let sensitive = object["sensitive_result_pending"]?.boolValue,
            let sequence = voiceInteger(object["sequence"]),
            let resultId = OptionalOpaque.optionalParse(object, "result_id"),
            voiceOptionalString(object, "message", maximum: 240).valid,
            let occurred = voiceTimestamp(object["occurred_at"])
        else { return nil }
        let english = language.map { $0 == "en" || $0.hasPrefix("en-") } ?? false
        switch language {
        case nil where policy != "pending" || outputReason != "language_pending": return nil
        case _? where english && (policy != "full_recap" || outputReason != "ready"):
            return nil
        case _?
        where !english
            && (policy != "english_lifecycle_only" || outputReason != "output_language_unsupported"):
            return nil
        default: break
        }
        if state == "recognizing" && language != nil { return nil }
        if state != "recognizing" && state != "abandoned" && language == nil { return nil }
        let speechOutcome: VoiceSpeechOutcome?
        if let rawOutcome = object["speech_outcome"] {
            guard let rawValue = rawOutcome.stringValue,
                let parsed = VoiceSpeechOutcome(rawValue: rawValue), state == "succeeded"
            else { return nil }
            speechOutcome = parsed
        } else {
            speechOutcome = nil
        }
        self.sessionId = session
        self.connectionGeneration = connection
        self.generation = generation
        self.mediaGrantRevision = grant
        self.turnId = turn
        self.clientTurnId = clientTurn
        self.submissionId = submission
        self.requestGeneration = request
        self.chatId = chat
        self.chatContextRevision = context
        self.detectedLanguage = language
        self.spokenOutputPolicy = policy
        self.outputReason = outputReason
        self.state = state
        self.foreground = foreground
        self.sensitiveResultPending = sensitive
        self.sequence = sequence
        self.speechOutcome = speechOutcome
        self.resultId = resultId
        self.message = voiceOptionalString(object, "message", maximum: 240).value
        self.occurredAt = occurred
    }
}

private enum VoiceNullableLanguage {
    static func parse(_ raw: JSONValue?) -> String?? {
        guard let raw else { return nil }
        if raw == .null { return .some(nil) }
        guard let value = voiceLanguage(raw) else { return nil }
        return .some(value)
    }
}

private enum OptionalOpaque {
    static func optionalParse(_ object: [String: JSONValue], _ key: String) -> String?? {
        guard let raw = object[key] else { return .some(nil) }
        if raw == .null { return .some(nil) }
        guard let value = voiceOpaque(raw) else { return nil }
        return .some(value)
    }
}

public struct VoiceSubmissionRejected: Sendable, Equatable {
    public let sessionId: String
    public let connectionGeneration: String
    public let generation: Int
    public let mediaGrantRevision: Int
    public let turnId: String
    public let clientTurnId: String
    public let submissionId: String
    public let requestGeneration: String
    public let chatId: String
    public let reason: String
    public let retryPolicy: String
    public let message: String?
    public let occurredAt: String

    public init?(frame: InboundFrame) {
        guard frame.name == "voice_submission_rejected", let object = frame.payload.objectValue else { return nil }
        let required: Set<String> = [
            "type", "schema_version", "session_id", "connection_generation", "generation",
            "media_grant_revision", "turn_id", "client_turn_id", "submission_id",
            "request_generation", "chat_id", "reason", "retry_policy", "occurred_at",
        ]
        guard voiceExact(object, required: required, optional: ["message"]),
            object["schema_version"]?.stringValue == "1",
            let session = voiceUUID4(object["session_id"]),
            let connection = voiceUUID4(object["connection_generation"]),
            let generation = voiceInteger(object["generation"], minimum: 1),
            let grant = voiceInteger(object["media_grant_revision"], minimum: 1),
            let turn = voiceUUID4(object["turn_id"]),
            let clientTurn = voiceUUID4(object["client_turn_id"]),
            let submission = voiceUUID4(object["submission_id"]),
            let request = voiceUUID4(object["request_generation"]),
            let chat = voiceUUID4(object["chat_id"]),
            let reason = object["reason"]?.stringValue,
            [
                "capacity_exhausted", "chat_unavailable", "invalid_binding", "invalid_proof",
                "proof_expired", "permission_denied", "stale_session", "malformed_final",
            ].contains(reason),
            let retry = object["retry_policy"]?.stringValue,
            ["explicit_user_retry", "none"].contains(retry),
            voiceOptionalString(object, "message", maximum: 240).valid,
            let occurred = voiceTimestamp(object["occurred_at"])
        else { return nil }
        self.sessionId = session
        self.connectionGeneration = connection
        self.generation = generation
        self.mediaGrantRevision = grant
        self.turnId = turn
        self.clientTurnId = clientTurn
        self.submissionId = submission
        self.requestGeneration = request
        self.chatId = chat
        self.reason = reason
        self.retryPolicy = retry
        self.message = voiceOptionalString(object, "message", maximum: 240).value
        self.occurredAt = occurred
    }
}

/// A durable, text-first explanation for the last terminal voice request.
///
/// The notice deliberately carries an explicit symbol-independent title. The
/// clients may tint it with their theme, but color is never the only signal.
/// `serverMessage` is a validated, bounded wire value and remains plain text.
public struct VoiceTerminalNotice: Sendable, Equatable, Identifiable {
    public enum Kind: String, Sendable {
        case requestFailure = "request_failure"
        case speechFailure = "speech_failure"
    }

    public let kind: Kind
    public let turnId: String?
    public let occurredAt: String?
    public let title: String
    public let serverMessage: String
    public let guidance: String?

    public var id: String { "\(kind.rawValue):\(turnId ?? "session")" }

    public var displayText: String {
        var parts = ["Warning. \(title)", serverMessage]
        if let guidance { parts.append(guidance) }
        return parts.joined(separator: " ")
    }

    public var accessibilityLabel: String { displayText }

    fileprivate init(
        kind: Kind,
        turnId: String?,
        occurredAt: String?,
        title: String,
        serverMessage: String,
        guidance: String? = nil
    ) {
        self.kind = kind
        self.turnId = turnId
        self.occurredAt = occurredAt
        self.title = title
        self.serverMessage = serverMessage
        self.guidance = guidance
    }
}

/// Applies terminal voice-request notices consistently on iOS, macOS, and
/// watchOS. A notice survives ordinary session/composer churn and even a
/// same-turn out-of-order lifecycle update. Only a different accepted,
/// processing, or successful turn supersedes it; explicit session end/reset
/// remains a client-controller responsibility.
public enum VoiceTerminalNoticeReducer {
    private static let completedStates: Set<String> = [
        "failed", "refused", "cancelled", "abandoned",
    ]
    private static let supersedingStates: Set<String> = [
        "accepted", "processing", "succeeded",
    ]

    public static func reduce(
        current: VoiceTerminalNotice?, turn: VoiceTurnState
    ) -> VoiceTerminalNotice? {
        guard canApply(current: current, turnId: turn.turnId, occurredAt: turn.occurredAt)
        else { return current }
        if completedStates.contains(turn.state) {
            return requestFailure(turn)
        }
        if turn.state == "succeeded", turn.speechOutcome == .failed {
            return speechFailure(
                message: turn.message, turnId: turn.turnId,
                occurredAt: turn.occurredAt, textResultCommitted: true)
        }
        if supersedingStates.contains(turn.state), current?.turnId != turn.turnId {
            return nil
        }
        return current
    }

    public static func speechFailure(
        message: String?, turnId: String?, occurredAt: String? = nil,
        textResultCommitted: Bool = false
    ) -> VoiceTerminalNotice {
        VoiceTerminalNotice(
            kind: .speechFailure,
            turnId: turnId,
            occurredAt: occurredAt,
            title: "Speech playback failed.",
            serverMessage: resolvedMessage(
                message, fallback: "Assistant speech could not be played."),
            guidance: textResultCommitted
                ? "The text result is still available in the conversation."
                : "The text result may still be available in the conversation.")
    }

    public static func submissionRejected(
        _ rejection: VoiceSubmissionRejected
    ) -> VoiceTerminalNotice {
        VoiceTerminalNotice(
            kind: .requestFailure,
            turnId: rejection.turnId,
            occurredAt: rejection.occurredAt,
            title: "Request did not start.",
            serverMessage: resolvedMessage(
                rejection.message,
                fallback: "That spoken request was not accepted."),
            guidance: rejection.retryPolicy == "explicit_user_retry"
                ? "Please say it again when you are ready." : nil)
    }

    public static func reduce(
        current: VoiceTerminalNotice?, rejection: VoiceSubmissionRejected
    ) -> VoiceTerminalNotice? {
        guard
            canApply(
                current: current, turnId: rejection.turnId,
                occurredAt: rejection.occurredAt)
        else { return current }
        return submissionRejected(rejection)
    }

    /// Whether a validated lifecycle event can supersede the visible notice.
    /// Same-turn updates remain eligible so terminal detail can be refreshed;
    /// a different turn must prove that it is not older than the notice.
    public static func canApply(
        current: VoiceTerminalNotice?, turnId: String, occurredAt: String
    ) -> Bool {
        guard let current, current.turnId != turnId else { return true }
        guard let currentOccurredAt = current.occurredAt,
            let currentDate = voiceTimestampDate(currentOccurredAt),
            let incomingDate = voiceTimestampDate(occurredAt)
        else { return false }
        return incomingDate >= currentDate
    }

    private static func requestFailure(_ turn: VoiceTurnState) -> VoiceTerminalNotice {
        let refused = turn.state == "refused"
        return VoiceTerminalNotice(
            kind: .requestFailure,
            turnId: turn.turnId,
            occurredAt: turn.occurredAt,
            title: refused ? "Request did not start." : "Request did not complete.",
            serverMessage: resolvedMessage(
                turn.message,
                fallback: refused
                    ? "That spoken request was not accepted."
                    : "That spoken request ended before completion."))
    }

    private static func resolvedMessage(_ message: String?, fallback: String) -> String {
        guard let message, !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return fallback }
        return message
    }
}

public struct VoiceOrigin: Sendable, Equatable, CustomStringConvertible {
    public let schemaVersion: String
    public let sessionId: String
    public let generation: Int
    public let mediaGrantRevision: Int
    public let turnId: String
    public let clientTurnId: String
    public let chatContextRevision: Int
    public let sourceParticipantIdentity: String
    public let detectedLanguage: String
    public let textDigestSHA256: String
    public let transcriptProof: String
    public let proofExpiresAt: String

    public var description: String {
        "VoiceOrigin(sessionId=\(sessionId), generation=\(generation), turnId=\(turnId), proof=[REDACTED])"
    }

    fileprivate var json: JSONValue {
        .object([
            "schema_version": .string(schemaVersion),
            "session_id": .string(sessionId),
            "generation": .number(Double(generation)),
            "media_grant_revision": .number(Double(mediaGrantRevision)),
            "turn_id": .string(turnId),
            "client_turn_id": .string(clientTurnId),
            "chat_context_revision": .number(Double(chatContextRevision)),
            "source_participant_identity": .string(sourceParticipantIdentity),
            "detected_language": .string(detectedLanguage),
            "text_digest_sha256": .string(textDigestSHA256),
            "transcript_proof": .string(transcriptProof),
            "proof_expires_at": .string(proofExpiresAt),
        ])
    }

    fileprivate static func parse(_ json: JSONValue) -> VoiceOrigin? {
        guard let object = json.objectValue,
            voiceExact(
                object,
                required: [
                    "schema_version", "session_id", "generation", "media_grant_revision",
                    "turn_id", "client_turn_id", "chat_context_revision",
                    "source_participant_identity", "detected_language", "text_digest_sha256",
                    "transcript_proof", "proof_expires_at",
                ]),
            object["schema_version"]?.stringValue == "1",
            let session = voiceUUID4(object["session_id"]),
            let generation = voiceInteger(object["generation"], minimum: 1),
            let grantRevision = voiceInteger(object["media_grant_revision"], minimum: 1),
            let turn = voiceUUID4(object["turn_id"]),
            let clientTurn = voiceUUID4(object["client_turn_id"]),
            let contextRevision = voiceInteger(object["chat_context_revision"], minimum: 1),
            let source = voiceOpaque(object["source_participant_identity"]),
            let language = voiceLanguage(object["detected_language"]),
            let digest = object["text_digest_sha256"]?.stringValue,
            voiceMatches(digest, voiceSHA256Pattern),
            let proof = object["transcript_proof"]?.stringValue,
            voiceMatches(proof, voiceSHA256Pattern),
            let proofExpiry = voiceTimestamp(object["proof_expires_at"])
        else { return nil }
        return VoiceOrigin(
            schemaVersion: "1", sessionId: session, generation: generation,
            mediaGrantRevision: grantRevision, turnId: turn, clientTurnId: clientTurn,
            chatContextRevision: contextRevision, sourceParticipantIdentity: source,
            detectedLanguage: language, textDigestSHA256: digest,
            transcriptProof: proof, proofExpiresAt: proofExpiry)
    }
}

public struct VoiceTranscript: Sendable, Equatable, CustomStringConvertible {
    public let sessionId: String
    public let generation: Int
    public let turnId: String
    public let clientTurnId: String
    public let submissionId: String
    public let requestGeneration: String
    public let chatId: String
    public let chatContextRevision: Int
    public let mediaGrantRevision: Int
    public let sequence: Int
    public let final: Bool
    public let text: String
    public let detectedLanguage: String?
    public let textDigestSHA256: String?
    public let transcriptProof: String?
    public let proofExpiresAt: String?
    public let sourceParticipantIdentity: String

    public var description: String {
        "VoiceTranscript(sessionId=\(sessionId), turnId=\(turnId), sequence=\(sequence), final=\(final), text=[REDACTED], proof=[REDACTED])"
    }

    public var origin: VoiceOrigin? {
        guard final, let detectedLanguage, let textDigestSHA256, let transcriptProof,
            let proofExpiresAt
        else { return nil }
        return VoiceOrigin(
            schemaVersion: "1", sessionId: sessionId, generation: generation,
            mediaGrantRevision: mediaGrantRevision, turnId: turnId, clientTurnId: clientTurnId,
            chatContextRevision: chatContextRevision,
            sourceParticipantIdentity: sourceParticipantIdentity,
            detectedLanguage: detectedLanguage, textDigestSHA256: textDigestSHA256,
            transcriptProof: transcriptProof, proofExpiresAt: proofExpiresAt)
    }

    public init?(frame: InboundFrame, packetBytes: Int? = nil) {
        if let packetBytes, packetBytes > VoiceContractLimits.transcriptPacketBytes { return nil }
        guard frame.name == "voice_transcript", let object = frame.payload.objectValue else { return nil }
        let required: Set<String> = [
            "type", "schema_version", "session_id", "generation", "turn_id", "client_turn_id",
            "submission_id", "request_generation", "chat_id", "chat_context_revision",
            "media_grant_revision", "sequence", "final", "text", "detected_language",
            "source_participant_identity",
        ]
        let proofFields: Set<String> = ["text_digest_sha256", "transcript_proof", "proof_expires_at"]
        guard voiceExact(object, required: required, optional: proofFields),
            object["schema_version"]?.stringValue == "1",
            let session = voiceUUID4(object["session_id"]),
            let generation = voiceInteger(object["generation"], minimum: 1),
            let turn = voiceUUID4(object["turn_id"]),
            let clientTurn = voiceUUID4(object["client_turn_id"]),
            let submission = voiceUUID4(object["submission_id"]),
            let request = voiceUUID4(object["request_generation"]),
            let chat = voiceUUID4(object["chat_id"]),
            let context = voiceInteger(object["chat_context_revision"], minimum: 1),
            let grant = voiceInteger(object["media_grant_revision"], minimum: 1),
            let sequence = voiceInteger(object["sequence"]),
            let final = object["final"]?.boolValue,
            let text = object["text"]?.stringValue, text.count <= 8_000,
            let language = VoiceNullableLanguage.parse(object["detected_language"]),
            let source = voiceOpaque(object["source_participant_identity"])
        else { return nil }

        let digest = object["text_digest_sha256"]?.stringValue
        let proof = object["transcript_proof"]?.stringValue
        let proofExpiry = voiceTimestamp(object["proof_expires_at"])
        if final {
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                language != nil,
                digest.map({ voiceMatches($0, voiceSHA256Pattern) }) == true,
                proof.map({ voiceMatches($0, voiceSHA256Pattern) }) == true,
                proofExpiry != nil,
                proofFields.isSubset(of: Set(object.keys))
            else { return nil }
        } else if !proofFields.isDisjoint(with: Set(object.keys)) {
            return nil
        }
        self.sessionId = session
        self.generation = generation
        self.turnId = turn
        self.clientTurnId = clientTurn
        self.submissionId = submission
        self.requestGeneration = request
        self.chatId = chat
        self.chatContextRevision = context
        self.mediaGrantRevision = grant
        self.sequence = sequence
        self.final = final
        self.text = text
        self.detectedLanguage = language
        self.textDigestSHA256 = digest
        self.transcriptProof = proof
        self.proofExpiresAt = proofExpiry
        self.sourceParticipantIdentity = source
    }
}

public struct VoiceAnnouncementMedia: Sendable, Equatable {
    public let sessionId: String
    public let generation: Int
    public let mediaGrantRevision: Int
    public let announcementId: String
    public let announcementSequence: Int
    public let turnId: String?
    public let kind: String
    public let quantumRole: String
    public let quantumIndex: Int
    public let transport: VoiceTransport
    public let workerIdentity: String
    public let sampleRateHz: Int
    public let durationSamples: Int
    public let resultReservedSamplesAfter: Int?
    public let trackSid: String?
    public let trackName: String?
    public let firstMediaSequence: Int?
    public let lastMediaSequence: Int?

    public init?(frame: InboundFrame, packetBytes: Int? = nil) {
        if let packetBytes, packetBytes > VoiceContractLimits.announcementPacketBytes { return nil }
        guard frame.name == "voice_announcement_media", let object = frame.payload.objectValue else { return nil }
        let required: Set<String> = [
            "type", "schema_version", "session_id", "generation", "media_grant_revision",
            "announcement_id", "announcement_sequence", "turn_id", "kind", "quantum_role",
            "quantum_index", "transport", "worker_identity", "sample_rate_hz", "duration_samples",
        ]
        let optional: Set<String> = [
            "result_reserved_samples_after", "track_sid", "track_name", "first_media_sequence",
            "last_media_sequence",
        ]
        guard voiceExact(object, required: required, optional: optional),
            object["schema_version"]?.stringValue == "1",
            let session = voiceUUID4(object["session_id"]),
            let generation = voiceInteger(object["generation"], minimum: 1),
            let grant = voiceInteger(object["media_grant_revision"], minimum: 1),
            let announcement = voiceUUID4(object["announcement_id"]),
            let announcementSequence = voiceInteger(object["announcement_sequence"], minimum: 1),
            let turn = OptionalUUID.parse(object, "turn_id"),
            let kind = object["kind"]?.stringValue,
            [
                "greeting", "acknowledgement", "progress", "waiting", "result", "sensitive_notice",
                "failure", "refusal", "cancellation",
            ].contains(kind),
            let role = object["quantum_role"]?.stringValue,
            ["single", "result_opening", "result_continuation"].contains(role),
            let index = voiceInteger(object["quantum_index"], maximum: 31),
            let transportValue = object["transport"]?.stringValue,
            let transport = VoiceTransport(rawValue: transportValue),
            let worker = voiceOpaque(object["worker_identity"]),
            voiceInteger(object["sample_rate_hz"]) == 24_000,
            let duration = voiceInteger(
                object["duration_samples"], minimum: 1,
                maximum: VoiceContractLimits.quantumSamples),
            let reserved = OptionalBoundedInteger.optionalParse(
                object, "result_reserved_samples_after", minimum: 1,
                maximum: VoiceContractLimits.resultAggregateSamples),
            let trackSid = OptionalOpaque.optionalAbsentParse(object, "track_sid"),
            let trackName = OptionalOpaque.optionalAbsentParse(object, "track_name"),
            let first = OptionalBoundedInteger.optionalParse(
                object, "first_media_sequence", minimum: 0),
            let last = OptionalBoundedInteger.optionalParse(
                object, "last_media_sequence", minimum: 0)
        else { return nil }
        guard (kind == "greeting") == (turn == nil) else { return nil }
        switch role {
        case "single":
            guard kind != "result", index == 0, reserved == nil else { return nil }
        case "result_opening":
            guard kind == "result", index == 0,
                duration <= VoiceContractLimits.resultOpeningSamples,
                reserved.map({ (1...VoiceContractLimits.resultOpeningSamples).contains($0) }) == true
            else { return nil }
        case "result_continuation":
            guard kind == "result", (1...31).contains(index), reserved != nil else { return nil }
        default: return nil
        }
        switch transport {
        case .liveKit:
            guard trackSid != nil, trackName != nil, first == nil, last == nil else { return nil }
        case .watchPCMWebSocket:
            guard trackSid == nil, trackName == nil, let first, let last, last >= first,
                (last - first + 1) * 480 == duration
            else { return nil }
        }
        self.sessionId = session
        self.generation = generation
        self.mediaGrantRevision = grant
        self.announcementId = announcement
        self.announcementSequence = announcementSequence
        self.turnId = turn
        self.kind = kind
        self.quantumRole = role
        self.quantumIndex = index
        self.transport = transport
        self.workerIdentity = worker
        self.sampleRateHz = 24_000
        self.durationSamples = duration
        self.resultReservedSamplesAfter = reserved
        self.trackSid = trackSid
        self.trackName = trackName
        self.firstMediaSequence = first
        self.lastMediaSequence = last
    }
}

private enum OptionalBoundedInteger {
    static func optionalParse(
        _ object: [String: JSONValue], _ key: String, minimum: Int, maximum: Int = maximumSafeWireInteger
    ) -> Int?? {
        guard object[key] != nil else { return .some(nil) }
        guard let value = voiceInteger(object[key], minimum: minimum, maximum: maximum) else { return nil }
        return .some(value)
    }
}

extension OptionalOpaque {
    static func optionalAbsentParse(_ object: [String: JSONValue], _ key: String) -> String?? {
        guard object[key] != nil else { return .some(nil) }
        guard let value = voiceOpaque(object[key]) else { return nil }
        return .some(value)
    }
}

public struct VoiceMessageAcknowledgement: Sendable, Equatable {
    public let chatId: String
    public let messageId: Int
    public let submissionId: String
    public let requestGeneration: String
    public let connectionGeneration: String
    public let voiceTurnId: String?

    public init?(frame: InboundFrame) {
        guard frame.name == "user_message_acked", let object = frame.payload.objectValue,
            voiceExact(
                object,
                required: [
                    "type", "schema_version", "chat_id", "message_id", "submission_id",
                    "request_generation", "connection_generation", "voice_turn_id",
                ]),
            object["schema_version"]?.stringValue == "1",
            let chat = voiceUUID4(object["chat_id"]),
            let message = voiceInteger(object["message_id"], minimum: 1),
            let submission = voiceUUID4(object["submission_id"]),
            let request = voiceUUID4(object["request_generation"]),
            let connection = voiceUUID4(object["connection_generation"]),
            let voiceTurn = OptionalUUID.parse(object, "voice_turn_id")
        else { return nil }
        self.chatId = chat
        self.messageId = message
        self.submissionId = submission
        self.requestGeneration = request
        self.connectionGeneration = connection
        self.voiceTurnId = voiceTurn
    }
}

public struct CorrelatedVoiceNewChat: Sendable, Equatable {
    public let connectionGeneration: String
    public let submissionId: String
    public let requestGeneration: String

    public init?(frame: InboundFrame) {
        guard frame.name == "ui_event", let object = frame.payload.objectValue,
            voiceExact(
                object,
                required: [
                    "type", "action", "schema_version", "connection_generation",
                    "submission_id", "request_generation", "payload",
                ]),
            object["action"]?.stringValue == "new_chat",
            object["schema_version"]?.stringValue == "1",
            let connection = voiceUUID4(object["connection_generation"]),
            let submission = voiceUUID4(object["submission_id"]),
            let request = voiceUUID4(object["request_generation"]),
            let payload = object["payload"]?.objectValue,
            voiceExact(
                payload,
                required: [
                    "schema_version", "connection_generation", "submission_id", "request_generation",
                ]),
            payload["schema_version"]?.stringValue == "1",
            payload["connection_generation"]?.stringValue == connection,
            payload["submission_id"]?.stringValue == submission,
            payload["request_generation"]?.stringValue == request
        else { return nil }
        self.connectionGeneration = connection
        self.submissionId = submission
        self.requestGeneration = request
    }
}

public struct CorrelatedVoiceChatCreated: Sendable, Equatable {
    public let chatId: String
    public let connectionGeneration: String
    public let submissionId: String
    public let requestGeneration: String

    public init?(frame: InboundFrame) {
        guard frame.name == "chat_created", let object = frame.payload.objectValue,
            voiceExact(
                object,
                required: [
                    "type", "schema_version", "connection_generation", "submission_id",
                    "request_generation", "payload",
                ]),
            object["schema_version"]?.stringValue == "1",
            let connection = voiceUUID4(object["connection_generation"]),
            let submission = voiceUUID4(object["submission_id"]),
            let request = voiceUUID4(object["request_generation"]),
            let payload = object["payload"]?.objectValue,
            voiceExact(
                payload,
                required: [
                    "schema_version", "chat_id", "from_message", "connection_generation",
                    "submission_id", "request_generation",
                ]),
            payload["schema_version"]?.stringValue == "1",
            let chat = voiceUUID4(payload["chat_id"]),
            payload["from_message"]?.boolValue == false,
            payload["connection_generation"]?.stringValue == connection,
            payload["submission_id"]?.stringValue == submission,
            payload["request_generation"]?.stringValue == request
        else { return nil }
        self.chatId = chat
        self.connectionGeneration = connection
        self.submissionId = submission
        self.requestGeneration = request
    }
}

public struct VoiceMediaContext: Sendable, Equatable {
    public var expectedWorkerIdentity: String?
    public var expectedParticipantIdentity: String?
    public var expectedDeviceId: String?
    public var expectedConnectionGeneration: String?
    public var expectedSessionId: String?
    public var expectedGeneration: Int?
    public var expectedMediaGrantRevision: Int?

    public init(
        expectedWorkerIdentity: String? = nil, expectedParticipantIdentity: String? = nil,
        expectedDeviceId: String? = nil, expectedConnectionGeneration: String? = nil,
        expectedSessionId: String? = nil, expectedGeneration: Int? = nil,
        expectedMediaGrantRevision: Int? = nil
    ) {
        self.expectedWorkerIdentity = expectedWorkerIdentity
        self.expectedParticipantIdentity = expectedParticipantIdentity
        self.expectedDeviceId = expectedDeviceId
        self.expectedConnectionGeneration = expectedConnectionGeneration
        self.expectedSessionId = expectedSessionId
        self.expectedGeneration = expectedGeneration
        self.expectedMediaGrantRevision = expectedMediaGrantRevision
    }

    public func accepts(_ transcript: VoiceTranscript, participantIdentity: String?) -> Bool {
        if let expectedWorkerIdentity,
            transcript.sourceParticipantIdentity != expectedWorkerIdentity
        {
            return false
        }
        if let expectedParticipantIdentity, participantIdentity != expectedParticipantIdentity { return false }
        if let expectedSessionId, transcript.sessionId != expectedSessionId { return false }
        if let expectedGeneration, transcript.generation != expectedGeneration { return false }
        if let expectedMediaGrantRevision,
            transcript.mediaGrantRevision != expectedMediaGrantRevision
        {
            return false
        }
        return true
    }

    public func accepts(_ announcement: VoiceAnnouncementMedia, participantIdentity: String?) -> Bool {
        if let expectedWorkerIdentity, announcement.workerIdentity != expectedWorkerIdentity { return false }
        if let expectedParticipantIdentity, participantIdentity != expectedParticipantIdentity { return false }
        if let expectedSessionId, announcement.sessionId != expectedSessionId { return false }
        if let expectedGeneration, announcement.generation != expectedGeneration { return false }
        if let expectedMediaGrantRevision,
            announcement.mediaGrantRevision != expectedMediaGrantRevision
        {
            return false
        }
        return true
    }
}

public enum VoiceMediaEnvelope: Sendable, Equatable {
    case transcript(VoiceTranscript)
    case announcement(VoiceAnnouncementMedia)

    public init?(
        topic: String?, participantIdentity: String?, data: Data,
        context: VoiceMediaContext
    ) {
        guard let frame = InboundFrame.parse(String(decoding: data, as: UTF8.self)) else { return nil }
        switch topic {
        case voiceTranscriptTopic:
            guard let transcript = VoiceTranscript(frame: frame, packetBytes: data.count),
                context.accepts(transcript, participantIdentity: participantIdentity)
            else { return nil }
            self = .transcript(transcript)
        case voiceAnnouncementTopic:
            guard let announcement = VoiceAnnouncementMedia(frame: frame, packetBytes: data.count),
                context.accepts(announcement, participantIdentity: participantIdentity)
            else { return nil }
            self = .announcement(announcement)
        default: return nil
        }
    }
}

/// Enforces manifest sequence, result quantum order, and the 30-second result ceiling.
public struct VoiceAnnouncementLedger: Sendable {
    private var lastSequence = 0
    private var resultSamples: [String: Int] = [:]
    private var resultIndex: [String: Int] = [:]
    private var resultReservation: [String: Int] = [:]

    public init() {}

    public mutating func accept(_ value: VoiceAnnouncementMedia) -> Bool {
        guard value.announcementSequence > lastSequence else { return false }
        if value.kind == "result" {
            guard let turn = value.turnId else { return false }
            let expectedIndex = resultIndex[turn] ?? 0
            guard value.quantumIndex == expectedIndex else { return false }
            let nextSamples = (resultSamples[turn] ?? 0) + value.durationSamples
            let priorReservation = resultReservation[turn] ?? 0
            guard let reservation = value.resultReservedSamplesAfter else { return false }
            guard nextSamples <= VoiceContractLimits.resultAggregateSamples,
                reservation >= nextSamples, reservation >= priorReservation
            else { return false }
            resultSamples[turn] = nextSamples
            resultIndex[turn] = expectedIndex + 1
            resultReservation[turn] = reservation
        }
        lastSequence = value.announcementSequence
        return true
    }
}

public struct VoicePlayoutEvent: Sendable, Equatable {
    public let deviceId: String
    public let connectionGeneration: String
    public let sessionId: String
    public let generation: Int
    public let mediaGrantRevision: Int
    public let announcementId: String
    public let announcementSequence: Int
    public let turnId: String?
    public let kind: String
    public let quantumRole: String
    public let quantumIndex: Int
    public let resultReservedSamplesAfter: Int?
    public let phase: String
    public let clientSequence: Int
    public let observedAt: String

    public init?(
        deviceId: String, connectionGeneration: String, announcement: VoiceAnnouncementMedia,
        phase: String, clientSequence: Int, observedAt: String
    ) {
        guard voiceMatches(deviceId, voiceUUID4Pattern),
            voiceMatches(connectionGeneration, voiceUUID4Pattern),
            ["started", "finished", "interrupted"].contains(phase), clientSequence >= 0,
            voiceTimestamp(.string(observedAt)) != nil
        else { return nil }
        self.deviceId = deviceId
        self.connectionGeneration = connectionGeneration
        self.sessionId = announcement.sessionId
        self.generation = announcement.generation
        self.mediaGrantRevision = announcement.mediaGrantRevision
        self.announcementId = announcement.announcementId
        self.announcementSequence = announcement.announcementSequence
        self.turnId = announcement.turnId
        self.kind = announcement.kind
        self.quantumRole = announcement.quantumRole
        self.quantumIndex = announcement.quantumIndex
        self.resultReservedSamplesAfter = announcement.resultReservedSamplesAfter
        self.phase = phase
        self.clientSequence = clientSequence
        self.observedAt = observedAt
    }

    public init?(frame: InboundFrame, packetBytes: Int? = nil) {
        if let packetBytes, packetBytes > VoiceContractLimits.playoutPacketBytes { return nil }
        guard frame.name == "voice_playout_event", let object = frame.payload.objectValue else { return nil }
        let required: Set<String> = [
            "type", "schema_version", "device_id", "connection_generation", "session_id",
            "generation", "media_grant_revision", "announcement_id", "announcement_sequence",
            "turn_id", "kind", "quantum_role", "quantum_index", "phase", "client_sequence",
            "observed_at",
        ]
        guard voiceExact(object, required: required, optional: ["result_reserved_samples_after"]),
            object["schema_version"]?.stringValue == "1",
            let device = voiceUUID4(object["device_id"]),
            let connection = voiceUUID4(object["connection_generation"]),
            let session = voiceUUID4(object["session_id"]),
            let generation = voiceInteger(object["generation"], minimum: 1),
            let grant = voiceInteger(object["media_grant_revision"], minimum: 1),
            let announcement = voiceUUID4(object["announcement_id"]),
            let announcementSequence = voiceInteger(object["announcement_sequence"], minimum: 1),
            let turn = OptionalUUID.parse(object, "turn_id"),
            let kind = object["kind"]?.stringValue,
            [
                "greeting", "acknowledgement", "progress", "waiting", "result", "sensitive_notice",
                "failure", "refusal", "cancellation",
            ].contains(kind),
            let role = object["quantum_role"]?.stringValue,
            ["single", "result_opening", "result_continuation"].contains(role),
            let index = voiceInteger(object["quantum_index"], maximum: 31),
            let reserved = OptionalBoundedInteger.optionalParse(
                object, "result_reserved_samples_after", minimum: 1,
                maximum: VoiceContractLimits.resultAggregateSamples),
            let phase = object["phase"]?.stringValue,
            ["started", "finished", "interrupted"].contains(phase),
            let clientSequence = voiceInteger(object["client_sequence"]),
            let observed = voiceTimestamp(object["observed_at"])
        else { return nil }
        guard (kind == "greeting") == (turn == nil) else { return nil }
        switch role {
        case "single":
            guard kind != "result", index == 0, reserved == nil else { return nil }
        case "result_opening":
            guard kind == "result", index == 0,
                reserved.map({ $0 <= VoiceContractLimits.resultOpeningSamples }) == true
            else { return nil }
        case "result_continuation":
            guard kind == "result", (1...31).contains(index), reserved != nil else { return nil }
        default: return nil
        }
        self.deviceId = device
        self.connectionGeneration = connection
        self.sessionId = session
        self.generation = generation
        self.mediaGrantRevision = grant
        self.announcementId = announcement
        self.announcementSequence = announcementSequence
        self.turnId = turn
        self.kind = kind
        self.quantumRole = role
        self.quantumIndex = index
        self.resultReservedSamplesAfter = reserved
        self.phase = phase
        self.clientSequence = clientSequence
        self.observedAt = observed
    }

    fileprivate var json: JSONValue {
        var object: [String: JSONValue] = [
            "type": .string("voice_playout_event"),
            "schema_version": .string("1"),
            "device_id": .string(deviceId),
            "connection_generation": .string(connectionGeneration),
            "session_id": .string(sessionId),
            "generation": .number(Double(generation)),
            "media_grant_revision": .number(Double(mediaGrantRevision)),
            "announcement_id": .string(announcementId),
            "announcement_sequence": .number(Double(announcementSequence)),
            "turn_id": turnId.map(JSONValue.string) ?? .null,
            "kind": .string(kind),
            "quantum_role": .string(quantumRole),
            "quantum_index": .number(Double(quantumIndex)),
            "phase": .string(phase),
            "client_sequence": .number(Double(clientSequence)),
            "observed_at": .string(observedAt),
        ]
        if let resultReservedSamplesAfter {
            object["result_reserved_samples_after"] = .number(Double(resultReservedSamplesAfter))
        }
        return .object(object)
    }
}

/// A strictly validated voice frame whose identities and transcript proof are
/// meaningful only on the currently established UI socket. These frames must
/// never be retained by the ordinary offline-operation replay queue.
public struct VoiceCurrentConnectionFrame: Sendable, Equatable {
    public enum Kind: String, Sendable, Equatable {
        case playoutEvent = "voice_playout_event"
        case finalTranscript = "voice_final_transcript"
        case correlatedNewChat = "voice_correlated_new_chat"
    }

    public let kind: Kind
    let frameText: String

    public init?(frameText: String) {
        guard let data = frameText.data(using: .utf8),
            let root = try? JSONValue.parse(data),
            let object = root.objectValue,
            let type = object["type"]?.stringValue
        else { return nil }

        switch type {
        case "voice_playout_event":
            guard data.count <= VoiceContractLimits.playoutPacketBytes,
                let frame = InboundFrame.parse(frameText),
                VoicePlayoutEvent(frame: frame, packetBytes: data.count) != nil
            else { return nil }
            kind = .playoutEvent
        case "ui_event":
            switch object["action"]?.stringValue {
            case "chat_message":
                guard Self.isProofBoundFinalTranscript(object, packetBytes: data.count)
                else { return nil }
                kind = .finalTranscript
            case "new_chat":
                guard data.count <= VoiceContractLimits.playoutPacketBytes,
                    let frame = InboundFrame.parse(frameText),
                    CorrelatedVoiceNewChat(frame: frame) != nil
                else { return nil }
                kind = .correlatedNewChat
            default:
                return nil
            }
        default:
            return nil
        }
        self.frameText = frameText
    }

    /// True when a frame claims a voice-only current-connection shape, even
    /// if its required fields are malformed. The replay parser uses this to
    /// prevent malformed voice frames from degrading into generic UI events.
    static func claimsCurrentConnectionSemantics(frameText: String) -> Bool {
        guard let data = frameText.data(using: .utf8),
            let root = try? JSONValue.parse(data),
            let object = root.objectValue,
            let type = object["type"]?.stringValue
        else { return false }
        if type == "voice_playout_event" { return true }
        guard type == "ui_event", let action = object["action"]?.stringValue else {
            return false
        }
        let payload = object["payload"]?.objectValue
        if action == "chat_message" { return payload?["voice_origin"] != nil }
        guard action == "new_chat" else { return false }
        return object["schema_version"] != nil
            || object["connection_generation"] != nil
            || payload?["schema_version"] != nil
            || payload?["connection_generation"] != nil
    }

    private static func isProofBoundFinalTranscript(
        _ root: [String: JSONValue], packetBytes: Int
    ) -> Bool {
        guard packetBytes <= VoiceContractLimits.transcriptPacketBytes,
            voiceExact(
                root,
                required: [
                    "type", "action", "session_id", "connection_generation",
                    "submission_id", "request_generation", "payload",
                ]),
            root["type"]?.stringValue == "ui_event",
            root["action"]?.stringValue == "chat_message",
            let session = voiceUUID4(root["session_id"]),
            let connection = voiceUUID4(root["connection_generation"]),
            let submission = voiceUUID4(root["submission_id"]),
            let request = voiceUUID4(root["request_generation"]),
            let payload = root["payload"]?.objectValue,
            voiceExact(
                payload,
                required: [
                    "message", "chat_id", "connection_generation", "submission_id",
                    "request_generation", "snapshot_purpose", "voice_origin",
                ]),
            let message = payload["message"]?.stringValue,
            !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
            message.count <= 8_000,
            payload["chat_id"]?.stringValue == session,
            payload["connection_generation"]?.stringValue == connection,
            payload["submission_id"]?.stringValue == submission,
            payload["request_generation"]?.stringValue == request,
            payload["snapshot_purpose"]?.stringValue == "commit",
            let originJSON = payload["voice_origin"],
            VoiceOrigin.parse(originJSON) != nil
        else { return false }
        return true
    }
}

extension Outbound {
    public static func voiceChatMessage(
        transcript: VoiceTranscript, connectionGeneration: String
    ) -> String {
        guard voiceMatches(connectionGeneration, voiceUUID4Pattern), transcript.final,
            !transcript.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
            let origin = transcript.origin
        else { return "{}" }
        let payload: JSONValue = .object([
            "message": .string(transcript.text),
            "chat_id": .string(transcript.chatId),
            "connection_generation": .string(connectionGeneration),
            "submission_id": .string(transcript.submissionId),
            "request_generation": .string(transcript.requestGeneration),
            "snapshot_purpose": .string("commit"),
            "voice_origin": origin.json,
        ])
        return encode(
            .object([
                "type": .string("ui_event"),
                "action": .string("chat_message"),
                "session_id": .string(transcript.chatId),
                "connection_generation": .string(connectionGeneration),
                "submission_id": .string(transcript.submissionId),
                "request_generation": .string(transcript.requestGeneration),
                "payload": payload,
            ]))
    }

    public static func correlatedVoiceNewChat(
        connectionGeneration: String, submissionId: String, requestGeneration: String
    ) -> String {
        guard
            [connectionGeneration, submissionId, requestGeneration]
                .allSatisfy({ voiceMatches($0, voiceUUID4Pattern) })
        else { return "{}" }
        let identity: [String: JSONValue] = [
            "schema_version": .string("1"),
            "connection_generation": .string(connectionGeneration),
            "submission_id": .string(submissionId),
            "request_generation": .string(requestGeneration),
        ]
        var frame = identity
        frame["type"] = .string("ui_event")
        frame["action"] = .string("new_chat")
        frame["payload"] = .object(identity)
        return encode(.object(frame))
    }

    public static func voicePlayoutEvent(_ event: VoicePlayoutEvent) -> String {
        let encoded = encode(event.json)
        return encoded.utf8.count <= VoiceContractLimits.playoutPacketBytes ? encoded : "{}"
    }
}
