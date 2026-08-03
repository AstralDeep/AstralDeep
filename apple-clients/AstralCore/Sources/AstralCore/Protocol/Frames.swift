// Feature 051 — WS frame parsing (inbound) and builders (outbound).
// Inbound frames are decoded leniently: every frame becomes an InboundFrame
// (name + JSONValue payload) plus typed accessors for the handled set, so an
// unknown or future frame can never crash a client (FR-003).
import Foundation

public struct InboundFrame: Sendable, Equatable {
    public let name: String
    public let payload: JSONValue

    public init(name: String, payload: JSONValue) {
        self.name = name
        self.payload = payload
    }

    public static func parse(_ text: String) -> InboundFrame? {
        guard let data = text.data(using: .utf8),
            let json = try? JSONValue.parse(data),
            let type = json["type"]?.stringValue, !type.isEmpty
        else { return nil }
        return InboundFrame(name: type, payload: json)
    }

    // MARK: typed accessors (handled set)

    /// ui_render — components already ROTE-adapted for THIS socket.
    public var renderComponents: [AstralComponent] {
        AstralComponent.list(from: payload["components"])
    }

    public var renderTarget: String {
        payload["target"]?.stringValue ?? "canvas"
    }

    /// 051: spoken rendition (watch sockets only; absent elsewhere).
    public var speech: Speech? { Speech(json: payload["speech"]) }

    /// ui_upsert ops.
    public var upsertOps: [UpsertOp] {
        payload["ops"]?.arrayValue?.compactMap { UpsertOp(json: $0) } ?? []
    }

    public var chatId: String? {
        payload["chat_id"]?.stringValue ?? payload["chatId"]?.stringValue
    }

    /// ui_stream_data — incremental narrative components.
    public var streamComponents: [AstralComponent] {
        AstralComponent.list(from: payload["components"])
    }

    public var streamTerminal: Bool {
        payload["terminal"]?.boolValue ?? false
    }

    /// error / stream_error — normalized human message (044 contract).
    public var errorMessage: String {
        payload["message"]?.stringValue
            ?? payload["payload"]?["message"]?.stringValue
            ?? payload["error"]?.stringValue
            ?? "Something went wrong."
    }

    /// auth_required — reason: expired | invalid | hard_cap.
    public var authReason: String {
        payload["reason"]?.stringValue ?? "expired"
    }

    /// chrome_surface — presentation mode (054 first-run gate). "mandatory"
    /// means: accept the surface even though unsolicited and suppress every
    /// dismissal affordance until the server replaces or closes it. Additive
    /// field on an EXISTING frame type (no manifest change); absent on
    /// pre-054 servers ⇒ "replace".
    public var surfaceMode: String {
        payload["mode"]?.stringValue ?? "replace"
    }

    /// chat_status / chat_step progress text. The wire `status` is a MACHINE
    /// code ("thinking"/"executing"/"done"/…) and `message` carries the human
    /// text; `step` is an object on chat_step. Terminal codes resolve to nil
    /// so a finished turn clears the status line instead of sticking on a
    /// literal "done" (parity with the web client's status map).
    public var statusText: String? {
        let status = payload["status"]?.stringValue
        if status == "done" || status == "idle" { return nil }
        if let message = payload["message"]?.stringValue, !message.isEmpty { return message }
        switch status {
        case "thinking": return "Thinking…"
        case "executing", "processing_async", "fixing": return "Working…"
        default: break
        }
        if let step = payload["step"] {
            if let s = step.stringValue, !s.isEmpty { return s }
            if let name = step["name"]?.stringValue ?? step["kind"]?.stringValue, !name.isEmpty {
                return name
            }
        }
        if let status, !status.isEmpty { return status }
        return nil
    }
}

// MARK: - Feature 060 canonical reliability frames

private let maximumExactJSONInteger = UInt64(9_007_199_254_740_991)

