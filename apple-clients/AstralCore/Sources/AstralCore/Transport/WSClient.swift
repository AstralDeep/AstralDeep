// Feature 051 — orchestrator WebSocket transport.
// Shared reconnect contract with Windows/Android (FR-005): backoff 1 s base,
// ×2 per attempt, 30 s cap, reset on success; bounded 64-frame outbound FIFO
// queue while disconnected with drop-oldest + a user-visible drop signal.
// The pure pieces (BackoffPolicy, BoundedQueue) are separated for testing.
import Foundation

public struct BackoffPolicy: Sendable {
    public let base: TimeInterval
    public let factor: Double
    public let cap: TimeInterval
    private(set) var attempt: Int = 0

    public init(base: TimeInterval = 1.0, factor: Double = 2.0, cap: TimeInterval = 30.0) {
        self.base = base
        self.factor = factor
        self.cap = cap
    }

    /// Delay for the NEXT reconnect attempt (1, 2, 4, … capped at 30).
    public mutating func next() -> TimeInterval {
        let delay = min(base * pow(factor, Double(attempt)), cap)
        attempt += 1
        return delay
    }

    public mutating func reset() { attempt = 0 }
}

public struct BoundedQueue<Element>: Sendable where Element: Sendable {
    public let limit: Int
    private(set) var elements: [Element] = []
    public private(set) var droppedCount: Int = 0

    public init(limit: Int = 64) {
        self.limit = limit
    }

    /// Append; drops the OLDEST element when full. Returns true if a drop
    /// occurred (surface it to the user — never drop silently).
    @discardableResult
    public mutating func append(_ element: Element) -> Bool {
        appendReturningDropped(element) != nil
    }

    /// Append and return the exact oldest element removed at capacity.
    public mutating func appendReturningDropped(_ element: Element) -> Element? {
        var dropped: Element?
        if elements.count >= limit {
            dropped = elements.removeFirst()
            droppedCount += 1
        }
        elements.append(element)
        return dropped
    }

    public mutating func drainAll() -> [Element] {
        let out = elements
        elements = []
        return out
    }

    public var count: Int { elements.count }
}

public enum WSEvent: Sendable {
    case connected
    case disconnected(reason: String)
    case frame(InboundFrame)
    case sendDropped(total: Int)
    case queuedOperationDropped(QueuedOperationReplay, reason: String)
    case sendRejected(action: String)
}

private struct QueuedOutboundFrame: Sendable {
    let text: String
    let replay: QueuedOperationReplay
}

