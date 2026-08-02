// Feature 051 — REST surface shared by the three Apple clients: chat list /
// detail / creation, and the 044 native sign-out (client_id attribution).
import Foundation

public struct ChatSummary: Sendable, Identifiable, Equatable {
    public let id: String
    public let title: String
    public let updatedAt: String

    public init?(json: JSONValue) {
        guard
            let id = json["id"]?.stringValue
                ?? json["chat_id"]?.stringValue
        else { return nil }
        self.id = id
        self.title = json["title"]?.stringValue ?? "Untitled chat"
        self.updatedAt = json["updated_at"]?.stringValue ?? ""
    }
}

/// Payload-free retained operation projection returned by the authenticated
/// feature-060 reconciliation endpoint. Credentials and submitted fields are
/// deliberately absent from this type.
public struct OperationProjection: Sendable, Equatable {
    public let operationId: String
    public let operationKind: String
    public let requestGeneration: String?
    public let state: String
    public let phaseCode: String?
    public let terminalCode: String?
    public let safeSummary: String?
    public let retryAfterMs: UInt64?
    public let stateRevision: UInt64

    public init?(json: JSONValue) {
        guard let operationId = Self.uuid(json["operation_id"]),
            let operationKind = json["operation_kind"]?.stringValue,
            let state = json["state"]?.stringValue,
            ["queued", "running", "completed", "failed", "cancelled", "retryable"]
                .contains(state),
            let stateRevision = Self.unsigned(json["state_revision"])
        else { return nil }
        let requestGeneration: String?
        if json["request_generation"] == .null {
            requestGeneration = nil
        } else {
            guard let value = Self.uuid(json["request_generation"]) else { return nil }
            requestGeneration = value
        }
        let retryAfterMs: UInt64?
        if json["retry_after_ms"] == .null {
            retryAfterMs = nil
        } else {
            guard state == "retryable", let value = Self.unsigned(json["retry_after_ms"])
            else { return nil }
            retryAfterMs = value
        }
        self.operationId = operationId
        self.operationKind = operationKind
        self.requestGeneration = requestGeneration
        self.state = state
        self.phaseCode = json["phase_code"]?.stringValue
        self.terminalCode = json["terminal_code"]?.stringValue
        self.safeSummary = json["safe_summary"]?.stringValue
        self.retryAfterMs = retryAfterMs
        self.stateRevision = stateRevision
    }

    private static func uuid(_ value: JSONValue?) -> String? {
        guard let text = value?.stringValue,
            let parsed = UUID(uuidString: text),
            parsed.uuidString.lowercased() == text
        else { return nil }
        return text
    }

    private static func unsigned(_ value: JSONValue?) -> UInt64? {
        guard let number = value?.numberValue, number.isFinite, number >= 0,
            number.rounded() == number, number <= 9_007_199_254_740_991
        else { return nil }
        return UInt64(number)
    }
}

/// The immutable retained result of one owner-scoped submission identity.
public enum OperationSubmissionProjection: Sendable, Equatable {
    case accepted(OperationProjection)
    case refused(code: String, retryable: Bool, retryAfterMs: UInt64?)

    public init?(json: JSONValue) {
        guard let accepted = json["accepted"]?.boolValue else { return nil }
        if accepted {
            guard let operation = json["operation"].flatMap(OperationProjection.init(json:))
            else { return nil }
            self = .accepted(operation)
            return
        }
        guard let code = json["code"]?.stringValue,
            let retryable = json["retryable"]?.boolValue
        else { return nil }
        let retryAfterMs: UInt64?
        if json["retry_after_ms"] == .null {
            retryAfterMs = nil
        } else {
            guard retryable, let number = json["retry_after_ms"]?.numberValue,
                number.isFinite, number >= 0, number.rounded() == number,
                number <= 9_007_199_254_740_991
            else { return nil }
            retryAfterMs = UInt64(number)
        }
        self = .refused(code: code, retryable: retryable, retryAfterMs: retryAfterMs)
    }
}

public struct RestClient: Sendable {
    public typealias Transport = @Sendable (URLRequest) async throws -> (Int, Data)

    public let serverBase: URL
    private let transport: Transport
    private let tokenProvider: @Sendable () async -> String?

    public init(
        serverBase: URL,
        tokenProvider: @escaping @Sendable () async -> String?,
        transport: Transport? = nil
    ) {
        self.serverBase = serverBase
        self.tokenProvider = tokenProvider
        self.transport =
            transport ?? { request in
                let (data, response) = try await NoStoreHTTP.session.data(for: request)
                return ((response as? HTTPURLResponse)?.statusCode ?? 0, data)
            }
    }

    /// ws(s):// twin of the server base for the orchestrator socket.
    public var webSocketURL: URL {
        var comps = URLComponents(url: serverBase, resolvingAgainstBaseURL: false)!
        comps.scheme = comps.scheme == "https" ? "wss" : "ws"
        comps.path = "/ws"
        return comps.url!
    }