private func canonicalUUID(_ value: JSONValue?) -> String? {
    continuityUUID4(value?.stringValue)
}

private func unsignedInteger(_ value: JSONValue?) -> UInt64? {
    guard let number = value?.numberValue, number.isFinite,
        number >= 0, number.rounded() == number,
        number <= Double(maximumExactJSONInteger)
    else { return nil }
    return UInt64(number)
}

private func isRFC3339UTC(_ value: String) -> Bool {
    guard value.hasSuffix("Z") else { return false }
    if ISO8601DateFormatter().date(from: value) != nil { return true }
    // A plain ISO8601DateFormatter rejects fractional seconds, which valid
    // RFC 3339 producers may emit — accept them rather than dropping the frame.
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return fractional.date(from: value) != nil
}

private func isSnakeCase(_ value: String) -> Bool {
    value.range(
        of: "^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
        options: .regularExpression) != nil
}

private func hasExactKeys(_ object: [String: JSONValue], _ keys: Set<String>) -> Bool {
    Set(object.keys) == keys
}

private func explicitNullableUUID(_ value: JSONValue?) -> (valid: Bool, value: String?) {
    guard let value else { return (false, nil) }
    if value == .null { return (true, nil) }
    guard let uuid = canonicalUUID(value) else { return (false, nil) }
    return (true, uuid)
}

public struct ConversationSnapshot: Sendable, Equatable {
    public let schemaVersion: Int
    public let snapshotId: String
    public let chatId: String
    public let connectionGeneration: String
    public let requestGeneration: String
    public let snapshotPurpose: String
    public let renderRevision: UInt64
    public let committedAt: String
    public let transcript: [JSONValue]
    public let canvas: JSONValue
    public let messages: [ConversationMessage]
    public let canvasComponents: [AstralComponent]

    public init?(frame: InboundFrame) {
        guard frame.name == "conversation_snapshot",
            let object = frame.payload.objectValue,
            hasExactKeys(
                object,
                [
                    "type", "schema_version", "snapshot_id", "chat_id",
                    "connection_generation", "request_generation", "snapshot_purpose",
                    "render_revision", "committed_at", "transcript", "canvas",
                ]),
            object["type"]?.stringValue == "conversation_snapshot",
            object["schema_version"]?.numberValue == 1,
            let snapshotId = canonicalUUID(object["snapshot_id"]),
            let chatId = canonicalUUID(object["chat_id"]),
            let connectionGeneration = canonicalUUID(object["connection_generation"]),
            let requestGeneration = canonicalUUID(object["request_generation"]),
            let snapshotPurpose = object["snapshot_purpose"]?.stringValue,
            ["hydration", "commit"].contains(snapshotPurpose),
            let renderRevision = unsignedInteger(object["render_revision"]),
            let committedAt = object["committed_at"]?.stringValue,
            isRFC3339UTC(committedAt),
            let transcript = object["transcript"]?.arrayValue,
            let canvasObject = object["canvas"]?.objectValue,
            hasExactKeys(canvasObject, ["target", "components"]),
            canvasObject["target"]?.stringValue == "canvas",
            let rawCanvasComponents = canvasObject["components"]?.arrayValue,
            rawCanvasComponents.allSatisfy({ $0["_presentation"] == nil }),
            let canvas = object["canvas"]
        else { return nil }
        let messages = transcript.compactMap(ConversationMessage.init(json:))
        let canvasComponents = rawCanvasComponents.compactMap(AstralComponent.init(json:))
        guard messages.count == transcript.count,
            canvasComponents.count == rawCanvasComponents.count
        else { return nil }
        self.schemaVersion = 1
        self.snapshotId = snapshotId
        self.chatId = chatId
        self.connectionGeneration = connectionGeneration
        self.requestGeneration = requestGeneration
        self.snapshotPurpose = snapshotPurpose
        self.renderRevision = renderRevision
        self.committedAt = committedAt
        self.transcript = transcript
        self.canvas = canvas
        self.messages = messages
        self.canvasComponents = canvasComponents
    }
}

