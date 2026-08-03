import AstralCore
import Foundation

// Feature 065 — strict, watch-local last-mile contracts. The watch cannot use
// LiveKit's WebRTC client, so it receives the same transcript and announcement
// envelopes through a bounded PCM WebSocket relay. These models deliberately
// reject additive/unknown fields: voice media is an authority-sensitive input,
// unlike the lenient server-rendered UI vocabulary.

enum WatchVoicePermission: String, Equatable, Sendable {
    case notDetermined = "not_determined"
    case authorized
    case denied
    case restricted
}

enum WatchVoiceState: String, Equatable, Sendable {
    case off
    case unavailable
    case connecting
    case greeting
    case listening
    case speechDetected = "speech_detected"
    case transcribing
    case acknowledging
    case processing
    case waitingOnUser = "waiting_on_user"
    case speakingProgress = "speaking_progress"
    case speakingResult = "speaking_result"
    case muted
    case suspended
    case reconnecting
    case error
    case ended

    var active: Bool {
        switch self {
        case .connecting, .greeting, .listening, .speechDetected, .transcribing,
            .acknowledging, .processing, .waitingOnUser, .speakingProgress,
            .speakingResult, .muted, .reconnecting:
            return true
        case .off, .unavailable, .suspended, .error, .ended:
            return false
        }
    }

}

struct WatchVoiceControl: Equatable, Sendable {
    let key: String
    let action: String
    let label: String
    let icon: String
    let visible: Bool
    let enabled: Bool
    let pressed: Bool
    let busy: Bool

    nonisolated init(_ value: VoiceControl) {
        key = value.key
        action = value.action.rawValue
        label = value.label
        icon = value.icon
        visible = value.visible
        enabled = value.enabled
        pressed = value.pressed
        busy = value.busy
    }
}

struct WatchVoiceComposer: Equatable, Sendable {
    let revision: UInt64
    let connectionGeneration: String
    let available: Bool
    let state: WatchVoiceState
    let speechMuted: Bool
    let microphoneEnabled: Bool
    let foregroundActive: Bool
    let reason: String
    let message: String?
    let sessionId: String?
    let generation: UInt64?
    let mediaGrantRevision: UInt64?
    let visibleChatId: String?
    let controls: [WatchVoiceControl]

    init?(frame: InboundFrame, expectedConnection: String?) {
        guard let parsed = VoiceComposerState(frame: frame),
            expectedConnection == nil || parsed.connectionGeneration == expectedConnection,
            let state = WatchVoiceState(rawValue: parsed.voice.state.rawValue)
        else { return nil }
        var controls: [WatchVoiceControl] = []
        for control in parsed.voice.controls { controls.append(WatchVoiceControl(control)) }
        guard Set(controls.map(\.key)).count == controls.count else { return nil }
        revision = UInt64(parsed.revision)
        connectionGeneration = parsed.connectionGeneration
        available = parsed.voice.available
        self.state = state
        speechMuted = parsed.voice.speechMuted
        microphoneEnabled = parsed.voice.microphoneEnabled
        foregroundActive = parsed.voice.foregroundActive
        reason = parsed.voice.reason.rawValue
        message = parsed.voice.message
        sessionId = parsed.voice.sessionId
        generation = parsed.voice.generation.map(UInt64.init)
        mediaGrantRevision = parsed.voice.mediaGrantRevision.map(UInt64.init)
        visibleChatId = parsed.voice.visibleChatId
        self.controls = controls
    }

    func control(action: String) -> WatchVoiceControl? {
        controls.first { $0.action == action && $0.visible }
    }
}

struct WatchVoiceControlBinding: Equatable, Sendable {
    let deviceId: String
    let connectionGeneration: String
    let bindingId: String
    let bearer: String
    let expiresAt: Date

