import AVFoundation
// Feature 065 — included conversational voice for iOS and macOS.
//
// The controller never dispatches through a voice-only query endpoint. Final
// ASR text is retained as one immutable ordinary chat_message and retried until
// a completely correlated acknowledgement or rejection arrives.
import AstralCore
import Foundation
import LiveKit
import Observation

#if os(macOS)
    import CoreAudio
#endif

struct AppleVoiceMediaCapability: Sendable, Equatable {
    let hasMicrophone: Bool
    let hasAudioOutput: Bool
    let microphonePermission: String
    let fullDuplex: Bool
}

struct AppleVoiceAudioEndpointSnapshot: Sendable, Equatable {
    let deviceID: UInt32?
    let sampleRateHz: Double?
    let channelCount: UInt32?
}

struct AppleVoiceAudioRouteSnapshot: Sendable, Equatable {
    let input: AppleVoiceAudioEndpointSnapshot
    let output: AppleVoiceAudioEndpointSnapshot

    init(input: AppleVoiceAudioEndpointSnapshot, output: AppleVoiceAudioEndpointSnapshot) {
        self.input = input
        self.output = output
    }

    init(
        inputDeviceID: UInt32?, inputSampleRateHz: Double?, inputChannelCount: UInt32?,
        outputDeviceID: UInt32?, outputSampleRateHz: Double?, outputChannelCount: UInt32?
    ) {
        input = AppleVoiceAudioEndpointSnapshot(
            deviceID: inputDeviceID, sampleRateHz: inputSampleRateHz,
            channelCount: inputChannelCount)
        output = AppleVoiceAudioEndpointSnapshot(
            deviceID: outputDeviceID, sampleRateHz: outputSampleRateHz,
            channelCount: outputChannelCount)
    }
}

enum AppleVoicePermission {
    static var status: String {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: "authorized"
        case .denied: "denied"
        case .restricted: "restricted"
        case .notDetermined: "not_determined"
        @unknown default: "restricted"
        }
    }

    static func requestCapability() async -> AppleVoiceMediaCapability {
        if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
            _ = await AVCaptureDevice.requestAccess(for: .audio)
        }
        #if os(iOS)
            let hasMicrophone = AVCaptureDevice.default(for: .audio) != nil
            let hasAudioOutput = !AVAudioSession.sharedInstance().currentRoute.outputs.isEmpty
        #else
            let availability = macOSHardwareAvailability(currentAudioRouteSnapshot())
            let hasMicrophone = availability.hasMicrophone
            let hasAudioOutput = availability.hasAudioOutput
        #endif
        return AppleVoiceMediaCapability(
            hasMicrophone: hasMicrophone,
            hasAudioOutput: hasAudioOutput,
            microphonePermission: status,
            fullDuplex: true)
    }

    static func currentAudioRouteSnapshot() -> AppleVoiceAudioRouteSnapshot? {
        #if os(macOS)
            return AppleVoiceAudioRouteSnapshot(
                input: audioEndpointSnapshot(
                    defaultAudioInputDeviceID(), scope: kAudioDevicePropertyScopeInput),
                output: audioEndpointSnapshot(
                    defaultAudioOutputDeviceID(), scope: kAudioDevicePropertyScopeOutput))
        #else
            return nil
        #endif
    }

    #if os(macOS)
        private static func defaultAudioInputDeviceID() -> AudioObjectID? {
            defaultAudioDeviceID(kAudioHardwarePropertyDefaultInputDevice)
        }

        /// Reads macOS's default output device without instantiating an audio graph.
        ///
        /// Inspecting an `AVAudioEngine` output format instantiates an I/O graph solely
        /// for capability detection and can fault before its node is initialized. The
        /// hardware property is the authoritative, side-effect-free capability probe
        /// and does not start or retain audio resources.
        static func defaultAudioOutputDeviceID() -> AudioObjectID? {
            defaultAudioDeviceID(kAudioHardwarePropertyDefaultOutputDevice)
        }

        private static func defaultAudioDeviceID(
            _ selector: AudioObjectPropertySelector
        ) -> AudioObjectID? {
            var address = AudioObjectPropertyAddress(
                mSelector: selector,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain)
            let systemObject = AudioObjectID(kAudioObjectSystemObject)
            guard AudioObjectHasProperty(systemObject, &address) else { return nil }

            var deviceID = AudioObjectID(kAudioObjectUnknown)
            var size = UInt32(MemoryLayout<AudioObjectID>.size)
            let status = AudioObjectGetPropertyData(
                systemObject, &address, 0, nil, &size, &deviceID)
            guard status == noErr,
                size == UInt32(MemoryLayout<AudioObjectID>.size)
            else { return nil }
            return deviceID
        }

        private static func audioEndpointSnapshot(
            _ deviceID: AudioObjectID?, scope: AudioObjectPropertyScope
        ) -> AppleVoiceAudioEndpointSnapshot {
            guard let deviceID, hasUsableAudioOutputDevice(deviceID) else {
                return AppleVoiceAudioEndpointSnapshot(
                    deviceID: nil, sampleRateHz: nil, channelCount: nil)
            }
            return AppleVoiceAudioEndpointSnapshot(
                deviceID: deviceID,
                sampleRateHz: nominalSampleRate(deviceID),
                channelCount: channelCount(deviceID, scope: scope))
        }

        private static func nominalSampleRate(_ deviceID: AudioObjectID) -> Double? {
            var address = AudioObjectPropertyAddress(
                mSelector: kAudioDevicePropertyNominalSampleRate,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain)
            guard AudioObjectHasProperty(deviceID, &address) else { return nil }
            var value = Float64.zero
            var size = UInt32(MemoryLayout<Float64>.size)
            guard
                AudioObjectGetPropertyData(
                    deviceID, &address, 0, nil, &size, &value) == noErr,
                size == UInt32(MemoryLayout<Float64>.size), value > 0
            else { return nil }
            return value
        }

        private static func channelCount(
            _ deviceID: AudioObjectID, scope: AudioObjectPropertyScope
        ) -> UInt32? {
            var address = AudioObjectPropertyAddress(
                mSelector: kAudioDevicePropertyStreamConfiguration,
                mScope: scope,
                mElement: kAudioObjectPropertyElementMain)
            guard AudioObjectHasProperty(deviceID, &address) else { return nil }
            var size = UInt32.zero
            guard
                AudioObjectGetPropertyDataSize(
                    deviceID, &address, 0, nil, &size) == noErr,
                size >= UInt32(MemoryLayout<AudioBufferList>.size)
            else { return nil }

            let storage = UnsafeMutableRawPointer.allocate(
                byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
            defer { storage.deallocate() }
            var mutableSize = size
            guard
                AudioObjectGetPropertyData(
                    deviceID, &address, 0, nil, &mutableSize, storage) == noErr
            else { return nil }
            let buffers = UnsafeMutableAudioBufferListPointer(
                storage.assumingMemoryBound(to: AudioBufferList.self))
            let total = buffers.reduce(UInt32.zero) { $0 + $1.mNumberChannels }
            return total > 0 ? total : nil
        }

        /// Separates value validation from hardware access for deterministic tests.
        static func hasUsableAudioOutputDevice(_ deviceID: AudioObjectID?) -> Bool {
            hasUsableAudioDevice(deviceID)
        }

        static func hasUsableAudioInputDevice(_ deviceID: AudioObjectID?) -> Bool {
            hasUsableAudioDevice(deviceID)
        }

        static func macOSHardwareAvailability(
            _ snapshot: AppleVoiceAudioRouteSnapshot?
        ) -> (hasMicrophone: Bool, hasAudioOutput: Bool) {
            (
                hasUsableAudioInputDevice(snapshot?.input.deviceID),
                hasUsableAudioOutputDevice(snapshot?.output.deviceID)
            )
        }

        private static func hasUsableAudioDevice(_ deviceID: AudioObjectID?) -> Bool {
            guard let deviceID else { return false }
            return deviceID != AudioObjectID(kAudioObjectUnknown)
        }
    #endif
}

struct AppleVoiceTakeoverTarget: Sendable, Equatable {
    let sessionId: String
    let deviceKind: String
    let deviceLabel: String?
    let generation: Int
    let mediaGrantRevision: Int
}

struct AppleVoiceRestSession: Sendable, Equatable {
    let sessionId: String
    let deviceId: String
    let deviceKind: String
    let transport: String
    let ownerConnectionGeneration: String
    let visibleChatId: String
    let appliedVisibleChatId: String?
    let generation: Int
    let mediaGrantRevision: Int
    let chatContextRevision: Int
    let appliedChatContextRevision: Int?
    let chatContextSynced: Bool
    let state: String
    let foregroundActive: Bool
    let foregroundReason: String
    let speechMuted: Bool
    let microphoneEnabled: Bool
    let leaseExpiresAt: String
}

struct AppleVoiceSessionFence: Sendable, Equatable {
    let sessionId: String
    let generation: Int
    let mediaGrantRevision: Int

    init(session: AppleVoiceRestSession) {
        sessionId = session.sessionId
        generation = session.generation
        mediaGrantRevision = session.mediaGrantRevision
    }

    init(sessionId: String, generation: Int, mediaGrantRevision: Int) {
        self.sessionId = sessionId
        self.generation = generation
        self.mediaGrantRevision = mediaGrantRevision
    }
}

final class AppleLiveKitGrant: @unchecked Sendable, CustomStringConvertible {
    let grantId: String
    let sessionId: String
    let generation: Int
    let mediaGrantRevision: Int
    let expiresAt: String
    let url: String
    let joinToken: String
    let roomName: String
    let participantIdentity: String
    let workerIdentity: String

    init(
        grantId: String, sessionId: String, generation: Int, mediaGrantRevision: Int,
        expiresAt: String, url: String, joinToken: String, roomName: String,
        participantIdentity: String, workerIdentity: String
    ) {
        self.grantId = grantId
        self.sessionId = sessionId
        self.generation = generation
        self.mediaGrantRevision = mediaGrantRevision
        self.expiresAt = expiresAt
        self.url = url
        self.joinToken = joinToken
        self.roomName = roomName
        self.participantIdentity = participantIdentity
        self.workerIdentity = workerIdentity
    }

    var description: String {
        "AppleLiveKitGrant(sessionId=\(sessionId), generation=\(generation), mediaGrantRevision=\(mediaGrantRevision), url=[REDACTED], joinToken=[REDACTED])"
    }
}

final class AppleVoiceUIBinding: @unchecked Sendable, CustomStringConvertible {
    let token: String
    let serverBase: URL
    let deviceId: String
    let deviceKind: String
    let connectionGeneration: String
    let control: VoiceControlBinding
    let visibleChatId: String

    init(
        token: String, serverBase: URL, deviceId: String, deviceKind: String,
        connectionGeneration: String, control: VoiceControlBinding, visibleChatId: String
    ) {
        self.token = token
        self.serverBase = serverBase
        self.deviceId = deviceId
        self.deviceKind = deviceKind
        self.connectionGeneration = connectionGeneration
        self.control = control
        self.visibleChatId = visibleChatId
    }

    var description: String {
        "AppleVoiceUIBinding(deviceId=\(deviceId), connectionGeneration=\(connectionGeneration), token=[REDACTED], control=[REDACTED])"
    }
}

enum AppleVoiceStartOutcome {
    case started(AppleVoiceRestSession, AppleLiveKitGrant)
    case takeoverRequired(AppleVoiceTakeoverTarget, String?)
    case failed(String, String?)
}

enum AppleVoiceRefreshOutcome {
    case refreshed(AppleVoiceRestSession, AppleLiveKitGrant)
    case current(AppleVoiceRestSession, retryable: Bool)
    case failed(String, String?)
}

@MainActor
protocol AppleVoiceControlAPI: AnyObject {
    func start(
        binding: AppleVoiceUIBinding, activationId: String,
        capability: AppleVoiceMediaCapability
    ) async -> AppleVoiceStartOutcome
    func takeover(
        binding: AppleVoiceUIBinding, activationId: String,
        target: AppleVoiceTakeoverTarget, capability: AppleVoiceMediaCapability
    ) async -> AppleVoiceStartOutcome
    func update(
        binding: AppleVoiceUIBinding, session: AppleVoiceRestSession,
        fields: [String: JSONValue]
    ) async -> AppleVoiceRestSession?
    func refresh(
        binding: AppleVoiceUIBinding, session: AppleVoiceRestSession,
        refreshId: String
    ) async -> AppleVoiceRefreshOutcome
    func stopSpeech(binding: AppleVoiceUIBinding, session: AppleVoiceRestSession) async -> Bool
    func consent(
        binding: AppleVoiceUIBinding, session: AppleVoiceRestSession,
        resultId: String, turnId: String
    ) async -> Bool
    func end(binding: AppleVoiceUIBinding, fence: AppleVoiceSessionFence) async -> Bool
}

enum AppleVoiceMediaEvent: @unchecked Sendable {
    case connected
    case reconnecting
    case data(topic: String?, participantIdentity: String?, payload: Data)
    case playout(VoiceAnnouncementMedia, phase: String)
    case announcementDropped(VoiceAnnouncementMedia)
    case disconnected(unexpected: Bool)
    case failed
}

@MainActor
protocol AppleVoiceMediaClient: AnyObject {
    var eventHandler: ((AppleVoiceMediaEvent) -> Void)? { get set }
    func connect(_ grant: AppleLiveKitGrant) async throws
    func setMicrophoneEnabled(_ enabled: Bool) async throws
    @discardableResult
    func authorize(_ announcement: VoiceAnnouncementMedia) -> Bool
    func interruptPlayout()
    func disconnect()
}

struct AppleVoicePublishedTrack: Sendable, Equatable {
    let sid: String
    let name: String
    let workerIdentity: String
    let isAudio: Bool
}

enum AppleVoicePlayoutMatchDecision: Sendable, Equatable {
    case none
    case start(VoiceAnnouncementMedia, AppleVoicePublishedTrack)
    case drop(VoiceAnnouncementMedia, AppleVoicePublishedTrack)
}

/// Pure manifest/track matching state used by the LiveKit adapter. Keeping
/// ordering and bounds independent of SDK callbacks makes races deterministic
/// and lets the same policy run in focused tests without synthetic audio.
struct AppleVoicePlayoutMatcher: Sendable {
    struct Active: Sendable, Equatable {
        let manifest: VoiceAnnouncementMedia
        let track: AppleVoicePublishedTrack
    }

    struct Cleared: Sendable, Equatable {
        let active: Active?
        let pending: [VoiceAnnouncementMedia]
        let tracks: [AppleVoicePublishedTrack]
    }

