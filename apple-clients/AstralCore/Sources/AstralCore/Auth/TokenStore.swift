// Feature 051 — token persistence (Keychain on device, in-memory for tests)
// and the refresh strategy per platform (research D7):
//   iOS/macOS → refresh directly against the IdP token endpoint (Windows
//               precedent, public client);
//   watch     → refresh via the backend broker (single TLS peer).
import Foundation

#if canImport(Security)
    import Security
#endif

public protocol TokenStorage: Sendable {
    func load() -> StoredTokens?
    func save(_ tokens: StoredTokens)
    func wipe()
}

public struct StoredTokens: Codable, Sendable, Equatable {
    public var accessToken: String
    public var refreshToken: String?
    public var expiresAt: Date

    public init(from set: TokenSet) {
        self.accessToken = set.accessToken
        self.refreshToken = set.refreshToken
        self.expiresAt = set.expiresAt
    }

    public var tokenSet: TokenSet {
        TokenSet(
            accessToken: accessToken, refreshToken: refreshToken,
            expiresIn: expiresAt.timeIntervalSinceNow)
    }
}

public final class InMemoryTokenStore: TokenStorage, @unchecked Sendable {
    private var tokens: StoredTokens?
    private let lock = NSLock()

    public init() {}

    public func load() -> StoredTokens? {
        lock.lock()
        defer { lock.unlock() }
        return tokens
    }

    public func save(_ tokens: StoredTokens) {
        lock.lock()
        defer { lock.unlock() }
        self.tokens = tokens
    }

    public func wipe() {
        lock.lock()
        defer { lock.unlock() }
        tokens = nil
    }
}

#if canImport(Security)
    /// Keychain-backed store (FR-007: tokens live in the platform keychain).
    public final class KeychainTokenStore: TokenStorage, @unchecked Sendable {
        private let service: String

        public init(service: String = "com.personalailabs.astraldeep.tokens") {
            self.service = service
        }

        private var query: [String: Any] {
            [
                kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: service,
                kSecAttrAccount as String: "session",
            ]
        }

        public func load() -> StoredTokens? {
            var q = query
            q[kSecReturnData as String] = true
            q[kSecMatchLimit as String] = kSecMatchLimitOne
            var out: AnyObject?
            guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess,
                let data = out as? Data
            else { return nil }
            return try? JSONDecoder().decode(StoredTokens.self, from: data)
        }

        public func save(_ tokens: StoredTokens) {
            guard let data = try? JSONEncoder().encode(tokens) else { return }
            // Delete-then-add so the accessibility class is always applied
            // (SecItemUpdate cannot change it on an existing item).
            SecItemDelete(query as CFDictionary)
            var add = query
            add[kSecValueData as String] = data
            // Available after first unlock: cold launches (including before the
            // UI is unlocked post-reboot) restore the session without sign-in.
            // ThisDeviceOnly: refresh tokens never ride iCloud Keychain backups.
            add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            SecItemAdd(add as CFDictionary, nil)
        }

        public func wipe() {
            SecItemDelete(query as CFDictionary)
        }
    }
#endif

/// Outcome of a refresh attempt, classified so callers can tell a definitive
/// credential rejection (wipe and re-authenticate) from a transient failure
/// (KEEP the stored tokens — an offline launch must never destroy a session).
public enum RefreshResult: Sendable {
    case ok(TokenSet)
    /// The IdP/broker definitively refused the refresh token
    /// (revoked / expired / hard-cap). Wipe and go to interactive sign-in.
    case rejected(String)
    /// Network unreachable, timeout, rate limit, or server unavailable.
    /// Credentials stay valid — retry later.
    case transient(String)
}

/// How a session obtains a fresh access token when the current one nears
/// expiry. Both paths keep the sign-in interactive anchor untouched — the
/// realm's session-max policy bounds them (research D7).
public enum RefreshStrategy: Sendable {
    /// Direct to the IdP token endpoint (iOS/macOS; Windows precedent).
    case direct(OIDCConfig)
    /// Via the backend broker (watch; single TLS peer, FR-021).
    case broker(DeviceLoginClient)

    public func refresh(refreshToken: String) async throws -> TokenSet {
        switch self {
        case .direct(let config):
            let request = Self.directRequest(config: config, refreshToken: refreshToken)
            let (data, response): (Data, URLResponse)
            do {
                (data, response) = try await NoStoreHTTP.session.data(for: request)
            } catch {
                throw DeviceLoginError.transport(error.localizedDescription)
            }
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status == 400 || status == 401 {
                throw DeviceLoginError.rejected("refresh rejected (HTTP \(status))")
            }
            guard status == 200, let json = try? JSONValue.parse(data),
                let tokens = TokenSet(json: json)
            else {
                throw DeviceLoginError.unavailable("refresh failed (HTTP \(status))")
            }
            return tokens
        case .broker(let client):
            return try await client.refresh(refreshToken: refreshToken)
        }
    }

    static func directRequest(config: OIDCConfig, refreshToken: String) -> URLRequest {
        NoStoreHTTP.request(
            url: config.tokenEndpoint,
            method: "POST",
            body: Data(config.refreshRequestBody(refreshToken: refreshToken).utf8),
            contentType: "application/x-www-form-urlencoded")
    }

    /// `refresh` with the failure mode classified (never throws). If the IdP
    /// rotates without returning a new refresh token, the previous one is
    /// preserved — dropping it would silently force a re-login at next expiry.
    public func attempt(refreshToken: String) async -> RefreshResult {
        do {
            var set = try await refresh(refreshToken: refreshToken)
            if set.refreshToken == nil {
                set = TokenSet(
                    accessToken: set.accessToken,
                    refreshToken: refreshToken,
                    expiresIn: set.expiresAt.timeIntervalSinceNow)
            }
            return .ok(set)
        } catch let error as DeviceLoginError {
            switch error {
            case .rejected(let detail):
                return .rejected(detail)
            case .invalidHandle:
                return .rejected("invalid_grant")
            case .unavailable(let detail), .transport(let detail):
                return .transient(detail)
            case .rateLimited:
                return .transient("rate limited")
            }
        } catch {
            return .transient(error.localizedDescription)
        }
    }
}