    init?(frame: InboundFrame, expectedDeviceId: String, expectedConnection: String?) {
        guard frame.name == "voice_control_binding", let root = frame.payload.objectValue,
            watchVoiceHasExactKeys(
                root,
                [
                    "type", "schema_version", "device_id", "connection_generation",
                    "binding_id", "binding", "expires_at",
                ]),
            root["type"]?.stringValue == "voice_control_binding",
            root["schema_version"]?.stringValue == "1",
            let deviceId = watchVoiceUUID4(root["device_id"]), deviceId == expectedDeviceId,
            let connection = watchVoiceUUID4(root["connection_generation"]),
            expectedConnection == nil || connection == expectedConnection,
            let bindingId = watchVoiceUUID4(root["binding_id"]),
            let bearer = root["binding"]?.stringValue,
            (32...512).contains(bearer.utf8.count),
            bearer.range(of: "^[A-Za-z0-9._~-]+$", options: .regularExpression) != nil,
            let expiresAt = watchVoiceDate(root["expires_at"]), expiresAt > Date()
        else { return nil }
        self.deviceId = deviceId
        connectionGeneration = connection
        self.bindingId = bindingId
        self.bearer = bearer
        self.expiresAt = expiresAt
    }
}

struct WatchVoiceSession: Equatable, Sendable {
    let sessionId: String
    let deviceId: String
    let ownerConnectionGeneration: String
    let generation: UInt64
    let mediaGrantRevision: UInt64
    let visibleChatId: String
    let chatContextRevision: UInt64
    let chatContextSynced: Bool
    let speechMuted: Bool
    let microphoneEnabled: Bool
    let foregroundActive: Bool

    init?(json: JSONValue) {
        guard let root = json.objectValue,
            watchVoiceHasExactKeys(
                root,
                required: [
                    "session_id", "device_id", "device_kind", "transport", "state",
                    "generation", "media_grant_revision", "owner_connection_generation",
                    "visible_chat_id", "applied_visible_chat_id", "chat_context_revision",
                    "applied_chat_context_revision", "chat_context_synced", "foreground_active",
                    "foreground_reason", "foreground_changed_at", "speech_muted",
                    "microphone_enabled", "lease_expires_at", "started_at",
                ],
                optional: ["idle_expires_at"]),
            let sessionId = watchVoiceUUID4(root["session_id"]),
            let deviceId = watchVoiceUUID4(root["device_id"]),
            root["device_kind"]?.stringValue == "watchos",
            root["transport"]?.stringValue == "watch_pcm_websocket",
            let state = root["state"]?.stringValue,
            ["starting", "active", "suspended", "reconnecting", "ending", "ended", "error"]
                .contains(state),
            let generation = watchVoiceUInt(root["generation"], minimum: 1),
            let revision = watchVoiceUInt(root["media_grant_revision"], minimum: 1),
            let ownerConnection = watchVoiceUUID4(root["owner_connection_generation"]),
            let visibleChatId = watchVoiceUUID4(root["visible_chat_id"]),
            watchVoiceNullableUUID4(root["applied_visible_chat_id"]).valid,
            let contextRevision = watchVoiceUInt(root["chat_context_revision"], minimum: 1),
            watchVoiceNullableUInt(
                root["applied_chat_context_revision"], minimum: 1
            ).valid,
            let contextSynced = root["chat_context_synced"]?.boolValue,
            let foreground = root["foreground_active"]?.boolValue,
            let foregroundReason = root["foreground_reason"]?.stringValue,
            let muted = root["speech_muted"]?.boolValue,
            let microphone = root["microphone_enabled"]?.boolValue,
            watchVoiceDate(root["foreground_changed_at"]) != nil,
            watchVoiceDate(root["lease_expires_at"]) != nil,
            watchVoiceDate(root["started_at"]) != nil,
            watchVoiceNullableDate(root["idle_expires_at"]).valid,
            !contextSynced
                || (watchVoiceNullableUUID4(root["applied_visible_chat_id"]).value == visibleChatId
                    && watchVoiceNullableUInt(
                        root["applied_chat_context_revision"], minimum: 1
                    ).value
                        == contextRevision),
            foreground
                ? (foregroundReason == "foreground"
                    && ["starting", "active", "reconnecting", "ending", "error"].contains(state))
                : (!microphone
                    && [
                        "backgrounded", "locked", "audio_interrupted",
                        "route_unavailable", "connection_lost",
                    ].contains(foregroundReason)
                    && ["suspended", "reconnecting", "ending", "ended", "error"].contains(state))
        else { return nil }
        self.sessionId = sessionId
        self.deviceId = deviceId
        ownerConnectionGeneration = ownerConnection
        self.generation = generation
        mediaGrantRevision = revision
        self.visibleChatId = visibleChatId
        chatContextRevision = contextRevision
        chatContextSynced = contextSynced
        speechMuted = muted
        microphoneEnabled = microphone
        foregroundActive = foreground
    }
}