    static let maximumPendingAnnouncements = 8

    let sessionId: String
    let generation: Int
    let mediaGrantRevision: Int
    let workerIdentity: String

    private(set) var active: Active?
    private(set) var pendingCount = 0
    private var lastAnnouncementSequence = 0
    private var manifests: [String: VoiceAnnouncementMedia] = [:]
    private var tracks: [String: AppleVoicePublishedTrack] = [:]

    init(
        sessionId: String, generation: Int, mediaGrantRevision: Int,
        workerIdentity: String
    ) {
        self.sessionId = sessionId
        self.generation = generation
        self.mediaGrantRevision = mediaGrantRevision
        self.workerIdentity = workerIdentity
    }

    mutating func enqueue(_ value: VoiceAnnouncementMedia) -> Bool {
        guard value.transport == .liveKit,
            value.sessionId == sessionId,
            value.generation == generation,
            value.mediaGrantRevision == mediaGrantRevision,
            value.workerIdentity == workerIdentity,
            value.sampleRateHz == 24_000,
            (1...VoiceContractLimits.quantumSamples).contains(value.durationSamples),
            let sid = value.trackSid,
            value.trackName != nil,
            value.announcementSequence > lastAnnouncementSequence,
            manifests[sid] == nil,
            active?.manifest.trackSid != sid,
            pendingCount < Self.maximumPendingAnnouncements
        else { return false }
        manifests[sid] = value
        pendingCount += 1
        lastAnnouncementSequence = value.announcementSequence
        return true
    }

    mutating func remember(_ value: AppleVoicePublishedTrack) -> Bool {
        guard value.workerIdentity == workerIdentity, value.isAudio,
            !value.sid.isEmpty, !value.name.isEmpty,
            active?.track.sid != value.sid,
            tracks[value.sid] == nil,
            tracks.count < Self.maximumPendingAnnouncements
        else { return false }
        tracks[value.sid] = value
        return true
    }

    func hasExactMatch(for sid: String) -> Bool {
        guard let manifest = manifests[sid], let track = tracks[sid] else { return false }
        return manifest.workerIdentity == track.workerIdentity
            && manifest.trackSid == track.sid
            && manifest.trackName == track.name
            && track.isAudio
    }

    mutating func next() -> AppleVoicePlayoutMatchDecision {
        guard active == nil else { return .none }
        guard
            let manifest = manifests.values.min(by: {
                $0.announcementSequence < $1.announcementSequence
            }),
            let sid = manifest.trackSid,
            let track = tracks[sid]
        else { return .none }
        manifests.removeValue(forKey: sid)
        tracks.removeValue(forKey: sid)
        pendingCount -= 1
        guard manifest.workerIdentity == track.workerIdentity,
            manifest.trackName == track.name, track.isAudio
        else { return .drop(manifest, track) }
        let next = Active(manifest: manifest, track: track)
        active = next
        return .start(manifest, track)
    }

    mutating func finish(announcementId: String) -> Active? {
        guard active?.manifest.announcementId == announcementId else { return nil }
        defer { active = nil }
        return active
    }

    /// Expire only a still-unmatched half-pair. An exact pair waiting behind
    /// current speech is already matched and may remain in the bounded queue.
    mutating func expireUnmatched(sid: String) -> (
        manifest: VoiceAnnouncementMedia?, track: AppleVoicePublishedTrack?
    )? {
        guard active?.track.sid != sid, !hasExactMatch(for: sid) else { return nil }
        return removeWaiting(sid: sid)
    }

    mutating func removeWaiting(sid: String) -> (
        manifest: VoiceAnnouncementMedia?, track: AppleVoicePublishedTrack?
    )? {
        guard active?.track.sid != sid else { return nil }
        let manifest = manifests.removeValue(forKey: sid)
        let track = tracks.removeValue(forKey: sid)
        guard manifest != nil || track != nil else { return nil }
        if manifest != nil { pendingCount -= 1 }
        return (manifest, track)
    }

    mutating func clear() -> Cleared {
        let pending = manifests.values.sorted {
            $0.announcementSequence < $1.announcementSequence
        }
        let rememberedTracks = Array(tracks.values)
        let result = Cleared(active: active, pending: pending, tracks: rememberedTracks)
        active = nil
        manifests.removeAll()
        tracks.removeAll()
        pendingCount = 0
        // Preserve the sequence fence. Stop/mute/reconnect must never replay a
        // cleared announcement on the same grant merely because the queue reset.
        return result
    }
}

/// Counts input PCM in fixed 24-kHz-equivalent mono samples. LiveKit normally
/// supplies 48-kHz PCM; 24-kHz is accepted too. Other rates, channel counts,
/// and fractional-equivalent frames fail closed instead of being guessed.
struct AppleVoiceSampleBudget: Sendable, Equatable {
    let targetSamples: Int
    private(set) var consumedSamples = 0

    var complete: Bool { consumedSamples == targetSamples }

    init(targetSamples: Int) {
        self.targetSamples = targetSamples
    }

    mutating func accept(
        sampleRateHz: Int, channelCount: Int, inputFrames: Int,
        outputFrames: Int
    ) -> Int? {
        guard [24_000, 48_000].contains(sampleRateHz), (1...2).contains(channelCount),
            inputFrames > 0, outputFrames > 0
        else { return nil }
        let scale = sampleRateHz / 24_000
        guard inputFrames.isMultiple(of: scale) else { return nil }
        let remaining = targetSamples - consumedSamples
        guard remaining > 0 else { return 0 }
        let acceptedSamples = min(inputFrames / scale, outputFrames, remaining)
        consumedSamples += acceptedSamples
        return acceptedSamples
    }
}

/// Official LiveKit direct-RTC adapter. Auto-subscribe is disabled so no
/// assistant audio can play before a valid, expected-worker manifest.
@MainActor
final class AppleLiveKitVoiceMediaClient: NSObject, AppleVoiceMediaClient {
    var eventHandler: ((AppleVoiceMediaEvent) -> Void)?

    private var room: Room?
    private var grant: AppleLiveKitGrant?
    private var matcher: AppleVoicePlayoutMatcher?
    private var publications: [String: RemoteTrackPublication] = [:]
    private var matchTimeouts: [String: Task<Void, Never>] = [:]
    private var activeTimeout: Task<Void, Never>?
    private var activePublication: RemoteTrackPublication?
    private var activeRenderer: BoundedVoiceAudioRenderer?
    private var activeStartedReported = false
    private var playoutEpoch = 0

    private static let vendorLoggingDisabled: Void = {
        // Default SDK diagnostics can include credentialed signaling/SDP.
        // Product-owned failures remain bounded and content-free below.
        LiveKitSDK.disableLogging()
    }()

    func connect(_ grant: AppleLiveKitGrant) async throws {
        _ = Self.vendorLoggingDisabled
        disconnect()
        self.grant = grant
        matcher = AppleVoicePlayoutMatcher(
            sessionId: grant.sessionId, generation: grant.generation,
            mediaGrantRevision: grant.mediaGrantRevision,
            workerIdentity: grant.workerIdentity)
        let options = ConnectOptions(autoSubscribe: false, enableMicrophone: false)
        let next = Room(delegate: self, connectOptions: options, roomOptions: RoomOptions())
        room = next
        do {
            try await next.connect(
                url: grant.url, token: grant.joinToken,
                connectOptions: options, roomOptions: nil)
            eventHandler?(.connected)
        } catch {
            disconnect()
            eventHandler?(.failed)
            throw error
        }
    }

    func setMicrophoneEnabled(_ enabled: Bool) async throws {
        guard let room else { return }
        try await room.localParticipant.setMicrophone(enabled: enabled)
    }

    @discardableResult
    func authorize(_ announcement: VoiceAnnouncementMedia) -> Bool {
        guard var next = matcher, next.enqueue(announcement),
            let sid = announcement.trackSid
        else { return false }
        matcher = next
        armMatchTimeout(for: sid)
        if publications[sid] == nil, let existing = publication(sid: sid) {
            remember(existing, participantIdentity: grant?.workerIdentity)
        } else {
            reconcilePlayout()
        }
        return true
    }

    func interruptPlayout() {
        playoutEpoch += 1
        let cleared = matcher?.clear()
        for task in matchTimeouts.values { task.cancel() }
        matchTimeouts.removeAll()
        activeTimeout?.cancel()
        activeTimeout = nil
        let renderer = activeRenderer
        let startedWasReported = activeStartedReported
        activeRenderer = nil
        activeStartedReported = false
        let active = cleared?.active?.manifest
        activePublication = nil
        renderer?.interrupt()
        for publication in publications.values {
            Task { try? await publication.set(subscribed: false) }
        }
        publications.removeAll()
        if let active {
            eventHandler?(
                startedWasReported
                    ? .playout(active, phase: "interrupted")
                    : .announcementDropped(active))
        }
        for pending in cleared?.pending ?? [] {
            eventHandler?(.announcementDropped(pending))
        }
    }

    func disconnect() {
        interruptPlayout()
        let previousRoom = room
        room = nil
        grant = nil
        matcher = nil
        publications.removeAll()
        if let previousRoom {
            Task {
                _ = try? await previousRoom.localParticipant.setMicrophone(enabled: false)
                await previousRoom.disconnect()
            }
        }
    }

    private func publication(sid: String) -> RemoteTrackPublication? {
        guard let room, let worker = grant?.workerIdentity else { return nil }
        return room.remoteParticipants.values
            .first(where: { $0.identity?.stringValue == worker })?
            .trackPublications.values
            .compactMap { $0 as? RemoteTrackPublication }
            .first(where: { $0.sid.stringValue == sid })
    }

    private func remember(
        _ publication: RemoteTrackPublication, participantIdentity: String?
    ) {
        let sid = publication.sid.stringValue
        guard let participantIdentity, participantIdentity == grant?.workerIdentity,
            publication.kind == .audio,
            var next = matcher,
            next.remember(
                AppleVoicePublishedTrack(
                    sid: sid, name: publication.name,
                    workerIdentity: participantIdentity, isAudio: true))
        else {
            Task { try? await publication.set(subscribed: false) }
            return
        }
        matcher = next
        publications[sid] = publication
        armMatchTimeout(for: sid)
        if next.hasExactMatch(for: sid) { cancelMatchTimeout(for: sid) }
        reconcilePlayout()
    }

    private func reconcilePlayout() {
        while var next = matcher {
            let decision = next.next()
            matcher = next
            switch decision {
            case .none:
                return
            case .drop(let announcement, let track):
                cancelMatchTimeout(for: track.sid)
                if let publication = publications.removeValue(forKey: track.sid) {
                    Task { try? await publication.set(subscribed: false) }
                }
                eventHandler?(.announcementDropped(announcement))
            case .start(let announcement, let track):
                cancelMatchTimeout(for: track.sid)
                guard let publication = publications[track.sid] else {
                    dropActive(announcementId: announcement.announcementId)
                    return
                }
                activePublication = publication
                activeStartedReported = false
                subscribe(
                    publication, announcement: announcement, epoch: playoutEpoch)
                return
            }
        }
    }

    private func subscribe(
        _ publication: RemoteTrackPublication, announcement: VoiceAnnouncementMedia,
        epoch: Int
    ) {
        armActiveTimeout(
            announcementId: announcement.announcementId, epoch: epoch,
            nanoseconds: 1_000_000_000)
        Task { [weak self, weak publication] in
            guard let self, let publication else { return }
            do {
                try await publication.set(subscribed: true)
                guard
                    self.isActive(
                        announcementId: announcement.announcementId, epoch: epoch)
                else {
                    try? await publication.set(subscribed: false)
                    return
                }
                self.attachRendererIfReady(
                    publication, announcement: announcement, epoch: epoch)
            } catch {
                self.dropActive(
                    announcementId: announcement.announcementId, epoch: epoch)
            }
        }
    }

    private func attachRendererIfReady(
        _ publication: RemoteTrackPublication, announcement: VoiceAnnouncementMedia,
        epoch: Int
    ) {
        guard isActive(announcementId: announcement.announcementId, epoch: epoch),
            activeRenderer == nil,
            publication.sid.stringValue == announcement.trackSid,
            publication.name == announcement.trackName,
            publication.kind == .audio,
            let track = publication.track as? RemoteAudioTrack,
            track.kind == .audio,
            track.sid?.stringValue == announcement.trackSid,
            track.name == announcement.trackName
        else { return }
        // The SDK's default playout cannot trim a malicious/buggy track at an
        // exact manifest sample boundary. Keep it silent and replay only the
        // renderer's bounded copy through a private, memory-only audio engine.
        track.volume = 0
        do {
            let renderer = try BoundedVoiceAudioRenderer(
                announcement: announcement, track: track,
                started: { [weak self] in
                    Task { @MainActor [weak self] in
                        guard let self,
                            self.isActive(
                                announcementId: announcement.announcementId,
                                epoch: epoch)
                        else { return }
                        self.activeStartedReported = true
                        self.eventHandler?(.playout(announcement, phase: "started"))
                    }
                },
                completion: { [weak self] phase in
                    Task { @MainActor [weak self] in
                        self?.finishActive(
                            announcementId: announcement.announcementId,
                            phase: phase, epoch: epoch)
                    }
                })
            activeRenderer = renderer
            activeTimeout?.cancel()
            let duration = Double(announcement.durationSamples) / 24_000.0
            armActiveTimeout(
                announcementId: announcement.announcementId, epoch: epoch,
                nanoseconds: UInt64((duration + 1.0) * 1_000_000_000))
            track.add(audioRenderer: renderer)
        } catch {
            dropActive(announcementId: announcement.announcementId, epoch: epoch)
        }
    }

    private func isActive(announcementId: String, epoch: Int) -> Bool {
        epoch == playoutEpoch
            && matcher?.active?.manifest.announcementId == announcementId
    }

    private func finishActive(
        announcementId: String, phase: String, epoch: Int
    ) {
        guard isActive(announcementId: announcementId, epoch: epoch),
            var next = matcher,
            let finished = next.finish(announcementId: announcementId)
        else { return }
        matcher = next
        activeTimeout?.cancel()
        activeTimeout = nil
        let startedWasReported = activeStartedReported
        activeRenderer = nil
        activeStartedReported = false
        activePublication = nil
        let sid = finished.track.sid
        if let publication = publications.removeValue(forKey: sid) {
            Task { try? await publication.set(subscribed: false) }
        }
        if phase == "dropped" || !startedWasReported {
            eventHandler?(.announcementDropped(finished.manifest))
        } else {
            eventHandler?(
                .playout(
                    finished.manifest,
                    phase: phase == "finished" ? "finished" : "interrupted"))
        }
        reconcilePlayout()
    }

