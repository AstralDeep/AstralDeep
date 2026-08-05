// Feature 060 — native conversation continuity models and equality fences.
//
// The server is the only committed-state authority. Apple clients keep a
// non-secret active-chat locator, open one UUID4 request generation for each
// hydration/turn, and replace transcript + canvas only after this reducer has
// validated one complete snapshot. Legacy render frames are disposable
// overlays and never advance `lastCommittedRenderRevision`.
import CryptoKit
import Foundation

public enum ConversationGenerationPurpose: String, Sendable, Equatable {
    case hydration
    case commit
}

public struct ConversationAccount: Sendable, Equatable {
    public let issuer: String
    public let subject: String

    public init?(issuer: String, subject: String) {
        guard !issuer.isEmpty, !subject.isEmpty, subject != "unknown" else { return nil }
        self.issuer = issuer
        self.subject = subject
    }

    /// Opaque account scope used by every Apple locator store.
    public var locatorStorageKey: String {
        var input = Data(issuer.utf8)
        input.append(0)
        input.append(contentsOf: subject.utf8)
        let digest = SHA256.hash(data: input).map { String(format: "%02x", $0) }.joined()
        return "astraldeep.active_chat.v1.\(digest)"
    }
}

extension TokenSet {
    /// Non-authoritative display claims are sufficient only for selecting a
    /// local preference namespace. Server ownership is still validated on
    /// every resume/load request.
    public var conversationAccount: ConversationAccount? {
        guard let issuer = claims?["iss"]?.stringValue,
            let subject = claims?["sub"]?.stringValue
        else { return nil }
        return ConversationAccount(issuer: issuer, subject: subject)
    }
}

public struct ConversationResumeLocator: Codable, Sendable, Equatable {
    public let schemaVersion: Int
    public let chatId: String
    public let updatedAt: String

    public init(chatId: String, updatedAt: String) {
        self.schemaVersion = 1
        self.chatId = chatId
        self.updatedAt = updatedAt
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case chatId = "chat_id"
        case updatedAt = "updated_at"
    }
}

public struct ConversationResumeRegistration: Sendable, Equatable {
    public let schemaVersion = 1
    public let activeChatId: String
    public let requestGeneration: String

    public init?(activeChatId: String, requestGeneration: String) {
        guard continuityUUID4(activeChatId) != nil,
            continuityUUID4(requestGeneration) != nil
        else { return nil }
        self.activeChatId = activeChatId
        self.requestGeneration = requestGeneration
    }

    var json: JSONValue {
        .object([
            "schema_version": .number(1),
            "active_chat_id": .string(activeChatId),
            "request_generation": .string(requestGeneration),
        ])
    }
}

public enum ConversationPart: Sendable, Equatable {
    case text(String)
    case components([AstralComponent])
    case structured(value: JSONValue, plainText: String)
    case recovery(code: String, message: String)

    init?(json: JSONValue) {
        guard let object = json.objectValue, let type = object["type"]?.stringValue else {
            return nil
        }
        switch type {
        case "text":
            // 066 T023: exactly {type, text} plus an OPTIONAL bounded variant
            // (backend CANONICAL_TEXT_PART_VARIANTS twin). The weight hint is
            // validated then dropped here; rendering parity is tracked with
            // the parity checklist.
            guard Set(object.keys) == ["type", "text"] || Self.boundedCaptionShape(object),
                let text = object["text"]?.stringValue,
                continuityNonBlank(text)
            else { return nil }
            self = .text(text)
        case "components":
            guard Set(object.keys) == ["type", "components"],
                let rawComponents = object["components"]?.arrayValue,
                !rawComponents.isEmpty,
                rawComponents.allSatisfy({ $0["_presentation"] == nil })
            else { return nil }
            let components = rawComponents.compactMap(AstralComponent.init(json:))
            guard components.count == rawComponents.count else { return nil }
            self = .components(components)
        case "structured":
            guard Set(object.keys) == ["type", "value", "plain_text"],
                let value = object["value"], value != .null,
                let plainText = object["plain_text"]?.stringValue,
                continuityNonBlank(plainText)
            else { return nil }
            self = .structured(value: value, plainText: plainText)
        case "recovery":
            guard Set(object.keys) == ["type", "code", "message"],
                let code = object["code"]?.stringValue,
                continuitySnakeCase(code),
                let message = object["message"]?.stringValue,
                continuityNonBlank(message)
            else { return nil }
            self = .recovery(code: code, message: message)
        default:
            return nil
        }
    }

    /// 066 T023: the bounded caption shape a text part may additionally take.
    private static func boundedCaptionShape(_ object: [String: JSONValue]) -> Bool {
        Set(object.keys) == ["type", "text", "variant"] && object["variant"]?.stringValue == "caption"
    }

    public var visibleText: String? {
        switch self {
        case .text(let text): return text
        case .structured(_, let plainText): return plainText
        case .recovery(_, let message): return message
        case .components: return nil
        }
    }