public struct OperationStatus: Sendable, Equatable {
    public let operationId: String
    public let action: String
    public let surface: String
    public let chatId: String?
    public let connectionGeneration: String
    public let requestGeneration: String
    public let sequence: UInt64
    public let state: String
    public let phase: String
    public let label: String
    public let terminal: Bool
    public let retryable: Bool
    public let error: JSONValue
    public let retryAfterMs: UInt64?
    public let updatedAt: String

    public init?(frame: InboundFrame) {
        guard frame.name == "operation_status",
            let object = frame.payload.objectValue,
            hasExactKeys(
                object,
                [
                    "type", "operation_id", "action", "surface", "chat_id",
                    "connection_generation", "request_generation", "sequence", "state",
                    "phase", "label", "terminal", "retryable", "error",
                    "retry_after_ms", "updated_at",
                ]),
            object["type"]?.stringValue == "operation_status",
            let operationId = canonicalUUID(object["operation_id"]),
            let action = object["action"]?.stringValue, isSnakeCase(action),
            let surface = object["surface"]?.stringValue, isSnakeCase(surface),
            explicitNullableUUID(object["chat_id"]).valid,
            let connectionGeneration = canonicalUUID(object["connection_generation"]),
            let requestGeneration = canonicalUUID(object["request_generation"]),
            let sequence = unsignedInteger(object["sequence"]),
            let state = object["state"]?.stringValue,
            let phase = object["phase"]?.stringValue, isSnakeCase(phase),
            let label = object["label"]?.stringValue, !label.isEmpty,
            let terminal = object["terminal"]?.boolValue,
            let retryable = object["retryable"]?.boolValue,
            let error = object["error"],
            let updatedAt = object["updated_at"]?.stringValue,
            isRFC3339UTC(updatedAt)
        else { return nil }
        let flags: [String: (Bool, Bool)] = [
            "accepted": (false, false), "validating": (false, false),
            "persisting": (false, false), "running": (false, false),
            "completed": (true, false), "failed": (true, false),
            "cancelled": (true, false), "retryable": (true, true),
        ]
        guard let expected = flags[state], expected == (terminal, retryable) else { return nil }
        let requiresError = ["failed", "cancelled", "retryable"].contains(state)
        if requiresError {
            guard let errorObject = error.objectValue,
                hasExactKeys(errorObject, ["code", "message"]),
                let code = errorObject["code"]?.stringValue,
                Self.canonicalErrorCodes.contains(code),
                let message = errorObject["message"]?.stringValue,
                !message.isEmpty
            else { return nil }
        } else if error != .null {
            return nil
        }
        let retryAfter: UInt64?
        if object["retry_after_ms"] == .null {
            retryAfter = nil
        } else {
            guard state == "retryable",
                let value = unsignedInteger(object["retry_after_ms"])
            else { return nil }
            retryAfter = value
        }
        self.operationId = operationId
        self.action = action
        self.surface = surface
        self.chatId = explicitNullableUUID(object["chat_id"]).value
        self.connectionGeneration = connectionGeneration
        self.requestGeneration = requestGeneration
        self.sequence = sequence
        self.state = state
        self.phase = phase
        self.label = label
        self.terminal = terminal
        self.retryable = retryable
        self.error = error
        self.retryAfterMs = retryAfter
        self.updatedAt = updatedAt
    }

    private static let canonicalErrorCodes: Set<String> = [
        "invalid_input", "validation_failed", "provider_unavailable",
        "network_unavailable", "deadline_exceeded", "capacity_exceeded",
        "queue_wait_expired", "registration_timeout", "disconnected",
        "cancelled_by_user", "operation_failed", "conflict",
        "incompatible_runtime", "agent_offline", "stale_generation",
    ]
}