    private func dropActive(announcementId: String, epoch: Int? = nil) {
        if let epoch, !isActive(announcementId: announcementId, epoch: epoch) { return }
        guard var next = matcher,
            let dropped = next.finish(announcementId: announcementId)
        else { return }
        matcher = next
        activeTimeout?.cancel()
        activeTimeout = nil
        activeRenderer = nil
        activeStartedReported = false
        activePublication = nil
        if let publication = publications.removeValue(forKey: dropped.track.sid) {
            Task { try? await publication.set(subscribed: false) }
        }
        eventHandler?(.announcementDropped(dropped.manifest))
        reconcilePlayout()
    }

    private func armMatchTimeout(for sid: String) {
        guard matchTimeouts[sid] == nil else { return }
        let epoch = playoutEpoch
        matchTimeouts[sid] = Task { [weak self] in
            do {
                try await Task.sleep(nanoseconds: 1_000_000_000)
            } catch {
                return
            }
            guard let self, self.playoutEpoch == epoch else { return }
            self.expireUnmatched(sid: sid)
        }
    }

    private func cancelMatchTimeout(for sid: String) {
        matchTimeouts.removeValue(forKey: sid)?.cancel()
    }

    private func expireUnmatched(sid: String, force: Bool = false) {
        cancelMatchTimeout(for: sid)
        guard var next = matcher else { return }
        let expired =
            force
            ? next.removeWaiting(sid: sid)
            : next.expireUnmatched(sid: sid)
        matcher = next
        guard let expired else { return }
        if let publication = publications.removeValue(forKey: sid) {
            Task { try? await publication.set(subscribed: false) }
        }
        if let manifest = expired.manifest {
            eventHandler?(.announcementDropped(manifest))
        }
        reconcilePlayout()
    }

    private func armActiveTimeout(
        announcementId: String, epoch: Int, nanoseconds: UInt64
    ) {
        activeTimeout?.cancel()
        activeTimeout = Task { [weak self] in
            do {
                try await Task.sleep(nanoseconds: nanoseconds)
            } catch {
                return
            }
            guard let self,
                self.isActive(announcementId: announcementId, epoch: epoch)
            else { return }
            if let renderer = self.activeRenderer {
                renderer.interrupt()
            } else {
                self.dropActive(announcementId: announcementId, epoch: epoch)
            }
        }
    }

    private func handleTrackRemoval(sid: String) {
        cancelMatchTimeout(for: sid)
        publications.removeValue(forKey: sid)
        guard matcher?.active?.track.sid == sid else {
            expireUnmatched(sid: sid, force: true)
            return
        }
        // Once the exact declared sample budget has been copied into the
        // private player, LiveKit may retire its one-quantum source track while
        // the final scheduled frames are still reaching the output device.
        if activeRenderer?.sampleBudgetComplete == true { return }
        if let activeRenderer {
            activeRenderer.interrupt()
        } else if let announcementId = matcher?.active?.manifest.announcementId {
            dropActive(announcementId: announcementId)
        }
    }
}

extension AppleLiveKitVoiceMediaClient: RoomDelegate {
    nonisolated func roomDidConnect(_ room: Room) {
        Task { @MainActor [weak self] in
            guard let self, self.room === room else { return }
            self.eventHandler?(.connected)
        }
    }

    nonisolated func roomIsReconnecting(_ room: Room) {
        Task { @MainActor [weak self] in
            guard let self, self.room === room else { return }
            self.interruptPlayout()
            self.eventHandler?(.reconnecting)
        }
    }

    nonisolated func room(_ room: Room, didStartReconnectWithMode reconnectMode: ReconnectMode) {
        Task { @MainActor [weak self] in
            guard let self, self.room === room else { return }
            self.interruptPlayout()
            self.eventHandler?(.reconnecting)
        }
    }

    nonisolated func roomDidReconnect(_ room: Room) {
        Task { @MainActor [weak self] in
            guard let self, self.room === room else { return }
            self.eventHandler?(.connected)
        }
    }

    nonisolated func room(_ room: Room, didCompleteReconnectWithMode reconnectMode: ReconnectMode) {
        Task { @MainActor [weak self] in
            guard let self, self.room === room else { return }
            self.eventHandler?(.connected)
        }
    }

    nonisolated func room(_ room: Room, didFailToConnectWithError error: LiveKitError?) {
        Task { @MainActor [weak self] in
            guard let self, self.room === room else { return }
            self.interruptPlayout()
            self.eventHandler?(.failed)
        }
    }

    nonisolated func room(_ room: Room, didDisconnectWithError error: LiveKitError?) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            // Intentional/stale rooms are cleared before awaiting disconnect;
            // their delayed delegate callback must not overwrite a newly
            // connected room with a spurious reconnecting state.
            guard self.room === room else { return }
            self.interruptPlayout()
            self.room = nil
            self.eventHandler?(.disconnected(unexpected: true))
        }
    }

    nonisolated func room(
        _ room: Room, participant: RemoteParticipant?, didReceiveData data: Data,
        forTopic topic: String, encryptionType: EncryptionType
    ) {
        let identity = participant?.identity?.stringValue
        Task { @MainActor [weak self] in
            guard let self, self.room === room else { return }
            self.eventHandler?(.data(topic: topic, participantIdentity: identity, payload: data))
        }
    }

    nonisolated func room(
        _ room: Room, participant: RemoteParticipant,
        didPublishTrack publication: RemoteTrackPublication
    ) {
        let identity = participant.identity?.stringValue
        Task { @MainActor [weak self] in
            guard let self, self.room === room else { return }
            guard identity == self.grant?.workerIdentity else {
                try? await publication.set(subscribed: false)
                return
            }
            self.remember(publication, participantIdentity: identity)
        }
    }

    nonisolated func room(
        _ room: Room, participant: RemoteParticipant,
        didSubscribeTrack publication: RemoteTrackPublication
    ) {
        let identity = participant.identity?.stringValue
        Task { @MainActor [weak self] in
            guard let self, self.room === room else { return }
            let sid = publication.sid.stringValue
            guard identity == self.grant?.workerIdentity,
                let announcement = self.matcher?.active?.manifest,
                announcement.trackSid == sid
            else {
                try? await publication.set(subscribed: false)
                return
            }
            self.attachRendererIfReady(
                publication, announcement: announcement, epoch: self.playoutEpoch)
        }
    }

    nonisolated func room(
        _ room: Room, participant: RemoteParticipant,
        didUnpublishTrack publication: RemoteTrackPublication
    ) {
        let identity = participant.identity?.stringValue
        let sid = publication.sid.stringValue
        Task { @MainActor [weak self] in
            guard let self, self.room === room,
                identity == self.grant?.workerIdentity
            else { return }
            self.handleTrackRemoval(sid: sid)
        }
    }

    nonisolated func room(
        _ room: Room, participant: RemoteParticipant,
        didUnsubscribeTrack publication: RemoteTrackPublication
    ) {
        let identity = participant.identity?.stringValue
        let sid = publication.sid.stringValue
        Task { @MainActor [weak self] in
            guard let self, self.room === room,
                identity == self.grant?.workerIdentity
            else { return }
            self.handleTrackRemoval(sid: sid)
        }
    }

    nonisolated func room(
        _ room: Room, participant: RemoteParticipant,
        didFailToSubscribeTrackWithSid trackSid: Track.Sid, error: LiveKitError
    ) {
        let identity = participant.identity?.stringValue
        let sid = trackSid.stringValue
        Task { @MainActor [weak self] in
            guard let self, self.room === room,
                identity == self.grant?.workerIdentity
            else { return }
            self.handleTrackRemoval(sid: sid)
        }
    }
}

/// Observes PCM only to impose a hard sample ceiling on the already-authorized
/// LiveKit track. It records nothing and owns no file handle.
private final class BoundedVoiceAudioRenderer: AudioRenderer, @unchecked Sendable {
    private let lock = NSLock()
    private weak var track: RemoteAudioTrack?
    private let started: @Sendable () -> Void
    private let completion: @Sendable (String) -> Void
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let outputFormat: AVAudioFormat
    private let targetOutputFrames: Int
    private var converter: AVAudioConverter?
    private var converterInputFormat: AVAudioFormat?
    private var sampleBudget: AppleVoiceSampleBudget
    private var scheduledOutputFrames = 0
    private var startReported = false
    private var accepting = true
    private var terminal = false

    var sampleBudgetComplete: Bool {
        lock.lock()
        defer { lock.unlock() }
        return sampleBudget.complete && scheduledOutputFrames == targetOutputFrames
    }

    init(
        announcement: VoiceAnnouncementMedia, track: RemoteAudioTrack,
        started: @escaping @Sendable () -> Void,
        completion: @escaping @Sendable (String) -> Void
    ) throws {
        guard
            let format = AVAudioFormat(
                commonFormat: .pcmFormatFloat32, sampleRate: 24_000,
                channels: 1, interleaved: false),
            announcement.durationSamples > 0
        else {
            throw NSError(domain: "AstralVoiceAudio", code: 1)
        }
        outputFormat = format
        targetOutputFrames = announcement.durationSamples
        sampleBudget = AppleVoiceSampleBudget(targetSamples: announcement.durationSamples)
        self.track = track
        self.started = started
        self.completion = completion
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: format)
        engine.prepare()
        try engine.start()
    }

    func render(pcmBuffer: AVAudioPCMBuffer) {
        lock.lock()
        guard accepting, !terminal else {
            lock.unlock()
            return
        }
        let rawSampleRate = pcmBuffer.format.sampleRate
        guard rawSampleRate.rounded() == rawSampleRate,
            let converted = convertLocked(pcmBuffer),
            let allowed = sampleBudget.accept(
                sampleRateHz: Int(rawSampleRate),
                channelCount: Int(pcmBuffer.format.channelCount),
                inputFrames: Int(pcmBuffer.frameLength),
                outputFrames: Int(converted.frameLength))
        else {
            lock.unlock()
            finish("interrupted")
            return
        }
        guard allowed > 0 else {
            accepting = false
            lock.unlock()
            return
        }
        converted.frameLength = AVAudioFrameCount(allowed)
        scheduledOutputFrames += allowed
        let shouldReportStart = !startReported
        startReported = true
        let finished = sampleBudget.complete && scheduledOutputFrames == targetOutputFrames
        if finished { accepting = false }
        if finished {
            player.scheduleBuffer(
                converted, completionCallbackType: .dataPlayedBack
            ) { [weak self] _ in
                self?.finish("finished")
            }
        } else {
            player.scheduleBuffer(converted)
        }
        if !player.isPlaying { player.play() }
        if shouldReportStart { started() }
        lock.unlock()
    }

    func interrupt() {
        finish("interrupted")
    }

    private func finish(_ requestedPhase: String) {
        lock.lock()
        guard !terminal else {
            lock.unlock()
            return
        }
        terminal = true
        accepting = false
        let completed = sampleBudget.complete && scheduledOutputFrames == targetOutputFrames
        let phase =
            requestedPhase == "finished" && completed
            ? "finished" : (startReported ? "interrupted" : "dropped")
        lock.unlock()

        // AVAudioPlayerNode invokes .dataPlayedBack on its private completion
        // queue. Stopping the player from that same queue waits on itself and
        // prevents the terminal playout proof from ever reaching the server.
        // Publish the exactly-once terminal first, then leave that queue before
        // touching the player, engine, or LiveKit renderer registration.
        completion(phase)
        DispatchQueue.main.async { [self] in
            player.stop()
            engine.stop()
            track?.remove(audioRenderer: self)
        }
    }

    /// Converts into fixed 24-kHz mono PCM while the renderer lock serializes
    /// AVAudioConverter state. The returned buffer is newly owned; LiveKit may
    /// immediately reuse its input after this callback returns.
    private func convertLocked(_ input: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        if let converterInputFormat, converterInputFormat != input.format {
            return nil
        }
        if converter == nil {
            converter = AVAudioConverter(from: input.format, to: outputFormat)
            converterInputFormat = input.format
        }
        guard let converter else { return nil }
        let ratio = outputFormat.sampleRate / input.format.sampleRate
        guard ratio.isFinite, ratio > 0 else { return nil }
        let capacity = AVAudioFrameCount(
            max(1, (Double(input.frameLength) * ratio).rounded(.up) + 32))
        guard
            let output = AVAudioPCMBuffer(
                pcmFormat: outputFormat, frameCapacity: capacity)
        else { return nil }
        var supplied = false
        var conversionError: NSError?
        let status = converter.convert(to: output, error: &conversionError) {
            _, inputStatus in
            guard !supplied else {
                inputStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            inputStatus.pointee = .haveData
            return input
        }
        guard status != .error, conversionError == nil, output.frameLength > 0 else {
            return nil
        }
        return output
    }
}

@MainActor
final class URLSessionAppleVoiceControlAPI: AppleVoiceControlAPI {
    private let session: URLSession

    init(session: URLSession? = nil) {
        self.session = session ?? NoStoreHTTP.session
    }

    func start(
        binding: AppleVoiceUIBinding, activationId: String,
        capability: AppleVoiceMediaCapability
    ) async -> AppleVoiceStartOutcome {
        await startRequest(
            binding: binding, path: "api/voice/sessions",
            body: activationBody(binding, activationId: activationId, capability: capability))
    }

    func takeover(
        binding: AppleVoiceUIBinding, activationId: String,
        target: AppleVoiceTakeoverTarget, capability: AppleVoiceMediaCapability
    ) async -> AppleVoiceStartOutcome {
        var body = activationBody(binding, activationId: activationId, capability: capability)
        body["expected_generation"] = .number(Double(target.generation))
        body["expected_media_grant_revision"] = .number(Double(target.mediaGrantRevision))
        return await startRequest(
            binding: binding,
            path: "api/voice/sessions/\(target.sessionId)/takeover", body: body)
    }

    func update(
        binding: AppleVoiceUIBinding, session current: AppleVoiceRestSession,
        fields: [String: JSONValue]
    ) async -> AppleVoiceRestSession? {
        var body = generationBody(current)
        for (key, value) in fields { body[key] = value }
        let result = await request(
            binding: binding, path: "api/voice/sessions/\(current.sessionId)",
            method: "PATCH", body: body)
        guard (200...299).contains(result.status),
            let updated = result.body.flatMap(parseSession),
            updated.sessionId == current.sessionId,
            updated.deviceId == binding.deviceId,
            updated.ownerConnectionGeneration == binding.connectionGeneration,
            updated.generation == current.generation,
            updated.mediaGrantRevision == current.mediaGrantRevision,
            appFutureTimestamp(updated.leaseExpiresAt)
        else { return nil }
        if let expectedChat = fields["visible_chat_id"]?.stringValue,
            updated.visibleChatId != expectedChat
        {
            return nil
        }
        return updated
    }