struct WatchVoiceBridgeGrant: Equatable, Sendable {
    let grantId: String
    let sessionId: String
    let generation: UInt64
    let mediaGrantRevision: UInt64
    let expiresAt: Date
    let url: URL
    let ticket: String
    let workerIdentity: String

    init?(json: JSONValue) {
        guard let root = json.objectValue,
            watchVoiceHasExactKeys(
                root,
                [
                    "grant_id", "transport", "session_id", "generation",
                    "media_grant_revision", "expires_at", "url", "ticket",
                    "worker_identity", "capture", "playback",
                ]),
            let grantId = watchVoiceOpaque(root["grant_id"]),
            root["transport"]?.stringValue == "watch_pcm_websocket",
            let sessionId = watchVoiceUUID4(root["session_id"]),
            let generation = watchVoiceUInt(root["generation"], minimum: 1),
            let revision = watchVoiceUInt(root["media_grant_revision"], minimum: 1),
            let expiresAt = watchVoiceDate(root["expires_at"]), expiresAt > Date(),
            let urlString = root["url"]?.stringValue, let url = URL(string: urlString),
            url.scheme?.lowercased() == "wss", url.host != nil,
            url.user == nil, url.password == nil,
            url.query == nil, url.fragment == nil,
            let ticket = root["ticket"]?.stringValue,
            (32...8192).contains(ticket.utf8.count),
            !ticket.contains("\r"), !ticket.contains("\n"),
            let worker = watchVoiceOpaque(root["worker_identity"]),
            WatchVoicePCMProfile.capture.matches(root["capture"]),
            WatchVoicePCMProfile.playback.matches(root["playback"])
        else { return nil }
        self.grantId = grantId
        self.sessionId = sessionId
        self.generation = generation
        mediaGrantRevision = revision
        self.expiresAt = expiresAt
        self.url = url
        self.ticket = ticket
        workerIdentity = worker
    }
}

struct WatchVoiceSessionGrant: Equatable, Sendable {
    let session: WatchVoiceSession
    let grant: WatchVoiceBridgeGrant

    init?(session: WatchVoiceSession, grant: WatchVoiceBridgeGrant) {
        guard session.sessionId == grant.sessionId,
            session.generation == grant.generation,
            session.mediaGrantRevision == grant.mediaGrantRevision
        else { return nil }
        self.session = session
        self.grant = grant
    }

    init?(json: JSONValue) {
        guard let root = json.objectValue,
            watchVoiceHasExactKeys(root, ["session", "grant"]),
            let session = WatchVoiceSession(json: root["session"] ?? .null),
            let grant = WatchVoiceBridgeGrant(json: root["grant"] ?? .null),
            session.sessionId == grant.sessionId,
            session.generation == grant.generation,
            session.mediaGrantRevision == grant.mediaGrantRevision,
            session.deviceId == watchVoiceUUID4(root["session"]?["device_id"])
        else { return nil }
        self.session = session
        self.grant = grant
    }
}