public struct AdmissionRefusal: Sendable, Equatable {
    public let submissionId: String
    public let code: String
    public let message: String
    public let retryable: Bool
    public let retryAfterMs: UInt64?

    public init?(frame: InboundFrame) {
        guard frame.name == "error",
            let object = frame.payload.objectValue,
            hasExactKeys(
                object,
                [
                    "type", "submission_id", "accepted", "code", "message",
                    "retryable", "retry_after_ms",
                ]),
            object["type"]?.stringValue == "error",
            object["accepted"]?.boolValue == false,
            let submissionId = canonicalUUID(object["submission_id"]),
            let code = object["code"]?.stringValue,
            Self.canonicalCodes.contains(code),
            let message = object["message"]?.stringValue,
            !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
            let retryable = object["retryable"]?.boolValue
        else { return nil }

        let retryAfterMs: UInt64?
        if object["retry_after_ms"] == .null {
            retryAfterMs = nil
        } else {
            guard retryable,
                let value = unsignedInteger(object["retry_after_ms"])
            else { return nil }
            retryAfterMs = value
        }
        self.submissionId = submissionId
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retryAfterMs = retryAfterMs
    }

    private static let canonicalCodes: Set<String> = [
        "capacity_exceeded", "registration_required", "registration_timeout",
        "idempotency_conflict", "connection_closing", "service_draining",
        "invalid_input", "registration_queue_full", "operation_failed",
    ]
}

public struct AgentLifecycle: Sendable, Equatable {
    public let agentId: String
    public let revisionId: String?
    public let runtimeInstanceId: String?
    public let lifecycleGeneration: UInt64
    public let stateRevision: UInt64
    public let state: String
    public let reasonCode: String?
    public let label: String
    public let updatedAt: String

    public init?(frame: InboundFrame) {
        guard frame.name == "agent_lifecycle",
            let object = frame.payload.objectValue,
            hasExactKeys(
                object,
                [
                    "type", "agent_id", "revision_id", "runtime_instance_id",
                    "lifecycle_generation", "state_revision", "state", "reason_code",
                    "label", "updated_at",
                ]),
            object["type"]?.stringValue == "agent_lifecycle",
            let agentId = object["agent_id"]?.stringValue, !agentId.isEmpty,
            explicitNullableUUID(object["revision_id"]).valid,
            explicitNullableUUID(object["runtime_instance_id"]).valid,
            let lifecycleGeneration = unsignedInteger(object["lifecycle_generation"]),
            let stateRevision = unsignedInteger(object["state_revision"]),
            let state = object["state"]?.stringValue,
            ["starting", "online", "updating", "failed", "offline"].contains(state),
            let reasonValue = object["reason_code"],
            let label = object["label"]?.stringValue, !label.isEmpty,
            let updatedAt = object["updated_at"]?.stringValue,
            isRFC3339UTC(updatedAt)
        else { return nil }
        let revisionId = explicitNullableUUID(object["revision_id"]).value
        let runtimeInstanceId = explicitNullableUUID(object["runtime_instance_id"]).value
        if ["starting", "online", "updating"].contains(state) {
            guard revisionId != nil, runtimeInstanceId != nil else { return nil }
        }
        guard runtimeInstanceId == nil || revisionId != nil else { return nil }
        let reasonCode: String?
        if reasonValue == .null {
            reasonCode = nil
        } else {
            guard let value = reasonValue.stringValue,
                Self.canonicalReasonCodes.contains(value)
            else { return nil }
            reasonCode = value
        }
        if ["starting", "online", "updating"].contains(state), reasonCode != nil {
            return nil
        }
        self.agentId = agentId
        self.revisionId = revisionId
        self.runtimeInstanceId = runtimeInstanceId
        self.lifecycleGeneration = lifecycleGeneration
        self.stateRevision = stateRevision
        self.state = state
        self.reasonCode = reasonCode
        self.label = label
        self.updatedAt = updatedAt
    }