    func refresh(
        binding: AppleVoiceUIBinding, session current: AppleVoiceRestSession,
        refreshId: String
    ) async -> AppleVoiceRefreshOutcome {
        guard appUUID(.string(refreshId)) == refreshId else {
            return .failed("invalid_request", nil)
        }
        let path = "api/voice/sessions/\(current.sessionId)/media-grants"
        let observed = await request(
            binding: binding, path: path, method: "GET", body: nil)
        guard observed.status == 200,
            let observedBody = observed.body?.objectValue,
            appExact(observedBody, required: ["session", "grant_state"]),
            let baseline = observedBody["session"].flatMap(parseSession),
            validCredentialFreeGrantState(
                observedBody["grant_state"], session: baseline),
            baseline.sessionId == current.sessionId,
            baseline.deviceId == binding.deviceId,
            baseline.ownerConnectionGeneration == binding.connectionGeneration,
            baseline.generation == current.generation,
            baseline.mediaGrantRevision >= current.mediaGrantRevision,
            appFutureTimestamp(baseline.leaseExpiresAt)
        else {
            return .failed(
                observed.body?["code"]?.stringValue ?? "network_interrupted",
                observed.body?["message"]?.stringValue)
        }

        var body = generationBody(baseline)
        body["refresh_id"] = .string(refreshId)
        body["device_id"] = .string(binding.deviceId)
        guard let encodedBody = try? JSONValue.object(body).encoded() else {
            return .failed("invalid_request", nil)
        }
        var result = await request(
            binding: binding, path: path,
            method: "POST", body: nil, encodedBody: encodedBody)
        // A transport failure may have happened after the server committed the
        // CAS. Retry the byte-identical UUID4 request once so the replay window
        // can return the same grant instead of minting an untracked publisher.
        if result.status == 0 {
            result = await request(
                binding: binding, path: path, method: "POST", body: nil,
                encodedBody: encodedBody)
        }
        if result.status == 200 || result.status == 201 {
            let required: Set<String> = [
                "refresh_id", "replayed", "replay_expires_at", "session", "grant",
            ]
            guard let object = result.body?.objectValue,
                appExact(object, required: required),
                appUUID(object["refresh_id"]) == refreshId,
                let replayed = object["replayed"]?.boolValue,
                replayed == (result.status == 200),
                let replayExpiry = appTimestamp(object["replay_expires_at"]),
                appFutureTimestamp(replayExpiry),
                let session = object["session"].flatMap(parseSession),
                let grant = object["grant"].flatMap(parseGrant),
                session.sessionId == current.sessionId,
                session.sessionId == grant.sessionId,
                session.deviceId == binding.deviceId,
                session.ownerConnectionGeneration == binding.connectionGeneration,
                session.generation == baseline.generation,
                session.generation == grant.generation,
                session.mediaGrantRevision == grant.mediaGrantRevision,
                session.mediaGrantRevision > baseline.mediaGrantRevision,
                appFutureTimestamp(session.leaseExpiresAt),
                appFutureTimestamp(grant.expiresAt)
            else { return .failed("malformed_media_grant", nil) }
            return .refreshed(session, grant)
        }
        if result.status == 409,
            let problem = result.body?.objectValue,
            appExact(
                problem,
                required: ["code", "message", "retryable", "current"]),
            let code = problem["code"]?.stringValue,
            [
                "stale_generation", "stale_media_grant_revision",
                "refresh_id_payload_mismatch", "refresh_replay_expired",
            ].contains(code),
            let retryable = problem["retryable"]?.boolValue,
            let currentState = problem["current"]?.objectValue,
            appExact(currentState, required: ["session", "grant_state"]),
            let authoritative = currentState["session"].flatMap(parseSession),
            validCredentialFreeGrantState(
                currentState["grant_state"], session: authoritative),
            authoritative.sessionId == current.sessionId,
            authoritative.deviceId == binding.deviceId,
            authoritative.ownerConnectionGeneration == binding.connectionGeneration,
            appFutureTimestamp(authoritative.leaseExpiresAt)
        {
            return .current(authoritative, retryable: retryable)
        }
        return .failed(
            result.body?["code"]?.stringValue ?? "network_interrupted",
            result.body?["message"]?.stringValue)
    }

    func stopSpeech(binding: AppleVoiceUIBinding, session current: AppleVoiceRestSession) async -> Bool {
        let result = await request(
            binding: binding, path: "api/voice/sessions/\(current.sessionId)/speech/stop",
            method: "POST", body: generationBody(current))
        return result.status == 202
    }

    func consent(
        binding: AppleVoiceUIBinding, session current: AppleVoiceRestSession,
        resultId: String, turnId: String
    ) async -> Bool {
        var body = generationBody(current)
        body["turn_id"] = .string(turnId)
        body["consent_method"] = .string("tap")
        let result = await request(
            binding: binding,
            path: "api/voice/sessions/\(current.sessionId)/results/\(resultId)/read-consent",
            method: "POST", body: body)
        return result.status == 202
    }

    func end(binding: AppleVoiceUIBinding, fence: AppleVoiceSessionFence) async -> Bool {
        let path =
            "api/voice/sessions/\(fence.sessionId)" + "?expected_generation=\(fence.generation)"
            + "&expected_media_grant_revision=\(fence.mediaGrantRevision)"
        let result = await request(binding: binding, path: path, method: "DELETE", body: nil)
        if result.status == 204 { return true }
        // A session the lease reaper already ended is a clean end for the
        // owning device (the server treats the matching-fence user DELETE as
        // idempotent; older servers answer 409 session_already_ended) —
        // showing "stale_generation" for it read as a broken Stop button.
        return result.status == 409
            && result.body?["code"]?.stringValue == "session_already_ended"
    }

    private func startRequest(
        binding: AppleVoiceUIBinding, path: String, body: [String: JSONValue]
    ) async -> AppleVoiceStartOutcome {
        let result = await request(binding: binding, path: path, method: "POST", body: body)
        if result.status == 409,
            result.body?["code"]?.stringValue == "voice_takeover_required",
            let owner = result.body?["owner"], let target = parseTakeover(owner)
        {
            return .takeoverRequired(target, result.body?["message"]?.stringValue)
        }
        guard (200...299).contains(result.status),
            let response = result.body?.objectValue,
            Set(response.keys) == ["session", "grant"],
            let session = result.body?["session"].flatMap(parseSession),
            let grant = result.body?["grant"].flatMap(parseGrant),
            session.sessionId == grant.sessionId,
            session.generation == grant.generation,
            session.mediaGrantRevision == grant.mediaGrantRevision
        else {
            return .failed(
                result.body?["code"]?.stringValue ?? "network_interrupted",
                result.body?["message"]?.stringValue)
        }
        return .started(session, grant)
    }

    private func request(
        binding: AppleVoiceUIBinding, path: String, method: String,
        body: [String: JSONValue]?, encodedBody: Data? = nil
    ) async -> (status: Int, body: JSONValue?) {
        guard let url = URL(string: path, relativeTo: binding.serverBase)?.absoluteURL else { return (0, nil) }
        let payload = encodedBody ?? body.flatMap { try? JSONValue.object($0).encoded() }
        var request = NoStoreHTTP.request(
            url: url,
            method: method,
            body: payload,
            contentType: payload == nil ? nil : "application/json")
        request.timeoutInterval = 20
        request.setValue("Bearer \(binding.token)", forHTTPHeaderField: "Authorization")
        request.setValue(binding.deviceId, forHTTPHeaderField: "X-Astral-Device-Id")
        request.setValue(binding.connectionGeneration, forHTTPHeaderField: "X-Astral-Connection-Generation")
        request.setValue(binding.control.binding, forHTTPHeaderField: "X-Astral-Voice-Control-Binding")
        do {
            let (data, response) = try await session.data(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let parsed = data.isEmpty ? nil : try? JSONValue.parse(data)
            return (status, parsed)
        } catch {
            return (0, nil)
        }
    }

    private func activationBody(
        _ binding: AppleVoiceUIBinding, activationId: String,
        capability: AppleVoiceMediaCapability
    ) -> [String: JSONValue] {
        [
            "device_id": .string(binding.deviceId),
            "device_kind": .string(binding.deviceKind),
            "visible_chat_id": .string(binding.visibleChatId),
            "activation_id": .string(activationId),
            "capability": .object([
                "has_microphone": .bool(capability.hasMicrophone),
                "has_audio_output": .bool(capability.hasAudioOutput),
                "microphone_permission": .string(capability.microphonePermission),
                "full_duplex": .bool(capability.fullDuplex),
                "transport": .string("livekit"),
            ]),
            "foreground_active": .bool(true),
        ]
    }

    private func generationBody(_ value: AppleVoiceRestSession) -> [String: JSONValue] {
        [
            "expected_generation": .number(Double(value.generation)),
            "expected_media_grant_revision": .number(Double(value.mediaGrantRevision)),
        ]
    }

    private func parseSession(_ value: JSONValue) -> AppleVoiceRestSession? {
        let required: Set<String> = [
            "session_id", "device_id", "device_kind", "transport", "state",
            "generation", "media_grant_revision", "owner_connection_generation",
            "visible_chat_id", "applied_visible_chat_id", "chat_context_revision",
            "applied_chat_context_revision", "chat_context_synced", "foreground_active",
            "foreground_reason", "foreground_changed_at", "speech_muted",
            "microphone_enabled", "lease_expires_at", "started_at",
        ]
        guard let object = value.objectValue,
            appExact(object, required: required, optional: ["idle_expires_at"]),
            let session = appUUID(object["session_id"]),
            let device = appUUID(object["device_id"]),
            let deviceKind = object["device_kind"]?.stringValue,
            ["web", "windows", "android", "ios", "macos", "watchos"].contains(deviceKind),
            object["transport"]?.stringValue == "livekit",
            let connection = appUUID(object["owner_connection_generation"]),
            let visible = appUUID(object["visible_chat_id"]),
            let appliedVisible = appNullableUUID(object["applied_visible_chat_id"]),
            let generation = appPositiveInt(object["generation"]),
            let grant = appPositiveInt(object["media_grant_revision"]),
            let context = appPositiveInt(object["chat_context_revision"]),
            let appliedContext = appNullablePositiveInt(object["applied_chat_context_revision"]),
            let synced = object["chat_context_synced"]?.boolValue,
            let state = object["state"]?.stringValue,
            ["starting", "active", "suspended", "reconnecting", "ending", "ended", "error"].contains(state),
            let foreground = object["foreground_active"]?.boolValue,
            let foregroundReason = object["foreground_reason"]?.stringValue,
            [
                "foreground", "backgrounded", "locked", "audio_interrupted",
                "route_unavailable", "connection_lost",
            ].contains(foregroundReason),
            appTimestamp(object["foreground_changed_at"]) != nil,
            let muted = object["speech_muted"]?.boolValue,
            let microphone = object["microphone_enabled"]?.boolValue,
            let leaseExpires = appTimestamp(object["lease_expires_at"]),
            appTimestamp(object["started_at"]) != nil,
            appNullableTimestamp(object["idle_expires_at"]),
            foreground
                ? (foregroundReason == "foreground"
                    && ["starting", "active", "reconnecting", "ending", "error"].contains(state))
                : (!microphone && foregroundReason != "foreground"
                    && ["suspended", "reconnecting", "ending", "ended", "error"].contains(state)),
            !synced || (appliedVisible == visible && appliedContext == context)
        else { return nil }
        return AppleVoiceRestSession(
            sessionId: session, deviceId: device, deviceKind: deviceKind,
            transport: "livekit", ownerConnectionGeneration: connection,
            visibleChatId: visible, appliedVisibleChatId: appliedVisible,
            generation: generation, mediaGrantRevision: grant,
            chatContextRevision: context, appliedChatContextRevision: appliedContext,
            chatContextSynced: synced, state: state, foregroundActive: foreground,
            foregroundReason: foregroundReason, speechMuted: muted,
            microphoneEnabled: microphone, leaseExpiresAt: leaseExpires)
    }

    private func parseGrant(_ value: JSONValue) -> AppleLiveKitGrant? {
        let required: Set<String> = [
            "grant_id", "transport", "session_id", "generation", "media_grant_revision",
            "expires_at", "url", "join_token", "room_name", "participant_identity",
            "worker_identity",
        ]
        guard let object = value.objectValue, appExact(object, required: required),
            object["transport"]?.stringValue == "livekit",
            let grantId = appOpaque(object["grant_id"]),
            let session = appUUID(object["session_id"]),
            let generation = appPositiveInt(object["generation"]),
            let revision = appPositiveInt(object["media_grant_revision"]),
            let expires = appTimestamp(object["expires_at"]),
            let url = object["url"]?.stringValue,
            let components = URLComponents(string: url), ["ws", "wss"].contains(components.scheme),
            components.host?.isEmpty == false,
            let token = object["join_token"]?.stringValue, (32...8_192).contains(token.count),
            let room = appOpaque(object["room_name"]),
            let participant = appOpaque(object["participant_identity"]),
            let worker = appOpaque(object["worker_identity"]), participant != worker
        else { return nil }
        return AppleLiveKitGrant(
            grantId: grantId, sessionId: session, generation: generation,
            mediaGrantRevision: revision, expiresAt: expires, url: url,
            joinToken: token, roomName: room, participantIdentity: participant,
            workerIdentity: worker)
    }

    private func parseTakeover(_ value: JSONValue) -> AppleVoiceTakeoverTarget? {
        guard let object = value.objectValue,
            let session = appUUID(object["session_id"]),
            let kind = object["device_kind"]?.stringValue,
            let generation = appPositiveInt(object["generation"]),
            let revision = appPositiveInt(object["media_grant_revision"])
        else { return nil }
        return AppleVoiceTakeoverTarget(
            sessionId: session, deviceKind: kind,
            deviceLabel: object["device_label"]?.stringValue,
            generation: generation, mediaGrantRevision: revision)
    }

    private func validCredentialFreeGrantState(
        _ value: JSONValue?, session: AppleVoiceRestSession
    ) -> Bool {
        let required: Set<String> = [
            "transport", "media_grant_revision", "status", "expires_at",
        ]
        guard let object = value?.objectValue, appExact(object, required: required),
            object["transport"]?.stringValue == "livekit",
            appPositiveInt(object["media_grant_revision"]) == session.mediaGrantRevision,
            let status = object["status"]?.stringValue,
            ["pending_worker", "active", "expired", "unavailable"].contains(status),
            object["expires_at"] == .null || appTimestamp(object["expires_at"]) != nil
        else { return false }
        return true
    }
}

private let appUUID4Pattern =
    "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
private let appOpaquePattern = "^[A-Za-z0-9._:-]+$"

private func appExact(
    _ object: [String: JSONValue], required: Set<String>, optional: Set<String> = []
) -> Bool {
    let keys = Set(object.keys)
    return required.isSubset(of: keys) && keys.isSubset(of: required.union(optional))
}

private func appUUID(_ value: JSONValue?) -> String? {
    guard let value = value?.stringValue,
        value.range(of: appUUID4Pattern, options: .regularExpression) != nil
    else { return nil }
    return value
}

private func appOpaque(_ value: JSONValue?) -> String? {
    guard let value = value?.stringValue, (1...128).contains(value.count),
        value.range(of: appOpaquePattern, options: .regularExpression) != nil
    else { return nil }
    return value
}

private func appPositiveInt(_ value: JSONValue?) -> Int? {
    guard let value = value?.numberValue, value.isFinite, value.rounded() == value,
        value >= 1, value <= 9_007_199_254_740_991
    else { return nil }
    return Int(value)
}

private func appNullableUUID(_ value: JSONValue?) -> String?? {
    guard let value else { return nil }
    if value == .null { return .some(nil) }
    guard let parsed = appUUID(value) else { return nil }
    return .some(parsed)
}

private func appNullablePositiveInt(_ value: JSONValue?) -> Int?? {
    guard let value else { return nil }
    if value == .null { return .some(nil) }
    guard let parsed = appPositiveInt(value) else { return nil }
    return .some(parsed)
}

private func appTimestamp(_ value: JSONValue?) -> String? {
    guard let value = value?.stringValue else { return nil }
    let normal = ISO8601DateFormatter()
    if normal.date(from: value) != nil { return value }
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return fractional.date(from: value) == nil ? nil : value
}

private func appNullableTimestamp(_ value: JSONValue?) -> Bool {
    guard let value else { return true }
    return value == .null || appTimestamp(value) != nil
}

private func appFutureTimestamp(_ value: String) -> Bool {
    let normal = ISO8601DateFormatter()
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    guard let date = normal.date(from: value) ?? fractional.date(from: value) else { return false }
    return date > Date()
}

@MainActor @Observable
final class AppleVoiceSessionController {
    private static let terminalRefreshReasons: Set<String> = [
        "session_ended", "session_already_ended", "voice_session_not_found",
        "session_not_found", "worker_assignment_unavailable",
    ]
    private static let transientRefreshReasons: Set<String> = [
        "network_interrupted", "voice_unavailable", "media_grant_apply_failed",
        "invalid_voice_runtime_response",
    ]