struct WatchVoiceBridgeReady: Equatable, Sendable {
    let sessionId: String
    let generation: UInt64
    let mediaGrantRevision: UInt64
    let workerIdentity: String

    init?(json: JSONValue, grant: WatchVoiceBridgeGrant) {
        guard let root = json.objectValue,
            watchVoiceHasExactKeys(
                root,
                [
                    "type", "schema_version", "session_id", "generation",
                    "media_grant_revision", "worker_identity", "capture", "playback",
                ]),
            root["type"]?.stringValue == "bridge_ready",
            root["schema_version"]?.stringValue == "1",
            let sessionId = watchVoiceUUID4(root["session_id"]), sessionId == grant.sessionId,
            let generation = watchVoiceUInt(root["generation"], minimum: 1),
            generation == grant.generation,
            let revision = watchVoiceUInt(root["media_grant_revision"], minimum: 1),
            revision == grant.mediaGrantRevision,
            let worker = watchVoiceOpaque(root["worker_identity"]),
            worker == grant.workerIdentity,
            WatchVoicePCMProfile.capture.matches(root["capture"]),
            WatchVoicePCMProfile.playback.matches(root["playback"])
        else { return nil }
        self.sessionId = sessionId
        self.generation = generation
        mediaGrantRevision = revision
        workerIdentity = worker
    }
}

struct WatchVoiceTranscript: Equatable, Sendable {
    let sessionId: String
    let generation: UInt64
    let turnId: String
    let clientTurnId: String
    let submissionId: String
    let requestGeneration: String
    let chatId: String
    let chatContextRevision: UInt64
    let mediaGrantRevision: UInt64
    let sequence: UInt64
    let final: Bool
    let text: String
    let detectedLanguage: String?
    let textDigest: String?
    let transcriptProof: String?
    let proofExpiresAt: String?
    let sourceParticipantIdentity: String

    init?(json: JSONValue) {
        let language = watchVoiceNullableLanguage(json["detected_language"])
        guard (try? json.encoded().count) ?? Int.max <= 12 * 1024,
            let root = json.objectValue,
            watchVoiceHasExactKeys(
                root,
                required: [
                    "type", "schema_version", "session_id", "generation", "turn_id",
                    "client_turn_id", "submission_id", "request_generation", "chat_id",
                    "chat_context_revision", "media_grant_revision", "sequence", "final",
                    "text", "detected_language", "source_participant_identity",
                ],
                optional: ["text_digest_sha256", "transcript_proof", "proof_expires_at"]),
            root["type"]?.stringValue == "voice_transcript",
            root["schema_version"]?.stringValue == "1",
            let sessionId = watchVoiceUUID4(root["session_id"]),
            let generation = watchVoiceUInt(root["generation"], minimum: 1),
            let turnId = watchVoiceUUID4(root["turn_id"]),
            let clientTurnId = watchVoiceUUID4(root["client_turn_id"]),
            let submissionId = watchVoiceUUID4(root["submission_id"]),
            let request = watchVoiceUUID4(root["request_generation"]),
            let chatId = watchVoiceUUID4(root["chat_id"]),
            let contextRevision = watchVoiceUInt(root["chat_context_revision"], minimum: 1),
            let grantRevision = watchVoiceUInt(root["media_grant_revision"], minimum: 1),
            let sequence = watchVoiceUInt(root["sequence"], minimum: 0),
            let final = root["final"]?.boolValue,
            let text = root["text"]?.stringValue, text.count <= 8000,
            language.valid,
            let worker = watchVoiceOpaque(root["source_participant_identity"])
        else { return nil }
        let proofKeys = ["text_digest_sha256", "transcript_proof", "proof_expires_at"]
        if final {
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                language.value != nil,
                let digest = root["text_digest_sha256"]?.stringValue,
                watchVoiceIsLowerSHA256(digest),
                let proof = root["transcript_proof"]?.stringValue,
                watchVoiceIsLowerSHA256(proof),
                watchVoiceDate(root["proof_expires_at"]) != nil
            else { return nil }
        } else if proofKeys.contains(where: { root[$0] != nil }) {
            return nil
        }
        self.sessionId = sessionId
        self.generation = generation
        self.turnId = turnId
        self.clientTurnId = clientTurnId
        self.submissionId = submissionId
        requestGeneration = request
        self.chatId = chatId
        chatContextRevision = contextRevision
        mediaGrantRevision = grantRevision
        self.sequence = sequence
        self.final = final
        self.text = text
        detectedLanguage = language.value
        textDigest = root["text_digest_sha256"]?.stringValue
        transcriptProof = root["transcript_proof"]?.stringValue
        proofExpiresAt = root["proof_expires_at"]?.stringValue
        sourceParticipantIdentity = worker
    }