    private static let canonicalReasonCodes: Set<String> = [
        "invalid_host_registration", "runtime_contract_unsupported",
        "runtime_lock_mismatch", "bundle_digest_mismatch", "bundle_install_failed",
        "child_start_failed", "child_registration_timeout", "child_exited",
        "child_hung", "host_lost", "agent_offline", "agent_deleted",
        "stale_runtime_generation", "revision_promotion_failed",
        "inventory_required", "process_cleanup_timeout",
    ]
}

public struct AgentHostRegistration: Sendable, Equatable {
    public let hostId: String
    public let supportedRuntimeContractVersions: [Int]
    public let runtimeLockSHA256: String
    public let platform: String
    public let clientVersion: String

    public init?(
        hostId: String, supportedRuntimeContractVersions: [Int],
        runtimeLockSHA256: String, platform: String, clientVersion: String
    ) {
        guard canonicalUUID(.string(hostId)) != nil,
            !supportedRuntimeContractVersions.isEmpty,
            supportedRuntimeContractVersions.allSatisfy({ $0 > 0 }),
            Array(Set(supportedRuntimeContractVersions)).sorted()
                == supportedRuntimeContractVersions,
            runtimeLockSHA256.range(
                of: "^[0-9a-f]{64}$",
                options: .regularExpression) != nil,
            ["windows", "macos"].contains(platform),
            clientVersion.range(
                of: "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(?:[-+][0-9A-Za-z.-]+)?$",
                options: .regularExpression
            ) != nil
        else { return nil }
        self.hostId = hostId
        self.supportedRuntimeContractVersions = supportedRuntimeContractVersions
        self.runtimeLockSHA256 = runtimeLockSHA256
        self.platform = platform
        self.clientVersion = clientVersion
    }

    public var json: JSONValue {
        .object([
            "host_id": .string(hostId),
            "supported_runtime_contract_versions": .array(
                supportedRuntimeContractVersions.map { .number(Double($0)) }),
            "runtime_lock_sha256": .string(runtimeLockSHA256),
            "platform": .string(platform),
            "client_version": .string(clientVersion),
        ])
    }
}

public struct AgentHostRegistered: Sendable, Equatable {
    public let hostId: String
    public let hostSessionId: String
    public let inventoryRequired: Bool
    public let acceptedAt: String

    public init?(frame: InboundFrame) {
        guard frame.name == "agent_host_registered",
            let object = frame.payload.objectValue,
            hasExactKeys(
                object,
                [
                    "type", "host_id", "host_session_id", "inventory_required", "accepted_at",
                ]),
            object["type"]?.stringValue == "agent_host_registered",
            let hostId = canonicalUUID(object["host_id"]),
            let hostSessionId = canonicalUUID(object["host_session_id"]),
            let inventoryRequired = object["inventory_required"]?.boolValue,
            let acceptedAt = object["accepted_at"]?.stringValue,
            isRFC3339UTC(acceptedAt)
        else { return nil }
        self.hostId = hostId
        self.hostSessionId = hostSessionId
        self.inventoryRequired = inventoryRequired
        self.acceptedAt = acceptedAt
    }
}

public struct PersonalAgentHostCapability: Sendable, Equatable {
    public let supported: Bool
    public let runtimeContractVersions: [Int]
    public let sourceFeature: String?

    public init?(json: JSONValue) {
        guard let object = json.objectValue,
            hasExactKeys(
                object,
                [
                    "supported", "runtime_contract_versions", "source_feature",
                ]),
            let supported = object["supported"]?.boolValue,
            let rawVersions = object["runtime_contract_versions"]?.arrayValue
        else { return nil }
        let versions = rawVersions.compactMap { value -> Int? in
            guard let number = unsignedInteger(value), number > 0,
                number <= UInt64(Int.max)
            else { return nil }
            return Int(number)
        }
        guard versions.count == rawVersions.count,
            Array(Set(versions)).sorted() == versions
        else { return nil }
        let source: String?
        if object["source_feature"] == .null {
            source = nil
        } else {
            guard let value = object["source_feature"]?.stringValue else { return nil }
            source = value
        }
        if supported {
            guard versions.contains(2), source == "059" else { return nil }
        } else {
            guard versions.isEmpty, source == nil else { return nil }
        }
        self.supported = supported
        self.runtimeContractVersions = versions
        self.sourceFeature = source
    }
}

