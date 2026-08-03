import AVFoundation
import AstralCore
import Foundation
import Observation

// Feature 065 — watchOS foreground PCM relay. The bridge owns only ephemeral
// grants and audio buffers. It never persists a ticket, transcript proof, raw
// PCM, or provider response and never falls back to platform ASR/TTS.

enum WatchVoiceBridgeState: Equatable, Sendable {
    case idle
    case connecting
    case ready
    case reconnecting
    case failed(String)
    case ended
}

enum WatchVoicePlayoutPhase: String, Equatable, Sendable {
    case started
    case finished
    case interrupted
}

enum WatchVoiceAudioEvent: Equatable, Sendable {
    case interrupted(String)
    case recovered
}

struct WatchVoicePlayoutObservation: Equatable, Sendable {
    let announcement: WatchVoiceAnnouncement
    let phase: WatchVoicePlayoutPhase
}

@MainActor
protocol WatchVoiceBridgeControlling: AnyObject {
    var state: WatchVoiceBridgeState { get }
    var microphonePermission: WatchVoicePermission { get }
    func requestMicrophonePermission() async -> WatchVoicePermission
    func connect(
        grant: WatchVoiceBridgeGrant,
        onState: @escaping @MainActor (WatchVoiceBridgeState) -> Void,
        onTranscript: @escaping @MainActor (WatchVoiceTranscript) -> Void,
        onPlayout: @escaping @MainActor (WatchVoicePlayoutObservation) -> Void
    ) async throws
    func setCaptureEnabled(_ enabled: Bool)
    func interruptPlayback()
    func disconnect(reason: String)
}

@MainActor
protocol WatchVoiceAudioIO: AnyObject {
    var microphonePermission: WatchVoicePermission { get }
    func requestMicrophonePermission() async -> WatchVoicePermission
    func setEventHandler(_ handler: @escaping @MainActor (WatchVoiceAudioEvent) -> Void)
    func prepare() throws
    func startCapture(_ handler: @escaping @Sendable (Data) -> Void) throws
    func stopCapture()
    func enqueuePlayback(
        _ pcm: Data,
        startsAnnouncement: Bool,
        endsAnnouncement: Bool,
        onStarted: @escaping @MainActor () -> Void,
        onFinished: @escaping @MainActor () -> Void
    ) throws
    func stopPlayback()
    func stop()
}

protocol WatchVoiceWebSocket: AnyObject {
    func resume()
    func receive() async throws -> URLSessionWebSocketTask.Message
    func send(_ message: URLSessionWebSocketTask.Message) async throws
    func cancel(with closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?)
}

extension URLSessionWebSocketTask: WatchVoiceWebSocket {}

@MainActor
@Observable
final class WatchVoiceBridge: WatchVoiceBridgeControlling {
    private static let closePolicyViolation = URLSessionWebSocketTask.CloseCode.policyViolation

    private let audio: WatchVoiceAudioIO
    private let socketFactory: (URLRequest) -> (URLSession?, any WatchVoiceWebSocket)
    @ObservationIgnored private var urlSession: URLSession?
    @ObservationIgnored private var socket: (any WatchVoiceWebSocket)?
    @ObservationIgnored private var receiveTask: Task<Void, Never>?
    @ObservationIgnored private var grant: WatchVoiceBridgeGrant?
    @ObservationIgnored private var sequenceGate = WatchVoicePCMSequenceGate()
    @ObservationIgnored private var captureSequence: UInt64 = 0
    @ObservationIgnored private var captureStartedAt = ContinuousClock.now
    @ObservationIgnored private var captureEnabled = false
    @ObservationIgnored private var activeAnnouncement: WatchVoiceAnnouncement?
    @ObservationIgnored private var announcementLedger = WatchVoiceAnnouncementLedger()
    @ObservationIgnored private var announcementStarted = false
    @ObservationIgnored private var awaitingAudioRecovery = false
    @ObservationIgnored private var stateHandler: (@MainActor (WatchVoiceBridgeState) -> Void)?
    @ObservationIgnored private var transcriptHandler: (@MainActor (WatchVoiceTranscript) -> Void)?
    @ObservationIgnored private var playoutHandler: (@MainActor (WatchVoicePlayoutObservation) -> Void)?

    private(set) var state: WatchVoiceBridgeState = .idle

    convenience init() {
        self.init(audio: WatchVoiceAudioEngine())
    }

