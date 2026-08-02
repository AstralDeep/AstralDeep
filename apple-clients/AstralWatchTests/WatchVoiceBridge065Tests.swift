import AstralCore
import Foundation
import XCTest

@testable import AstralWatch

@MainActor
final class WatchVoiceBridge065Tests: XCTestCase {
    private let sessionId = "00000000-0000-4000-8000-000000000101"
    private let turnId = "00000000-0000-4000-8000-000000000102"
    private let announcementId = "00000000-0000-4000-8000-000000000103"
    private let worker = "voice-worker-bridge-01"

    func testReadyHandshakeUsesHeaderThenManifestGatesHalfDuplexPlayback() async throws {
        let audio = MockWatchVoiceAudioIO()
        let socket = MockWatchVoiceWebSocket(messages: [.string(try readyJSON())])
        var request: URLRequest?
        let bridge = WatchVoiceBridge(
            audio: audio,
            socketFactory: {
                request = $0
                return (nil, socket)
            })
        let grant = try XCTUnwrap(WatchVoiceBridgeGrant(json: grantJSON()))
        var states: [WatchVoiceBridgeState] = []
        var playout: [WatchVoicePlayoutPhase] = []

        try await bridge.connect(
            grant: grant,
            onState: { states.append($0) },
            onTranscript: { _ in XCTFail("No transcript expected") },
            onPlayout: { playout.append($0.phase) })
        bridge.setCaptureEnabled(true)

        XCTAssertEqual(request?.value(forHTTPHeaderField: "Authorization"), "Bearer \(grant.ticket)")
        XCTAssertNil(request?.url?.query)
        XCTAssertEqual(states, [.connecting, .ready])
        XCTAssertEqual(audio.startCaptureCount, 1)

        socket.push(.string(try announcementJSON(worker: worker)))
        socket.push(.data(assistantFrame(sequence: 10).encoded))
        socket.push(.data(assistantFrame(sequence: 11).encoded))
        try await Task.sleep(for: .milliseconds(60))

        XCTAssertEqual(playout, [.started, .finished])
        XCTAssertEqual(audio.playedPayloads.count, 2)
        XCTAssertGreaterThanOrEqual(audio.stopCaptureCount, 1)
        XCTAssertEqual(audio.startCaptureCount, 2, "Half-duplex capture resumes after playout")

        bridge.disconnect(reason: "ended_by_user")
        XCTAssertEqual(socket.cancelCode, .goingAway)
        XCTAssertEqual(bridge.state, .ended)
    }

    func testUnexpectedTicketBoundWorkerFailsBeforeAnyAudioPlayback() async throws {
        let audio = MockWatchVoiceAudioIO()
        let socket = MockWatchVoiceWebSocket(messages: [.string(try readyJSON())])
        let bridge = WatchVoiceBridge(
            audio: audio,
            socketFactory: { _ in (nil, socket) })
        let grant = try XCTUnwrap(WatchVoiceBridgeGrant(json: grantJSON()))

        try await bridge.connect(
            grant: grant,
            onState: { _ in },
            onTranscript: { _ in },
            onPlayout: { _ in })
        socket.push(.string(try announcementJSON(worker: "voice-worker-evil")))
        try await Task.sleep(for: .milliseconds(40))

        XCTAssertEqual(bridge.state, .failed("invalid_control"))
        XCTAssertTrue(audio.playedPayloads.isEmpty)
        XCTAssertEqual(socket.cancelCode, .policyViolation)
    }

    private func assistantFrame(sequence: UInt64) -> WatchVoicePCMFrame {
        WatchVoicePCMFrame(
            kind: .assistant,
            sequence: sequence,
            timestampMicroseconds: sequence * 20_000,
            payload: Data(repeating: 0x05, count: WatchVoicePCMFrame.assistantPayloadLength))!
    }

    private func grantJSON() -> JSONValue {
        .object([
            "grant_id": .string("watch-grant-bridge-01"),
            "transport": .string("watch_pcm_websocket"),
            "session_id": .string(sessionId),
            "generation": .number(1),
            "media_grant_revision": .number(1),
            "expires_at": .string(timestamp(seconds: 300)),
            "url": .string("wss://astraldeep.example/api/voice/watch-bridge"),
            "ticket": .string(String(repeating: "s", count: 48)),
            "worker_identity": .string(worker),
            "capture": profile(rate: 16_000),
            "playback": profile(rate: 24_000),
        ])
    }

