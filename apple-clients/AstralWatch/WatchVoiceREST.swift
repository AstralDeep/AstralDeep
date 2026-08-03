import AstralCore
import Foundation

// Feature 065 — authenticated voice control. Every mutation carries the
// Keycloak bearer plus the short-lived device/connection binding. Grant and
// binding material stay in memory and errors expose only bounded reason codes.

struct WatchVoiceRESTClient: Sendable {
    typealias Transport = @Sendable (URLRequest) async throws -> (Int, Data)

    let serverBase: URL
    let deviceId: String
    let connectionGeneration: String
    let controlBinding: WatchVoiceControlBinding
    let tokenProvider: @Sendable () async -> String?
    let transport: Transport

    init(
        serverBase: URL,
        deviceId: String,
        connectionGeneration: String,
        controlBinding: WatchVoiceControlBinding,
        tokenProvider: @escaping @Sendable () async -> String?,
        transport: Transport? = nil
    ) {
        self.serverBase = serverBase
        self.deviceId = deviceId
        self.connectionGeneration = connectionGeneration
        self.controlBinding = controlBinding
        self.tokenProvider = tokenProvider
        self.transport =
            transport ?? { request in
                let configuration = URLSessionConfiguration.ephemeral
                configuration.urlCache = nil
                configuration.httpCookieStorage = nil
                let session = URLSession(configuration: configuration)
                defer { session.finishTasksAndInvalidate() }
                let (data, response) = try await session.data(for: request)
                return ((response as? HTTPURLResponse)?.statusCode ?? 0, data)
            }
    }

    func createSession(
        chatId: String,
        activationId: String,
        permission: WatchVoicePermission
    ) async throws -> WatchVoiceSessionGrant {
        let body = clientActivationBody(
            chatId: chatId,
            activationId: activationId,
            permission: permission)
        let json = try await request("POST", path: "api/voice/sessions", body: body, success: [200, 201])
        guard let result = WatchVoiceSessionGrant(json: json),
            result.session.deviceId == deviceId,
            result.session.ownerConnectionGeneration == connectionGeneration
        else { throw WatchVoiceRESTError.malformedResponse }
        return result
    }

    func takeOverSession(
        sessionId: String,
        chatId: String,
        activationId: String,
        expectedGeneration: UInt64,
        expectedMediaGrantRevision: UInt64,
        permission: WatchVoicePermission
    ) async throws -> WatchVoiceSessionGrant {
        guard watchVoiceRESTUUID4(sessionId) else { throw WatchVoiceRESTError.invalidRequest }
        var object =
            clientActivationBody(
                chatId: chatId,
                activationId: activationId,
                permission: permission
            ).objectValue ?? [:]
        object["expected_generation"] = .number(Double(expectedGeneration))
        object["expected_media_grant_revision"] = .number(Double(expectedMediaGrantRevision))
        let json = try await request(
            "POST",
            path: "api/voice/sessions/\(sessionId)/takeover",
            body: .object(object),
            success: [200])
        guard let result = WatchVoiceSessionGrant(json: json),
            result.session.deviceId == deviceId,
            result.session.ownerConnectionGeneration == connectionGeneration
        else { throw WatchVoiceRESTError.malformedResponse }
        return result
    }

    func updateSession(
        _ session: WatchVoiceSession,
        changes: [String: JSONValue]
    ) async throws -> WatchVoiceSession {
        guard session.deviceId == deviceId, !changes.isEmpty,
            Set(changes.keys).isSubset(of: [
                "visible_chat_id", "speech_muted", "microphone_enabled",
                "foreground_active", "foreground_reason", "interaction",
            ])
        else { throw WatchVoiceRESTError.invalidRequest }
        var body = changes
        body["expected_generation"] = .number(Double(session.generation))
        body["expected_media_grant_revision"] = .number(Double(session.mediaGrantRevision))
        let json = try await request(
            "PATCH",
            path: "api/voice/sessions/\(session.sessionId)",
            body: .object(body),
            success: [200])
        guard let result = WatchVoiceSession(json: json), result.deviceId == deviceId,
            result.ownerConnectionGeneration == connectionGeneration
        else { throw WatchVoiceRESTError.malformedResponse }
        return result
    }

    func endSession(_ session: WatchVoiceSession) async throws {
        var components = URLComponents(
            url: serverBase.appendingPathComponent("api/voice/sessions/\(session.sessionId)"),
            resolvingAgainstBaseURL: false)
        components?.queryItems = [
            URLQueryItem(name: "expected_generation", value: String(session.generation)),
            URLQueryItem(
                name: "expected_media_grant_revision",
                value: String(session.mediaGrantRevision)),
        ]
        guard let url = components?.url else { throw WatchVoiceRESTError.invalidRequest }
        _ = try await request("DELETE", url: url, body: nil, success: [204])
    }

    func stopSpeech(_ session: WatchVoiceSession) async throws {
        _ = try await request(
            "POST",
            path: "api/voice/sessions/\(session.sessionId)/speech/stop",
            body: generationBody(session),
            success: [202])
    }