    private struct UIConnection {
        var token: String
        var serverBase: URL
        var deviceId: String
        var deviceKind: String
        var connectionGeneration: String
        var visibleChatId: String?
    }

    private struct PendingFinal {
        let transcript: VoiceTranscript
        let connectionGeneration: String
        let wireText: String
        let bytes: Int
        let retry: Task<Void, Never>
    }

    private let api: AppleVoiceControlAPI
    private let media: AppleVoiceMediaClient
    private let permissionProvider: () async -> AppleVoiceMediaCapability
    private let audioRouteSnapshotProvider: @MainActor () -> AppleVoiceAudioRouteSnapshot?
    private let uuid: () -> String
    private let retryNanoseconds: UInt64
    private let leaseRenewalNanoseconds: UInt64
    private var connection: UIConnection?
    private var pendingControl: VoiceControlBinding?
    private var binding: AppleVoiceUIBinding?
    private var session: AppleVoiceRestSession?
    private var grant: AppleLiveKitGrant?
    private var takeoverTarget: AppleVoiceTakeoverTarget?
    private var composerRevision = -1
    private var transcriptSequences: [String: Int] = [:]
    private var announcementLedger = VoiceAnnouncementLedger()
    private var pendingFinals: [String: PendingFinal] = [:]
    private var pendingNewChat: (submission: String, request: String)?
    private var pendingActivation = false
    private var pendingTakeoverActivation = false
    private var pendingCapability: AppleVoiceMediaCapability?
    private var activationInFlight = false
    private var mediaConnectInFlight = false
    private var recoveryRequired = false
    private var recoveryRevision = 0
    private var endRequested = false
    private var lastSuspensionReason = "backgrounded"
    private var currentTurn: VoiceTurnState?
    private var playoutSequence = 0
    private var leaseRenewal: Task<Void, Never>?
    private var suspensionTask: Task<Void, Never>?
    private var recoveryTask: Task<Void, Never>?
    private var lifecycleEpoch = 0
    private var foregroundEligible = true
    private var sessionIsLocked = false
    private var audioInterruptionActive = false
    private var audioRouteAvailable = true
    private var audioRouteSnapshot: AppleVoiceAudioRouteSnapshot?
    private var audioRouteRevision = 0
    private var controlTransportAvailable = true
    private var microphoneDesired = true
    private var frameSender: ((String) -> Bool)?
    private var chatAdopter: ((String) -> Void)?

    private(set) var composer: VoiceComposerModel?
    private(set) var phase = "off"
    private(set) var reason = "ready"
    private(set) var message: String?
    private(set) var terminalNotice: VoiceTerminalNotice?
    private(set) var transcriptPreview: String?
    private(set) var mediaConnected = false
    private(set) var awaitingAcceptance = 0

    var active: Bool { !["off", "unavailable", "ended"].contains(phase) }
    var takeoverAvailable: Bool { takeoverTarget != nil }

    private var recoveryEligible: Bool {
        foregroundEligible && !sessionIsLocked && !audioInterruptionActive
            && audioRouteAvailable && controlTransportAvailable && !endRequested
    }

    init(
        api: AppleVoiceControlAPI? = nil,
        media: AppleVoiceMediaClient? = nil,
        permissionProvider: @escaping () async -> AppleVoiceMediaCapability = {
            await AppleVoicePermission.requestCapability()
        },
        audioRouteSnapshotProvider: @escaping @MainActor () -> AppleVoiceAudioRouteSnapshot? = {
            AppleVoicePermission.currentAudioRouteSnapshot()
        },
        uuid: @escaping () -> String = { UUID().uuidString.lowercased() },
        retryNanoseconds: UInt64 = 2_500_000_000,
        leaseRenewalNanoseconds: UInt64 = 20_000_000_000
    ) {
        let resolvedAPI = api ?? URLSessionAppleVoiceControlAPI()
        let resolvedMedia = media ?? AppleLiveKitVoiceMediaClient()
        self.api = resolvedAPI
        self.media = resolvedMedia
        self.permissionProvider = permissionProvider
        self.audioRouteSnapshotProvider = audioRouteSnapshotProvider
        audioRouteSnapshot = audioRouteSnapshotProvider()
        self.uuid = uuid
        self.retryNanoseconds = retryNanoseconds
        self.leaseRenewalNanoseconds = leaseRenewalNanoseconds
        resolvedMedia.eventHandler = { [weak self] event in self?.consume(event) }
    }

    func setFrameSender(_ sender: @escaping (String) -> Bool) { frameSender = sender }
    func setChatAdopter(_ adopter: @escaping (String) -> Void) { chatAdopter = adopter }

    #if DEBUG
        /// Visual/accessibility fixture seam. Protocol correlation remains in
        /// `consumeTurnState`; UI automation uses this only to render the same
        /// shared reducer output without opening media or a real session.
        func installTerminalNoticeForUITesting(_ turn: VoiceTurnState) {
            terminalNotice = VoiceTerminalNoticeReducer.reduce(
                current: terminalNotice, turn: turn)
        }
    #endif

    func installUIConnection(
        token: String, serverBase: URL, deviceId: String, deviceKind: String,
        connectionGeneration: String, visibleChatId: String?
    ) {
        guard appUUID(.string(deviceId)) != nil,
            appUUID(.string(connectionGeneration)) != nil
        else { return }
        let changed = connection?.connectionGeneration != connectionGeneration
        connection = UIConnection(
            token: token, serverBase: serverBase, deviceId: deviceId,
            deviceKind: deviceKind, connectionGeneration: connectionGeneration,
            visibleChatId: visibleChatId)
        if changed {
            reserializePendingFinals(for: connectionGeneration)
            stopLeaseRenewal()
            lifecycleEpoch += 1
            recoveryTask?.cancel()
            recoveryTask = nil
            pendingControl = nil
            binding = nil
            composerRevision = -1
            media.disconnect()
            mediaConnected = false
            controlTransportAvailable = false
            if session != nil {
                markRecoveryRequired()
                feedback("reconnecting", "network_interrupted")
            }
        }
    }

    func updateVisibleChatLocally(_ chatId: String?) {
        connection?.visibleChatId = chatId
        rebuildBinding()
        guard let chatId, let session, session.visibleChatId != chatId,
            let binding
        else { return }
        Task {
            try? await media.setMicrophoneEnabled(false)
            if let updated = await api.update(
                binding: binding, session: session,
                fields: ["visible_chat_id": .string(chatId)])
            {
                self.session = updated
                if updated.chatContextSynced && updated.foregroundActive {
                    // The PATCH response already confirms the new chat
                    // context is applied — restore capture immediately
                    // instead of wedging on "Updating the voice chat
                    // context…" until a server push. (The server also emits
                    // voice_session_state after every session PATCH, which
                    // authoritatively reconciles phase if we diverge.)
                    if microphoneDesired {
                        try? await media.setMicrophoneEnabled(true)
                    }
                    feedback("listening", "ready")
                } else {
                    feedback(
                        "connecting", "chat_context_unavailable",
                        "Updating the voice chat context…")
                }
            } else {
                // A silently-dropped context PATCH used to leave a phantom
                // live session (mic off, no renewals visible to the user).
                // Surface it and enter the normal recovery path.
                markRecoveryRequired()
                scheduleRecovery()
                feedback(
                    "reconnecting", "network_interrupted",
                    "Voice is re-syncing the chat context…")
            }
        }
    }

    func consume(_ frame: InboundFrame) {
        switch frame.name {
        case "composer_state": consumeComposer(frame)
        case "voice_control_binding": consumeBinding(frame)
        case "voice_session_state": consumeSessionState(frame)
        case "voice_turn_state": consumeTurnState(frame)
        case "user_message_acked": consumeAcknowledgement(frame)
        case "voice_submission_rejected": consumeRejection(frame)
        case "chat_created": consumeChatCreated(frame)
        case "auth_required": invalidateForAuthentication()
        default: break
        }
    }

    func activate() async {
        guard connection != nil else {
            feedback("error", "network_interrupted")
            return
        }
        guard !pendingActivation, !pendingTakeoverActivation, !activationInFlight else { return }
        endRequested = false
        refreshAudioRouteBaseline()
        pendingActivation = true
        if connection?.visibleChatId == nil {
            requestCorrelatedNewChat()
            return
        }
        let capability = await permissionProvider()
        pendingCapability = capability
        await continueActivationIfReady()
    }

    func takeover() async {
        guard let target = takeoverTarget else { return }
        guard !pendingActivation, !pendingTakeoverActivation, !activationInFlight else { return }
        endRequested = false
        refreshAudioRouteBaseline()
        pendingTakeoverActivation = true
        let capability = await permissionProvider()
        pendingCapability = capability
        await continueTakeoverIfReady(target: target)
    }

    func perform(_ action: VoiceControlAction) async {
        switch action {
        case .start: await activate()
        case .takeover: await takeover()
        case .end: await end()
        case .microphone: await setMicrophoneEnabled(!(session?.microphoneEnabled ?? false))
        case .stopSpeech: await stopSpeech()
        case .muteSpeech: await setSpeechMuted(!(session?.speechMuted ?? false))
        case .visibleChat: updateVisibleChatLocally(connection?.visibleChatId)
        case .sensitiveRecap: await consentToSensitiveRecap()
        }
    }

    func sceneBecameInactive(reason: String = "backgrounded") {
        foregroundEligible = false
        suspendMedia(reason: sessionIsLocked ? "locked" : reason)
    }

    func sceneBecameActive() {
        foregroundEligible = true
        if pendingActivation {
            Task { await continueActivationIfReady() }
            return
        }
        if pendingTakeoverActivation, let target = takeoverTarget {
            Task { await continueTakeoverIfReady(target: target) }
            return
        }
        scheduleRecovery()
    }

    func audioSessionInterrupted() {
        audioInterruptionActive = true
        suspendMedia(reason: "audio_interrupted")
    }

    func audioSessionInterruptionEnded() {
        audioInterruptionActive = false
        scheduleRecovery()
    }

    func audioRouteChanged() {
        handleAudioRouteChange(expectedSnapshot: nil)
    }

    func audioEngineConfigurationChanged() {
        let next = audioRouteSnapshotProvider()
        guard let next else {
            handleAudioRouteChange(expectedSnapshot: nil)
            return
        }
        let previous = audioRouteSnapshot
        audioRouteSnapshot = next
        guard session != nil, previous != next else { return }
        handleAudioRouteChange(expectedSnapshot: next)
    }

    private func handleAudioRouteChange(expectedSnapshot: AppleVoiceAudioRouteSnapshot?) {
        guard let routeSessionId = session?.sessionId else { return }
        audioRouteRevision += 1
        let revision = audioRouteRevision
        audioRouteAvailable = false
        suspendMedia(reason: "route_unavailable")
        let routeEpoch = lifecycleEpoch
        Task { [weak self] in
            guard let self else { return }
            let capability = await self.permissionProvider()
            guard revision == self.audioRouteRevision,
                routeEpoch == self.lifecycleEpoch,
                routeSessionId == self.session?.sessionId,
                expectedSnapshot == nil || expectedSnapshot == self.audioRouteSnapshot
            else { return }
            if capability.hasMicrophone && capability.hasAudioOutput {
                self.audioRouteAvailable = true
                self.scheduleRecovery()
            } else {
                self.audioRouteAvailable = false
            }
        }
    }

    private func refreshAudioRouteBaseline() {
        if let current = audioRouteSnapshotProvider() {
            audioRouteSnapshot = current
        }
    }

    func controlTransportDisconnected() {
        controlTransportAvailable = false
        suspendMedia(reason: "connection_lost")
    }

    func sessionLocked() {
        sessionIsLocked = true
        suspendMedia(reason: "locked")
    }

