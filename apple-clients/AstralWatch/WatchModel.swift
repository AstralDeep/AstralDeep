import AstralCore
// Feature 051 — the watch app's brain: device-login lifecycle (start → QR →
// poll → tokens, auto-rotate before expiry), broker-based refresh, WS session
// with the `watch` device profile, transcript state, and speech coordination.
import Foundation
import SwiftUI

#if os(watchOS)
    import WatchKit
#endif

@MainActor
@Observable
final class WatchModel {

    enum Phase: Equatable {
        case signedOut
        case waitingApproval
        case loginFailed(String)
        case unavailable(String)
        case signedIn
    }

    enum Entry: Identifiable, Equatable {
        case user(id: String, text: String, attachments: [String])
        case status(id: String, text: String)
        case turn(id: String, components: [AstralComponent])

        var id: String {
            switch self {
            case .user(let id, _, _), .status(let id, _), .turn(let id, _): return id
            }
        }
    }

    // MARK: observable state

    var phase: Phase = .signedOut
    var login: DeviceLoginStart?
    var loginExpiresAt: Date = .distantFuture
    var recents: [ChatSummary] = []
    var entries: [Entry] = []
    /// The live canvas — identity-keyed workspace components. `ui_upsert` ops
    /// apply in place (replace/remove by component_id) instead of stacking
    /// duplicate transcript entries (FR-013 as it reaches the watch).
    var canvas: [AstralComponent] = []
    var transientEntries: [Entry] = []
    var transientCanvas: [AstralComponent]?
    var statusText: String?
    /// Separates live progress from informational/error notices so the watch
    /// never presents a terminal message with an indeterminate spinner.
    var statusShowsActivity = false
    var errorBanner: String?
    var connected = false
    var accountName = ""
    var pendingDictation = ""
    var operationStatuses: [String: OperationStatus] = [:]
    var agentLifecycles: [String: AgentLifecycle] = [:]
    var localOperationSubmissions: [String: LocalOperationSubmission] = [:]

    var visibleEntries: [Entry] { entries + transientEntries }
    var visibleCanvas: [AstralComponent] { transientCanvas ?? canvas }
    var pendingSurfaceRequestGenerations: Set<String> {
        Set(
            localOperationSubmissions.values.compactMap { submission in
                submission.chatId == nil ? submission.requestGeneration : nil
            })
    }
    var pendingChatRequestGenerations: Set<String> {
        Set(
            localOperationSubmissions.values.compactMap { submission in
                submission.chatId == activeChatId ? submission.requestGeneration : nil
            })
    }
    var rootStatusText: String? {
        if let statusText, !statusText.isEmpty { return statusText }
        var pendingGenerations = pendingChatRequestGenerations.union(
            pendingSurfaceRequestGenerations)
        if let currentRequest = continuity.requestGeneration {
            pendingGenerations.insert(currentRequest)
        }
        if let operation = operationStatuses.values.filter({
            !$0.terminal && pendingGenerations.contains($0.requestGeneration)
        }).max(by: {
            ($0.updatedAt, $0.sequence) < ($1.updatedAt, $1.sequence)
        }) {
            return operation.error.objectValue?["message"]?.stringValue ?? operation.label
        }
        if let lifecycle = agentLifecycles.values.max(by: {
            ($0.lifecycleGeneration, $0.stateRevision)
                < ($1.lifecycleGeneration, $1.stateRevision)
        }) {
            return "\(lifecycle.agentId): \(lifecycle.label)"
        }
        return nil
    }

    let speaker = Speaker()

    // Feature 065 — conversational voice is server-owned and independent of
    // the legacy spoken-rendition `Speaker` above. The bridge carries exact
    // worker ASR/TTS media; `Speaker` is never used as a conversation fallback.
    var voiceComposer: WatchVoiceComposer?
    var voiceState: WatchVoiceState = .off
    var voiceReason = "ready"
    var voiceMessage: String?
    var voiceTerminalNotice: VoiceTerminalNotice?
    var voicePartialTranscript: String?
    var voiceActivationBusy = false

    var visibleVoiceControls: [WatchVoiceControl] {
        voiceComposer?.controls.filter(\.visible) ?? []
    }

    var primaryVoiceControl: WatchVoiceControl? {
        for action in ["voice_session_end", "voice_session_takeover", "voice_session_start"] {
            if let control = visibleVoiceControls.first(where: { $0.action == action }) {
                return control
            }
        }
        return nil
    }

    var voiceStatusLabel: String {
        if let voiceMessage, !voiceMessage.isEmpty { return voiceMessage }
        switch voiceState {
        case .off: return "Voice conversation off"
        case .unavailable: return "Voice conversation unavailable"
        case .connecting: return "Connecting voice conversation"
        case .greeting: return "Greeting"
        case .listening: return "Listening"
        case .speechDetected: return "Speech detected"
        case .transcribing: return "Transcribing"
        case .acknowledging: return "On it"
        case .processing: return "Working"
        case .waitingOnUser: return "Waiting for you"
        case .speakingProgress: return "Speaking progress"
        case .speakingResult: return "Speaking result"
        case .muted: return "Assistant speech muted"
        case .suspended: return "Voice conversation suspended"
        case .reconnecting: return "Reconnecting voice conversation"
        case .error: return "Voice conversation error"
        case .ended: return "Voice conversation ended"
        }
    }

    // MARK: config + session

    /// The backend this watch talks to: a validated override pushed by the iPhone
    /// companion if one has ever arrived, else the build-time endpoint from
    /// Config/*.xcconfig (feature 053, FR-011). A watch with no companion — which
    /// is a fully supported state — simply keeps the build-time value.
    var serverBase = WatchOverrideSync.resolvedServerBase()
    /// The server-issued chat id this session is talking to. The backend
    /// routes by `session_id` FIRST — a made-up id would send every message
    /// to a phantom chat, so this is adopted from chat_created/chat_loaded
    /// and nil until the server assigns one.
    @ObservationIgnored var activeChatId: String?
    private let store: TokenStorage = {
        #if canImport(Security)
            KeychainTokenStore(service: "com.personalailabs.astraldeep.watch")
        #else
            InMemoryTokenStore()
        #endif
    }()

    /// Retained so the companion-override observer outlives `bootstrap()`.
    @ObservationIgnored private var overrideObserver: NSObjectProtocol?

    @ObservationIgnored private var tokens: TokenSet?
    @ObservationIgnored private var loginTask: Task<Void, Never>?
    @ObservationIgnored private var wsTask: Task<Void, Never>?
    @ObservationIgnored private var ws: WSClient?
    /// Single-flight refresh (see `refreshOutcome`) + a session generation so
    /// a refresh resolving after sign-out can never resurrect wiped
    /// credentials or be joined by the next account's session.
    @ObservationIgnored private var refreshTask: Task<RefreshResult, Never>?
    @ObservationIgnored private var refreshTaskGeneration = -1
    @ObservationIgnored private var sessionGeneration = 0
    @ObservationIgnored private var conversationResumeStore: ConversationResumeStore
    @ObservationIgnored private var conversationAccount: ConversationAccount?
    @ObservationIgnored private var continuity = ConversationContinuityReducer()
    @ObservationIgnored private var pendingCommitRequestGeneration: String?
    @ObservationIgnored private var seqState: [String: Int] = [:]
    @ObservationIgnored private var statusLifecycle = StatusLifecycleReducer()
    @ObservationIgnored private(set) var voiceDeviceId = WatchVoiceDeviceIdentity.load()
    @ObservationIgnored var voiceBridge: WatchVoiceBridgeControlling = WatchVoiceBridge()
    @ObservationIgnored var voiceRESTTransport: WatchVoiceRESTClient.Transport?
    @ObservationIgnored var voiceTokenProvider: (@Sendable () async -> String?)?
    @ObservationIgnored private var voiceControlBinding: WatchVoiceControlBinding?
    @ObservationIgnored var voiceSession: WatchVoiceSession?
    @ObservationIgnored var voiceGrant: WatchVoiceBridgeGrant?
    @ObservationIgnored private var pendingVoiceActivation: PendingVoiceActivation?
    @ObservationIgnored private var pendingVoiceSubmissions: [String: PendingVoiceSubmission] = [:]
    @ObservationIgnored private var voiceTranscriptSequences: [String: UInt64] = [:]
    @ObservationIgnored private var voiceTurnSequences: [String: Int] = [:]
    @ObservationIgnored private var currentVoiceTurnId: String?
    @ObservationIgnored private var currentVoiceTurnOccurredAt: String?
    @ObservationIgnored private var currentSensitiveVoiceTurn: VoiceTurnState?
    @ObservationIgnored private var voiceRetryTask: Task<Void, Never>?
    @ObservationIgnored private var voiceResumeTask: Task<Void, Never>?
    @ObservationIgnored private var voiceLeaseTask: Task<Void, Never>?
    @ObservationIgnored var voiceLeaseInterval: Duration = .seconds(20)
    @ObservationIgnored private var voicePlayoutSequence: UInt64 = 0
    @ObservationIgnored private var voiceForegroundActive = true

    /// Test seam: observes the exact frame before it reaches the socket.
    @ObservationIgnored var outboundTap: ((String) -> Void)?

    /// Test-only override for the dedicated, current-connection voice path.
    /// Production always falls through to `WSClient.sendCurrentConnectionVoice`.
    @ObservationIgnored var currentConnectionVoiceSendOverride: ((String) -> Void)?

    var deviceLogin: DeviceLoginClient {
        DeviceLoginClient(serverBase: serverBase)
    }

