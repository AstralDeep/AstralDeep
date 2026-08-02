// Feature 051 — watch-side device-login state machine against a scripted
// broker (contracts/device-login.md): pacing (pending keeps interval,
// slow_down raises it, never poll faster), terminal states, error mapping.
import XCTest

@testable import AstralCore

final class DeviceLoginTests: XCTestCase {

    final class ScriptedBroker: @unchecked Sendable {
        var responses: [(Int, String)]
        private(set) var calls: [(String, JSONValue)] = []
        private let lock = NSLock()

        init(_ responses: [(Int, String)]) {
            self.responses = responses
        }

        var transport: DeviceLoginClient.Transport {
            { [self] url, body in
                lock.lock()
                defer { lock.unlock() }
                let payload = (try? JSONValue.parse(body)) ?? .null
                calls.append((url.lastPathComponent, payload))
                guard !responses.isEmpty else {
                    throw DeviceLoginError.transport("unexpected call")
                }
                let (status, text) = responses.removeFirst()
                return (status, Data(text.utf8))
            }
        }
    }

    static let startBody = """
        {"handle":"h-opaque","user_code":"WDJB-MJHT",
         "verification_uri":"https://idp/device",
         "verification_uri_complete":"https://idp/device?user_code=WDJB-MJHT",
         "expires_in":600,"interval":5,
         "qr_png_base64":"\(Data([0x89, 0x50, 0x4E, 0x47]).base64EncodedString())"}
        """

    static let approvedBody = """
        {"status":"approved","tokens":{"access_token":"at","refresh_token":"rt",
         "expires_in":300,"token_type":"Bearer"}}
        """

    func client(_ broker: ScriptedBroker) -> DeviceLoginClient {
        DeviceLoginClient(
            serverBase: URL(string: "http://127.0.0.1:8001")!,
            transport: broker.transport)
    }

    func testStartParsesQRAndCode() async throws {
        let broker = ScriptedBroker([(200, Self.startBody)])
        let start = try await client(broker).start()
        XCTAssertEqual(start.userCode, "WDJB-MJHT")
        XCTAssertEqual(start.interval, 5)
        XCTAssertEqual(start.qrPNG?.prefix(4), Data([0x89, 0x50, 0x4E, 0x47]))
        XCTAssertEqual(broker.calls.first?.0, "start")
        XCTAssertEqual(broker.calls.first?.1["client"]?.stringValue, "astral-watch")
    }