    func matches(grant: WatchVoiceBridgeGrant) -> Bool {
        sessionId == grant.sessionId && generation == grant.generation
            && mediaGrantRevision == grant.mediaGrantRevision
            && sourceParticipantIdentity == grant.workerIdentity
    }
}

struct WatchVoiceAnnouncement: Equatable, Sendable {
    let sessionId: String
    let generation: UInt64
    let mediaGrantRevision: UInt64
    let announcementId: String
    let announcementSequence: UInt64
    let turnId: String?
    let kind: String
    let quantumRole: String
    let quantumIndex: UInt64
    let workerIdentity: String
    let durationSamples: UInt64
    let resultReservedSamplesAfter: UInt64?
    let firstMediaSequence: UInt64
    let lastMediaSequence: UInt64

    init?(json: JSONValue) {
        let turn = watchVoiceNullableUUID4(json["turn_id"])
        guard (try? json.encoded().count) ?? Int.max <= 12 * 1024,
            let root = json.objectValue,
            watchVoiceHasExactKeys(
                root,
                required: [
                    "type", "schema_version", "session_id", "generation",
                    "media_grant_revision", "announcement_id", "announcement_sequence",
                    "turn_id", "kind", "quantum_role", "quantum_index", "transport",
                    "worker_identity", "sample_rate_hz", "duration_samples",
                    "first_media_sequence", "last_media_sequence",
                ],
                optional: ["result_reserved_samples_after"]),
            root["type"]?.stringValue == "voice_announcement_media",
            root["schema_version"]?.stringValue == "1",
            root["transport"]?.stringValue == "watch_pcm_websocket",
            let sessionId = watchVoiceUUID4(root["session_id"]),
            let generation = watchVoiceUInt(root["generation"], minimum: 1),
            let grantRevision = watchVoiceUInt(root["media_grant_revision"], minimum: 1),
            let announcementId = watchVoiceUUID4(root["announcement_id"]),
            let announcementSequence = watchVoiceUInt(root["announcement_sequence"], minimum: 1),
            turn.valid,
            let kind = root["kind"]?.stringValue,
            WatchVoiceContract.announcementKinds.contains(kind),
            let role = root["quantum_role"]?.stringValue,
            WatchVoiceContract.quantumRoles.contains(role),
            let index = watchVoiceUInt(root["quantum_index"], minimum: 0), index <= 31,
            let worker = watchVoiceOpaque(root["worker_identity"]),
            watchVoiceUInt(root["sample_rate_hz"], minimum: 1) == 24_000,
            let duration = watchVoiceUInt(root["duration_samples"], minimum: 1),
            duration <= 96_000,
            let first = watchVoiceUInt(root["first_media_sequence"], minimum: 0),
            let last = watchVoiceUInt(root["last_media_sequence"], minimum: 0), last >= first,
            (last - first + 1) * 480 == duration
        else { return nil }
        let reserved = watchVoiceOptionalUInt(root["result_reserved_samples_after"], minimum: 1)
        guard reserved.valid, (reserved.value ?? 0) <= 720_000,
            (kind == "greeting") == (turn.value == nil)
        else { return nil }
        switch role {
        case "single":
            guard kind != "result", index == 0, reserved.value == nil else { return nil }
        case "result_opening":
            guard kind == "result", index == 0, duration <= 36_000,
                let amount = reserved.value, amount <= 36_000
            else { return nil }
        case "result_continuation":
            guard kind == "result", (1...31).contains(index), reserved.value != nil else {
                return nil
            }
        default:
            return nil
        }
        self.sessionId = sessionId
        self.generation = generation
        mediaGrantRevision = grantRevision
        self.announcementId = announcementId
        self.announcementSequence = announcementSequence
        turnId = turn.value
        self.kind = kind
        quantumRole = role
        quantumIndex = index
        workerIdentity = worker
        durationSamples = duration
        resultReservedSamplesAfter = reserved.value
        firstMediaSequence = first
        lastMediaSequence = last
    }

