import Foundation

/// HTTP transport for authenticated and credential-bearing requests.
///
/// `URLSession.shared` uses the process-wide URL cache. Some Apple platforms
/// persist that cache to disk, including request bodies and authorization
/// headers. AstralCore must keep those values in the keychain or in memory,
/// never in CFNetwork's response cache.
public enum NoStoreHTTP {
    public static let session: URLSession = {
        prepareForLaunch()
        return URLSession(configuration: configuration())
    }()

    /// Fail closed if an older client left credential-bearing CFNetwork cache
    /// artifacts that this process cannot remove from its own sandbox.
    public static func prepareForLaunch() {
        guard purgeLegacyPersistentURLCache() else {
            fatalError("Unable to clear AstralDeep's legacy network cache")
        }
    }

    /// Remove credential-bearing response-cache files produced by older
    /// clients before every authenticated request moved to an ephemeral
    /// session. This is intentionally limited to CFNetwork's bundle-scoped
    /// cache database; unrelated application caches are left untouched.
    static func purgeLegacyPersistentURLCache(
        cacheRoot: URL? = nil,
        bundleIdentifier: String? = Bundle.main.bundleIdentifier,
        fileManager: FileManager = .default,
        removeItem: ((URL) throws -> Void)? = nil
    ) -> Bool {
        URLCache.shared.memoryCapacity = 0
        URLCache.shared.diskCapacity = 0
        URLCache.shared.removeAllCachedResponses()

        guard
            let bundleIdentifier,
            !bundleIdentifier.isEmpty,
            let root = cacheRoot
                ?? fileManager.urls(for: .cachesDirectory, in: .userDomainMask).first
        else { return false }

        let legacyDirectory = root.appendingPathComponent(
            bundleIdentifier,
            isDirectory: true)
        let remover = removeItem ?? { try fileManager.removeItem(at: $0) }
        var succeeded = true
        for filename in [
            "Cache.db",
            "Cache.db-shm",
            "Cache.db-wal",
            "Cache.db-journal",
            "fsCachedData",
        ] {
            let artifact = legacyDirectory.appendingPathComponent(filename)
            guard fileManager.fileExists(atPath: artifact.path) else { continue }
            do {
                try remover(artifact)
            } catch {
                succeeded = false
            }
            if fileManager.fileExists(atPath: artifact.path) {
                succeeded = false
            }
        }
        return succeeded
    }

    static func configuration() -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        configuration.urlCache = nil
        configuration.urlCredentialStorage = nil
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        return configuration
    }

    public static func request(
        url: URL,
        method: String = "GET",
        body: Data? = nil,
        contentType: String? = nil
    ) -> URLRequest {
        var request = URLRequest(
            url: url,
            cachePolicy: .reloadIgnoringLocalAndRemoteCacheData)
        request.httpMethod = method
        request.httpBody = body
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        request.setValue("no-cache", forHTTPHeaderField: "Pragma")
        if let contentType {
            request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        }
        return request
    }
}