    init(
        audio: WatchVoiceAudioIO,
        sessionFactory: @escaping () -> URLSession = {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
            configuration.urlCache = nil
            configuration.httpCookieStorage = nil
            configuration.waitsForConnectivity = false
            return URLSession(configuration: configuration)
        },
        socketFactory: ((URLRequest) -> (URLSession?, any WatchVoiceWebSocket))? = nil
    ) {
        self.audio = audio
        self.socketFactory =
            socketFactory ?? { request in
                let session = sessionFactory()
                return (session, session.webSocketTask(with: request))
            }
        audio.setEventHandler { [weak self] event in self?.consumeAudioEvent(event) }
    }

    var microphonePermission: WatchVoicePermission { audio.microphonePermission }

    func requestMicrophonePermission() async -> WatchVoicePermission {
        await audio.requestMicrophonePermission()
    }

    func connect(
        grant: WatchVoiceBridgeGrant,
        onState: @escaping @MainActor (WatchVoiceBridgeState) -> Void,
        onTranscript: @escaping @MainActor (WatchVoiceTranscript) -> Void,
        onPlayout: @escaping @MainActor (WatchVoicePlayoutObservation) -> Void
    ) async throws {
        disconnect(reason: "replacement")
        guard grant.expiresAt > Date() else { throw WatchVoiceBridgeError.expiredGrant }
        self.grant = grant
        stateHandler = onState
        transcriptHandler = onTranscript
        playoutHandler = onPlayout
        sequenceGate.reset()
        captureSequence = 0
        activeAnnouncement = nil
        announcementLedger.reset()
        announcementStarted = false
        awaitingAudioRecovery = false
        captureEnabled = false
        setState(.connecting)

        do {
            try audio.prepare()
        } catch {
            fail(.audioFailure)
            throw WatchVoiceBridgeError.audioFailure
        }
        var request = URLRequest(url: grant.url)
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        request.timeoutInterval = 15
        request.setValue("Bearer \(grant.ticket)", forHTTPHeaderField: "Authorization")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        let (session, task) = socketFactory(request)
        urlSession = session
        socket = task
        task.resume()

        do {
            let first = try await task.receive()
            guard case .string(let text) = first,
                text.utf8.count <= 12 * 1024,
                let data = text.data(using: .utf8),
                let json = try? JSONValue.parse(data),
                WatchVoiceBridgeReady(json: json, grant: grant) != nil
            else {
                throw WatchVoiceBridgeError.invalidHandshake
            }
            setState(.ready)
            receiveTask = Task { [weak self] in await self?.receiveLoop() }
        } catch {
            fail(.invalidHandshake)
            throw error
        }
    }

    func setCaptureEnabled(_ enabled: Bool) {
        let next = enabled && state == .ready
        guard next != captureEnabled else { return }
        captureEnabled = next
        guard captureEnabled else {
            audio.stopCapture()
            return
        }
        captureStartedAt = ContinuousClock.now
        startCaptureIfNeeded()
    }

    private func startCaptureIfNeeded() {
        guard captureEnabled, state == .ready, activeAnnouncement == nil else { return }
        do {
            try audio.startCapture { [weak self] pcm in
                Task { @MainActor [weak self] in await self?.sendMicrophonePCM(pcm) }
            }
        } catch {
            fail(.audioFailure)
        }
    }

    func interruptPlayback() {
        audio.stopPlayback()
        if let activeAnnouncement, announcementStarted {
            playoutHandler?(
                WatchVoicePlayoutObservation(
                    announcement: activeAnnouncement,
                    phase: .interrupted))
        }
        self.activeAnnouncement = nil
        announcementStarted = false
        startCaptureIfNeeded()
    }

    func disconnect(reason: String) {
        receiveTask?.cancel()
        receiveTask = nil
        captureEnabled = false
        audio.stop()
        if let activeAnnouncement, announcementStarted {
            playoutHandler?(
                WatchVoicePlayoutObservation(
                    announcement: activeAnnouncement,
                    phase: .interrupted))
        }
        activeAnnouncement = nil
        announcementStarted = false
        socket?.cancel(with: .goingAway, reason: Data(reason.prefix(64).utf8))
        socket = nil
        urlSession?.invalidateAndCancel()
        urlSession = nil
        grant = nil
        sequenceGate.reset()
        announcementLedger.reset()
        if reason != "audio_interrupted" && reason != "route_unavailable" {
            awaitingAudioRecovery = false
        }
        if state != .idle { setState(.ended) }
    }