    func request(_ method: String, _ path: String, body: JSONValue? = nil) async throws -> (Int, JSONValue) {
        var request = NoStoreHTTP.request(
            url: serverBase.appendingPathComponent(path),
            method: method,
            body: try body?.encoded(),
            contentType: body == nil ? nil : "application/json")
        if let token = await tokenProvider() {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let (status, data) = try await transport(request)
        return (status, (try? JSONValue.parse(data)) ?? .object([:]))
    }

    public func chats() async throws -> [ChatSummary] {
        let (status, json) = try await request("GET", "api/chats")
        guard status == 200 else { return [] }
        let items = json["chats"]?.arrayValue ?? json.arrayValue ?? []
        return items.compactMap { ChatSummary(json: $0) }
    }

    public func deleteChat(id: String) async throws -> Bool {
        let (status, _) = try await request("DELETE", "api/chats/\(id)")
        return (200...299).contains(status)
    }

    /// Reconcile one retained user-owned accepted operation. A non-disclosing
    /// 404 returns nil; transport and malformed-success responses throw.
    public func operation(id: String) async throws -> OperationProjection? {
        let (status, json) = try await request("GET", "api/operations/\(id)")
        if status == 404 { return nil }
        guard status == 200, let operation = OperationProjection(json: json) else {
            throw URLError(.cannotParseResponse)
        }
        return operation
    }

    /// Resolve acceptance/refusal by the original client submission UUID when
    /// the socket closed before an operation ID reached the client.
    public func operationSubmission(id: String) async throws -> OperationSubmissionProjection? {
        let (status, json) = try await request("GET", "api/operation-submissions/\(id)")
        if status == 404 { return nil }
        guard status == 200, let result = OperationSubmissionProjection(json: json) else {
            throw URLError(.cannotParseResponse)
        }
        return result
    }

    /// 044 native sign-out: server-side revocation attributed to this client.
    public func logout(clientId: String, refreshToken: String) async throws -> Bool {
        let (status, json) = try await request(
            "POST", "api/auth/logout",
            body: .object([
                "client_id": .string(clientId),
                "refresh_token": .string(refreshToken),
            ]))
        let ok = json["revoked"]?.boolValue == true || json["queued"]?.boolValue == true
        return status == 200 && ok
    }

    /// The per-user, hash-chained audit log (`GET /api/audit`).
    public func audit() async -> [AuditEvent] {
        var req = NoStoreHTTP.request(url: serverBase.appendingPathComponent("api/audit"))
        if let token = await tokenProvider() {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        guard let (data, response) = try? await NoStoreHTTP.session.data(for: req),
            (response as? HTTPURLResponse)?.statusCode == 200
        else { return [] }
        return AuditEvent.parse(data)
    }

    /// Upload one file (`POST /api/upload`, multipart `file` field) — the exact
    /// web/Android contract. Returns the attachment metadata or nil on failure.
    public func uploadAttachment(
        filename: String, mimeType: String?,
        data fileData: Data
    ) async -> AttachmentUpload? {
        let boundary = "Boundary-\(UUID().uuidString)"
        var req = NoStoreHTTP.request(
            url: serverBase.appendingPathComponent("api/upload"),
            method: "POST",
            contentType: "multipart/form-data; boundary=\(boundary)")
        if let token = await tokenProvider() {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        var body = Data()
        let mime = mimeType ?? "application/octet-stream"
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: \(mime)\r\n\r\n".data(using: .utf8)!)
        body.append(fileData)
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        req.httpBody = body
        guard let (respData, response) = try? await NoStoreHTTP.session.data(for: req),
            (200...299).contains((response as? HTTPURLResponse)?.statusCode ?? 0),
            let json = try? JSONValue.parse(respData),
            let id = json["attachment_id"]?.stringValue
        else { return nil }
        return AttachmentUpload(
            attachmentId: id,
            filename: json["filename"]?.stringValue ?? filename,
            category: json["category"]?.stringValue ?? "file",
            parserStatus: json["parser_status"]?.stringValue)
    }

    /// Download a server file with Bearer auth — the native twin of the web's
    /// cookie-carrying anchor click on `file_download` components. Handles the
    /// root-relative `/api/download/{session}/{filename}` URLs agents emit by
    /// resolving them against `serverBase`; absolute OFF-origin URLs (e.g. a
    /// `download_card`'s GitHub release asset) are fetched WITHOUT the token —
    /// credentials never leave our origin. Returns a temporary file URL whose
    /// last path component is the intended filename (for share/save UIs).
    public func downloadFile(
        from urlString: String,
        suggestedFilename: String? = nil
    ) async throws -> URL {
        guard let url = URL(string: urlString, relativeTo: serverBase)?.absoluteURL else {
            throw URLError(.badURL)
        }
        var req = NoStoreHTTP.request(url: url)
        if url.host == serverBase.host, let token = await tokenProvider() {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let (data, response) = try await NoStoreHTTP.session.data(for: req)
        guard let http = response as? HTTPURLResponse,
            (200...299).contains(http.statusCode)
        else {
            throw URLError(.badServerResponse)
        }
        var name = suggestedFilename ?? http.suggestedFilename ?? url.lastPathComponent
        if name.isEmpty || name == "/" { name = "download" }
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("astral-downloads", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let destination = dir.appendingPathComponent(name)
        try data.write(to: destination)
        return destination
    }

    /// Toggle one tool's permission (feature-013 per-(tool,kind) shape):
    /// `PUT /api/agents/{id}/permissions {per_tool_permissions:{tool:{kind:enabled}}}`.
    @discardableResult
    public func setToolPermission(
        agentId: String, tool: String, kind: String,
        enabled: Bool
    ) async -> Bool {
        let body = JSONValue.object([
            "per_tool_permissions": .object([tool: .object([kind: .bool(enabled)])])
        ])
        let result = try? await request("PUT", "api/agents/\(agentId)/permissions", body: body)
        return (200...299).contains(result?.0 ?? 0)
    }
}

/// Metadata returned by `POST /api/upload` for a staged attachment (feature 031).
public struct AttachmentUpload: Sendable {
    public let attachmentId: String
    public let filename: String
    public let category: String
    /// covered | preparing | pending_admin_approval | unavailable
    public let parserStatus: String?
}