public struct CandidateCapabilityMap: Sendable, Equatable {
    public let macOSPersonalAgentHost: PersonalAgentHostCapability

    public init?(json: JSONValue) {
        guard let root = json.objectValue,
            hasExactKeys(root, ["capabilities"]),
            let capabilities = root["capabilities"]?.objectValue,
            hasExactKeys(capabilities, ["personal_agent_host"]),
            let personalAgentHost = capabilities["personal_agent_host"]?.objectValue,
            hasExactKeys(personalAgentHost, ["macos"]),
            let macOS = personalAgentHost["macos"],
            let value = PersonalAgentHostCapability(json: macOS)
        else { return nil }
        self.macOSPersonalAgentHost = value
    }
}

public struct UpsertOp: Sendable, Equatable {
    public let op: String  // "upsert" | "remove"
    public let componentId: String?
    public let component: AstralComponent?

    public init(op: String, componentId: String?, component: AstralComponent?) {
        self.op = op
        self.componentId = componentId
        self.component = component
    }

    public init?(json: JSONValue) {
        guard let o = json.objectValue, let op = o["op"]?.stringValue else { return nil }
        self.op = op
        self.componentId = o["component_id"]?.stringValue
        self.component = o["component"].flatMap { AstralComponent(json: $0) }
    }
}

// MARK: - Outbound

/// Device identity reported in register_ui — drives the server-side ROTE
/// profile (FR-002). `supportedTypes` is the capability negotiation set: the
/// component types this client renders natively (everything else is
/// substituted server-side).
public struct DeviceDescriptor: Sendable {
    /// Stable, non-secret installation UUID. The live socket binding remains
    /// the authority for every voice control mutation.
    public var deviceId: String?
    public var deviceType: String
    public var viewportWidth: Int
    public var viewportHeight: Int
    public var pixelRatio: Double
    public var hasTouch: Bool
    public var hasMicrophone: Bool
    public var hasAudioOutput: Bool
    public var microphonePermission: String
    public var fullDuplex: Bool
    public var voiceTransport: String
    public var supportedTypes: [String]
    public var userAgent: String
    /// 066 capability-envelope fields. Additive on the wire — older servers
    /// ignore unknown device keys, so sending them is always safe.
    public var reducedMotion: Bool
    public var pointerType: String

    public init(
        deviceType: String, viewportWidth: Int, viewportHeight: Int,
        deviceId: String? = nil,
        pixelRatio: Double = 2.0, hasTouch: Bool = true,
        hasMicrophone: Bool = true, hasAudioOutput: Bool = true,
        microphonePermission: String = "not_determined",
        fullDuplex: Bool = true, voiceTransport: String = "livekit",
        supportedTypes: [String],
        userAgent: String,
        reducedMotion: Bool = false,
        pointerType: String = "coarse"
    ) {
        self.deviceId = deviceId
        self.deviceType = deviceType
        self.viewportWidth = viewportWidth
        self.viewportHeight = viewportHeight
        self.pixelRatio = pixelRatio
        self.hasTouch = hasTouch
        self.hasMicrophone = hasMicrophone
        self.hasAudioOutput = hasAudioOutput
        self.microphonePermission = microphonePermission
        self.fullDuplex = fullDuplex
        self.voiceTransport = voiceTransport
        self.supportedTypes = supportedTypes
        self.userAgent = userAgent
        self.reducedMotion = reducedMotion
        self.pointerType = pointerType
    }