    public var renderedComponents: [AstralComponent] {
        if case .components(let components) = self { return components }
        return []
    }
}

public struct ConversationMessage: Sendable, Equatable {
    public let messageId: String
    public let role: String
    public let createdAt: String
    public let parts: [ConversationPart]
    public let attachments: [JSONValue]

    init?(json: JSONValue) {
        guard let object = json.objectValue,
            Set(object.keys) == ["message_id", "role", "created_at", "parts", "attachments"],
            let messageId = object["message_id"]?.stringValue,
            !messageId.isEmpty,
            let role = object["role"]?.stringValue,
            ["user", "assistant", "system", "tool"].contains(role),
            let createdAt = object["created_at"]?.stringValue,
            continuityRFC3339UTC(createdAt),
            let rawParts = object["parts"]?.arrayValue,
            !rawParts.isEmpty,
            let attachments = object["attachments"]?.arrayValue,
            attachments.allSatisfy({ $0.objectValue != nil })
        else { return nil }
        let parts = rawParts.compactMap(ConversationPart.init(json:))
        guard parts.count == rawParts.count else { return nil }
        self.messageId = messageId
        self.role = role
        self.createdAt = createdAt
        self.parts = parts
        self.attachments = attachments
    }

    public var visibleText: String {
        parts.compactMap(\.visibleText).joined(separator: "\n")
    }

    public var components: [AstralComponent] {
        parts.flatMap(\.renderedComponents)
    }

    public var attachmentNames: [String] {
        attachments.compactMap { attachment in
            attachment["filename"]?.stringValue ?? attachment["name"]?.stringValue
        }
    }
}

public struct ConversationCommitReady: Sendable, Equatable {
    public let schemaVersion: Int
    public let chatId: String
    public let connectionGeneration: String
    public let requestGeneration: String
    public let renderRevision: UInt64

    public init?(frame: InboundFrame) {
        guard frame.name == "conversation_commit_ready",
            let object = frame.payload.objectValue,
            Set(object.keys) == [
                "type", "schema_version", "chat_id", "connection_generation",
                "request_generation", "render_revision",
            ],
            object["type"]?.stringValue == "conversation_commit_ready",
            object["schema_version"]?.numberValue == 1,
            let chatId = continuityUUID4(object["chat_id"]?.stringValue),
            let connection = continuityUUID4(object["connection_generation"]?.stringValue),
            let request = continuityUUID4(object["request_generation"]?.stringValue),
            let revision = continuityUnsignedInteger(object["render_revision"])
        else { return nil }
        self.schemaVersion = 1
        self.chatId = chatId
        self.connectionGeneration = connection
        self.requestGeneration = request
        self.renderRevision = revision
    }
}

public enum ConversationSnapshotRejection: Sendable, Equatable {
    case noOpenGeneration
    case scopeMismatch
    case purposeMismatch
    case staleRevision
    case unexpectedEqualCommit
    case revisionConflict
    case generationAlreadyCompleted
    case unexpectedRevision
}

public enum ConversationSnapshotApplyResult: Sendable, Equatable {
    case applied
    case replay
    case rejected(ConversationSnapshotRejection)
}

/// Pure, reusable continuity reducer shared by iOS, macOS, and watchOS.
public struct ConversationContinuityReducer: Sendable {
    public private(set) var activeChatId: String?
    public private(set) var connectionGeneration: String?
    public private(set) var requestGeneration: String?
    public private(set) var requestPurpose: ConversationGenerationPurpose?
    public private(set) var lastCommittedRenderRevision: UInt64
    public private(set) var acceptedSnapshot: ConversationSnapshot?

    private var expectedRenderRevision: UInt64?
    private var lastTransientSequence: UInt64 = 0
    private var usedRequestGenerations: Set<String> = []

    public init(lastCommittedRenderRevision: UInt64 = 0) {
        self.lastCommittedRenderRevision = lastCommittedRenderRevision
    }

    @discardableResult
    public mutating func beginConnection(_ generation: String) -> Bool {
        guard let generation = continuityUUID4(generation) else { return false }
        connectionGeneration = generation
        requestGeneration = nil
        requestPurpose = nil
        acceptedSnapshot = nil
        expectedRenderRevision = nil
        lastTransientSequence = 0
        usedRequestGenerations = []
        return true
    }

    /// Reset the local revision only for an intentional switch to a different
    /// chat. Reconnect of the same chat preserves the committed revision.
    @discardableResult
    public mutating func selectChat(_ chatId: String, resetRevision: Bool) -> Bool {
        guard let chatId = continuityUUID4(chatId) else { return false }
        if resetRevision { lastCommittedRenderRevision = 0 }
        activeChatId = chatId
        acceptedSnapshot = nil
        expectedRenderRevision = nil
        lastTransientSequence = 0
        return true
    }