    func sessionUnlocked() {
        sessionIsLocked = false
        scheduleRecovery()
    }

    func close() {
        let remoteEnd: (AppleVoiceUIBinding, AppleVoiceSessionFence)?
        if let binding = currentBinding(),
            let fence = session.map({ AppleVoiceSessionFence(session: $0) })
                ?? authoritativeComposerFence()
        {
            remoteEnd = (binding, fence)
        } else {
            remoteEnd = nil
        }
        lifecycleEpoch += 1
        stopLeaseRenewal()
        suspensionTask?.cancel()
        suspensionTask = nil
        recoveryTask?.cancel()
        recoveryTask = nil
        media.disconnect()
        mediaConnected = false
        clearPendingFinals()
        connection = nil
        binding = nil
        pendingControl = nil
        session = nil
        grant = nil
        takeoverTarget = nil
        currentTurn = nil
        terminalNotice = nil
        pendingNewChat = nil
        pendingActivation = false
        pendingTakeoverActivation = false
        pendingCapability = nil
        activationInFlight = false
        mediaConnectInFlight = false
        recoveryRequired = false
        endRequested = false
        lastSuspensionReason = "backgrounded"
        transcriptPreview = nil
        transcriptSequences.removeAll()
        announcementLedger = VoiceAnnouncementLedger()
        composer = nil
        foregroundEligible = true
        sessionIsLocked = false
        audioInterruptionActive = false
        audioRouteAvailable = true
        audioRouteSnapshot = audioRouteSnapshotProvider()
        audioRouteRevision &+= 1
        controlTransportAvailable = true
        microphoneDesired = true
        feedback("off", "ready", nil)
        if let (binding, fence) = remoteEnd {
            Task { _ = await api.end(binding: binding, fence: fence) }
        }
    }

    private func consumeComposer(_ frame: InboundFrame) {
        guard let value = VoiceComposerState(frame: frame),
            value.connectionGeneration == connection?.connectionGeneration,
            value.revision > composerRevision
        else { return }
        composerRevision = value.revision
        composer = value.voice

        if [.off, .ended].contains(value.voice.state) {
            terminalNotice = nil
        } else if value.voice.reason == .speechError {
            terminalNotice = VoiceTerminalNoticeReducer.speechFailure(
                message: value.voice.message
                    ?? messageFor(
                        phase: value.voice.state.rawValue,
                        reason: value.voice.reason.rawValue),
                turnId: currentTurn?.turnId,
                occurredAt: terminalNotice?.occurredAt ?? currentTurn?.occurredAt)
        }

        if [.off, .unavailable, .ended].contains(value.voice.state) {
            if session != nil || mediaConnected || mediaConnectInFlight {
                clearMediaSession(retainPending: true)
            }
            phase = value.voice.state.rawValue
            reason = value.voice.reason.rawValue
            message = value.voice.message ?? messageFor(phase: phase, reason: reason)
            return
        }

        let ownedHere = value.voice.ownerDevice?.deviceId == connection?.deviceId
        let localMediaMatches =
            mediaConnected
            && value.voice.sessionId == session?.sessionId
            && value.voice.generation == session?.generation
            && value.voice.mediaGrantRevision == session?.mediaGrantRevision
        if ownedHere && !localMediaMatches {
            if session != nil, !activationInFlight, !mediaConnectInFlight {
                if recoveryTask == nil { markRecoveryRequired() }
                scheduleRecovery()
            }
            feedback(
                "reconnecting", "network_interrupted",
                "Voice media is reconnecting. You can end the session and start again if needed.")
            return
        }

        phase = value.voice.state.rawValue
        reason = value.voice.reason.rawValue
        message = value.voice.message ?? messageFor(phase: phase, reason: reason)
    }

    private func consumeBinding(_ frame: InboundFrame) {
        guard let value = VoiceControlBinding(frame: frame),
            value.deviceId == connection?.deviceId,
            value.connectionGeneration == connection?.connectionGeneration,
            future(value.expiresAt)
        else { return }
        pendingControl = value
        controlTransportAvailable = true
        rebuildBinding()
        startLeaseRenewalIfNeeded()
        scheduleRecovery()
        if pendingActivation { Task { await continueActivationIfReady() } }
        if pendingTakeoverActivation, let target = takeoverTarget {
            Task { await continueTakeoverIfReady(target: target) }
        }
    }

    private func rebuildBinding() {
        guard let connection, let control = pendingControl,
            control.deviceId == connection.deviceId,
            control.connectionGeneration == connection.connectionGeneration,
            let chat = connection.visibleChatId
        else {
            binding = nil
            return
        }
        binding = AppleVoiceUIBinding(
            token: connection.token, serverBase: connection.serverBase,
            deviceId: connection.deviceId, deviceKind: connection.deviceKind,
            connectionGeneration: connection.connectionGeneration,
            control: control, visibleChatId: chat)
    }

    private func consumeSessionState(_ frame: InboundFrame) {
        guard let value = VoiceSessionState(frame: frame),
            value.connectionGeneration == connection?.connectionGeneration,
            value.sessionId == session?.sessionId,
            value.generation == session?.generation,
            value.mediaGrantRevision == session?.mediaGrantRevision
        else { return }
        phase = value.state.rawValue
        reason = value.reason.rawValue
        message = value.message ?? messageFor(phase: phase, reason: reason)
        if value.state == .ended {
            terminalNotice = nil
        } else if value.reason == .speechError {
            terminalNotice = VoiceTerminalNoticeReducer.speechFailure(
                message: message, turnId: currentTurn?.turnId,
                occurredAt: terminalNotice?.occurredAt ?? currentTurn?.occurredAt)
        }
        guard var current = session else { return }
        current = AppleVoiceRestSession(
            sessionId: current.sessionId, deviceId: current.deviceId,
            deviceKind: current.deviceKind, transport: current.transport,
            ownerConnectionGeneration: current.ownerConnectionGeneration,
            visibleChatId: value.visibleChatId,
            appliedVisibleChatId: value.chatContextSynced ? value.visibleChatId : nil,
            generation: value.generation, mediaGrantRevision: value.mediaGrantRevision,
            chatContextRevision: value.chatContextRevision,
            appliedChatContextRevision: value.appliedChatContextRevision,
            chatContextSynced: value.chatContextSynced, state: current.state,
            foregroundActive: value.foregroundActive,
            foregroundReason: value.foregroundActive
                ? "foreground"
                : ([
                    "backgrounded", "audio_interrupted", "route_unavailable",
                    "connection_lost",
                ].contains(value.reason.rawValue) ? value.reason.rawValue : "connection_lost"),
            speechMuted: value.speechMuted, microphoneEnabled: value.microphoneEnabled,
            leaseExpiresAt: current.leaseExpiresAt)
        session = current
        if value.foregroundActive && value.chatContextSynced && value.microphoneEnabled
            && recoveryEligible
        {
            microphoneDesired = true
            startLeaseRenewalIfNeeded()
            Task { try? await media.setMicrophoneEnabled(true) }
        } else {
            if !value.foregroundActive { stopLeaseRenewal() }
            Task { try? await media.setMicrophoneEnabled(false) }
        }
        if value.state == .ended { clearMediaSession(retainPending: true) }
    }

    private func consumeTurnState(_ frame: InboundFrame) {
        guard let value = VoiceTurnState(frame: frame),
            value.connectionGeneration == connection?.connectionGeneration,
            value.sessionId == session?.sessionId,
            value.generation == session?.generation,
            value.mediaGrantRevision == session?.mediaGrantRevision,
            value.chatId == connection?.visibleChatId,
            VoiceTerminalNoticeReducer.canApply(
                current: terminalNotice, turnId: value.turnId,
                occurredAt: value.occurredAt),
            currentTurn == nil || currentTurn?.turnId != value.turnId
                || value.sequence > (currentTurn?.sequence ?? -1)
        else { return }
        currentTurn = value
        terminalNotice = VoiceTerminalNoticeReducer.reduce(
            current: terminalNotice, turn: value)
        let speechFailed = value.state == "succeeded" && value.speechOutcome == .failed
        if value.foreground {
            if speechFailed {
                phase = "error"
                reason = VoiceReason.speechError.rawValue
                message = terminalNotice?.displayText ?? value.message
                return
            }
            phase =
                switch value.state {
                case "recognizing": "transcribing"
                case "submitting": "acknowledging"
                case "accepted", "processing": "processing"
                case "waiting_on_user": "waiting_on_user"
                case "succeeded": "speaking_result"
                case "failed", "refused", "cancelled": "error"
                default: phase
                }
            message = value.message ?? messageFor(phase: phase, reason: value.outputReason)
        }
    }

    private func consumeChatCreated(_ frame: InboundFrame) {
        guard let value = CorrelatedVoiceChatCreated(frame: frame),
            let pending = pendingNewChat,
            value.connectionGeneration == connection?.connectionGeneration,
            value.submissionId == pending.submission,
            value.requestGeneration == pending.request
        else { return }
        pendingNewChat = nil
        connection?.visibleChatId = value.chatId
        rebuildBinding()
        chatAdopter?(value.chatId)
        Task {
            let capability: AppleVoiceMediaCapability
            if let pendingCapability {
                capability = pendingCapability
            } else {
                capability = await permissionProvider()
            }
            pendingCapability = capability
            await continueActivationIfReady()
        }
    }

    private func requestCorrelatedNewChat() {
        guard pendingNewChat == nil, let connection else { return }
        let submission = uuid()
        let request = uuid()
        let wire = Outbound.correlatedVoiceNewChat(
            connectionGeneration: connection.connectionGeneration,
            submissionId: submission, requestGeneration: request)
        guard wire != "{}", frameSender?(wire) == true else {
            feedback("error", "network_interrupted")
            return
        }
        pendingNewChat = (submission, request)
        feedback("connecting", "chat_context_unavailable", "Creating a chat for voice…")
    }

    private func continueActivationIfReady() async {
        guard pendingActivation, let capability = pendingCapability else { return }
        if let failure = capabilityFailure(capability) {
            pendingActivation = false
            pendingCapability = nil
            feedback("error", failure)
            return
        }
        guard foregroundEligible else {
            feedback("suspended", lastSuspensionReason)
            return
        }
        guard !activationInFlight else { return }
        guard let binding = currentBinding() else {
            feedback("connecting", "auth_expired", "Voice controls are reconnecting…")
            return
        }
        pendingActivation = false
        pendingCapability = nil
        activationInFlight = true
        feedback("connecting", "ready")
        let outcome = await api.start(
            binding: binding, activationId: uuid(), capability: capability)
        await apply(outcome, expected: binding)
        activationInFlight = false
        scheduleRecovery()
    }

    private func continueTakeoverIfReady(target: AppleVoiceTakeoverTarget) async {
        guard pendingTakeoverActivation, takeoverTarget == target,
            let capability = pendingCapability
        else { return }
        if let failure = capabilityFailure(capability) {
            pendingTakeoverActivation = false
            pendingCapability = nil
            feedback("error", failure)
            return
        }
        guard foregroundEligible else {
            feedback("suspended", lastSuspensionReason)
            return
        }
        guard !activationInFlight else { return }
        guard let binding = currentBinding() else {
            feedback("connecting", "auth_expired", "Voice controls are reconnecting…")
            return
        }
        pendingTakeoverActivation = false
        pendingCapability = nil
        activationInFlight = true
        feedback("connecting", "ready", "Taking over voice on this device…")
        let outcome = await api.takeover(
            binding: binding, activationId: uuid(), target: target,
            capability: capability)
        await apply(outcome, expected: binding)
        activationInFlight = false
        scheduleRecovery()
    }

    private func apply(_ outcome: AppleVoiceStartOutcome, expected: AppleVoiceUIBinding) async {
        switch outcome {
        case .failed(let reason, let message):
            feedback("error", reason, message)
        case .takeoverRequired(let target, let message):
            takeoverTarget = target
            feedback("error", "takeover_required", message)
        case .started(let session, let grant):
            guard session.deviceId == expected.deviceId,
                session.deviceKind == expected.deviceKind,
                session.transport == "livekit",
                session.ownerConnectionGeneration == expected.connectionGeneration,
                session.visibleChatId == expected.visibleChatId,
                grant.sessionId == session.sessionId,
                grant.generation == session.generation,
                grant.mediaGrantRevision == session.mediaGrantRevision,
                future(grant.expiresAt), future(session.leaseExpiresAt)
            else {
                feedback("error", "stale_generation")
                return
            }
            takeoverTarget = nil
            self.session = session
            self.grant = grant
            microphoneDesired = session.microphoneEnabled || session.chatContextSynced
            announcementLedger = VoiceAnnouncementLedger()
            transcriptSequences.removeAll()
            if !foregroundEligible {
                suspendMedia(reason: lastSuspensionReason)
                return
            }
            mediaConnectInFlight = true
            refreshAudioRouteBaseline()
            let connectEpoch = lifecycleEpoch
            let connectRecoveryRevision = recoveryRevision
            do {
                try await media.connect(grant)
                guard lifecycleEpoch == connectEpoch, foregroundEligible, recoveryEligible,
                    recoveryRevision == connectRecoveryRevision,
                    self.session?.sessionId == session.sessionId,
                    self.session?.generation == session.generation,
                    self.session?.mediaGrantRevision == session.mediaGrantRevision
                else {
                    media.disconnect()
                    mediaConnected = false
                    mediaConnectInFlight = false
                    if self.session == nil || endRequested {
                        recoveryRequired = false
                    } else {
                        markRecoveryRequired()
                        if foregroundEligible {
                            feedback("reconnecting", "network_interrupted")
                        } else {
                            feedback("suspended", lastSuspensionReason)
                        }
                    }
                    return
                }
                if session.chatContextSynced && session.foregroundActive && recoveryEligible {
                    try await media.setMicrophoneEnabled(true)
                }
                guard lifecycleEpoch == connectEpoch, foregroundEligible, recoveryEligible,
                    recoveryRevision == connectRecoveryRevision,
                    self.session?.sessionId == session.sessionId,
                    self.session?.generation == session.generation,
                    self.session?.mediaGrantRevision == session.mediaGrantRevision
                else {
                    media.disconnect()
                    mediaConnected = false
                    mediaConnectInFlight = false
                    if self.session == nil || endRequested {
                        recoveryRequired = false
                    } else if !recoveryRequired {
                        markRecoveryRequired()
                    }
                    return
                }
                mediaConnected = true
                startLeaseRenewalIfNeeded()
                if session.chatContextSynced && session.foregroundActive {
                    feedback("greeting", "ready")
                } else {
                    feedback("connecting", "chat_context_unavailable")
                }
                recoveryRequired = false
            } catch {
                media.disconnect()
                mediaConnected = false
                markRecoveryRequired()
                feedback(
                    "reconnecting", "media_error",
                    "Voice media is reconnecting. Typed chat is still available.")
            }
            mediaConnectInFlight = false
        }
    }