    var rest: RestClient {
        RestClient(serverBase: serverBase) { [weak self] in
            await self?.freshAccessToken()
        }
    }

    // MARK: lifecycle

    convenience init() {
        self.init(conversationResumeStore: ConversationResumeStore())
    }

    init(conversationResumeStore: ConversationResumeStore) {
        self.conversationResumeStore = conversationResumeStore
    }

    func bindConversationAccount(_ account: ConversationAccount) {
        if conversationAccount != account {
            continuity.clear()
            resetConversationState()
        }
        conversationAccount = account
        activeChatId = conversationResumeStore.load(for: account)?.chatId
    }

    func registrationFrame(token: String, resumed: Bool) -> String {
        let connection = UUID().uuidString.lowercased()
        guard beginConversationConnection(connection) else { return "{}" }
        var resume: ConversationResumeRegistration?
        if let account = conversationAccount,
            let chatId = conversationResumeStore.load(for: account)?.chatId
        {
            guard conversationResumeStore.save(chatId: chatId, for: account) else {
                return "{}"
            }
            activeChatId = chatId
            let request = UUID().uuidString.lowercased()
            if openConversationRequest(
                chatId: chatId,
                requestGeneration: request,
                purpose: .hydration)
            {
                resume = ConversationResumeRegistration(
                    activeChatId: chatId,
                    requestGeneration: request)
            }
        }
        let (width, height) = viewport
        var device = DeviceDescriptor.watch(viewportWidth: width, viewportHeight: height)
        device.deviceId = voiceDeviceId
        device.hasAudioOutput = true
        device.microphonePermission = voiceBridge.microphonePermission.rawValue
        device.fullDuplex = false
        device.voiceTransport = "watch_pcm_websocket"
        return Outbound.registerUI(
            token: token,
            sessionId: activeChatId,
            device: device,
            resumed: resumed,
            connectionGeneration: connection,
            resume: resume)
    }

    @discardableResult
    func beginConversationConnection(_ generation: String) -> Bool {
        clearPendingOperationSubmissions()
        transientEntries = []
        transientCanvas = nil
        guard continuity.beginConnection(generation) else { return false }
        reframePendingVoiceSubmissions(for: generation)
        voiceControlBinding = nil
        pendingVoiceActivation = nil
        voiceResumeTask?.cancel()
        voiceResumeTask = nil
        stopVoiceLeaseRenewal()
        voiceBridge.disconnect(reason: "ui_connection_replaced")
        if voiceSession != nil {
            voiceState = .reconnecting
            voiceReason = "network_interrupted"
            voiceMessage = "Reconnecting voice conversation…"
        }
        return true
    }

    @discardableResult
    func openConversationRequest(
        chatId: String,
        requestGeneration: String,
        purpose: ConversationGenerationPurpose
    ) -> Bool {
        let resetRevision =
            continuity.activeChatId != nil
            && continuity.activeChatId != chatId
        guard continuity.selectChat(chatId, resetRevision: resetRevision),
            continuity.openRequest(
                chatId: chatId,
                requestGeneration: requestGeneration,
                purpose: purpose)
        else { return false }
        activeChatId = chatId
        transientEntries = []
        transientCanvas = nil
        return true
    }

    var lastCommittedRenderRevision: UInt64 {
        continuity.lastCommittedRenderRevision
    }

    func bootstrap() async {
        // Feature 053 — listen for an endpoint override from the iPhone companion.
        // Opportunistic: activate() no-ops without a companion, and the observer
        // simply never fires. `deviceLogin`/`rest` are computed from `serverBase`,
        // so adopting a new endpoint rebuilds them on the next use.
        WatchOverrideSync.shared.activate()
        overrideObserver = NotificationCenter.default.addObserver(
            forName: WatchOverrideSync.didChangeNotification,
            object: nil, queue: .main
        ) { [weak self] _ in
            // Delivered on `queue: .main`, and WatchModel is MainActor-isolated,
            // so we are already where we need to be — no hop, no captured-var
            // concurrency warning.
            MainActor.assumeIsolated {
                self?.serverBase = WatchOverrideSync.resolvedServerBase()
            }
        }

        if let stored = store.load() {
            tokens = stored.tokenSet
            // Enter the signed-in home IMMEDIATELY: the WS dial starts now
            // and the register frame waits on the (single-flight) broker
            // refresh inside onConnect, so the two round trips overlap
            // instead of running back-to-back behind the QR spinner. An
            // offline launch keeps the stored session (sign in once per
            // device) — the home screen shows "Reconnecting…" and the WS
            // backoff loop registers when the network returns. Only a
            // definitive IdP rejection returns the watch to the QR screen.
            enterSignedIn()
            if case .rejected = await refreshOutcome() {
                await signOut(revokeRemote: false)  // ends at the QR screen
            }
            return
        }
        beginDeviceLogin()
    }

    // MARK: US3 — QR sign-in

    func beginDeviceLogin() {
        loginTask?.cancel()
        phase = .signedOut
        login = nil
        loginTask = Task { await runDeviceLogin() }
    }

    private func runDeviceLogin() async {
        while !Task.isCancelled {
            do {
                let start = try await deviceLogin.start()
                login = start
                loginExpiresAt = Date().addingTimeInterval(start.expiresIn)
                phase = .waitingApproval

                // Rotate to a fresh code shortly before expiry (FR-023). The
                // rotation timer must be a DIRECT child of the group: the
                // group awaits every child before returning, and a child that
                // awaits an outer Task's `.value` is uncancellable — it would
                // pin an approved sign-in to the full ~10-minute timer.
                let result: DeviceLoginPoll = try await withThrowingTaskGroup(of: DeviceLoginPoll?.self) { group in
                    group.addTask { try await self.deviceLogin.waitForApproval(start: start) }
                    group.addTask { [expiresIn = start.expiresIn] in
                        // Task.sleep is cancellation-aware; cancelAll() ends it.
                        try? await Task.sleep(nanoseconds: UInt64(max(expiresIn - 10, 5) * 1_000_000_000))
                        return nil
                    }
                    defer { group.cancelAll() }
                    while let next = try await group.next() {
                        if let terminal = next { return terminal }
                        return .expired  // rotation fired first
                    }
                    return .expired
                }

                switch result {
                case .approved(let set):
                    tokens = set
                    store.save(StoredTokens(from: set))
                    enterSignedIn()
                    return
                case .denied(let reason):
                    phase = .loginFailed(
                        reason == "denied_no_access"
                            ? "This account doesn't have access."
                            : "Sign-in was declined.")
                    return
                case .expired:
                    continue  // auto-rotate: fetch a fresh QR
                case .pending, .slowDown:
                    continue
                }
            } catch DeviceLoginError.unavailable(let detail) {
                phase = .unavailable(detail)
                return
            } catch is CancellationError {
                return
            } catch {
                phase = .unavailable("Can't reach the server.")
                return
            }
        }
    }

    // MARK: session

    /// Ensure a live access token via the backend broker, with failures
    /// classified (rejected → QR screen; transient/offline → keep session).
    /// SINGLE-FLIGHT: concurrent callers (the WS onConnect and the recents /
    /// audit REST tokenProvider) join one in-flight broker round trip — two
    /// parallel grants with the same rotating refresh token can revoke the
    /// whole session at the IdP.
    private func refreshOutcome() async -> RefreshResult {
        if let inFlight = refreshTask, refreshTaskGeneration == sessionGeneration {
            return await inFlight.value
        }
        guard let current = tokens else { return .rejected("no session") }
        if !current.needsRefresh() { return .ok(current) }
        return await runRefresh()
    }

    /// Start (and register) a refresh attempt unconditionally —
    /// `refreshOutcome` gates it behind expiry, `handleAuthRequired` forces it.
    private func runRefresh() async -> RefreshResult {
        guard let refresh = tokens?.refreshToken else { return .rejected("no refresh token") }
        let generation = sessionGeneration
        let broker = deviceLogin
        let attempt = Task { await RefreshStrategy.broker(broker).attempt(refreshToken: refresh) }
        refreshTask = attempt
        refreshTaskGeneration = generation
        let result = await attempt.value
        if refreshTaskGeneration == generation { refreshTask = nil }
        // A sign-out while the request was in flight ended this session —
        // never resurrect wiped credentials.
        if case .ok(let set) = result, generation == sessionGeneration {
            tokens = set
            store.save(StoredTokens(from: set))
        }
        return result
    }

    private func freshAccessToken() async -> String? {
        if case .ok(let set) = await refreshOutcome() { return set.accessToken }
        return nil
    }

    private func enterSignedIn() {
        accountName = tokens?.displayName ?? ""
        if let account = tokens?.conversationAccount {
            bindConversationAccount(account)
        } else {
            conversationAccount = nil
            continuity.clear()
            resetConversationState()
        }
        phase = .signedIn
        connectWS()
        // Recents load via WatchHomeView's `.task` the moment home appears
        // (it appears on every path into `.signedIn`) — no eager fetch here.
    }