    @discardableResult
    public mutating func openRequest(
        chatId: String,
        requestGeneration: String,
        purpose: ConversationGenerationPurpose
    ) -> Bool {
        guard connectionGeneration != nil,
            let chatId = continuityUUID4(chatId),
            let requestGeneration = continuityUUID4(requestGeneration),
            !usedRequestGenerations.contains(requestGeneration)
        else { return false }
        activeChatId = chatId
        self.requestGeneration = requestGeneration
        requestPurpose = purpose
        acceptedSnapshot = nil
        expectedRenderRevision = nil
        lastTransientSequence = 0
        usedRequestGenerations.insert(requestGeneration)
        return true
    }

    /// Open a server-originated commit generation only when it is scoped to
    /// the current chat/connection and promises a strictly newer revision.
    @discardableResult
    public mutating func accept(_ ready: ConversationCommitReady) -> Bool {
        guard ready.chatId == activeChatId,
            ready.connectionGeneration == connectionGeneration,
            !usedRequestGenerations.contains(ready.requestGeneration),
            ready.renderRevision > lastCommittedRenderRevision
        else { return false }
        requestGeneration = ready.requestGeneration
        requestPurpose = .commit
        expectedRenderRevision = ready.renderRevision
        acceptedSnapshot = nil
        lastTransientSequence = 0
        usedRequestGenerations.insert(ready.requestGeneration)
        return true
    }

    public mutating func apply(_ snapshot: ConversationSnapshot) -> ConversationSnapshotApplyResult {
        guard let chat = activeChatId,
            let connection = connectionGeneration,
            let request = requestGeneration,
            let purpose = requestPurpose
        else { return .rejected(.noOpenGeneration) }
        guard snapshot.chatId == chat,
            snapshot.connectionGeneration == connection,
            snapshot.requestGeneration == request
        else { return .rejected(.scopeMismatch) }
        guard snapshot.snapshotPurpose == purpose.rawValue else {
            return .rejected(.purposeMismatch)
        }

        if let acceptedSnapshot {
            if snapshot == acceptedSnapshot { return .replay }
            if snapshot.renderRevision == acceptedSnapshot.renderRevision {
                return .rejected(.revisionConflict)
            }
            return .rejected(.generationAlreadyCompleted)
        }
        if snapshot.renderRevision < lastCommittedRenderRevision {
            return .rejected(.staleRevision)
        }
        if snapshot.renderRevision == lastCommittedRenderRevision, purpose == .commit {
            return .rejected(.unexpectedEqualCommit)
        }
        if let expectedRenderRevision, snapshot.renderRevision != expectedRenderRevision {
            return .rejected(.unexpectedRevision)
        }

        acceptedSnapshot = snapshot
        lastCommittedRenderRevision = snapshot.renderRevision
        expectedRenderRevision = nil
        lastTransientSequence = 0
        return .applied
    }

    /// Validate a disposable render overlay without mutating committed state.
    @discardableResult
    public mutating func acceptTransient(_ frame: InboundFrame) -> Bool {
        guard acceptedSnapshot == nil,
            let chat = activeChatId,
            let connection = connectionGeneration,
            let request = requestGeneration,
            ["ui_render", "ui_update", "ui_upsert", "ui_append", "ui_stream_data"]
                .contains(frame.name),
            let object = frame.payload.objectValue,
            object["chat_id"]?.stringValue == chat,
            object["connection_generation"]?.stringValue == connection,
            object["request_generation"]?.stringValue == request,
            continuityUnsignedInteger(object["base_render_revision"])
                == lastCommittedRenderRevision,
            let sequence = continuityUnsignedInteger(object["frame_sequence"]),
            sequence > lastTransientSequence
        else { return false }
        lastTransientSequence = sequence
        return true
    }

    public mutating func clear() {
        activeChatId = nil
        connectionGeneration = nil
        requestGeneration = nil
        requestPurpose = nil
        lastCommittedRenderRevision = 0
        acceptedSnapshot = nil
        expectedRenderRevision = nil
        lastTransientSequence = 0
        usedRequestGenerations = []
    }
}

func continuityUUID4(_ text: String?) -> String? {
    guard let text,
        text.range(
            of: "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            options: .regularExpression) != nil,
        UUID(uuidString: text) != nil
    else { return nil }
    return text
}

private func continuityUnsignedInteger(_ value: JSONValue?) -> UInt64? {
    guard let number = value?.numberValue,
        number.isFinite,
        number >= 0,
        number.rounded() == number,
        number <= 9_007_199_254_740_991
    else { return nil }
    return UInt64(number)
}

private func continuityRFC3339UTC(_ value: String) -> Bool {
    guard value.hasSuffix("Z") else { return false }
    if ISO8601DateFormatter().date(from: value) != nil { return true }
    // A plain ISO8601DateFormatter rejects fractional seconds, which valid
    // RFC 3339 producers may emit — accept them rather than dropping the
    // message (and with it the whole committed conversation snapshot).
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return fractional.date(from: value) != nil
}

private func continuitySnakeCase(_ value: String) -> Bool {
    value.range(
        of: "^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
        options: .regularExpression) != nil
}

private func continuityNonBlank(_ value: String) -> Bool {
    !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
}