    private func consumeAudioEvent(_ event: WatchVoiceAudioEvent) {
        switch event {
        case .interrupted(let reason):
            guard grant != nil else { return }
            awaitingAudioRecovery = true
            captureEnabled = false
            audio.stop()
            socket?.cancel(with: .goingAway, reason: Data(reason.utf8))
            socket = nil
            urlSession?.invalidateAndCancel()
            urlSession = nil
            grant = nil
            activeAnnouncement = nil
            announcementStarted = false
            setState(.failed(reason))
        case .recovered:
            guard awaitingAudioRecovery else { return }
            awaitingAudioRecovery = false
            setState(.reconnecting)
        }
    }

    private func receiveLoop() async {
        while !Task.isCancelled, let socket {
            do {
                let message = try await socket.receive()
                try await consume(message)
            } catch is CancellationError {
                return
            } catch let error as WatchVoiceBridgeError {
                if state != .ended { fail(error) }
                return
            } catch {
                if state != .ended { fail(.networkFailure) }
                return
            }
        }
    }

    private func consume(_ message: URLSessionWebSocketTask.Message) async throws {
        guard state == .ready, let grant else { throw WatchVoiceBridgeError.notReady }
        switch message {
        case .data(let data):
            try consumeAssistantPCM(data, grant: grant)
        case .string(let text):
            guard text.utf8.count <= 12 * 1024,
                let data = text.data(using: .utf8),
                let json = try? JSONValue.parse(data),
                let type = json["type"]?.stringValue
            else { throw WatchVoiceBridgeError.invalidControl }
            switch type {
            case "voice_transcript":
                guard let transcript = WatchVoiceTranscript(json: json), transcript.matches(grant: grant)
                else { throw WatchVoiceBridgeError.invalidControl }
                transcriptHandler?(transcript)
            case "voice_announcement_media":
                guard activeAnnouncement == nil,
                    let announcement = WatchVoiceAnnouncement(json: json),
                    announcement.matches(grant: grant),
                    announcementLedger.accept(announcement)
                else { throw WatchVoiceBridgeError.invalidControl }
                activeAnnouncement = announcement
                announcementStarted = false
            case "bridge_reconnecting":
                guard json.objectValue.map({ Set($0.keys) == ["type", "schema_version", "reason"] }) == true,
                    json["schema_version"]?.stringValue == "1",
                    let reason = json["reason"], watchVoiceBridgeReason(reason)
                else { throw WatchVoiceBridgeError.invalidControl }
                captureEnabled = false
                audio.stop()
                setState(.reconnecting)
            case "bridge_error", "bridge_ended":
                guard let object = json.objectValue,
                    Set(object.keys).isSubset(of: ["type", "schema_version", "reason"]),
                    Set(object.keys).isSuperset(of: ["type", "schema_version"]),
                    json["schema_version"]?.stringValue == "1",
                    json["reason"].map(watchVoiceBridgeReason) ?? true
                else { throw WatchVoiceBridgeError.invalidControl }
                if type == "bridge_ended" {
                    disconnect(reason: "server_ended")
                } else {
                    fail(.serverFailure)
                }
            case "speech_started", "speech_finished", "speech_interrupted", "ping", "pong":
                // Content-free lifecycle hints may arrive between authoritative
                // manifest/audio messages. They never authorize playout or a query.
                guard text.utf8.count <= 2 * 1024 else {
                    throw WatchVoiceBridgeError.invalidControl
                }
            default:
                throw WatchVoiceBridgeError.invalidControl
            }
        @unknown default:
            throw WatchVoiceBridgeError.invalidControl
        }
    }

    private func consumeAssistantPCM(_ data: Data, grant: WatchVoiceBridgeGrant) throws {
        guard let frame = WatchVoicePCMFrame(data: data), frame.kind == .assistant,
            sequenceGate.accept(frame),
            let announcement = activeAnnouncement,
            announcement.matches(grant: grant),
            frame.sequence >= announcement.firstMediaSequence,
            frame.sequence <= announcement.lastMediaSequence
        else { throw WatchVoiceBridgeError.invalidAudio }

        let starts = frame.sequence == announcement.firstMediaSequence
        let finishes = frame.sequence == announcement.lastMediaSequence
        if starts && announcementStarted { throw WatchVoiceBridgeError.invalidAudio }
        if !starts && !announcementStarted { throw WatchVoiceBridgeError.invalidAudio }
        if starts { audio.stopCapture() }
        try audio.enqueuePlayback(
            frame.payload,
            startsAnnouncement: starts,
            endsAnnouncement: finishes,
            onStarted: { [weak self] in
                guard let self, self.activeAnnouncement == announcement else { return }
                self.announcementStarted = true
                self.playoutHandler?(
                    WatchVoicePlayoutObservation(
                        announcement: announcement,
                        phase: .started))
            },
            onFinished: { [weak self] in
                guard let self, self.activeAnnouncement == announcement else { return }
                self.playoutHandler?(
                    WatchVoicePlayoutObservation(
                        announcement: announcement,
                        phase: .finished))
                self.activeAnnouncement = nil
                self.announcementStarted = false
                self.startCaptureIfNeeded()
            })
    }