    /// `revokeRemote: false` skips the server-side revocation round trip —
    /// used when the IdP has ALREADY refused the credential (nothing to
    /// revoke, and the call would only delay returning to the QR screen).
    func signOut(revokeRemote: Bool = true) async {
        // Snapshot remote-revocation inputs, then wipe the local session before
        // the first await. A suspended or killed watch app must never relaunch
        // into the account that was just signed out.
        let access = tokens?.accessToken
        let refresh = tokens?.refreshToken
        let logoutClient = RestClient(serverBase: serverBase) { access }
        let socket = ws
        let voiceSessionToEnd = voiceSession
        let voiceEndClient = access.flatMap { makeVoiceRESTClient(accessToken: $0) }
        if let account = conversationAccount {
            _ = conversationResumeStore.clear(.signOut, for: account)
        }
        sessionGeneration += 1
        refreshTask?.cancel()
        refreshTask = nil
        wsTask?.cancel()
        wsTask = nil
        ws = nil
        store.wipe()
        tokens = nil
        conversationAccount = nil
        continuity.clear()
        resetConversationState()
        clearPendingOperationSubmissions()
        statusLifecycle.clear()
        operationStatuses = [:]
        agentLifecycles = [:]
        recents = []
        resetVoiceState(reason: "sign_out")
        speaker.stop()
        beginDeviceLogin()

        // The local account is already gone; these network operations cannot
        // make the prior Keychain session durable again.
        await socket?.stop()
        if let voiceSessionToEnd, let voiceEndClient {
            try? await voiceEndClient.endSession(voiceSessionToEnd)
        }
        if revokeRemote, let refresh {
            _ = try? await logoutClient.logout(
                clientId: AstralConfig.watchClientId, refreshToken: refresh)
        }
    }

    func clearConversationForAccountRemoval() {
        let voiceSessionToEnd = voiceSession
        let voiceEndClient: WatchVoiceRESTClient?
        if let accessToken = tokens?.accessToken {
            voiceEndClient = makeVoiceRESTClient(accessToken: accessToken)
        } else {
            voiceEndClient = makeVoiceRESTClient()
        }
        if let account = conversationAccount {
            _ = conversationResumeStore.clear(.accountRemoval, for: account)
        }
        conversationAccount = nil
        continuity.clear()
        resetConversationState()
        clearPendingOperationSubmissions()
        statusLifecycle.clear()
        operationStatuses = [:]
        agentLifecycles = [:]
        resetVoiceState(reason: "account_removed")
        if let voiceSessionToEnd, let voiceEndClient {
            Task { try? await voiceEndClient.endSession(voiceSessionToEnd) }
        }
    }

    // MARK: WS

    private var viewport: (Int, Int) {
        #if os(watchOS)
            let bounds = WKInterfaceDevice.current().screenBounds
            return (Int(bounds.width), Int(bounds.height))
        #else
            return (198, 242)
        #endif
    }

    private func connectWS() {
        wsTask?.cancel()
        let client = WSClient(url: rest.webSocketURL)
        ws = client
        let resumeState = WatchRegistrationResumeState()
        wsTask = Task {
            let events = await client.events()
            await client.start(
                onConnect: { [weak self] in
                    guard let self else { return nil }
                    guard let token = await self.freshAccessToken() else { return nil }
                    let resumed = await resumeState.consume()
                    return await self.registrationFrame(token: token, resumed: resumed)
                },
                onReplay: { [weak self] replay in
                    guard let self else { return false }
                    return await self.replayQueuedOperation(replay)
                })
            for await event in events {
                await self.handle(event)
            }
        }
    }

    func handle(_ event: WSEvent) async {
        switch event {
        case .connected:
            connected = true
        case .disconnected:
            connected = false
            clearPendingOperationSubmissions()
            statusText = nil
            statusShowsActivity = false
            transientEntries = []
            transientCanvas = nil
            voiceControlBinding = nil
            voiceResumeTask?.cancel()
            voiceResumeTask = nil
            stopVoiceLeaseRenewal()
            voiceBridge.disconnect(reason: "ui_socket_disconnected")
            if voiceSession != nil {
                voiceState = .reconnecting
                voiceReason = "network_interrupted"
                voiceMessage = "Reconnecting voice conversation…"
            }
        case .sendDropped:
            errorBanner = "Connection is behind; some input was dropped."
        case .queuedOperationDropped(let replay, let reason):
            localOperationSubmissions.removeValue(forKey: replay.identity.submissionId)
            if localOperationSubmissions.isEmpty && statusText == "Submitting…" {
                statusText = nil
                statusShowsActivity = false
            }
            errorBanner = "Not sent: \(replay.action) (\(reason))"
        case .sendRejected(let action):
            clearPendingOperationSubmissions()
            errorBanner = "Not sent: \(action) (invalid queued identity)"
        case .frame(let frame):
            handleFrame(frame)
        }
    }

