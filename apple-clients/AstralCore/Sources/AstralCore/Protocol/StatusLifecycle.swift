// Feature 060 — shared monotonic reducers for canonical operation progress and
// personal-agent lifecycle projections.  App and Watch use the same equality
// and generation fences so a delayed frame cannot roll either client back.
import Foundation

public struct ClientOperationIdentity: Sendable, Equatable {
    public let submissionId: String
    public let requestGeneration: String

    public init?(submissionId: String, requestGeneration: String) {
        guard continuityUUID4(submissionId) != nil,
            continuityUUID4(requestGeneration) != nil
        else { return nil }
        self.submissionId = submissionId
        self.requestGeneration = requestGeneration
    }

    public static func fresh() -> ClientOperationIdentity {
        ClientOperationIdentity(
            submissionId: UUID().uuidString.lowercased(),
            requestGeneration: UUID().uuidString.lowercased())!
    }
}

/// Immediate client-only acknowledgement. It deliberately has no operation
/// ID and cannot represent durable server acceptance or a terminal result.
public struct LocalOperationSubmission: Sendable, Equatable, Identifiable {
    public let submissionId: String
    public let action: String
    public let surface: String
    public let chatId: String?
    public let connectionGeneration: String
    public let requestGeneration: String
    public let label: String

    public var id: String { submissionId }

    public init?(
        identity: ClientOperationIdentity,
        action: String,
        surface: String,
        chatId: String?,
        connectionGeneration: String,
        label: String = "Submitting…"
    ) {
        guard continuityUUID4(connectionGeneration) != nil,
            chatId == nil || continuityUUID4(chatId) != nil,
            action.range(
                of: "^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
                options: .regularExpression) != nil,
            surface.range(
                of: "^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
                options: .regularExpression) != nil,
            !label.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return nil }
        self.submissionId = identity.submissionId
        self.action = action
        self.surface = surface
        self.chatId = chatId
        self.connectionGeneration = connectionGeneration
        self.requestGeneration = identity.requestGeneration
        self.label = label
    }
}

/// Safe metadata parsed from the exact serialized UI event retained by the
/// offline queue. The two client identities never change across reconnects;
/// only the connection fence installed by the UI model is refreshed.
public struct QueuedOperationReplay: Sendable, Equatable {
    public let identity: ClientOperationIdentity
    public let action: String
    public let surface: String
    public let chatId: String?
    public let conversationPurpose: ConversationGenerationPurpose?

    public init?(frameText: String) {
        guard
            !VoiceCurrentConnectionFrame.claimsCurrentConnectionSemantics(
                frameText: frameText),
            let data = frameText.data(using: .utf8),
            let root = try? JSONValue.parse(data),
            root["type"]?.stringValue == "ui_event",
            let action = root["action"]?.stringValue,
            Self.isSnakeCase(action),
            let submissionId = continuityUUID4(root["submission_id"]?.stringValue),
            let requestGeneration = continuityUUID4(root["request_generation"]?.stringValue),
            let payload = root["payload"]?.objectValue,
            payload["submission_id"]?.stringValue == submissionId,
            payload["request_generation"]?.stringValue == requestGeneration,
            let identity = ClientOperationIdentity(
                submissionId: submissionId,
                requestGeneration: requestGeneration)
        else { return nil }

        let explicitChat = payload["chat_id"]
        if explicitChat != nil && continuityUUID4(explicitChat?.stringValue) == nil {
            return nil
        }
        let sessionChat = continuityUUID4(root["session_id"]?.stringValue)
        let chatId = continuityUUID4(explicitChat?.stringValue) ?? sessionChat

        let explicitSurface = payload["surface"]
        if explicitSurface != nil,
            explicitSurface?.stringValue == nil
                || !Self.isSnakeCase(explicitSurface?.stringValue ?? "")
        {
            return nil
        }
        let surface =
            explicitSurface?.stringValue
            ?? (["chat_message", "load_chat"].contains(action) ? "chat" : "operation")

        let purpose: ConversationGenerationPurpose?
        switch action {
        case "chat_message":
            guard payload["snapshot_purpose"]?.stringValue == "commit" else { return nil }
            purpose = .commit
        case "load_chat":
            guard chatId != nil,
                payload["snapshot_purpose"]?.stringValue == "hydration"
            else { return nil }
            purpose = .hydration
        default:
            guard payload["snapshot_purpose"] == nil else { return nil }
            purpose = nil
        }

        self.identity = identity
        self.action = action
        self.surface = surface
        self.chatId = chatId
        self.conversationPurpose = purpose
    }

    private static func isSnakeCase(_ value: String) -> Bool {
        value.range(
            of: "^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
            options: .regularExpression) != nil
    }
}

public struct StatusLifecycleReducer: Sendable {
    public private(set) var operations: [String: OperationStatus] = [:]
    public private(set) var agents: [String: AgentLifecycle] = [:]

    public init() {}

    public mutating func clear() {
        operations.removeAll()
        agents.removeAll()
    }

    @discardableResult
    public mutating func accept(
        operation status: OperationStatus,
        connectionGeneration: String?,
        conversationRequestGeneration: String?,
        activeChatId: String?,
        pendingChatRequestGenerations: Set<String>,
        pendingSurfaceRequestGenerations: Set<String>
    ) -> Bool {
        guard status.connectionGeneration == connectionGeneration else { return false }
        if let chatId = status.chatId {
            guard chatId == activeChatId,
                status.requestGeneration == conversationRequestGeneration
                    || pendingChatRequestGenerations.contains(status.requestGeneration)
            else { return false }
        } else {
            guard pendingSurfaceRequestGenerations.contains(status.requestGeneration)
            else { return false }
        }
        if let current = operations[status.operationId],
            current.terminal || status.sequence <= current.sequence
        {
            return false
        }
        operations[status.operationId] = status
        return true
    }

    /// Compatibility overload for chat-only reducers. Surface operations must
    /// call the explicit overload with their retained pending generations.
    @discardableResult
    public mutating func accept(
        operation status: OperationStatus,
        connectionGeneration: String?,
        requestGeneration: String?,
        activeChatId: String?
    ) -> Bool {
        accept(
            operation: status,
            connectionGeneration: connectionGeneration,
            conversationRequestGeneration: requestGeneration,
            activeChatId: activeChatId,
            pendingChatRequestGenerations: [],
            pendingSurfaceRequestGenerations: [])
    }

    @discardableResult
    public mutating func accept(lifecycle: AgentLifecycle) -> Bool {
        if let current = agents[lifecycle.agentId],
            lifecycle.lifecycleGeneration < current.lifecycleGeneration
                || (lifecycle.lifecycleGeneration == current.lifecycleGeneration
                    && lifecycle.stateRevision <= current.stateRevision)
        {
            return false
        }
        agents[lifecycle.agentId] = lifecycle
        return true
    }
}