    func matches(grant: WatchVoiceBridgeGrant) -> Bool {
        sessionId == grant.sessionId && generation == grant.generation
            && mediaGrantRevision == grant.mediaGrantRevision
            && workerIdentity == grant.workerIdentity
    }
}

struct WatchVoicePCMFrame: Equatable, Sendable {
    static let headerLength = 26
    static let capturePayloadLength = 640
    static let assistantPayloadLength = 960

    enum Kind: UInt8, Equatable, Sendable {
        case microphone = 1
        case assistant = 2

        var payloadLength: Int {
            self == .microphone
                ? WatchVoicePCMFrame.capturePayloadLength
                : WatchVoicePCMFrame.assistantPayloadLength
        }
    }

    let kind: Kind
    let sequence: UInt64
    let timestampMicroseconds: UInt64
    let payload: Data

    init?(data: Data) {
        guard data.count >= Self.headerLength,
            data.prefix(4) == Data([0x41, 0x44, 0x56, 0x43]),
            data[4] == 1,
            let kind = Kind(rawValue: data[5]),
            data[6] == 0, data[7] == 0
        else { return nil }
        let sequence = data.watchVoiceUInt64(at: 8)
        let timestamp = data.watchVoiceUInt64(at: 16)
        let length = Int(data.watchVoiceUInt16(at: 24))
        guard length == kind.payloadLength, data.count == Self.headerLength + length else {
            return nil
        }
        self.kind = kind
        self.sequence = sequence
        timestampMicroseconds = timestamp
        payload = data.subdata(in: Self.headerLength..<data.count)
    }

    init?(kind: Kind, sequence: UInt64, timestampMicroseconds: UInt64, payload: Data) {
        guard payload.count == kind.payloadLength else { return nil }
        self.kind = kind
        self.sequence = sequence
        self.timestampMicroseconds = timestampMicroseconds
        self.payload = payload
    }

    var encoded: Data {
        var data = Data(capacity: Self.headerLength + payload.count)
        data.append(contentsOf: [0x41, 0x44, 0x56, 0x43, 1, kind.rawValue, 0, 0])
        data.watchVoiceAppendBigEndian(sequence)
        data.watchVoiceAppendBigEndian(timestampMicroseconds)
        data.watchVoiceAppendBigEndian(UInt16(payload.count))
        data.append(payload)
        return data
    }
}

struct WatchVoicePCMSequenceGate: Sendable {
    private(set) var last: [WatchVoicePCMFrame.Kind: UInt64] = [:]

    mutating func accept(_ frame: WatchVoicePCMFrame) -> Bool {
        if let prior = last[frame.kind], prior == UInt64.max || frame.sequence != prior + 1 {
            return false
        }
        last[frame.kind] = frame.sequence
        return true
    }

    mutating func reset() { last.removeAll() }
}