    func handleFrame(_ frame: InboundFrame) {
        // Dispositions: ClientDispositions.watch — unlisted/ignored frames
        // fall through the default silently (FR-003).
        switch frame.name {
        case "composer_state":
            consumeVoiceComposer(frame)
            return
        case "voice_control_binding":
            consumeVoiceControlBinding(frame)
            return
        case "voice_session_state":
            consumeVoiceSessionState(frame)
            return
        case "voice_turn_state":
            consumeVoiceTurnState(frame)
            return
        case "voice_submission_rejected":
            consumeVoiceSubmissionRejected(frame)
            return
        default:
            break
        }
        if continuity.connectionGeneration != nil,
            ["ui_render", "ui_update", "ui_upsert", "ui_append", "ui_stream_data"]
                .contains(frame.name)
        {
            reduceTransient(frame)
            return
        }
        switch frame.name {
        case "conversation_snapshot":
            reduceConversationSnapshot(frame)
        case "conversation_commit_ready":
            if let ready = ConversationCommitReady(frame: frame), continuity.accept(ready) {
                transientEntries = []
                transientCanvas = nil
            }
        case "ui_render":
            let comps = frame.renderComponents
            guard !comps.isEmpty else { return }
            if frame.renderTarget == "chat" {
                // End-of-turn narrative — a transcript entry, NOT a canvas
                // replacement: clobbering here wiped the components the
                // ui_upsert just delivered (iOS diverts the same way).
                entries.append(.turn(id: "turn-\(entries.count)", components: comps))
            } else {
                canvas = comps
            }
            statusText = nil
            statusShowsActivity = false
            speakLegacy(frame.speech)
        case "ui_upsert":
            let ops = frame.upsertOps
            guard !ops.isEmpty else { return }
            // 055 uniform rule: the watch has no turn state, so the ephemeral
            // welcome (`wel_` identities) is purged whenever ops land — turn
            // content must never render under a retained welcome (the empty
            // blanking render was always dropped by the guard above).
            canvas = Canvas.apply(canvas.dropWelcome(), ops)
            speakLegacy(frame.speech)
        case "ui_stream_data":
            if let text = frame.streamComponents.first?.textContent {
                statusText = text
                statusShowsActivity = true
            }
        case "chat_status":
            statusText = frame.statusText
            statusShowsActivity = ["thinking", "executing", "fixing", "processing_async"]
                .contains(frame.payload["status"]?.stringValue)
        case "chat_step":
            statusText = frame.statusText
            statusShowsActivity = frame.payload["step"]?["status"]?.stringValue == "in_progress"
        case "operation_status":
            reduceOperationStatus(frame)
        case "agent_lifecycle":
            reduceAgentLifecycle(frame)
        case "user_message_acked":
            consumeVoiceMessageAcknowledgement(frame)
            if let chatId = nestedChatId(frame) { adoptChat(chatId) }
            statusText = "Thinking…"
            statusShowsActivity = true
        case "chat_created":
            if consumeVoiceChatCreated(frame) { return }
            // Adopt the server-issued chat id; the transcript the user is
            // looking at (their just-sent bubble) must NOT be wiped.
            if let chatId = nestedChatId(frame) { adoptChat(chatId) }
        case "chat_loaded":
            if continuity.connectionGeneration == nil {
                reduceChatLoaded(frame)
            }
        case "chat_deleted":
            if let chatId = nestedChatId(frame) { clearConfirmedDeletion(chatId) }
        case "error":
            if reduceAdmissionRefusal(frame) { return }
            fallthrough
        case "stream_error":
            errorBanner = frame.errorMessage
            statusText = nil
            statusShowsActivity = false
            transientEntries = []
            transientCanvas = nil
            let code = frame.payload["code"]?.stringValue
            if code == "chat_not_found" || code == "conversation_not_found" {
                if let chatId = nestedChatId(frame) ?? activeChatId {
                    clearConfirmedDeletion(chatId)
                    errorBanner = frame.errorMessage
                }
            }
        case "notification":
            // 055 background-task continuity (audit item 7): a completion that
            // happened elsewhere reaches the wrist as a brief status line and
            // is spoken through the same TTS path as delivery speech.
            let titled = [frame.payload["title"]?.stringValue, frame.payload["body"]?.stringValue]
                .compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: ": ")
            let message = titled.isEmpty ? (frame.payload["message"]?.stringValue ?? "") : titled
            guard !message.isEmpty else { return }
            statusText = message
            statusShowsActivity = false
            speakLegacy(AstralSpeech(ssml: "", text: message))
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: 8_000_000_000)
                guard let self, self.statusText == message, !self.statusShowsActivity else { return }
                self.statusText = nil  // brief: clear unless something replaced it
            }
        case "auth_required":
            Task { await self.handleAuthRequired() }
        default:
            break
        }
    }

    private func reduceOperationStatus(_ frame: InboundFrame) {
        guard let status = OperationStatus(frame: frame),
            statusLifecycle.accept(
                operation: status,
                connectionGeneration: continuity.connectionGeneration,
                conversationRequestGeneration: continuity.requestGeneration,
                activeChatId: activeChatId,
                pendingChatRequestGenerations: pendingChatRequestGenerations,
                pendingSurfaceRequestGenerations: pendingSurfaceRequestGenerations)
        else { return }
        operationStatuses = statusLifecycle.operations
        if status.terminal {
            clearLocalOperationSubmission(requestGeneration: status.requestGeneration)
            if let message = status.error.objectValue?["message"]?.stringValue {
                errorBanner = message
                transientEntries = []
                transientCanvas = nil
            }
            statusText = latestActiveOperationStatusText()
            statusShowsActivity = statusText != nil
        } else {
            statusText = status.label
            statusShowsActivity = true
        }
    }

    private func latestActiveOperationStatusText() -> String? {
        var pendingGenerations = pendingChatRequestGenerations.union(
            pendingSurfaceRequestGenerations)
        if let currentRequest = continuity.requestGeneration {
            pendingGenerations.insert(currentRequest)
        }
        let active = operationStatuses.values.filter {
            !$0.terminal && pendingGenerations.contains($0.requestGeneration)
        }.max {
            ($0.updatedAt, $0.sequence) < ($1.updatedAt, $1.sequence)
        }
        if let active {
            return active.label
        }
        return localOperationSubmissions.isEmpty ? nil : "Submitting…"
    }

    private func reduceAgentLifecycle(_ frame: InboundFrame) {
        guard let lifecycle = AgentLifecycle(frame: frame),
            statusLifecycle.accept(lifecycle: lifecycle)
        else { return }
        agentLifecycles = statusLifecycle.agents
        let message = "\(lifecycle.agentId): \(lifecycle.label)"
        statusText = message
        statusShowsActivity = false
        if lifecycle.state == "failed" {
            errorBanner = message
        }
    }

    @discardableResult
    private func reduceAdmissionRefusal(_ frame: InboundFrame) -> Bool {
        guard let refusal = AdmissionRefusal(frame: frame),
            localOperationSubmissions.removeValue(forKey: refusal.submissionId) != nil
        else { return false }
        statusText = latestActiveOperationStatusText()
        statusShowsActivity = statusText != nil
        errorBanner = refusal.message
        return true
    }

    private func reduceConversationSnapshot(_ frame: InboundFrame) {
        guard let snapshot = ConversationSnapshot(frame: frame),
            continuity.apply(snapshot) == .applied
        else { return }
        if let account = conversationAccount {
            _ = conversationResumeStore.save(chatId: snapshot.chatId, for: account)
        }

        var restored: [Entry] = []
        for message in snapshot.messages {
            if message.role == "user" {
                restored.append(
                    .user(
                        id: message.messageId,
                        text: message.visibleText,
                        attachments: message.attachmentNames))
                continue
            }
            let narrative = message.visibleText
            if !narrative.isEmpty {
                restored.append(.status(id: message.messageId, text: narrative))
            }
            if !message.components.isEmpty {
                restored.append(
                    .turn(
                        id: "\(message.messageId)-components",
                        components: message.components))
            }
        }

        activeChatId = snapshot.chatId
        entries = restored
        canvas = snapshot.canvasComponents
        transientEntries = []
        transientCanvas = nil
        statusText = nil
        statusShowsActivity = false
        pendingCommitRequestGeneration = nil
    }

    private func reduceTransient(_ frame: InboundFrame) {
        guard continuity.acceptTransient(frame) else { return }
        switch frame.name {
        case "ui_render", "ui_update":
            let components = frame.renderComponents
            if frame.renderTarget == "chat" {
                let pendingUsers = transientEntries.filter {
                    if case .user = $0 { return true }
                    return false
                }
                let response: [Entry] =
                    components.isEmpty
                    ? []
                    : [
                        .turn(
                            id: "preview-\(frame.payload["frame_sequence"]?.numberValue ?? 0)",
                            components: components)
                    ]
                transientEntries = pendingUsers + response
            } else {
                transientCanvas = components
            }
            speakLegacy(frame.speech)
        case "ui_append":
            let components = frame.renderComponents
            if frame.renderTarget == "chat" {
                if !components.isEmpty {
                    transientEntries.append(
                        .turn(
                            id: "preview-\(frame.payload["frame_sequence"]?.numberValue ?? 0)",
                            components: components))
                }
            } else {
                transientCanvas = (transientCanvas ?? canvas) + components
            }
            speakLegacy(frame.speech)
        case "ui_upsert":
            transientCanvas = Canvas.apply(transientCanvas ?? canvas, frame.upsertOps)
            speakLegacy(frame.speech)
        case "ui_stream_data":
            transientCanvas = Canvas.apply(
                transientCanvas ?? canvas,
                streamFrameToOps(frame, activeChat: activeChatId, seqState: &seqState))
        default:
            break
        }
    }

    private func nestedChatId(_ frame: InboundFrame) -> String? {
        frame.payload["payload"]?["chat_id"]?.stringValue
            ?? frame.payload["chat_id"]?.stringValue
    }

    private func speakLegacy(_ speech: AstralSpeech?) {
        guard voiceSession == nil else { return }
        speaker.speak(speech)
    }

    private func adoptChat(_ chatId: String) {
        if let account = conversationAccount {
            _ = conversationResumeStore.save(chatId: chatId, for: account)
        }
        activeChatId = chatId
        if voiceSession?.visibleChatId != chatId {
            voiceBridge.setCaptureEnabled(false)
            Task { await self.updateVoiceVisibleChat(chatId) }
        }
        guard let request = pendingCommitRequestGeneration else { return }
        if openConversationRequest(
            chatId: chatId,
            requestGeneration: request,
            purpose: .commit)
        {
            pendingCommitRequestGeneration = nil
        }
    }

    private func clearConfirmedDeletion(_ chatId: String) {
        if let account = conversationAccount {
            _ = conversationResumeStore.clear(
                .confirmedDeletion,
                for: account,
                chatId: chatId)
        }
        if voiceSession?.visibleChatId == chatId {
            endVoiceConversation(reason: "chat_deleted")
        }
        guard activeChatId == chatId else { return }
        clearContinuityChatKeepingConnection()
        resetConversationState()
    }

    private func clearContinuityChatKeepingConnection() {
        let connection = continuity.connectionGeneration
        continuity.clear()
        if let connection {
            _ = continuity.beginConnection(connection)
        }
    }

    /// The server refused our token. A near-expiry token refreshes anyway on
    /// the normal path, so join the in-flight refresh if one is running,
    /// otherwise FORCE a broker refresh: an unchanged/refused credential
    /// means the session is dead server-side (revoked / hard cap) — wipe and
    /// return to the QR screen instead of looping reconnects.
    private func handleAuthRequired() async {
        guard let refused = tokens?.accessToken else {
            await signOut()
            return
        }
        let result: RefreshResult
        if let inFlight = refreshTask, refreshTaskGeneration == sessionGeneration {
            result = await inFlight.value
        } else {
            result = await runRefresh()
        }
        switch result {
        case .ok(let set) where set.accessToken != refused:
            break  // the reconnect loop re-registers with the fresh token
        case .ok, .rejected:
            await signOut()  // same/refused token — the session is dead server-side
        case .transient:
            break  // offline blip; keep the session and retry
        }
    }

    /// Re-hydrate a loaded transcript: user text with read-only attachment
    /// name-chips (FR-033/T049 — the watch has no upload affordance) and
    /// assistant narrative. Rich canvas content arrives right after as a
    /// speech-free `ui_render` (the server re-hydrates the workspace).
    private func reduceChatLoaded(_ frame: InboundFrame) {
        let chat = frame.payload["chat"]
        activeChatId = chat?["id"]?.stringValue ?? activeChatId
        if let account = conversationAccount, let activeChatId {
            _ = conversationResumeStore.save(chatId: activeChatId, for: account)
        }
        canvas = []  // the server re-hydrates the workspace via ui_render next
        let messages = chat?["messages"]?.arrayValue ?? chat?["history"]?.arrayValue ?? []
        var loaded: [Entry] = []
        for (index, message) in messages.enumerated() {
            let role =
                message["role"]?.stringValue
                ?? (message["is_user"]?.boolValue == true ? "user" : "assistant")
            let text = message["content"]?.stringValue ?? message["text"]?.stringValue ?? ""
            if role == "user" {
                let names = (message["attachments"]?.arrayValue ?? [])
                    .compactMap { $0["filename"]?.stringValue }
                if !text.isEmpty || !names.isEmpty {
                    loaded.append(.user(id: "hist-\(index)", text: text, attachments: names))
                }
            } else if !text.isEmpty {
                loaded.append(.status(id: "hist-\(index)", text: text))
            }
        }
        entries = loaded
        statusText = nil
        statusShowsActivity = false
    }

    // MARK: US4 — conversation

    func refreshRecents() async {
        recents = Array((try? await rest.chats())?.prefix(10) ?? [])
    }

    private func resetConversationState() {
        entries = []
        canvas = []
        transientEntries = []
        transientCanvas = nil
        activeChatId = nil
        statusText = nil
        statusShowsActivity = false
        errorBanner = nil
        pendingCommitRequestGeneration = nil
        seqState.removeAll()
    }

    private func beginLocalOperationSubmission(
        identity: ClientOperationIdentity,
        action: String,
        surface: String,
        chatId: String?
    ) {
        guard let connectionGeneration = continuity.connectionGeneration,
            let submission = LocalOperationSubmission(
                identity: identity,
                action: action,
                surface: surface,
                chatId: chatId,
                connectionGeneration: connectionGeneration)
        else { return }
        localOperationSubmissions[submission.submissionId] = submission
        statusText = submission.label
        statusShowsActivity = true
    }

    /// Restore the exact client identity and current connection fence before
    /// shared transport replays retained bytes.
    @discardableResult
    func replayQueuedOperation(_ replay: QueuedOperationReplay) -> Bool {
        guard let connectionGeneration = continuity.connectionGeneration else { return false }
        if let purpose = replay.conversationPurpose {
            if let chatId = replay.chatId {
                guard
                    openConversationRequest(
                        chatId: chatId,
                        requestGeneration: replay.identity.requestGeneration,
                        purpose: purpose)
                else { return false }
            } else if purpose == .commit {
                pendingCommitRequestGeneration = replay.identity.requestGeneration
            } else {
                return false
            }
        }
        guard
            let submission = LocalOperationSubmission(
                identity: replay.identity,
                action: replay.action,
                surface: replay.surface,
                chatId: replay.chatId,
                connectionGeneration: connectionGeneration)
        else { return false }
        localOperationSubmissions[submission.submissionId] = submission
        statusText = submission.label
        statusShowsActivity = true
        return true
    }

    private func clearLocalOperationSubmission(requestGeneration: String) {
        localOperationSubmissions = localOperationSubmissions.filter {
            $0.value.requestGeneration != requestGeneration
        }
    }

    private func clearPendingOperationSubmissions() {
        let wasSubmitting = statusText == "Submitting…"
        localOperationSubmissions.removeAll()
        if wasSubmitting {
            statusText = nil
            statusShowsActivity = false
        }
    }

    private func rawSend(_ frame: String) {
        outboundTap?(frame)
        Task { await ws?.send(frame) }
    }

    /// Voice submissions and content-free playout evidence are fenced to the
    /// currently established UI socket. The voice controller owns transcript
    /// retry; none of these frames may enter the generic offline replay queue.
    private func sendCurrentConnectionVoice(_ frame: String) {
        guard connected, VoiceCurrentConnectionFrame(frameText: frame) != nil else { return }
        outboundTap?(frame)
        if let override = currentConnectionVoiceSendOverride {
            override(frame)
        } else if let ws {
            Task { _ = await ws.sendCurrentConnectionVoice(frame) }
        }
    }

    func newConversation() {
        pendingVoiceActivation = nil
        if voiceSession != nil {
            voiceBridge.setCaptureEnabled(false)
            voiceMessage = "Select the new chat before speaking."
        }
        if let account = conversationAccount {
            _ = conversationResumeStore.clear(.newChat, for: account)
        }
        clearContinuityChatKeepingConnection()
        resetConversationState()
        let identity = ClientOperationIdentity.fresh()
        beginLocalOperationSubmission(
            identity: identity,
            action: "new_chat",
            surface: "operation",
            chatId: nil)
        rawSend(
            Outbound.newChat(
                sessionId: nil,
                submissionId: identity.submissionId,
                requestGeneration: identity.requestGeneration))
    }

    func openChat(_ chat: ChatSummary) {
        pendingVoiceActivation = nil
        if let account = conversationAccount {
            guard conversationResumeStore.save(chatId: chat.id, for: account) else { return }
        }
        activeChatId = chat.id
        let identity = ClientOperationIdentity.fresh()
        let request = identity.requestGeneration
        if continuity.connectionGeneration != nil {
            guard
                openConversationRequest(
                    chatId: chat.id,
                    requestGeneration: request,
                    purpose: .hydration)
            else { return }
        }
        beginLocalOperationSubmission(
            identity: identity,
            action: "load_chat",
            surface: "chat",
            chatId: chat.id)
        rawSend(
            Outbound.loadChat(
                sessionId: chat.id,
                chatId: chat.id,
                submissionId: identity.submissionId,
                requestGeneration: request))
        if voiceSession?.visibleChatId != chat.id {
            voiceBridge.setCaptureEnabled(false)
            Task { await self.updateVoiceVisibleChat(chat.id) }
        }
    }

    /// Dictated text goes through the STANDARD chat path (FR-029) after the
    /// user confirms it (edge case: garbled dictation never auto-sends).
    func sendPending() {
        let text = pendingDictation.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let identity = ClientOperationIdentity.fresh()
        let request = identity.requestGeneration
        if continuity.connectionGeneration != nil {
            if let chatId = activeChatId {
                guard
                    openConversationRequest(
                        chatId: chatId,
                        requestGeneration: request,
                        purpose: .commit)
                else { return }
            } else {
                pendingCommitRequestGeneration = request
            }
        }
        pendingDictation = ""
        let entry = Entry.user(
            id: "pending-user-\(UUID().uuidString.lowercased())",
            text: text,
            attachments: [])
        if continuity.connectionGeneration == nil {
            entries.append(entry)
        } else {
            transientEntries.append(entry)
        }
        beginLocalOperationSubmission(
            identity: identity,
            action: "chat_message",
            surface: "chat",
            chatId: activeChatId)
        rawSend(
            Outbound.chatMessage(
                text,
                sessionId: activeChatId,
                submissionId: identity.submissionId,
                requestGeneration: request))
    }

    // MARK: Feature 065 — conversational voice

    func performVoiceAction(_ action: String) {
        guard let control = visibleVoiceControls.first(where: { $0.action == action }),
            control.enabled, !control.busy, !voiceActivationBusy
        else { return }
        switch action {
        case "voice_session_start":
            Task { await self.beginVoiceActivation(takeover: false) }
        case "voice_session_takeover":
            Task { await self.beginVoiceActivation(takeover: true) }
        case "voice_session_end":
            endVoiceConversation(reason: "ended_by_user")
        case "voice_microphone_set":
            Task { await self.setVoiceMicrophone(!(self.voiceSession?.microphoneEnabled ?? false)) }
        case "voice_speech_mute_set":
            Task { await self.setVoiceSpeechMuted(!(self.voiceSession?.speechMuted ?? false)) }
        case "voice_speech_stop":
            stopVoiceSpeech()
        case "voice_visible_chat_update":
            guard let chatId = activeChatId else { return }
            Task { await self.updateVoiceVisibleChat(chatId) }
        case "voice_sensitive_recap_request":
            Task { await self.consentSensitiveVoiceRecap() }
        default:
            break
        }
    }

    func performPrimaryVoiceAction() {
        guard let primaryVoiceControl else { return }
        performVoiceAction(primaryVoiceControl.action)
    }

    func handleVoiceScenePhase(_ phase: ScenePhase) {
        switch phase {
        case .active:
            guard !voiceForegroundActive else { return }
            voiceForegroundActive = true
            scheduleVoiceResume()
        case .inactive, .background:
            guard voiceForegroundActive else { return }
            voiceForegroundActive = false
            voiceResumeTask?.cancel()
            voiceResumeTask = nil
            stopVoiceLeaseRenewal()
            suspendVoiceImmediately(reason: "backgrounded")
        @unknown default:
            suspendVoiceImmediately(reason: "backgrounded")
        }
    }

    private func consumeVoiceComposer(_ frame: InboundFrame) {
        guard
            let composer = WatchVoiceComposer(
                frame: frame,
                expectedConnection: continuity.connectionGeneration)
        else { return }
        let shouldAcceptComposer =
            voiceComposer.map {
                composer.connectionGeneration != $0.connectionGeneration
                    || composer.revision > $0.revision
            } ?? true
        guard shouldAcceptComposer else { return }
        voiceComposer = composer
        voiceState = composer.state
        voiceReason = composer.reason
        voiceMessage = composer.message
        if composer.state == .off || composer.state == .ended {
            voiceTerminalNotice = nil
            currentVoiceTurnId = nil
            currentVoiceTurnOccurredAt = nil
        } else if composer.reason == VoiceReason.speechError.rawValue {
            voiceTerminalNotice = VoiceTerminalNoticeReducer.speechFailure(
                message: composer.message, turnId: currentVoiceTurnId,
                occurredAt: voiceTerminalNotice?.occurredAt ?? currentVoiceTurnOccurredAt)
        }
        if composer.state == .ended {
            voiceBridge.disconnect(reason: "server_ended")
            voiceSession = nil
            voiceGrant = nil
        }
    }

    private func consumeVoiceControlBinding(_ frame: InboundFrame) {
        guard
            let binding = WatchVoiceControlBinding(
                frame: frame,
                expectedDeviceId: voiceDeviceId,
                expectedConnection: continuity.connectionGeneration)
        else { return }
        voiceControlBinding = binding
        if voiceSession != nil, voiceForegroundActive, voiceState == .reconnecting {
            scheduleVoiceResume()
        }
    }

    private func consumeVoiceSessionState(_ frame: InboundFrame) {
        guard let update = VoiceSessionState(frame: frame),
            update.connectionGeneration == continuity.connectionGeneration,
            let session = voiceSession,
            update.sessionId == session.sessionId,
            UInt64(update.generation) == session.generation,
            UInt64(update.mediaGrantRevision) == session.mediaGrantRevision,
            let state = WatchVoiceState(rawValue: update.state.rawValue)
        else { return }
        voiceState = state
        voiceReason = update.reason.rawValue
        voiceMessage = update.message
        if state == .ended {
            voiceTerminalNotice = nil
            currentVoiceTurnId = nil
            currentVoiceTurnOccurredAt = nil
        } else if update.reason == .speechError {
            voiceTerminalNotice = VoiceTerminalNoticeReducer.speechFailure(
                message: update.message, turnId: currentVoiceTurnId,
                occurredAt: voiceTerminalNotice?.occurredAt ?? currentVoiceTurnOccurredAt)
        }
        if update.foregroundActive && update.microphoneEnabled && update.chatContextSynced
            && voiceForegroundActive && state.active
        {
            voiceBridge.setCaptureEnabled(true)
        } else {
            voiceBridge.setCaptureEnabled(false)
        }
        switch state {
        case .reconnecting:
            voiceBridge.disconnect(reason: update.reason.rawValue)
            scheduleVoiceResume()
        case .suspended, .error, .ended:
            voiceBridge.disconnect(reason: update.reason.rawValue)
            if state == .ended {
                voiceSession = nil
                voiceGrant = nil
            }
        default:
            break
        }
    }

    private func consumeVoiceTurnState(_ frame: InboundFrame) {
        guard let turn = VoiceTurnState(frame: frame),
            turn.connectionGeneration == continuity.connectionGeneration,
            let session = voiceSession,
            turn.sessionId == session.sessionId,
            UInt64(turn.generation) == session.generation,
            UInt64(turn.mediaGrantRevision) == session.mediaGrantRevision,
            VoiceTerminalNoticeReducer.canApply(
                current: voiceTerminalNotice, turnId: turn.turnId,
                occurredAt: turn.occurredAt),
            voiceTurnSequences[turn.turnId].map({ turn.sequence > $0 }) ?? true
        else { return }
        voiceTurnSequences[turn.turnId] = turn.sequence
        currentVoiceTurnId = turn.turnId
        currentVoiceTurnOccurredAt = turn.occurredAt
        voiceTerminalNotice = VoiceTerminalNoticeReducer.reduce(
            current: voiceTerminalNotice, turn: turn)
        let speechFailed = turn.state == "succeeded" && turn.speechOutcome == .failed
        if turn.sensitiveResultPending, turn.resultId != nil {
            currentSensitiveVoiceTurn = turn
        } else if currentSensitiveVoiceTurn?.turnId == turn.turnId {
            currentSensitiveVoiceTurn = nil
        }
        voiceMessage = speechFailed ? voiceTerminalNotice?.displayText : turn.message
        if speechFailed {
            voiceState = .error
            voiceReason = VoiceReason.speechError.rawValue
            return
        }
        switch turn.state {
        case "recognizing": voiceState = .transcribing
        case "submitting": voiceState = .acknowledging
        case "accepted", "processing": voiceState = .processing
        case "waiting_on_user": voiceState = .waitingOnUser
        case "succeeded": voiceState = .speakingResult
        case "failed", "refused": voiceState = .error
        case "cancelled", "abandoned": voiceState = .listening
        default: return
        }
    }

    private func consumeVoiceMessageAcknowledgement(_ frame: InboundFrame) {
        guard let acknowledgement = VoiceMessageAcknowledgement(frame: frame),
            acknowledgement.connectionGeneration == continuity.connectionGeneration,
            let turnId = acknowledgement.voiceTurnId,
            let pending = pendingVoiceSubmissions[turnId],
            acknowledgement.chatId == pending.transcript.chatId,
            acknowledgement.submissionId == pending.transcript.submissionId,
            acknowledgement.requestGeneration == pending.transcript.requestGeneration
        else { return }
        clearPendingVoiceSubmission(turnId)
        voicePartialTranscript = nil
    }

    private func consumeVoiceSubmissionRejected(_ frame: InboundFrame) {
        guard let rejection = VoiceSubmissionRejected(frame: frame),
            rejection.connectionGeneration == continuity.connectionGeneration,
            let pending = pendingVoiceSubmissions[rejection.turnId],
            rejection.sessionId == pending.transcript.sessionId,
            UInt64(rejection.generation) == pending.transcript.generation,
            UInt64(rejection.mediaGrantRevision) == pending.transcript.mediaGrantRevision,
            rejection.clientTurnId == pending.transcript.clientTurnId,
            rejection.submissionId == pending.transcript.submissionId,
            rejection.requestGeneration == pending.transcript.requestGeneration,
            rejection.chatId == pending.transcript.chatId
        else { return }
        clearPendingVoiceSubmission(rejection.turnId)
        guard
            VoiceTerminalNoticeReducer.canApply(
                current: voiceTerminalNotice, turnId: rejection.turnId,
                occurredAt: rejection.occurredAt)
        else { return }
        currentVoiceTurnId = rejection.turnId
        currentVoiceTurnOccurredAt = rejection.occurredAt
        voiceTerminalNotice = VoiceTerminalNoticeReducer.reduce(
            current: voiceTerminalNotice, rejection: rejection)
        voiceState = .error
        voiceReason = rejection.reason
        voiceMessage = voiceTerminalNotice?.displayText
    }

    @discardableResult
    private func consumeVoiceChatCreated(_ frame: InboundFrame) -> Bool {
        guard let pending = pendingVoiceActivation,
            let root = frame.payload.objectValue,
            root["submission_id"]?.stringValue == pending.submissionId,
            root["request_generation"]?.stringValue == pending.requestGeneration
        else { return false }
        pendingVoiceActivation = nil
        guard root["schema_version"]?.stringValue == "1",
            root["connection_generation"]?.stringValue == pending.connectionGeneration,
            continuity.connectionGeneration == pending.connectionGeneration,
            activeChatId == pending.selectedChatAtRequest,
            let payload = root["payload"]?.objectValue,
            payload["schema_version"]?.stringValue == "1",
            payload["connection_generation"]?.stringValue == pending.connectionGeneration,
            payload["submission_id"]?.stringValue == pending.submissionId,
            payload["request_generation"]?.stringValue == pending.requestGeneration,
            payload["from_message"]?.boolValue == false,
            let chatId = payload["chat_id"]?.stringValue
        else {
            voiceActivationBusy = false
            voiceMessage = "Voice start was cancelled because the chat changed."
            return true
        }
        adoptChat(chatId)
        Task {
            await self.activateVoiceSession(
                chatId: chatId,
                activationId: pending.activationId,
                takeover: pending.takeover)
        }
        return true
    }

    private func beginVoiceActivation(takeover: Bool) async {
        guard voiceForegroundActive, connected,
            let connection = continuity.connectionGeneration,
            voiceControlBinding?.connectionGeneration == connection
        else {
            voiceState = .unavailable
            voiceReason = "network_interrupted"
            voiceMessage = "Voice will be available after the chat reconnects."
            return
        }
        voiceActivationBusy = true
        voiceState = .connecting
        voiceMessage = "Preparing voice conversation…"
        let permission = await voiceBridge.requestMicrophonePermission()
        guard permission == .authorized else {
            voiceActivationBusy = false
            voiceState = .unavailable
            voiceReason = permission == .restricted ? "permission_restricted" : "permission_denied"
            voiceMessage = "Microphone permission is required for voice conversation."
            return
        }
        let activationId = UUID().uuidString.lowercased()
        guard let chatId = activeChatId else {
            let submissionId = UUID().uuidString.lowercased()
            let requestGeneration = UUID().uuidString.lowercased()
            pendingVoiceActivation = PendingVoiceActivation(
                activationId: activationId,
                submissionId: submissionId,
                requestGeneration: requestGeneration,
                connectionGeneration: connection,
                selectedChatAtRequest: nil,
                takeover: takeover)
            guard
                let frame = watchVoiceJSON([
                    "type": .string("ui_event"),
                    "action": .string("new_chat"),
                    "schema_version": .string("1"),
                    "connection_generation": .string(connection),
                    "submission_id": .string(submissionId),
                    "request_generation": .string(requestGeneration),
                    "payload": .object([
                        "schema_version": .string("1"),
                        "connection_generation": .string(connection),
                        "submission_id": .string(submissionId),
                        "request_generation": .string(requestGeneration),
                    ]),
                ])
            else {
                pendingVoiceActivation = nil
                voiceActivationBusy = false
                voiceState = .error
                voiceReason = "internal_error"
                return
            }
            sendCurrentConnectionVoice(frame)
            voiceMessage = "Creating a chat for voice…"
            return
        }
        await activateVoiceSession(
            chatId: chatId,
            activationId: activationId,
            takeover: takeover)
    }

    private func activateVoiceSession(
        chatId: String,
        activationId: String,
        takeover: Bool
    ) async {
        defer { voiceActivationBusy = false }
        guard let client = makeVoiceRESTClient() else {
            voiceState = .unavailable
            voiceReason = "authentication_required"
            voiceMessage = "Voice control authorization expired. Reconnect and try again."
            return
        }
        do {
            let result: WatchVoiceSessionGrant
            if takeover {
                guard let sessionId = voiceComposer?.sessionId,
                    let generation = voiceComposer?.generation,
                    let revision = voiceComposer?.mediaGrantRevision
                else { throw WatchVoiceRESTError.invalidRequest }
                result = try await client.takeOverSession(
                    sessionId: sessionId,
                    chatId: chatId,
                    activationId: activationId,
                    expectedGeneration: generation,
                    expectedMediaGrantRevision: revision,
                    permission: .authorized)
            } else {
                result = try await client.createSession(
                    chatId: chatId,
                    activationId: activationId,
                    permission: .authorized)
            }
            try await installVoiceSession(result)
        } catch WatchVoiceRESTError.refused(let status, let code) {
            voiceState = status == 409 ? .unavailable : .error
            voiceReason = status == 409 ? "takeover_required" : safeVoiceReason(code)
            voiceMessage =
                status == 409
                ? "Voice is active on another device. Choose Take Over to continue here."
                : "Voice conversation could not start."
        } catch {
            voiceState = .error
            voiceReason = "media_unavailable"
            voiceMessage = "Voice media is unavailable right now."
        }
    }

    private func installVoiceSession(_ result: WatchVoiceSessionGrant) async throws {
        guard result.session.deviceId == voiceDeviceId,
            result.session.ownerConnectionGeneration == continuity.connectionGeneration,
            result.session.sessionId == result.grant.sessionId
        else { throw WatchVoiceRESTError.malformedResponse }
        if voiceSession?.sessionId != result.session.sessionId
            || voiceSession?.generation != result.session.generation
        {
            voiceTurnSequences.removeAll()
            currentVoiceTurnId = nil
            currentVoiceTurnOccurredAt = nil
            currentSensitiveVoiceTurn = nil
        }
        voiceSession = result.session
        voiceGrant = result.grant
        voiceState = .connecting
        voiceReason = "ready"
        voiceMessage = "Connecting voice conversation…"
        speaker.stop()
        try await voiceBridge.connect(
            grant: result.grant,
            onState: { [weak self] state in self?.consumeVoiceBridgeState(state) },
            onTranscript: { [weak self] transcript in self?.consumeVoiceTranscript(transcript) },
            onPlayout: { [weak self] observation in self?.sendVoicePlayout(observation) })
        voiceState = .greeting
        voiceMessage = nil
        voiceBridge.setCaptureEnabled(
            result.session.foregroundActive && result.session.microphoneEnabled
                && result.session.chatContextSynced && voiceForegroundActive)
        startVoiceLeaseRenewal()
    }

    private func consumeVoiceBridgeState(_ state: WatchVoiceBridgeState) {
        switch state {
        case .idle:
            break
        case .connecting:
            voiceState = .connecting
        case .ready:
            if voiceState == .connecting { voiceState = .greeting }
        case .reconnecting:
            voiceState = .reconnecting
            voiceReason = "network_interrupted"
            voiceMessage = "Reconnecting voice conversation…"
            scheduleVoiceResume()
        case .failed(let reason):
            if reason == "network_interrupted" {
                voiceState = .reconnecting
                voiceReason = "network_interrupted"
                voiceMessage = "Reconnecting voice conversation…"
                scheduleVoiceResume()
            } else if reason == "audio_interrupted" || reason == "route_unavailable" {
                suspendVoiceImmediately(reason: reason)
            } else {
                voiceState = .error
                voiceReason = reason == "audio_interrupted" ? "audio_interrupted" : "media_error"
                voiceMessage = "Voice media stopped."
            }
        case .ended:
            if voiceSession != nil, voiceState != .suspended {
                voiceState = .reconnecting
                voiceReason = "network_interrupted"
            }
        }
    }

    func consumeVoiceTranscript(_ transcript: WatchVoiceTranscript) {
        guard let grant = voiceGrant, transcript.matches(grant: grant) else { return }
        if let last = voiceTranscriptSequences[transcript.turnId], transcript.sequence <= last {
            if transcript.final, let pending = pendingVoiceSubmissions[transcript.turnId],
                pending.transcript == transcript, connected,
                pending.connectionGeneration == continuity.connectionGeneration
            {
                sendCurrentConnectionVoice(pending.frame)
            }
            return
        }
        voiceTranscriptSequences[transcript.turnId] = transcript.sequence
        guard transcript.final else {
            voicePartialTranscript = transcript.text
            voiceState = .transcribing
            return
        }
        guard let proofExpiry = transcript.proofExpiresAt.flatMap(parseVoiceDate),
            proofExpiry > Date(),
            let connection = continuity.connectionGeneration,
            let frame = voiceChatFrame(transcript, connectionGeneration: connection)
        else {
            voiceState = .error
            voiceReason = "stale_generation"
            voiceMessage = "The voice transcript expired. Please say it again."
            return
        }
        let bytes = frame.utf8.count
        let retainedBytes = pendingVoiceSubmissions.values.reduce(0) { $0 + $1.frame.utf8.count }
        guard
            pendingVoiceSubmissions[transcript.turnId] != nil
                || (pendingVoiceSubmissions.count < VoiceContractLimits.pendingFinalCount
                    && retainedBytes + bytes <= VoiceContractLimits.pendingFinalBytes)
        else {
            voiceState = .error
            voiceReason = "capacity_exhausted"
            voiceMessage = "Too many voice requests are awaiting confirmation. Try again."
            return
        }
        pendingVoiceSubmissions[transcript.turnId] = PendingVoiceSubmission(
            transcript: transcript,
            frame: frame,
            connectionGeneration: connection)
        voicePartialTranscript = nil
        voiceState = .acknowledging
        sendCurrentConnectionVoice(frame)
        startVoiceRetryLoopIfNeeded()
    }

    private func reframePendingVoiceSubmissions(for connectionGeneration: String) {
        // Connection generation is a socket fence, not part of the worker's
        // transcript proof. Rebuild only that binding from the retained exact
        // transcript so an unacknowledged final can continue on the new UI
        // socket without entering the generic offline queue.
        var retainedBytes = pendingVoiceSubmissions.values.reduce(0) {
            $0 + $1.frame.utf8.count
        }
        var dropped = false
        for turnId in Array(pendingVoiceSubmissions.keys) {
            guard let pending = pendingVoiceSubmissions[turnId],
                pending.connectionGeneration != connectionGeneration
            else { continue }
            let oldBytes = pending.frame.utf8.count
            guard
                let frame = voiceChatFrame(
                    pending.transcript,
                    connectionGeneration: connectionGeneration),
                VoiceCurrentConnectionFrame(frameText: frame) != nil
            else {
                retainedBytes -= oldBytes
                pendingVoiceSubmissions.removeValue(forKey: turnId)
                dropped = true
                continue
            }
            let nextBytes = retainedBytes - oldBytes + frame.utf8.count
            guard nextBytes <= VoiceContractLimits.pendingFinalBytes else {
                retainedBytes -= oldBytes
                pendingVoiceSubmissions.removeValue(forKey: turnId)
                dropped = true
                continue
            }
            pendingVoiceSubmissions[turnId] = PendingVoiceSubmission(
                transcript: pending.transcript,
                frame: frame,
                connectionGeneration: connectionGeneration)
            retainedBytes = nextBytes
        }
        if pendingVoiceSubmissions.isEmpty {
            voiceRetryTask?.cancel()
            voiceRetryTask = nil
        }
        if dropped {
            voiceState = .error
            voiceReason = "stale_generation"
            voiceMessage = "That spoken request could not be restored. Please say it again."
        }
    }

    private func voiceChatFrame(
        _ transcript: WatchVoiceTranscript,
        connectionGeneration: String
    ) -> String? {
        guard let digest = transcript.textDigest,
            let proof = transcript.transcriptProof,
            let proofExpiry = transcript.proofExpiresAt,
            let language = transcript.detectedLanguage
        else { return nil }
        return watchVoiceJSON([
            "type": .string("ui_event"),
            "action": .string("chat_message"),
            "session_id": .string(transcript.chatId),
            "connection_generation": .string(connectionGeneration),
            "submission_id": .string(transcript.submissionId),
            "request_generation": .string(transcript.requestGeneration),
            "payload": .object([
                "message": .string(transcript.text),
                "chat_id": .string(transcript.chatId),
                "connection_generation": .string(connectionGeneration),
                "submission_id": .string(transcript.submissionId),
                "request_generation": .string(transcript.requestGeneration),
                "snapshot_purpose": .string("commit"),
                "voice_origin": .object([
                    "schema_version": .string("1"),
                    "session_id": .string(transcript.sessionId),
                    "generation": .number(Double(transcript.generation)),
                    "media_grant_revision": .number(Double(transcript.mediaGrantRevision)),
                    "turn_id": .string(transcript.turnId),
                    "client_turn_id": .string(transcript.clientTurnId),
                    "chat_context_revision": .number(Double(transcript.chatContextRevision)),
                    "source_participant_identity": .string(
                        transcript.sourceParticipantIdentity),
                    "detected_language": .string(language),
                    "text_digest_sha256": .string(digest),
                    "transcript_proof": .string(proof),
                    "proof_expires_at": .string(proofExpiry),
                ]),
            ]),
        ])
    }

    private func startVoiceRetryLoopIfNeeded() {
        guard voiceRetryTask == nil else { return }
        voiceRetryTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(2500))
                guard let self else { return }
                if self.pendingVoiceSubmissions.isEmpty {
                    self.voiceRetryTask = nil
                    return
                }
                guard self.connected, let connection = self.continuity.connectionGeneration else {
                    continue
                }
                for pending in self.pendingVoiceSubmissions.values
                where pending.connectionGeneration == connection {
                    self.sendCurrentConnectionVoice(pending.frame)
                }
            }
        }
    }

    private func clearPendingVoiceSubmission(_ turnId: String) {
        pendingVoiceSubmissions.removeValue(forKey: turnId)
        if pendingVoiceSubmissions.isEmpty {
            voiceRetryTask?.cancel()
            voiceRetryTask = nil
        }
    }

    func sendVoicePlayout(_ observation: WatchVoicePlayoutObservation) {
        guard let connection = continuity.connectionGeneration,
            voicePlayoutSequence < UInt64.max
        else { return }
        let announcement = observation.announcement
        var frame: [String: JSONValue] = [
            "type": .string("voice_playout_event"),
            "schema_version": .string("1"),
            "device_id": .string(voiceDeviceId),
            "connection_generation": .string(connection),
            "session_id": .string(announcement.sessionId),
            "generation": .number(Double(announcement.generation)),
            "media_grant_revision": .number(Double(announcement.mediaGrantRevision)),
            "announcement_id": .string(announcement.announcementId),
            "announcement_sequence": .number(Double(announcement.announcementSequence)),
            "turn_id": announcement.turnId.map(JSONValue.string) ?? .null,
            "kind": .string(announcement.kind),
            "quantum_role": .string(announcement.quantumRole),
            "quantum_index": .number(Double(announcement.quantumIndex)),
            "phase": .string(observation.phase.rawValue),
            "client_sequence": .number(Double(voicePlayoutSequence)),
            "observed_at": .string(voiceTimestamp()),
        ]
        if let reserved = announcement.resultReservedSamplesAfter {
            frame["result_reserved_samples_after"] = .number(Double(reserved))
        }
        guard let encoded = watchVoiceJSON(frame), encoded.utf8.count <= 2 * 1024 else { return }
        voicePlayoutSequence += 1
        sendCurrentConnectionVoice(encoded)
    }

    private func setVoiceMicrophone(_ enabled: Bool) async {
        guard let session = voiceSession, let client = makeVoiceRESTClient() else { return }
        if !enabled { voiceBridge.setCaptureEnabled(false) }
        if enabled {
            let permission = await voiceBridge.requestMicrophonePermission()
            guard permission == .authorized else {
                voiceState = .unavailable
                voiceReason = "permission_denied"
                return
            }
        }
        do {
            let updated = try await client.updateSession(
                session,
                changes: ["microphone_enabled": .bool(enabled)])
            voiceSession = updated
            voiceBridge.setCaptureEnabled(
                enabled && updated.chatContextSynced && updated.foregroundActive)
        } catch {
            voiceState = .error
            voiceReason = "stale_generation"
        }
    }

    private func setVoiceSpeechMuted(_ muted: Bool) async {
        guard let session = voiceSession, let client = makeVoiceRESTClient() else { return }
        do {
            voiceSession = try await client.updateSession(
                session,
                changes: ["speech_muted": .bool(muted)])
            if muted { voiceBridge.interruptPlayback() }
            voiceState = muted ? .muted : .listening
        } catch {
            voiceState = .error
            voiceReason = "stale_generation"
        }
    }

    private func stopVoiceSpeech() {
        voiceBridge.interruptPlayback()
        guard let session = voiceSession, let client = makeVoiceRESTClient() else { return }
        Task { try? await client.stopSpeech(session) }
    }

    private func consentSensitiveVoiceRecap() async {
        guard let session = voiceSession,
            let turn = currentSensitiveVoiceTurn,
            turn.sessionId == session.sessionId,
            UInt64(turn.generation) == session.generation,
            UInt64(turn.mediaGrantRevision) == session.mediaGrantRevision,
            turn.sensitiveResultPending,
            let resultId = turn.resultId,
            let client = makeVoiceRESTClient()
        else { return }
        do {
            try await client.consentSensitiveRecap(
                session,
                resultId: resultId,
                turnId: turn.turnId)
            voiceMessage = "Reading the approved result…"
        } catch {
            voiceState = .error
            voiceReason = "stale_generation"
            voiceMessage = "That result can no longer be read aloud."
        }
    }

    private func updateVoiceVisibleChat(_ chatId: String) async {
        guard let session = voiceSession, session.visibleChatId != chatId,
            let client = makeVoiceRESTClient()
        else { return }
        do {
            voiceSession = try await client.updateSession(
                session,
                changes: ["visible_chat_id": .string(chatId)])
            voiceMessage = "Updating voice chat…"
        } catch {
            voiceState = .error
            voiceReason = "chat_context_unavailable"
            voiceMessage = "Voice paused because this chat is unavailable."
        }
    }

    private func suspendVoiceImmediately(reason: String) {
        guard let session = voiceSession else { return }
        stopVoiceLeaseRenewal()
        voiceBridge.disconnect(reason: reason)
        voiceState = .suspended
        voiceReason = reason == "route_unavailable" ? "audio_interrupted" : reason
        voiceMessage =
            reason == "backgrounded"
            ? "Voice conversation suspended."
            : "Voice paused until audio is available again."
        guard let client = makeVoiceRESTClient() else { return }
        Task {
            self.voiceSession = try? await client.updateSession(
                session,
                changes: [
                    "foreground_active": .bool(false),
                    "foreground_reason": .string(reason),
                    "microphone_enabled": .bool(false),
                ])
        }
    }

    private func resumeVoiceInForeground() async {
        guard let session = voiceSession, let client = makeVoiceRESTClient() else { return }
        let permission = await voiceBridge.requestMicrophonePermission()
        guard permission == .authorized else {
            voiceState = .unavailable
            voiceReason = "permission_denied"
            return
        }
        voiceState = .reconnecting
        voiceMessage = "Reconnecting voice conversation…"
        do {
            let refreshed = try await client.refreshGrant(
                session,
                refreshId: UUID().uuidString.lowercased())
            let updated = try await client.updateSession(
                refreshed.session,
                changes: [
                    "foreground_active": .bool(true),
                    "foreground_reason": .string("foreground"),
                    "microphone_enabled": .bool(true),
                ])
            guard
                let resumed = WatchVoiceSessionGrant(
                    session: updated,
                    grant: refreshed.grant)
            else { throw WatchVoiceRESTError.malformedResponse }
            try await installVoiceSession(resumed)
        } catch {
            voiceState = .error
            voiceReason = "media_unavailable"
            voiceMessage = "Voice could not reconnect. End it and start again."
        }
    }

    private func scheduleVoiceResume() {
        guard voiceResumeTask == nil, voiceForegroundActive, voiceSession != nil else { return }
        stopVoiceLeaseRenewal()
        voiceResumeTask = Task { [weak self] in
            guard let self else { return }
            await self.resumeVoiceInForeground()
            self.voiceResumeTask = nil
        }
    }

    func startVoiceLeaseRenewal() {
        stopVoiceLeaseRenewal()
        guard voiceForegroundActive, voiceSession != nil else { return }
        voiceLeaseTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                do {
                    try await Task.sleep(for: self.voiceLeaseInterval)
                } catch {
                    return
                }
                guard !Task.isCancelled, self.voiceForegroundActive,
                    let session = self.voiceSession,
                    session.foregroundActive,
                    let client = self.makeVoiceRESTClient()
                else { return }
                // A lease renewal is transport liveness, never user activity:
                // omitting `interaction` preserves the server's true idle timer.
                _ = try? await client.updateSession(
                    session,
                    changes: [
                        "foreground_active": .bool(true),
                        "foreground_reason": .string("foreground"),
                    ])
            }
        }
    }

    private func stopVoiceLeaseRenewal() {
        voiceLeaseTask?.cancel()
        voiceLeaseTask = nil
    }

    private func endVoiceConversation(reason: String) {
        let session = voiceSession
        let client = makeVoiceRESTClient()
        voiceBridge.disconnect(reason: reason)
        voiceSession = nil
        voiceGrant = nil
        voiceTurnSequences.removeAll()
        currentVoiceTurnId = nil
        currentVoiceTurnOccurredAt = nil
        currentSensitiveVoiceTurn = nil
        voiceTerminalNotice = nil
        voiceResumeTask?.cancel()
        voiceResumeTask = nil
        stopVoiceLeaseRenewal()
        pendingVoiceActivation = nil
        voiceState = .ended
        voiceReason = reason == "chat_deleted" ? "chat_context_unavailable" : "ended_by_user"
        voiceMessage = nil
        if let session, let client {
            Task { try? await client.endSession(session) }
        }
    }

    private func resetVoiceState(reason: String) {
        voiceRetryTask?.cancel()
        voiceRetryTask = nil
        voiceBridge.disconnect(reason: reason)
        voiceControlBinding = nil
        voiceSession = nil
        voiceGrant = nil
        pendingVoiceActivation = nil
        pendingVoiceSubmissions.removeAll()
        voiceTranscriptSequences.removeAll()
        voiceTurnSequences.removeAll()
        currentVoiceTurnId = nil
        currentVoiceTurnOccurredAt = nil
        currentSensitiveVoiceTurn = nil
        voiceTerminalNotice = nil
        voiceResumeTask?.cancel()
        voiceResumeTask = nil
        stopVoiceLeaseRenewal()
        voiceComposer = nil
        voicePartialTranscript = nil
        voiceActivationBusy = false
        voiceState = .off
        voiceReason = "ready"
        voiceMessage = nil
    }

    private func makeVoiceRESTClient(accessToken: String? = nil) -> WatchVoiceRESTClient? {
        guard let connection = continuity.connectionGeneration,
            let binding = voiceControlBinding,
            binding.connectionGeneration == connection,
            binding.deviceId == voiceDeviceId,
            binding.expiresAt > Date()
        else { return nil }
        let tokenProvider: @Sendable () async -> String?
        if let accessToken {
            tokenProvider = { accessToken }
        } else {
            tokenProvider =
                voiceTokenProvider ?? { [weak self] in
                    await self?.freshAccessToken()
                }
        }
        return WatchVoiceRESTClient(
            serverBase: serverBase,
            deviceId: voiceDeviceId,
            connectionGeneration: connection,
            controlBinding: binding,
            tokenProvider: tokenProvider,
            transport: voiceRESTTransport)
    }

    private func safeVoiceReason(_ code: String) -> String {
        WatchVoiceContract.reasons.contains(code) ? code : "internal_error"
    }

    private func parseVoiceDate(_ value: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: value)
    }

    private func voiceTimestamp() -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: Date())
    }
}

private struct PendingVoiceActivation: Sendable {
    let activationId: String
    let submissionId: String
    let requestGeneration: String
    let connectionGeneration: String
    let selectedChatAtRequest: String?
    let takeover: Bool
}

private struct PendingVoiceSubmission: Sendable {
    let transcript: WatchVoiceTranscript
    let frame: String
    let connectionGeneration: String
}

private actor WatchRegistrationResumeState {
    private var next = false

    func consume() -> Bool {
        let value = next
        next = true
        return value
    }
}