    private func consume(_ event: AppleVoiceMediaEvent) {
        switch event {
        case .connected:
            guard session != nil, grant != nil else { return }
            guard foregroundEligible else {
                media.disconnect()
                mediaConnected = false
                return
            }
            mediaConnected = true
            if !mediaConnectInFlight, recoveryTask == nil { recoveryRequired = false }
        case .reconnecting:
            guard session != nil else { return }
            mediaConnected = false
            markRecoveryRequired()
            feedback("reconnecting", "network_interrupted")
        case .failed:
            guard session != nil else { return }
            mediaConnected = false
            markRecoveryRequired()
            feedback("reconnecting", "network_interrupted")
            scheduleRecovery()
        case .disconnected(let unexpected):
            guard session != nil else { return }
            mediaConnected = false
            if unexpected {
                markRecoveryRequired()
                feedback("reconnecting", "network_interrupted")
                scheduleRecovery()
            }
        case .data(let topic, let participant, let payload):
            guard let grant else { return }
            let context = VoiceMediaContext(
                expectedWorkerIdentity: grant.workerIdentity,
                expectedParticipantIdentity: grant.workerIdentity,
                expectedSessionId: grant.sessionId,
                expectedGeneration: grant.generation,
                expectedMediaGrantRevision: grant.mediaGrantRevision)
            guard
                let envelope = VoiceMediaEnvelope(
                    topic: topic, participantIdentity: participant, data: payload,
                    context: context)
            else { return }
            switch envelope {
            case .transcript(let transcript): consume(transcript, packetBytes: payload.count)
            case .announcement(let announcement):
                guard announcementLedger.accept(announcement) else { return }
                guard media.authorize(announcement) else {
                    reportAnnouncementSpeechFailure(
                        announcement,
                        "Assistant audio could not be matched. Typed chat is still available.")
                    return
                }
            }
        case .playout(let announcement, let playoutPhase):
            guard let connection, let session,
                session.sessionId == announcement.sessionId,
                session.generation == announcement.generation,
                session.mediaGrantRevision == announcement.mediaGrantRevision,
                ["started", "finished", "interrupted"].contains(playoutPhase)
            else { return }
            defer { playoutSequence += 1 }
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            guard
                let event = VoicePlayoutEvent(
                    deviceId: connection.deviceId,
                    connectionGeneration: connection.connectionGeneration,
                    announcement: announcement, phase: playoutPhase,
                    clientSequence: playoutSequence, observedAt: formatter.string(from: Date()))
            else { return }
            _ = frameSender?(Outbound.voicePlayoutEvent(event))
            if playoutPhase == "started" {
                let speakingPhase =
                    announcement.kind == "result" ? "speaking_result" : "speaking_progress"
                feedback(speakingPhase, "ready")
            } else {
                let settledPhase: String
                switch announcement.kind {
                case "acknowledgement", "progress": settledPhase = "processing"
                case "waiting": settledPhase = "waiting_on_user"
                default: settledPhase = "listening"
                }
                feedback(
                    settledPhase,
                    playoutPhase == "interrupted" ? "speech_interrupted" : "ready")
            }
        case .announcementDropped(let announcement):
            guard let session,
                session.sessionId == announcement.sessionId,
                session.generation == announcement.generation,
                session.mediaGrantRevision == announcement.mediaGrantRevision
            else { return }
            reportAnnouncementSpeechFailure(
                announcement,
                "Assistant audio was unavailable. Typed chat is still available.")
        }
    }

    private func consume(_ transcript: VoiceTranscript, packetBytes: Int) {
        guard let connectionGeneration = connection?.connectionGeneration,
            transcript.chatId == connection?.visibleChatId,
            transcript.chatContextRevision == session?.chatContextRevision
        else { return }
        let last = transcriptSequences[transcript.turnId] ?? -1
        guard transcript.sequence > last else { return }
        transcriptSequences[transcript.turnId] = transcript.sequence
        transcriptPreview = transcript.text
        guard transcript.final else {
            feedback("transcribing", "ready", transcript.text)
            return
        }
        guard let origin = transcript.origin, future(origin.proofExpiresAt) else {
            feedback(
                "error", "proof_expired",
                "That spoken request expired before submission. Please say it again.")
            return
        }
        guard pendingFinals[transcript.submissionId] == nil else { return }
        let wire = Outbound.voiceChatMessage(
            transcript: transcript,
            connectionGeneration: connectionGeneration)
        let bytes = wire.utf8.count
        guard wire != "{}", packetBytes <= VoiceContractLimits.transcriptPacketBytes,
            pendingFinals.count < VoiceContractLimits.pendingFinalCount,
            pendingFinals.values.reduce(0, { $0 + $1.bytes }) + bytes
                <= VoiceContractLimits.pendingFinalBytes
        else {
            feedback("error", "capacity_exhausted")
            return
        }
        let retry = Task { [weak self] in
            while !Task.isCancelled {
                guard let self,
                    let pending = self.pendingFinals[transcript.submissionId]
                else { return }
                if pending.connectionGeneration == self.connection?.connectionGeneration {
                    _ = self.frameSender?(pending.wireText)
                }
                try? await Task.sleep(nanoseconds: self.retryNanoseconds)
            }
        }
        pendingFinals[transcript.submissionId] = PendingFinal(
            transcript: transcript,
            connectionGeneration: connectionGeneration,
            wireText: wire, bytes: bytes, retry: retry)
        awaitingAcceptance = pendingFinals.count
        feedback("acknowledging", "ready", "On it!")
    }

    private func reserializePendingFinals(for connectionGeneration: String) {
        // The UI connection generation is socket-scoped and is deliberately
        // outside the worker's transcript proof. Rebuilding from the retained
        // immutable transcript changes only that socket binding; the
        // submission/request/turn IDs, text digest, expiry, and HMAC stay exact.
        var dropped = false
        for submissionId in Array(pendingFinals.keys) {
            guard let pending = pendingFinals[submissionId],
                pending.connectionGeneration != connectionGeneration
            else { continue }
            let wire = Outbound.voiceChatMessage(
                transcript: pending.transcript,
                connectionGeneration: connectionGeneration)
            let bytes = wire.utf8.count
            let retainedBytes = pendingFinals.values.reduce(0, { $0 + $1.bytes }) - pending.bytes
            guard wire != "{}",
                retainedBytes + bytes <= VoiceContractLimits.pendingFinalBytes
            else {
                pending.retry.cancel()
                pendingFinals.removeValue(forKey: submissionId)
                dropped = true
                continue
            }
            pendingFinals[submissionId] = PendingFinal(
                transcript: pending.transcript,
                connectionGeneration: connectionGeneration,
                wireText: wire, bytes: bytes, retry: pending.retry)
        }
        awaitingAcceptance = pendingFinals.count
        if dropped {
            feedback(
                "error", "stale_generation",
                "That spoken request could not be restored. Please say it again.")
        }
    }

    private func consumeAcknowledgement(_ frame: InboundFrame) {
        guard let value = VoiceMessageAcknowledgement(frame: frame),
            let pending = pendingFinals[value.submissionId],
            value.connectionGeneration == connection?.connectionGeneration,
            value.chatId == pending.transcript.chatId,
            value.requestGeneration == pending.transcript.requestGeneration,
            value.voiceTurnId == pending.transcript.turnId
        else { return }
        removePending(value.submissionId)
        feedback("processing", "ready", "On it!")
    }

    private func consumeRejection(_ frame: InboundFrame) {
        guard let value = VoiceSubmissionRejected(frame: frame),
            let pending = pendingFinals[value.submissionId],
            value.connectionGeneration == connection?.connectionGeneration,
            value.sessionId == pending.transcript.sessionId,
            value.generation == pending.transcript.generation,
            value.mediaGrantRevision == pending.transcript.mediaGrantRevision,
            value.turnId == pending.transcript.turnId,
            value.clientTurnId == pending.transcript.clientTurnId,
            value.requestGeneration == pending.transcript.requestGeneration,
            value.chatId == pending.transcript.chatId
        else { return }
        removePending(value.submissionId)
        guard
            VoiceTerminalNoticeReducer.canApply(
                current: terminalNotice, turnId: value.turnId,
                occurredAt: value.occurredAt)
        else { return }
        terminalNotice = VoiceTerminalNoticeReducer.reduce(
            current: terminalNotice, rejection: value)
        feedback(
            "error", value.reason,
            terminalNotice?.displayText)
    }

    private func removePending(_ submissionId: String) {
        pendingFinals.removeValue(forKey: submissionId)?.retry.cancel()
        awaitingAcceptance = pendingFinals.count
    }

    private func clearPendingFinals() {
        for pending in pendingFinals.values { pending.retry.cancel() }
        pendingFinals.removeAll()
        awaitingAcceptance = 0
    }

    private func setMicrophoneEnabled(_ enabled: Bool) async {
        guard let binding = currentBinding(), let session else {
            feedback(
                "reconnecting", "network_interrupted",
                "Voice controls are reconnecting. End the session and start again if needed.")
            return
        }
        if enabled && !session.chatContextSynced {
            feedback("connecting", "chat_context_unavailable")
            return
        }
        guard
            let updated = await api.update(
                binding: binding, session: session,
                fields: ["microphone_enabled": .bool(enabled)])
        else {
            feedback("error", "stale_generation")
            return
        }
        self.session = updated
        microphoneDesired = enabled
        try? await media.setMicrophoneEnabled(enabled)
        feedbackForMediaControls(updated)
    }

    private func setSpeechMuted(_ muted: Bool) async {
        guard let binding = currentBinding(), let session else {
            feedback(
                "reconnecting", "network_interrupted",
                "Voice controls are reconnecting. End the session and start again if needed.")
            return
        }
        guard
            let updated = await api.update(
                binding: binding, session: session,
                fields: ["speech_muted": .bool(muted)])
        else {
            feedback("error", "stale_generation")
            return
        }
        self.session = updated
        if muted { media.interruptPlayout() }
        feedbackForMediaControls(updated)
    }

    private func feedbackForMediaControls(_ session: AppleVoiceRestSession) {
        if session.speechMuted && !session.microphoneEnabled {
            feedback("muted", "ready", "Microphone and assistant speech are muted.")
        } else if session.speechMuted {
            feedback("muted", "ready", "Assistant speech is muted.")
        } else if !session.microphoneEnabled {
            feedback("listening", "ready", "Microphone is off.")
        } else {
            feedback("listening", "ready")
        }
    }

    private func stopSpeech() async {
        guard let binding = currentBinding(), let session else {
            feedback(
                "reconnecting", "network_interrupted",
                "Voice controls are reconnecting. End the session and start again if needed.")
            return
        }
        media.interruptPlayout()
        if await api.stopSpeech(binding: binding, session: session) {
            feedback("listening", "ready")
        } else {
            feedback("error", "speech_error")
        }
    }

    private func consentToSensitiveRecap() async {
        guard let binding = currentBinding(), let session,
            let currentTurn, currentTurn.sensitiveResultPending,
            let resultId = currentTurn.resultId
        else { return }
        if !(await api.consent(
            binding: binding, session: session, resultId: resultId,
            turnId: currentTurn.turnId))
        {
            feedback("error", "stale_generation")
        }
    }

    private func end() async {
        endRequested = true
        terminalNotice = nil
        lifecycleEpoch += 1
        stopLeaseRenewal()
        suspensionTask?.cancel()
        suspensionTask = nil
        recoveryTask?.cancel()
        recoveryTask = nil
        recoveryRequired = false
        try? await media.setMicrophoneEnabled(false)
        media.interruptPlayout()
        media.disconnect()
        mediaConnected = false
        guard let binding = currentBinding(),
            let fence = session.map({ AppleVoiceSessionFence(session: $0) })
                ?? authoritativeComposerFence()
        else {
            feedback(
                "error", "network_interrupted",
                "Voice controls are reconnecting. Try End again in a moment.")
            return
        }
        let ended = await api.end(binding: binding, fence: fence)
        clearMediaSession(retainPending: true)
        feedback(ended ? "ended" : "error", ended ? "ended_by_user" : "stale_generation")
    }

    private func suspendMedia(reason: String) {
        let allowed = [
            "backgrounded", "locked", "audio_interrupted", "route_unavailable",
            "connection_lost",
        ]
        guard allowed.contains(reason) else { return }
        lastSuspensionReason = reason
        if session != nil || mediaConnected || mediaConnectInFlight || activationInFlight {
            markRecoveryRequired()
        }
        lifecycleEpoch += 1
        let epoch = lifecycleEpoch
        stopLeaseRenewal()
        recoveryTask?.cancel()
        recoveryTask = nil
        media.interruptPlayout()
        media.disconnect()
        mediaConnected = false
        guard let binding = currentBinding(), let session else {
            feedback("suspended", reason)
            return
        }
        let previous = suspensionTask
        suspensionTask = Task { [weak self] in
            _ = await previous?.value
            guard let self, !Task.isCancelled else { return }
            try? await self.media.setMicrophoneEnabled(false)
            let updated = await self.api.update(
                binding: binding, session: session,
                fields: [
                    "foreground_active": .bool(false),
                    "foreground_reason": .string(reason),
                    "microphone_enabled": .bool(false),
                ])
            guard self.lifecycleEpoch == epoch else { return }
            if let updated { self.session = updated }
            self.suspensionTask = nil
        }
        feedback("suspended", reason)
    }

    private func markRecoveryRequired() {
        recoveryRequired = true
        recoveryRevision &+= 1
    }

    private func scheduleRecovery() {
        guard recoveryRequired, recoveryEligible, !mediaConnected,
            !activationInFlight, !mediaConnectInFlight,
            session != nil, currentBinding() != nil, recoveryTask == nil
        else { return }
        lifecycleEpoch += 1
        let epoch = lifecycleEpoch
        let revision = recoveryRevision
        let pendingSuspension = suspensionTask
        recoveryTask = Task { [weak self] in
            _ = await pendingSuspension?.value
            guard let self else { return }
            defer {
                if self.lifecycleEpoch == epoch {
                    self.recoveryTask = nil
                    if self.recoveryRequired, self.recoveryRevision != revision {
                        self.scheduleRecovery()
                    }
                }
            }
            guard !Task.isCancelled, self.lifecycleEpoch == epoch,
                self.recoveryEligible, self.recoveryRevision == revision
            else { return }
            self.suspensionTask = nil
            await self.recoverMedia(epoch: epoch, revision: revision)
        }
    }