    private func sendMicrophonePCM(_ pcm: Data) async {
        guard captureEnabled, state == .ready, let socket,
            let frame = WatchVoicePCMFrame(
                kind: .microphone,
                sequence: captureSequence,
                timestampMicroseconds: captureTimestampMicroseconds,
                payload: pcm)
        else { return }
        if captureSequence == UInt64.max {
            fail(.invalidAudio)
            return
        }
        captureSequence += 1
        do {
            try await socket.send(.data(frame.encoded))
        } catch {
            fail(.networkFailure)
        }
    }

    private var captureTimestampMicroseconds: UInt64 {
        let elapsed = captureStartedAt.duration(to: .now)
        let seconds = max(0, elapsed.components.seconds)
        let attoseconds = max(0, elapsed.components.attoseconds)
        let whole = UInt64(seconds) * 1_000_000
        let fraction = UInt64(attoseconds / 1_000_000_000_000)
        return whole &+ fraction
    }

    private func fail(_ error: WatchVoiceBridgeError) {
        captureEnabled = false
        audio.stop()
        socket?.cancel(with: Self.closePolicyViolation, reason: Data(error.safeReason.utf8))
        socket = nil
        urlSession?.invalidateAndCancel()
        urlSession = nil
        grant = nil
        activeAnnouncement = nil
        announcementStarted = false
        setState(.failed(error.safeReason))
    }

    private func setState(_ next: WatchVoiceBridgeState) {
        state = next
        stateHandler?(next)
    }
}

private func watchVoiceBridgeReason(_ value: JSONValue) -> Bool {
    guard let reason = value.stringValue, (1...64).contains(reason.utf8.count) else { return false }
    return reason.range(of: "^[a-z0-9_]+$", options: .regularExpression) != nil
}

enum WatchVoiceBridgeError: Error, Equatable, Sendable {
    case expiredGrant
    case invalidHandshake
    case invalidControl
    case invalidAudio
    case notReady
    case networkFailure
    case audioFailure
    case serverFailure

    var safeReason: String {
        switch self {
        case .expiredGrant: return "expired_grant"
        case .invalidHandshake: return "invalid_handshake"
        case .invalidControl: return "invalid_control"
        case .invalidAudio: return "invalid_audio"
        case .notReady: return "not_ready"
        case .networkFailure: return "network_interrupted"
        case .audioFailure: return "audio_interrupted"
        case .serverFailure: return "server_ended"
        }
    }
}