    public static func ios(viewportWidth: Int, viewportHeight: Int) -> DeviceDescriptor {
        DeviceDescriptor(
            deviceType: "ios", viewportWidth: viewportWidth,
            viewportHeight: viewportHeight,
            supportedTypes: ClientDispositions.ios.nativeComponentTypes,
            userAgent: "AstralDeep-iOS/0.1")
    }

    public static func macos(viewportWidth: Int, viewportHeight: Int) -> DeviceDescriptor {
        DeviceDescriptor(
            deviceType: "macos", viewportWidth: viewportWidth,
            viewportHeight: viewportHeight, pixelRatio: 2.0,
            hasTouch: false,
            supportedTypes: ClientDispositions.macos.nativeComponentTypes,
            userAgent: "AstralDeep-macOS/0.1",
            pointerType: "fine")
    }

    public static func watch(viewportWidth: Int, viewportHeight: Int) -> DeviceDescriptor {
        DeviceDescriptor(
            deviceType: "watch", viewportWidth: viewportWidth,
            viewportHeight: viewportHeight,
            supportedTypes: ClientDispositions.watch.nativeComponentTypes,
            userAgent: "AstralDeep-watchOS/0.1")
    }

    var json: JSONValue {
        .object([
            "device_type": .string(deviceType),
            "screen_width": .number(Double(viewportWidth)),
            "screen_height": .number(Double(viewportHeight)),
            "viewport_width": .number(Double(viewportWidth)),
            "viewport_height": .number(Double(viewportHeight)),
            "pixel_ratio": .number(pixelRatio),
            "has_touch": .bool(hasTouch),
            "has_microphone": .bool(hasMicrophone),
            "has_audio_output": .bool(hasAudioOutput),
            "microphone_permission": .string(microphonePermission),
            "full_duplex": .bool(fullDuplex),
            "voice_transport": .string(voiceTransport),
            "has_camera": .bool(false),
            "has_file_system": .bool(deviceType != "watch"),
            "connection_type": .string("wifi"),
            "reduced_motion": .bool(reducedMotion),
            "pointer_type": .string(pointerType),
            "user_agent": .string(userAgent),
            "supported_types": .array(supportedTypes.map { .string($0) }),
        ])
    }
}

public struct ChatAttachmentRef: Sendable {
    public let attachmentId: String
    public let filename: String
    public let category: String

    public init(attachmentId: String, filename: String, category: String) {
        self.attachmentId = attachmentId
        self.filename = filename
        self.category = category
    }

    var json: JSONValue {
        .object([
            "attachment_id": .string(attachmentId),
            "filename": .string(filename),
            "category": .string(category),
        ])
    }
}

public enum Outbound {
    static func encode(_ value: JSONValue) -> String {
        guard let data = try? value.encoded(),
            let text = String(data: data, encoding: .utf8)
        else { return "{}" }
        return text
    }

    public static func registerUI(
        token: String, sessionId: String?,
        device: DeviceDescriptor, resumed: Bool,
        connectionGeneration: String = UUID().uuidString.lowercased(),
        resume: ConversationResumeRegistration? = nil
    ) -> String {
        // Feature 060: every shipping Apple target is explicitly author-only.
        // Do not add `agent_host` or its capability here; feature 059 alone
        // may enable macOS by supplying the structured model above.
        guard continuityUUID4(connectionGeneration) != nil else { return "{}" }
        let voiceCapable =
            device.hasMicrophone && device.hasAudioOutput
            && device.deviceId.flatMap(continuityUUID4) != nil
        var capabilities = ["render", "stream"]
        if voiceCapable { capabilities.append("voice") }
        var frame: [String: JSONValue] = [
            "type": .string("register_ui"),
            "token": .string(token),
            "session_id": sessionId.map(JSONValue.string) ?? .null,
            "capabilities": .array(capabilities.map(JSONValue.string)),
            "device": device.json,
            "resumed": .bool(resumed),
            "connection_generation": .string(connectionGeneration),
        ]
        if voiceCapable, let deviceId = device.deviceId {
            frame["device_id"] = .string(deviceId)
        }
        if let resume { frame["resume"] = resume.json }
        return encode(.object(frame))
    }