struct WatchVoiceAnnouncementLedger: Sendable {
    private var lastSequence: UInt64 = 0
    private var resultSamples: [String: UInt64] = [:]
    private var resultIndex: [String: UInt64] = [:]

    mutating func accept(_ announcement: WatchVoiceAnnouncement) -> Bool {
        guard announcement.announcementSequence > lastSequence else { return false }
        if announcement.kind == "result" {
            guard let turnId = announcement.turnId else { return false }
            let prior = resultSamples[turnId] ?? 0
            guard
                resultSamples[turnId] != nil || resultSamples.count < 64,
                announcement.quantumIndex == (resultIndex[turnId] ?? 0),
                announcement.durationSamples <= 720_000 - prior,
                announcement.resultReservedSamplesAfter.map({
                    $0 >= announcement.durationSamples
                }) == true
            else { return false }
            let next = prior + announcement.durationSamples
            resultSamples[turnId] = next
            resultIndex[turnId] = announcement.quantumIndex + 1
        }
        lastSequence = announcement.announcementSequence
        return true
    }

    mutating func reset() {
        lastSequence = 0
        resultSamples.removeAll(keepingCapacity: false)
        resultIndex.removeAll(keepingCapacity: false)
    }
}

enum WatchVoicePCMProfile {
    case capture
    case playback

    var sampleRate: UInt64 { self == .capture ? 16_000 : 24_000 }

    func matches(_ json: JSONValue?) -> Bool {
        guard let object = json?.objectValue,
            watchVoiceHasExactKeys(
                object,
                ["encoding", "channels", "sample_rate_hz", "frame_duration_ms"])
        else { return false }
        return object["encoding"]?.stringValue == "pcm_s16le"
            && watchVoiceUInt(object["channels"], minimum: 1) == 1
            && watchVoiceUInt(object["sample_rate_hz"], minimum: 1) == sampleRate
            && watchVoiceUInt(object["frame_duration_ms"], minimum: 1) == 20
    }
}

enum WatchVoiceContract {
    static let actions: Set<String> = [
        "voice_session_start", "voice_session_takeover", "voice_session_end",
        "voice_microphone_set", "voice_speech_stop", "voice_speech_mute_set",
        "voice_visible_chat_update", "voice_sensitive_recap_request",
    ]
    static let reasons: Set<String> = [
        "ready", "feature_disabled", "authentication_required", "permission_not_determined",
        "permission_denied", "permission_restricted", "no_microphone", "no_audio_output",
        "media_unavailable", "worker_unavailable", "asr_unavailable", "tts_unavailable",
        "voice_unavailable", "output_language_unsupported", "capacity_exhausted",
        "takeover_required", "idle_expired", "backgrounded", "audio_interrupted",
        "chat_context_unavailable", "auth_expired", "network_interrupted", "media_error",
        "speech_error", "stale_generation", "ended_by_user", "internal_error",
    ]
    static let announcementKinds: Set<String> = [
        "greeting", "acknowledgement", "progress", "waiting", "result",
        "sensitive_notice", "failure", "refusal", "cancellation",
    ]
    static let quantumRoles: Set<String> = [
        "single", "result_opening", "result_continuation",
    ]
}

func watchVoiceJSON(_ object: [String: JSONValue]) -> String? {
    guard let data = try? JSONValue.object(object).encoded(), data.count <= 48 * 1024 else {
        return nil
    }
    return String(data: data, encoding: .utf8)
}

private func watchVoiceHasExactKeys(
    _ object: [String: JSONValue], _ keys: Set<String>
) -> Bool {
    Set(object.keys) == keys
}

private func watchVoiceHasExactKeys(
    _ object: [String: JSONValue], required: Set<String>, optional: Set<String>
) -> Bool {
    Set(object.keys).isSuperset(of: required)
        && Set(object.keys).isSubset(of: required.union(optional))
}