    private func readyJSON() throws -> String {
        let value: JSONValue = .object([
            "type": .string("bridge_ready"),
            "schema_version": .string("1"),
            "session_id": .string(sessionId),
            "generation": .number(1),
            "media_grant_revision": .number(1),
            "worker_identity": .string(worker),
            "capture": profile(rate: 16_000),
            "playback": profile(rate: 24_000),
        ])
        return String(decoding: try value.encoded(), as: UTF8.self)
    }

    private func announcementJSON(worker: String) throws -> String {
        let value: JSONValue = .object([
            "type": .string("voice_announcement_media"),
            "schema_version": .string("1"),
            "session_id": .string(sessionId),
            "generation": .number(1),
            "media_grant_revision": .number(1),
            "announcement_id": .string(announcementId),
            "announcement_sequence": .number(1),
            "turn_id": .string(turnId),
            "kind": .string("acknowledgement"),
            "quantum_role": .string("single"),
            "quantum_index": .number(0),
            "transport": .string("watch_pcm_websocket"),
            "worker_identity": .string(worker),
            "sample_rate_hz": .number(24_000),
            "duration_samples": .number(960),
            "first_media_sequence": .number(10),
            "last_media_sequence": .number(11),
        ])
        return String(decoding: try value.encoded(), as: UTF8.self)
    }

    private func profile(rate: Int) -> JSONValue {
        .object([
            "encoding": .string("pcm_s16le"),
            "channels": .number(1),
            "sample_rate_hz": .number(Double(rate)),
            "frame_duration_ms": .number(20),
        ])
    }

    private func timestamp(seconds: TimeInterval) -> String {
        ISO8601DateFormatter().string(from: Date().addingTimeInterval(seconds))
    }
}

@MainActor
private final class MockWatchVoiceAudioIO: WatchVoiceAudioIO {
    var microphonePermission: WatchVoicePermission = .authorized
    private(set) var startCaptureCount = 0
    private(set) var stopCaptureCount = 0
    private(set) var playedPayloads: [Data] = []
    private var captureHandler: (@Sendable (Data) -> Void)?

    func requestMicrophonePermission() async -> WatchVoicePermission { microphonePermission }
    func setEventHandler(_ handler: @escaping @MainActor (WatchVoiceAudioEvent) -> Void) {}
    func prepare() throws {}

    func startCapture(_ handler: @escaping @Sendable (Data) -> Void) throws {
        startCaptureCount += 1
        captureHandler = handler
    }

    func stopCapture() {
        stopCaptureCount += 1
        captureHandler = nil
    }

    func enqueuePlayback(
        _ pcm: Data,
        startsAnnouncement: Bool,
        endsAnnouncement: Bool,
        onStarted: @escaping @MainActor () -> Void,
        onFinished: @escaping @MainActor () -> Void
    ) throws {
        playedPayloads.append(pcm)
        if startsAnnouncement { onStarted() }
        if endsAnnouncement { onFinished() }
    }

    func stopPlayback() {}
    func stop() { stopCapture() }
}

@MainActor
private final class MockWatchVoiceWebSocket: WatchVoiceWebSocket {
    private var messages: [URLSessionWebSocketTask.Message]
    private var waiter: CheckedContinuation<URLSessionWebSocketTask.Message, Error>?
    private(set) var sent: [URLSessionWebSocketTask.Message] = []
    private(set) var cancelCode: URLSessionWebSocketTask.CloseCode?

    init(messages: [URLSessionWebSocketTask.Message]) {
        self.messages = messages
    }

    func resume() {}

    func receive() async throws -> URLSessionWebSocketTask.Message {
        if !messages.isEmpty { return messages.removeFirst() }
        return try await withCheckedThrowingContinuation { waiter = $0 }
    }

    func send(_ message: URLSessionWebSocketTask.Message) async throws {
        sent.append(message)
    }

    func cancel(with closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        cancelCode = closeCode
        waiter?.resume(throwing: URLError(.cancelled))
        waiter = nil
    }

    func push(_ message: URLSessionWebSocketTask.Message) {
        if let waiter {
            self.waiter = nil
            waiter.resume(returning: message)
        } else {
            messages.append(message)
        }
    }
}