    private func recoverMedia(epoch: Int, revision: Int) async {
        guard let binding = currentBinding(), let original = session else { return }
        let capability = await permissionProvider()
        guard !Task.isCancelled, lifecycleEpoch == epoch, recoveryEligible,
            recoveryRevision == revision
        else { return }
        if let failure = capabilityFailure(capability) {
            try? await media.setMicrophoneEnabled(false)
            feedback("error", failure)
            return
        }
        feedback("reconnecting", "network_interrupted", "Reconnecting voice conversation…")

        var refresh = await api.refresh(
            binding: binding, session: original, refreshId: uuid())
        if case .current(let authoritative, let retryable) = refresh, retryable,
            authoritative.generation == original.generation
        {
            refresh = await api.refresh(
                binding: binding, session: authoritative, refreshId: uuid())
        }
        guard !Task.isCancelled, lifecycleEpoch == epoch, recoveryEligible,
            recoveryRevision == revision
        else { return }
        if case .failed(let refreshReason, _) = refresh,
            Self.terminalRefreshReasons.contains(refreshReason)
        {
            retireTerminalMediaSession()
            return
        }
        if case .failed(let refreshReason, _) = refresh,
            Self.transientRefreshReasons.contains(refreshReason)
        {
            feedback(
                "reconnecting", "network_interrupted",
                "Voice connection was interrupted. Retrying…")
            await armTransientRecoveryRetry(epoch: epoch, revision: revision)
            return
        }
        guard case .refreshed(let refreshed, let nextGrant) = refresh,
            refreshed.sessionId == original.sessionId,
            refreshed.deviceId == binding.deviceId,
            refreshed.deviceKind == binding.deviceKind,
            refreshed.transport == "livekit",
            refreshed.ownerConnectionGeneration == binding.connectionGeneration,
            refreshed.generation == original.generation,
            refreshed.mediaGrantRevision == nextGrant.mediaGrantRevision,
            nextGrant.sessionId == refreshed.sessionId,
            nextGrant.generation == refreshed.generation,
            future(refreshed.leaseExpiresAt), future(nextGrant.expiresAt)
        else {
            feedback("error", "network_interrupted", "Voice could not reconnect. End it and start again.")
            return
        }

        var resumeFields: [String: JSONValue] = [
            "foreground_active": .bool(true),
            "foreground_reason": .string("foreground"),
            "microphone_enabled": .bool(microphoneDesired),
        ]
        if refreshed.visibleChatId != binding.visibleChatId {
            resumeFields["visible_chat_id"] = .string(binding.visibleChatId)
        }
        guard
            let resumed = await api.update(
                binding: binding, session: refreshed, fields: resumeFields)
        else {
            feedback("error", "stale_generation")
            return
        }
        guard !Task.isCancelled, lifecycleEpoch == epoch, recoveryEligible,
            recoveryRevision == revision,
            resumed.sessionId == refreshed.sessionId,
            resumed.mediaGrantRevision == nextGrant.mediaGrantRevision,
            resumed.visibleChatId == binding.visibleChatId
        else { return }

        media.disconnect()
        announcementLedger = VoiceAnnouncementLedger()
        transcriptSequences.removeAll()
        session = resumed
        grant = nextGrant
        mediaConnectInFlight = true
        refreshAudioRouteBaseline()
        do {
            try await media.connect(nextGrant)
            guard !Task.isCancelled, lifecycleEpoch == epoch, recoveryEligible,
                recoveryRevision == revision
            else {
                media.disconnect()
                mediaConnected = false
                mediaConnectInFlight = false
                return
            }
            if resumed.chatContextSynced && microphoneDesired {
                try await media.setMicrophoneEnabled(true)
            } else {
                try? await media.setMicrophoneEnabled(false)
            }
            guard !Task.isCancelled, lifecycleEpoch == epoch, recoveryEligible,
                recoveryRevision == revision,
                session?.sessionId == resumed.sessionId,
                session?.generation == resumed.generation,
                session?.mediaGrantRevision == resumed.mediaGrantRevision
            else {
                media.disconnect()
                mediaConnected = false
                mediaConnectInFlight = false
                return
            }
            mediaConnected = true
            startLeaseRenewalIfNeeded()
            if resumed.chatContextSynced && microphoneDesired {
                feedback("listening", "ready")
            } else {
                feedback("connecting", "chat_context_unavailable")
            }
            recoveryRequired = false
        } catch {
            media.disconnect()
            mediaConnected = false
            feedback("error", "media_error")
        }
        mediaConnectInFlight = false
    }

    private func armTransientRecoveryRetry(epoch: Int, revision: Int) async {
        do {
            try await Task.sleep(nanoseconds: retryNanoseconds)
        } catch {
            return
        }
        guard !Task.isCancelled, lifecycleEpoch == epoch, recoveryEligible,
            recoveryRevision == revision, session != nil
        else { return }
        markRecoveryRequired()
    }

    private func retireTerminalMediaSession() {
        clearMediaSession(retainPending: true)
        takeoverTarget = nil
        currentTurn = nil
        endRequested = false
        transcriptPreview = nil
        feedback(
            "error", "media_error",
            "Voice media ended. Start a new voice conversation or keep typing.")
    }

    private func currentBinding() -> AppleVoiceUIBinding? {
        guard let binding, future(binding.control.expiresAt),
            binding.visibleChatId == connection?.visibleChatId
        else { return nil }
        return binding
    }

    private func authoritativeComposerFence() -> AppleVoiceSessionFence? {
        guard let connection, let composer,
            ![VoiceState.off, .unavailable, .ended].contains(composer.state),
            composer.ownerDevice?.deviceId == connection.deviceId,
            composer.visibleChatId == connection.visibleChatId,
            let sessionId = composer.sessionId,
            let generation = composer.generation,
            composer.ownerDevice?.generation == generation,
            let mediaGrantRevision = composer.mediaGrantRevision
        else { return nil }
        return AppleVoiceSessionFence(
            sessionId: sessionId, generation: generation,
            mediaGrantRevision: mediaGrantRevision)
    }

    private func invalidateForAuthentication() {
        lifecycleEpoch += 1
        stopLeaseRenewal()
        suspensionTask?.cancel()
        suspensionTask = nil
        recoveryTask?.cancel()
        recoveryTask = nil
        media.disconnect()
        mediaConnected = false
        clearPendingFinals()
        connection = nil
        binding = nil
        pendingControl = nil
        session = nil
        grant = nil
        takeoverTarget = nil
        currentTurn = nil
        terminalNotice = nil
        pendingNewChat = nil
        pendingActivation = false
        pendingTakeoverActivation = false
        pendingCapability = nil
        activationInFlight = false
        mediaConnectInFlight = false
        recoveryRequired = false
        endRequested = false
        lastSuspensionReason = "backgrounded"
        transcriptPreview = nil
        transcriptSequences.removeAll()
        announcementLedger = VoiceAnnouncementLedger()
        audioRouteAvailable = true
        audioRouteSnapshot = audioRouteSnapshotProvider()
        audioRouteRevision &+= 1
        controlTransportAvailable = false
        feedback("unavailable", "authentication_required")
    }

    private func clearMediaSession(retainPending: Bool) {
        lifecycleEpoch += 1
        stopLeaseRenewal()
        suspensionTask?.cancel()
        suspensionTask = nil
        recoveryTask?.cancel()
        recoveryTask = nil
        media.disconnect()
        mediaConnected = false
        mediaConnectInFlight = false
        recoveryRequired = false
        session = nil
        grant = nil
        audioRouteAvailable = true
        audioRouteSnapshot = audioRouteSnapshotProvider()
        audioRouteRevision &+= 1
        transcriptSequences.removeAll()
        announcementLedger = VoiceAnnouncementLedger()
        if !retainPending { clearPendingFinals() }
    }

    /// Renews only the authenticated 45-second ownership lease. Reasserting
    /// foreground state is intentionally not an `interaction`, so this task
    /// cannot extend the independent five-minute true-idle deadline.
    private func startLeaseRenewalIfNeeded() {
        guard leaseRenewal == nil, recoveryEligible, session?.foregroundActive == true,
            currentBinding() != nil
        else { return }
        leaseRenewal = Task { [weak self] in
            while !Task.isCancelled {
                guard let delay = self?.leaseRenewalNanoseconds else { return }
                do {
                    try await Task.sleep(nanoseconds: delay)
                } catch {
                    return
                }
                guard let self else { return }
                guard !Task.isCancelled, self.recoveryEligible,
                    let binding = self.currentBinding(),
                    let current = self.session, current.foregroundActive
                else { continue }
                let expectedFence = AppleVoiceSessionFence(session: current)
                if let updated = await self.api.update(
                    binding: binding, session: current,
                    fields: [
                        "foreground_active": .bool(true),
                        "foreground_reason": .string("foreground"),
                    ])
                {
                    guard !Task.isCancelled, self.recoveryEligible,
                        self.session.map({ AppleVoiceSessionFence(session: $0) })
                            == expectedFence,
                        AppleVoiceSessionFence(session: updated) == expectedFence
                    else { return }
                    self.session = updated
                } else {
                    guard !Task.isCancelled, self.recoveryEligible,
                        self.session.map({ AppleVoiceSessionFence(session: $0) })
                            == expectedFence
                    else { return }
                    self.stopLeaseRenewal()
                    self.media.interruptPlayout()
                    self.media.disconnect()
                    self.mediaConnected = false
                    self.markRecoveryRequired()
                    self.feedback(
                        "reconnecting", "network_interrupted",
                        "Renewing the voice connection…")
                    self.scheduleRecovery()
                    return
                }
            }
        }
    }

    private func stopLeaseRenewal() {
        leaseRenewal?.cancel()
        leaseRenewal = nil
    }

    private func future(_ value: String) -> Bool {
        let plain = ISO8601DateFormatter()
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let date = plain.date(from: value) ?? fractional.date(from: value) else { return false }
        return date > Date()
    }

    private func capabilityFailure(_ value: AppleVoiceMediaCapability) -> String? {
        if !value.hasMicrophone { return "no_microphone" }
        if !value.hasAudioOutput { return "no_audio_output" }
        if value.microphonePermission == "denied" { return "permission_denied" }
        if value.microphonePermission == "restricted" { return "permission_restricted" }
        if value.microphonePermission != "authorized" { return "permission_not_determined" }
        return nil
    }

    private func feedback(_ phase: String, _ reason: String, _ message: String? = nil) {
        let resolvedMessage = message ?? messageFor(phase: phase, reason: reason)
        self.phase = phase
        self.reason = reason
        self.message = resolvedMessage
        if reason == VoiceReason.speechError.rawValue {
            terminalNotice = VoiceTerminalNoticeReducer.speechFailure(
                message: resolvedMessage, turnId: currentTurn?.turnId,
                occurredAt: terminalNotice?.occurredAt ?? currentTurn?.occurredAt)
        }
    }

    /// Local media events do not carry a lifecycle timestamp. Attribute a
    /// durable text-result notice only when the result announcement matches
    /// the current fenced turn (or its existing same-turn notice). A delayed
    /// result from an older turn must not relabel the current request, while
    /// greeting/progress failures remain session feedback rather than claims
    /// about a text result.
    private func reportAnnouncementSpeechFailure(
        _ announcement: VoiceAnnouncementMedia, _ message: String
    ) {
        guard announcement.kind == "result" else {
            phase = "error"
            reason = VoiceReason.speechError.rawValue
            self.message = message
            return
        }
        guard let turnId = announcement.turnId,
            terminalNotice == nil || terminalNotice?.turnId == turnId,
            currentTurn?.turnId == turnId || terminalNotice?.turnId == turnId
        else { return }

        let occurredAt =
            terminalNotice?.turnId == turnId
            ? terminalNotice?.occurredAt : currentTurn?.occurredAt
        phase = "error"
        reason = VoiceReason.speechError.rawValue
        self.message = message
        terminalNotice = VoiceTerminalNoticeReducer.speechFailure(
            message: message, turnId: turnId, occurredAt: occurredAt,
            textResultCommitted: true)
    }

    private func messageFor(phase: String, reason: String) -> String {
        switch reason {
        case "permission_not_determined": "Allow microphone access to start a voice conversation."
        case "permission_denied": "Microphone permission was denied. Allow it in Settings or keep typing."
        case "permission_restricted": "Microphone access is restricted. You can keep typing."
        case "no_microphone": "No microphone is available. Connect one or keep typing."
        case "no_audio_output": "No audio output is available. Connect one or keep typing."
        case "takeover_required": "Voice is active on another device. Choose Take over to continue here."
        case "idle_expired": "Voice ended after five idle minutes. Accepted requests keep running."
        case "chat_context_unavailable": "Waiting for the voice chat context…"
        case "route_unavailable": "Audio hardware changed. Reconnecting voice…"
        case "network_interrupted": "Voice connection was interrupted. Typed chat is still available."
        case "media_error": "Voice media ended. Start a new voice conversation or keep typing."
        case "ended_by_user": "Voice conversation ended. Accepted requests keep running."
        case "backgrounded": "Voice is paused while this app is in the background."
        // 066/P5: these unavailability reasons used to fall through to the
        // default "Voice is available." — an actively misleading line for a
        // disabled mic. Every reason the server can refuse with names itself.
        case "feature_disabled": "Voice is not enabled on this server. You can keep typing."
        case "worker_unavailable", "media_unavailable", "voice_unavailable",
            "asr_unavailable", "tts_unavailable":
            "Voice is temporarily unavailable. You can keep typing."
        case "capacity_exhausted": "Voice is at capacity right now. Try again shortly."
        case "authentication_required", "auth_expired":
            "Sign in again to use voice. Typed chat is still available."
        default:
            switch phase {
            case "connecting": "Connecting voice…"
            case "greeting": "Connected. Waiting for the greeting…"
            case "listening": "Listening…"
            case "speech_detected": "I hear you…"
            case "transcribing": "Understanding what you said…"
            case "acknowledging": "Submitting your spoken request…"
            case "processing": "Working on it…"
            case "waiting_on_user": "Waiting for your response…"
            case "speaking_progress": "Speaking a progress update…"
            case "speaking_result": "Speaking the completed result…"
            case "muted": "Assistant speech is muted."
            case "ended": "Voice conversation ended."
            case "unavailable": "Voice conversation unavailable. You can keep typing."
            default: "Voice is available."
            }
        }
    }
}