    func testDefaultBrokerRequestCannotEnterURLCache() throws {
        let url = try XCTUnwrap(URL(string: "https://astral.example.test/api/auth/device/poll"))
        let body = Data(#"{"handle":"synthetic-handle"}"#.utf8)

        let request = DeviceLoginClient.brokerRequest(url: url, body: body)

        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.httpBody, body)
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Cache-Control"), "no-store")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Pragma"), "no-cache")
        XCTAssertEqual(request.cachePolicy, .reloadIgnoringLocalAndRemoteCacheData)
    }

    func testNoStoreSessionHasNoPersistentCredentialOrResponseStores() {
        let configuration = NoStoreHTTP.configuration()

        XCTAssertEqual(
            configuration.requestCachePolicy,
            .reloadIgnoringLocalAndRemoteCacheData)
        XCTAssertNil(configuration.urlCache)
        XCTAssertNil(configuration.urlCredentialStorage)
        XCTAssertNil(configuration.httpCookieStorage)
        XCTAssertFalse(configuration.httpShouldSetCookies)
    }

    func testNoStoreSessionPurgesOnlyLegacyBundleCacheDatabase() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(
            "astral-no-store-\(UUID().uuidString)",
            isDirectory: true)
        let legacy = root.appendingPathComponent("test.astraldeep.watch", isDirectory: true)
        try FileManager.default.createDirectory(
            at: legacy,
            withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        for filename in [
            "Cache.db",
            "Cache.db-shm",
            "Cache.db-wal",
            "Cache.db-journal",
            "keep.txt",
        ] {
            try Data(filename.utf8).write(
                to: legacy.appendingPathComponent(filename))
        }

        let payloadDirectory = legacy.appendingPathComponent(
            "fsCachedData",
            isDirectory: true)
        try FileManager.default.createDirectory(
            at: payloadDirectory,
            withIntermediateDirectories: true)
        try Data("legacy-response-body".utf8).write(
            to: payloadDirectory.appendingPathComponent("payload"))

        XCTAssertTrue(
            NoStoreHTTP.purgeLegacyPersistentURLCache(
                cacheRoot: root,
                bundleIdentifier: "test.astraldeep.watch"))

        for filename in [
            "Cache.db",
            "Cache.db-shm",
            "Cache.db-wal",
            "Cache.db-journal",
            "fsCachedData",
        ] {
            XCTAssertFalse(
                FileManager.default.fileExists(
                    atPath: legacy.appendingPathComponent(filename).path))
        }
        XCTAssertTrue(
            FileManager.default.fileExists(
                atPath: legacy.appendingPathComponent("keep.txt").path))
    }

    func testLegacyCachePurgeReportsRemovalFailure() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(
            "astral-no-store-failure-\(UUID().uuidString)",
            isDirectory: true)
        let legacy = root.appendingPathComponent("test.astraldeep.watch", isDirectory: true)
        try FileManager.default.createDirectory(
            at: legacy,
            withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let cache = legacy.appendingPathComponent("Cache.db")
        try Data("legacy".utf8).write(to: cache)

        let succeeded = NoStoreHTTP.purgeLegacyPersistentURLCache(
            cacheRoot: root,
            bundleIdentifier: "test.astraldeep.watch",
            removeItem: { _ in throw CocoaError(.fileWriteNoPermission) })

        XCTAssertFalse(succeeded)
        XCTAssertTrue(FileManager.default.fileExists(atPath: cache.path))
    }

    func testDirectRefreshRequestCannotEnterURLCache() throws {
        let authority = try XCTUnwrap(URL(string: "https://idp.example.test/realms/astral"))
        let config = OIDCConfig(
            authority: authority,
            clientId: "synthetic-client",
            redirectURI: "synthetic-app:/callback")

        let request = RefreshStrategy.directRequest(
            config: config,
            refreshToken: "synthetic-refresh-value")

        XCTAssertEqual(request.url, config.tokenEndpoint)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertNotNil(request.httpBody)
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "Content-Type"),
            "application/x-www-form-urlencoded")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Cache-Control"), "no-store")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Pragma"), "no-cache")
        XCTAssertEqual(request.cachePolicy, .reloadIgnoringLocalAndRemoteCacheData)
    }

    func testBrokerUnavailableMapsTo503Error() async {
        let broker = ScriptedBroker([
            (503, #"{"detail":{"error":"device_login_unavailable","detail":"realm lacks grant"}}"#)
        ])
        do {
            _ = try await client(broker).start()
            XCTFail("expected unavailable")
        } catch let error as DeviceLoginError {
            XCTAssertEqual(error, .unavailable("realm lacks grant"))
        } catch {
            XCTFail("wrong error \(error)")
        }
    }

    func testPollTerminalStates() async throws {
        let broker = ScriptedBroker([
            (200, #"{"status":"pending","interval":5}"#),
            (200, #"{"status":"slow_down","interval":10}"#),
            (200, Self.approvedBody),
            (400, #"{"detail":{"error":"invalid_handle"}}"#),
        ])
        let c = client(broker)
        let pending = try await c.poll(handle: "h")
        XCTAssertEqual(pending, .pending(interval: 5))
        let slow = try await c.poll(handle: "h")
        XCTAssertEqual(slow, .slowDown(interval: 10))
        if case .approved(let tokens) = try await c.poll(handle: "h") {
            XCTAssertEqual(tokens.accessToken, "at")
            XCTAssertEqual(tokens.refreshToken, "rt")
        } else {
            XCTFail("expected approved")
        }
        do {
            _ = try await c.poll(handle: "h")
            XCTFail("expected invalidHandle")
        } catch let error as DeviceLoginError {
            XCTAssertEqual(error, .invalidHandle)
        }
    }

    func testWaitForApprovalHonorsServerPacing() async throws {
        let broker = ScriptedBroker([
            (200, #"{"status":"pending","interval":5}"#),
            (200, #"{"status":"slow_down","interval":10}"#),
            (200, #"{"status":"pending","interval":10}"#),
            (200, Self.approvedBody),
        ])
        let start = try await {
            let sb = ScriptedBroker([(200, Self.startBody)])
            return try await client(sb).start()
        }()

        let waits = Waits()
        let result = try await client(broker).waitForApproval(
            start: start,
            sleeper: { seconds in await waits.record(seconds) })
        if case .approved = result {} else { XCTFail("expected approved") }
        // 5 (initial) → 5 (pending keeps) → 10 (slow_down raises) → 10 (kept)
        let recorded = await waits.values
        XCTAssertEqual(recorded, [5, 5, 10, 10])
    }

    func testDeniedAndExpiredAreTerminal() async throws {
        let broker = ScriptedBroker([
            (200, #"{"status":"denied","reason":"denied_no_access"}"#),
            (200, #"{"status":"expired"}"#),
        ])
        let c = client(broker)
        let denied = try await c.poll(handle: "h")
        XCTAssertEqual(denied, .denied(reason: "denied_no_access"))
        let expired = try await c.poll(handle: "h")
        XCTAssertEqual(expired, .expired)
    }

    func testRefreshViaBroker() async throws {
        let broker = ScriptedBroker([
            (200, #"{"access_token":"at2","refresh_token":"rt2","expires_in":300}"#)
        ])
        let tokens = try await client(broker).refresh(refreshToken: "rt")
        XCTAssertEqual(tokens.accessToken, "at2")
        XCTAssertEqual(broker.calls.first?.0, "refresh")
        XCTAssertEqual(broker.calls.first?.1["client"]?.stringValue, "astral-watch")
    }
}

actor Waits {
    private(set) var values: [TimeInterval] = []
    func record(_ v: TimeInterval) { values.append(v) }
}