private func watchVoiceUUID4(_ value: JSONValue?) -> String? {
    guard let string = value?.stringValue,
        string.range(
            of: "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            options: .regularExpression) != nil,
        UUID(uuidString: string) != nil
    else { return nil }
    return string
}

private func watchVoiceNullableUUID4(_ value: JSONValue?) -> (valid: Bool, value: String?) {
    guard let value else { return (true, nil) }
    if value == .null { return (true, nil) }
    guard let uuid = watchVoiceUUID4(value) else { return (false, nil) }
    return (true, uuid)
}

private func watchVoiceUInt(_ value: JSONValue?, minimum: UInt64) -> UInt64? {
    guard let number = value?.numberValue, number.isFinite, number.rounded() == number,
        number >= Double(minimum), number <= 9_007_199_254_740_991
    else { return nil }
    return UInt64(number)
}

private func watchVoiceNullableUInt(
    _ value: JSONValue?, minimum: UInt64
) -> (valid: Bool, value: UInt64?) {
    guard let value else { return (true, nil) }
    if value == .null { return (true, nil) }
    guard let result = watchVoiceUInt(value, minimum: minimum) else { return (false, nil) }
    return (true, result)
}

private func watchVoiceOptionalUInt(
    _ value: JSONValue?, minimum: UInt64
) -> (valid: Bool, value: UInt64?) {
    guard let value else { return (true, nil) }
    guard let result = watchVoiceUInt(value, minimum: minimum) else { return (false, nil) }
    return (true, result)
}

private func watchVoiceBoundedString(
    _ value: JSONValue?, maximum: Int, nonempty: Bool = false
) -> String? {
    guard let string = value?.stringValue, string.count <= maximum,
        !nonempty || !string.isEmpty
    else { return nil }
    return string
}

private func watchVoiceOpaque(_ value: JSONValue?) -> String? {
    guard let string = value?.stringValue, (1...128).contains(string.utf8.count),
        string.range(of: "^[A-Za-z0-9._:-]+$", options: .regularExpression) != nil
    else { return nil }
    return string
}

private func watchVoiceDate(_ value: JSONValue?) -> Date? {
    guard let string = value?.stringValue else { return nil }
    let formatter = ISO8601DateFormatter()
    if let date = formatter.date(from: string) { return date }
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.date(from: string)
}

private func watchVoiceNullableDate(_ value: JSONValue?) -> (valid: Bool, value: Date?) {
    guard let value else { return (true, nil) }
    if value == .null { return (true, nil) }
    guard let date = watchVoiceDate(value) else { return (false, nil) }
    return (true, date)
}

private func watchVoiceNullableLanguage(
    _ value: JSONValue?
) -> (valid: Bool, value: String?) {
    guard let value else { return (false, nil) }
    if value == .null { return (true, nil) }
    guard let string = value.stringValue, (2...32).contains(string.count),
        string.range(
            of: "^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$",
            options: .regularExpression) != nil
    else { return (false, nil) }
    return (true, string)
}

private func watchVoiceIsLowerSHA256(_ value: String) -> Bool {
    value.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil
}

extension Data {
    fileprivate func watchVoiceUInt64(at offset: Int) -> UInt64 {
        var result: UInt64 = 0
        for byte in self[offset..<(offset + 8)] { result = (result << 8) | UInt64(byte) }
        return result
    }

    fileprivate func watchVoiceUInt16(at offset: Int) -> UInt16 {
        (UInt16(self[offset]) << 8) | UInt16(self[offset + 1])
    }

    fileprivate mutating func watchVoiceAppendBigEndian(_ value: UInt64) {
        append(contentsOf: (0..<8).reversed().map { UInt8(truncatingIfNeeded: value >> ($0 * 8)) })
    }

    fileprivate mutating func watchVoiceAppendBigEndian(_ value: UInt16) {
        append(UInt8(truncatingIfNeeded: value >> 8))
        append(UInt8(truncatingIfNeeded: value))
    }
}