@MainActor
final class WatchVoiceAudioEngine: WatchVoiceAudioIO {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let playbackFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: 24_000,
        channels: 1,
        interleaved: false)!
    private var captureProcessor: WatchVoiceCaptureProcessor?
    private var captureInstalled = false
    private var playbackRunning = false
    private var eventHandler: (@MainActor (WatchVoiceAudioEvent) -> Void)?
    private var notificationTokens: [NSObjectProtocol] = []

    init() {
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: playbackFormat)
        let center = NotificationCenter.default
        notificationTokens.append(
            center.addObserver(
                forName: AVAudioSession.interruptionNotification,
                object: nil,
                queue: nil
            ) { [weak self] notification in
                guard let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                    let type = AVAudioSession.InterruptionType(rawValue: raw)
                else { return }
                Task { @MainActor [weak self] in
                    self?.eventHandler?(
                        type == .began ? .interrupted("audio_interrupted") : .recovered)
                }
            })
        notificationTokens.append(
            center.addObserver(
                forName: AVAudioSession.routeChangeNotification,
                object: nil,
                queue: nil
            ) { [weak self] notification in
                guard let raw = notification.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt,
                    let reason = AVAudioSession.RouteChangeReason(rawValue: raw)
                else { return }
                let event: WatchVoiceAudioEvent?
                switch reason {
                case .oldDeviceUnavailable, .noSuitableRouteForCategory:
                    event = .interrupted("route_unavailable")
                case .newDeviceAvailable:
                    event = .recovered
                default:
                    event = nil
                }
                guard let event else { return }
                Task { @MainActor [weak self] in self?.eventHandler?(event) }
            })
    }

    deinit {
        for token in notificationTokens { NotificationCenter.default.removeObserver(token) }
    }

    func setEventHandler(_ handler: @escaping @MainActor (WatchVoiceAudioEvent) -> Void) {
        eventHandler = handler
    }

    var microphonePermission: WatchVoicePermission {
        switch AVAudioApplication.shared.recordPermission {
        case .undetermined: return .notDetermined
        case .denied: return .denied
        case .granted: return .authorized
        @unknown default: return .restricted
        }
    }

    func requestMicrophonePermission() async -> WatchVoicePermission {
        if microphonePermission != .notDetermined { return microphonePermission }
        let granted = await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission {
                continuation.resume(returning: $0)
            }
        }
        return granted ? .authorized : .denied
    }

    func prepare() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .voiceChat, options: [.duckOthers])
        try session.setActive(true)
        engine.prepare()
        if !engine.isRunning { try engine.start() }
    }

    func startCapture(_ handler: @escaping @Sendable (Data) -> Void) throws {
        guard microphonePermission == .authorized else { throw WatchVoiceBridgeError.audioFailure }
        if captureInstalled { return }
        let input = engine.inputNode
        let inputFormat = input.inputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0,
            let processor = WatchVoiceCaptureProcessor(inputFormat: inputFormat)
        else { throw WatchVoiceBridgeError.audioFailure }
        captureProcessor = processor
        input.installTap(onBus: 0, bufferSize: 960, format: inputFormat) { buffer, _ in
            processor.consume(buffer, handler: handler)
        }
        captureInstalled = true
        if !engine.isRunning { try engine.start() }
    }

    func stopCapture() {
        guard captureInstalled else { return }
        engine.inputNode.removeTap(onBus: 0)
        captureInstalled = false
        captureProcessor = nil
    }

    func enqueuePlayback(
        _ pcm: Data,
        startsAnnouncement: Bool,
        endsAnnouncement: Bool,
        onStarted: @escaping @MainActor () -> Void,
        onFinished: @escaping @MainActor () -> Void
    ) throws {
        guard pcm.count == WatchVoicePCMFrame.assistantPayloadLength,
            let buffer = AVAudioPCMBuffer(pcmFormat: playbackFormat, frameCapacity: 480),
            let channel = buffer.int16ChannelData?[0]
        else { throw WatchVoiceBridgeError.audioFailure }
        _ = pcm.copyBytes(to: UnsafeMutableBufferPointer(start: channel, count: 480))
        buffer.frameLength = 480
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { _ in
            guard endsAnnouncement else { return }
            Task { @MainActor in onFinished() }
        }
        if startsAnnouncement {
            if !engine.isRunning { try engine.start() }
            if !player.isPlaying { player.play() }
            playbackRunning = true
            onStarted()
        }
    }

    func stopPlayback() {
        if player.isPlaying { player.stop() }
        player.reset()
        playbackRunning = false
    }

    func stop() {
        stopCapture()
        stopPlayback()
        engine.stop()
        try? AVAudioSession.sharedInstance().setActive(
            false,
            options: .notifyOthersOnDeactivation)
    }
}

private final class WatchVoiceCaptureProcessor: @unchecked Sendable {
    private let converter: AVAudioConverter
    private let outputFormat: AVAudioFormat
    private let lock = NSLock()
    private var pending = Data()

    init?(inputFormat: AVAudioFormat) {
        guard
            let output = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: 16_000,
                channels: 1,
                interleaved: false),
            let converter = AVAudioConverter(from: inputFormat, to: output)
        else { return nil }
        outputFormat = output
        self.converter = converter
    }

    func consume(_ input: AVAudioPCMBuffer, handler: @escaping @Sendable (Data) -> Void) {
        lock.lock()
        defer { lock.unlock() }
        let ratio = outputFormat.sampleRate / max(1, input.format.sampleRate)
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * ratio) + 32)
        guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
            return
        }
        var supplied = false
        var conversionError: NSError?
        let status = converter.convert(to: output, error: &conversionError) { _, inputStatus in
            if supplied {
                inputStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            inputStatus.pointee = .haveData
            return input
        }
        guard conversionError == nil, status != .error,
            output.frameLength > 0, let samples = output.int16ChannelData?[0]
        else { return }
        pending.append(
            UnsafeBufferPointer(start: samples, count: Int(output.frameLength)))
        while pending.count >= WatchVoicePCMFrame.capturePayloadLength {
            let frame = Data(pending.prefix(WatchVoicePCMFrame.capturePayloadLength))
            pending.removeFirst(WatchVoicePCMFrame.capturePayloadLength)
            handler(frame)
        }
    }
}