    func consentSensitiveRecap(
        _ session: WatchVoiceSession,
        resultId: String,
        turnId: String
    ) async throws {
        guard watchVoiceRESTOpaque(resultId), watchVoiceRESTUUID4(turnId),
            session.deviceId == deviceId,
            session.ownerConnectionGeneration == connectionGeneration
        else { throw WatchVoiceRESTError.invalidRequest }
        var body = generationBody(session).objectValue ?? [:]
        body["turn_id"] = .string(turnId)
        body["consent_method"] = .string("tap")
        _ = try await request(
            "POST",
            path: "api/voice/sessions/\(session.sessionId)/results/\(resultId)/read-consent",
            body: .object(body),
            success: [202])
    }

    func refreshGrant(_ session: WatchVoiceSession, refreshId: String) async throws
        -> WatchVoiceSessionGrant
    {
        guard watchVoiceRESTUUID4(refreshId) else { throw WatchVoiceRESTError.invalidRequest }
        let body: JSONValue = .object([
            "refresh_id": .string(refreshId),
            "expected_generation": .number(Double(session.generation)),
            "expected_media_grant_revision": .number(Double(session.mediaGrantRevision)),
            "device_id": .string(deviceId),
        ])
        let json = try await request(
            "POST",
            path: "api/voice/sessions/\(session.sessionId)/media-grants",
            body: body,
            success: [200, 201])
        guard let root = json.objectValue,
            Set(root.keys) == ["refresh_id", "replayed", "replay_expires_at", "session", "grant"],
            root["refresh_id"]?.stringValue == refreshId,
            root["replayed"]?.boolValue != nil,
            watchVoiceRESTDate(root["replay_expires_at"]) != nil,
            let sessionJSON = root["session"], let grantJSON = root["grant"],
            let result = WatchVoiceSessionGrant(
                json: .object(["session": sessionJSON, "grant": grantJSON])),
            result.session.deviceId == deviceId,
            result.session.ownerConnectionGeneration == connectionGeneration
        else { throw WatchVoiceRESTError.malformedResponse }
        return result
    }

    private func clientActivationBody(
        chatId: String,
        activationId: String,
        permission: WatchVoicePermission
    ) -> JSONValue {
        .object([
            "device_id": .string(deviceId),
            "device_kind": .string("watchos"),
            "visible_chat_id": .string(chatId),
            "activation_id": .string(activationId),
            "capability": .object([
                "has_microphone": .bool(true),
                "has_audio_output": .bool(true),
                "microphone_permission": .string(permission.rawValue),
                "full_duplex": .bool(false),
                "transport": .string("watch_pcm_websocket"),
            ]),
            "foreground_active": .bool(true),
        ])
    }

    private func generationBody(_ session: WatchVoiceSession) -> JSONValue {
        .object([
            "expected_generation": .number(Double(session.generation)),
            "expected_media_grant_revision": .number(Double(session.mediaGrantRevision)),
        ])
    }

    private func request(
        _ method: String,
        path: String,
        body: JSONValue?,
        success: Set<Int>
    ) async throws -> JSONValue {
        try await request(
            method,
            url: serverBase.appendingPathComponent(path),
            body: body,
            success: success)
    }

    private func request(
        _ method: String,
        url: URL,
        body: JSONValue?,
        success: Set<Int>
    ) async throws -> JSONValue {
        guard controlBinding.deviceId == deviceId,
            controlBinding.connectionGeneration == connectionGeneration,
            controlBinding.expiresAt > Date(),
            let token = await tokenProvider(), !token.isEmpty
        else { throw WatchVoiceRESTError.authenticationRequired }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue(deviceId, forHTTPHeaderField: "X-Astral-Device-Id")
        request.setValue(
            connectionGeneration,
            forHTTPHeaderField: "X-Astral-Connection-Generation")
        request.setValue(
            controlBinding.bearer,
            forHTTPHeaderField: "X-Astral-Voice-Control-Binding")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try body.encoded()
        }
        let status: Int
        let data: Data
        do {
            (status, data) = try await transport(request)
        } catch {
            throw WatchVoiceRESTError.network
        }
        guard data.count <= 128 * 1024 else { throw WatchVoiceRESTError.malformedResponse }
        let json = (try? JSONValue.parse(data)) ?? .object([:])
        guard success.contains(status) else {
            let code = json["code"]?.stringValue?.prefix(80).description ?? "voice_unavailable"
            throw WatchVoiceRESTError.refused(status: status, code: code)
        }
        return json
    }
}

enum WatchVoiceRESTError: Error, Equatable, Sendable {
    case invalidRequest
    case authenticationRequired
    case network
    case refused(status: Int, code: String)
    case malformedResponse
}

private func watchVoiceRESTUUID4(_ value: String) -> Bool {
    value.range(
        of: "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        options: .regularExpression) != nil
}

private func watchVoiceRESTOpaque(_ value: String) -> Bool {
    (1...128).contains(value.utf8.count)
        && value.range(of: "^[A-Za-z0-9._:-]+$", options: .regularExpression) != nil
}

private func watchVoiceRESTDate(_ value: JSONValue?) -> Date? {
    guard let raw = value?.stringValue else { return nil }
    let formatter = ISO8601DateFormatter()
    if let date = formatter.date(from: raw) { return date }
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.date(from: raw)
}