    public static func chatMessage(
        _ message: String, sessionId: String?,
        displayMessage: String? = nil,
        attachments: [ChatAttachmentRef] = [],
        submissionId: String = UUID().uuidString.lowercased(),
        requestGeneration: String = UUID().uuidString.lowercased()
    ) -> String {
        var payload: [String: JSONValue] = ["message": .string(message)]
        if let display = displayMessage { payload["display_message"] = .string(display) }
        if !attachments.isEmpty {
            payload["attachments"] = .array(attachments.map { $0.json })
        }
        return uiEvent(
            action: "chat_message",
            sessionId: sessionId,
            payload: .object(payload),
            submissionId: submissionId,
            requestGeneration: requestGeneration,
            snapshotPurpose: .commit)
    }

    public static func newChat(
        sessionId: String?, agentId: String? = nil,
        submissionId: String = UUID().uuidString.lowercased(),
        requestGeneration: String = UUID().uuidString.lowercased()
    ) -> String {
        var payload: [String: JSONValue] = [:]
        if let agent = agentId { payload["agent_id"] = .string(agent) }
        return uiEvent(
            action: "new_chat", sessionId: sessionId, payload: .object(payload),
            submissionId: submissionId, requestGeneration: requestGeneration)
    }

    public static func loadChat(
        sessionId: String?,
        chatId: String,
        submissionId: String = UUID().uuidString.lowercased(),
        requestGeneration: String = UUID().uuidString.lowercased()
    ) -> String {
        uiEvent(
            action: "load_chat", sessionId: sessionId,
            payload: .object(["chat_id": .string(chatId)]),
            submissionId: submissionId,
            requestGeneration: requestGeneration,
            snapshotPurpose: .hydration)
    }

    public static func updateDevice(
        sessionId: String?,
        device: DeviceDescriptor,
        submissionId: String = UUID().uuidString.lowercased(),
        requestGeneration: String = UUID().uuidString.lowercased()
    ) -> String {
        // The orchestrator reads payload["device"] (orchestrator.py update_device
        // handler) — the descriptor must be NESTED, not spread into the payload.
        uiEvent(
            action: "update_device", sessionId: sessionId,
            payload: .object(["device": device.json]),
            submissionId: submissionId,
            requestGeneration: requestGeneration)
    }

    public static func uiEvent(
        action: String,
        sessionId: String?,
        payload: JSONValue,
        submissionId: String = UUID().uuidString.lowercased(),
        requestGeneration: String = UUID().uuidString.lowercased(),
        snapshotPurpose: ConversationGenerationPurpose? = nil
    ) -> String {
        guard continuityUUID4(submissionId) != nil,
            continuityUUID4(requestGeneration) != nil,
            var object = payload.objectValue
        else { return "{}" }
        if let supplied = object["submission_id"],
            supplied.stringValue != submissionId
        {
            return "{}"
        }
        if let supplied = object["request_generation"],
            supplied.stringValue != requestGeneration
        {
            return "{}"
        }
        object["submission_id"] = .string(submissionId)
        object["request_generation"] = .string(requestGeneration)
        if let snapshotPurpose {
            object["snapshot_purpose"] = .string(snapshotPurpose.rawValue)
        }
        return encode(
            .object([
                "type": .string("ui_event"),
                "action": .string(action),
                "session_id": sessionId.map(JSONValue.string) ?? .null,
                "submission_id": .string(submissionId),
                "request_generation": .string(requestGeneration),
                "payload": .object(object),
            ]))
    }
}