/// URLSession-backed client. The app layer supplies the register_ui frame on
/// every (re)connect via `onConnect` and consumes `events`.
public actor WSClient {
    public let url: URL
    private var task: URLSessionWebSocketTask?
    private var backoff = BackoffPolicy()
    private var queue = BoundedQueue<QueuedOutboundFrame>(limit: 64)
    private var running = false
    /// `URLSessionWebSocketTask.state == .running` only means `resume()` was
    /// called. It does not prove registration or the first server receive, so
    /// user work must remain in our replayable queue until this fence is true.
    private var established = false
    private var continuation: AsyncStream<WSEvent>.Continuation?
    private var onConnect: (@Sendable () async -> String?)?
    private var onReplay: (@Sendable (QueuedOperationReplay) async -> Bool)?

    public init(url: URL) {
        self.url = url
    }

    /// Event stream (single consumer). Call before `start()`.
    public func events() -> AsyncStream<WSEvent> {
        AsyncStream { continuation in
            self.continuation = continuation
        }
    }

    /// `onConnect` returns the register_ui frame to send first on every
    /// (re)connect (silent resume: `resumed: true` after the first).
    public func start(
        onConnect: @escaping @Sendable () async -> String?,
        onReplay: @escaping @Sendable (QueuedOperationReplay) async -> Bool = { _ in true }
    ) {
        guard !running else { return }
        running = true
        self.onConnect = onConnect
        self.onReplay = onReplay
        Task { await self.runLoop() }
    }

    public func stop() {
        running = false
        established = false
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        continuation?.finish()
    }

    /// Send or queue (bounded) while disconnected.
    public func send(_ text: String) {
        if let voiceFrame = VoiceCurrentConnectionFrame(frameText: text) {
            _ = sendCurrentConnectionVoice(voiceFrame)
            return
        }
        guard let replay = QueuedOperationReplay(frameText: text) else {
            continuation?.yield(.sendRejected(action: Self.actionHint(text)))
            return
        }
        let queued = QueuedOutboundFrame(text: text, replay: replay)
        if established, let task, task.state == .running {
            transmit(queued, using: task)
        } else {
            retain(queued)
        }
    }

    /// Send a proof-bound voice frame only on the socket that established its
    /// connection fence. A valid frame is intentionally dropped while
    /// disconnected; the voice controller owns bounded transcript retry, and
    /// playout evidence is never replayable.
    @discardableResult
    public func sendCurrentConnectionVoice(_ text: String) -> Bool {
        guard let frame = VoiceCurrentConnectionFrame(frameText: text) else {
            continuation?.yield(.sendRejected(action: Self.actionHint(text)))
            return false
        }
        return sendCurrentConnectionVoice(frame)
    }

    /// Typed overload for callers that validate before crossing actor
    /// isolation. Returns true only when the current established task accepted
    /// the send attempt; it never retains the frame for reconnect replay.
    @discardableResult
    public func sendCurrentConnectionVoice(_ frame: VoiceCurrentConnectionFrame) -> Bool {
        guard established, let task, task.state == .running else { return false }
        task.send(.string(frame.frameText)) { _ in }
        return true
    }

    private func runLoop() async {
        while running {
            // Dial FIRST, then fetch credentials while the TCP/TLS/WS
            // handshake is in flight — URLSession buffers sends until the
            // socket is open, so register_ui is still the first frame on the
            // wire. On a cold launch with a stale access token the IdP
            // refresh and the dial overlap instead of running back-to-back.
            // The shared session (vs. one per attempt) keeps the TLS session
            // cache warm across reconnects and never leaks session objects.
            // Nothing but register_ui is ever sent pre-registration: if the
            // credentials don't materialize, the socket is closed unused and
            // we wait out the backoff. Offline launches sit here.
            let task = NoStoreHTTP.session.webSocketTask(with: url)
            self.task = task
            established = false
            task.resume()

            guard let register = await onConnect?(), running else {
                task.cancel(with: .normalClosure, reason: nil)
                self.task = nil
                guard running else { break }
                continuation?.yield(.disconnected(reason: "waiting for credentials"))
                try? await Task.sleep(nanoseconds: UInt64(backoffNext() * 1_000_000_000))
                continue
            }
            task.send(.string(register)) { _ in }

            // `.connected` (and the backoff reset, per the shared contract:
            // reset ONLY on success) waits for the first successful receive —
            // resume() alone proves nothing when the network is down.
            receive: while running {
                do {
                    let message = try await task.receive()
                    if !established {
                        established = true
                        backoff.reset()
                        continuation?.yield(.connected)
                        for queued in queue.drainAll() {
                            guard await onReplay?(queued.replay) == true else {
                                continuation?.yield(
                                    .queuedOperationDropped(
                                        queued.replay,
                                        reason: "replay fence rejected"))
                                continue
                            }
                            transmit(queued, using: task)
                        }
                    }
                    switch message {
                    case .string(let text):
                        if let frame = InboundFrame.parse(text) {
                            continuation?.yield(.frame(frame))
                        }
                    case .data(let data):
                        if let text = String(data: data, encoding: .utf8),
                            let frame = InboundFrame.parse(text)
                        {
                            continuation?.yield(.frame(frame))
                        }
                    @unknown default:
                        break
                    }
                } catch {
                    established = false
                    continuation?.yield(.disconnected(reason: error.localizedDescription))
                    break receive
                }
            }
            established = false
            self.task = nil
            guard running else { break }
            let delay = backoffNext()
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
        }
    }

    private func backoffNext() -> TimeInterval {
        backoff.next()
    }

    private func transmit(
        _ queued: QueuedOutboundFrame,
        using task: URLSessionWebSocketTask
    ) {
        task.send(.string(queued.text)) { [weak self] error in
            guard error != nil else { return }
            // Reuse the exact identities. If delivery became ambiguous, the
            // server's durable submission idempotency fence prevents a second
            // mutation while retaining work that URLSession did not accept.
            Task { await self?.retain(queued) }
        }
    }

    private func retain(_ queued: QueuedOutboundFrame) {
        if let dropped = queue.appendReturningDropped(queued) {
            continuation?.yield(
                .queuedOperationDropped(dropped.replay, reason: "offline queue full"))
            continuation?.yield(.sendDropped(total: queue.droppedCount))
        }
    }

    private static func actionHint(_ text: String) -> String {
        InboundFrame.parse(text)?.payload["action"]?.stringValue ?? "message"
    }
}
