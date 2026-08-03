import AVFoundation
import AstralCore
import AuthenticationServices
// Feature 051 — the iOS/macOS app model: a faithful port of the Android
// `AppViewModel`. System-browser PKCE sign-in, WS session with the ios/macos
// device profile, and the full server-driven reduce: the "commit-on-done"
// canvas lifecycle (a replacing turn buffers full `ui_render` replaces into
// `pendingCanvas` and swaps them in on `chat_status done`, pushing the prior
// canvas onto the timeline, while identity-keyed ops morph the visible canvas
// live), server-owned chrome (`chrome_menu`/`chrome_surface`), agents/audit/
// history surfaces, streaming nodes, attachments, and live theming.
import Foundation
import SwiftUI
import UniformTypeIdentifiers

#if os(iOS)
    import UIKit
#else
    import AppKit
#endif

struct LLMFirstLoginOperation: Equatable {
    enum State: String, Equatable {
        case submitting
        case accepted
        case validating
        case persisting
        case running
        case completed
        case failed
        case cancelled
        case retryable
        case unconfirmed
    }

    let submissionId: String
    let requestGeneration: String
    let connectionGeneration: String
    let requiresAdvance: Bool
    var operationId: String?
    var sequence: UInt64?
    var state: State
    var phase: String
    var label: String
    var retryable: Bool
    var errorCode: String?
    var errorMessage: String?
    var phaseVisible: Bool
    var isAuthoritativelyTerminal: Bool
    var didAdvance: Bool

    var isLoading: Bool {
        !isAuthoritativelyTerminal
            && state != .unconfirmed
            && [.submitting, .accepted, .validating, .persisting, .running].contains(state)
    }

    var fieldsEditable: Bool { true }

    var presentedLabel: String {
        if phaseVisible && [State.submitting, .accepted].contains(state) {
            return "Waiting to check your provider credentials…"
        }
        // Never empty while loading: the status view is a Text, and an empty
        // Text exposes NO accessibility element — the status would vanish from
        // the AX tree during the brief pre-first-frame window.
        if label.isEmpty && isLoading { return "Submitting…" }
        return label
    }
}

enum LLMOperationReconciliation {
    case operation(OperationProjection)
    case submission(OperationSubmissionProjection)
    case unavailable
}

@MainActor
@Observable
final class AppModel: NSObject {

    enum Screen: Equatable { case chat, agents, history, audit, surface }

    struct ChatTurn: Identifiable, Equatable {
        let id: String
        let role: String  // "user" | "assistant" | "reasoning"
        let text: String
        let components: [AstralComponent]

        init(
            id: String,
            role: String,
            text: String,
            components: [AstralComponent] = []
        ) {
            self.id = id
            self.role = role
            self.text = text
            self.components = components
        }
    }

    struct StagedAttachment: Identifiable, Equatable {
        let uid: Int
        let filename: String
        var category: String
        var attachmentId: String?
        var state: String  // "uploading" | "ready" | "failed"
        var note: String?
        var id: Int { uid }
    }

    struct CanvasSnapshot: Identifiable, Equatable {
        let id = UUID()
        let label: String
        let components: [AstralComponent]
    }

    struct SurfaceContent: Equatable {
        let surfaceKey: String
        let title: String
        let components: [AstralComponent]
    }

    // MARK: configuration

    // UserDefaults-backed (the former @AppStorage pair — property wrappers
    // aren't allowed on @Observable stored properties). Seeded in init.
    var serverBaseText: String {
        didSet {
            UserDefaults.standard.set(serverBaseText, forKey: "serverBase")
            // Feature 053 — mirror the endpoint to the paired watch. Best-effort:
            // the watch runs independently and falls back to its build-time default.
            #if os(iOS)
                WatchOverrideSync.shared.push(serverBaseText)
            #endif
        }
    }
    var authorityText: String {
        didSet { UserDefaults.standard.set(authorityText, forKey: "authority") }
    }

    #if os(macOS)
        let clientId = AstralConfig.macosClientId
    #else
        let clientId = AstralConfig.iosClientId
    #endif
    let redirectURI = AstralConfig.redirectURI
    let voiceDeviceId: String
    @ObservationIgnored let voice = AppleVoiceSessionController()

    // MARK: observable state (mirrors Android UiState)

    var signedIn = false
    var accountName = ""
    var connected = false
    var everConnected = false
    var screen: Screen = .chat
    var activeChatId: String?

    var turns: [ChatTurn] = []
    var canvas: [AstralComponent] = []
    var transientTurns: [ChatTurn] = []
    var transientCanvas: [AstralComponent]?
    var pendingCanvas: [AstralComponent] = []
    var turnActive = false
    var pendingReplace = false
    /// Set by the first live canvas op of an armed turn — clears the skeleton
    /// while the turn stays active (web parity: first canvas content hides it).
    var liveOpsThisTurn = false
    var canvasLabel = ""
    var pendingLabel = ""
    var canvasHistory: [CanvasSnapshot] = []
    var viewingIndex: Int?

    var staged: [StagedAttachment] = []
    var statusText: String?
    var errorBanner: String?
    var bannerIsError = true
    var stepTrail: [String] = []
    var asyncDetached = false
    var operationStatuses: [String: OperationStatus] = [:]
    var agentLifecycles: [String: AgentLifecycle] = [:]
    var localOperationSubmissions: [String: LocalOperationSubmission] = [:]

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

    var agents: [Agent] = []
    var history: [ChatSummary] = []
    var audit: [AuditEvent] = []
    var agentsLoading = false
    var historyLoading = false
    var auditLoading = false

    var chromeMenu: ChromeMenuModel?
    var pendingSurfaceKey = ""
    var pendingSurfaceParams: JSONValue = .object([:])
    var pendingSurface: SurfaceContent?
    /// 054 first-run gate: the server pinned the current surface
    /// (`chrome_surface` `mode:"mandatory"`) — navigation is suppressed until
    /// the server replaces or closes it. Sign-out stays available (FR-013).
    var mandatorySurface = false
    var timelineReadOnly = false

    var signInError: String?

    /// One client-local projection for the current provider Save attempt. It
    /// never claims server acceptance; canonical operation frames or the
    /// authenticated reconciliation endpoints own that transition.
    var llmFirstLoginOperation: LLMFirstLoginOperation?

    let themeStore = ThemeStore()

    // Derived
    var visibleCanvas: [AstralComponent] {
        if let idx = viewingIndex, canvasHistory.indices.contains(idx) {
            return canvasHistory[idx].components
        }
        return transientCanvas ?? canvas
    }
    var visibleTurns: [ChatTurn] { turns + transientTurns }
    var isViewingHistory: Bool { viewingIndex != nil }
    var showSkeleton: Bool { pendingReplace && !liveOpsThisTurn && viewingIndex == nil }
    var mutationsLocked: Bool { timelineReadOnly }
    /// `statusText` also carries occasional non-progress notices.  Views must
    /// only pair it with an indeterminate indicator while a locally correlated
    /// operation or chat turn is genuinely still active.
    var statusShowsActivity: Bool {
        guard statusText != nil else { return false }
        if localOperationSubmissions.values.contains(where: localSubmissionShowsActivity) {
            return true
        }
        var pendingGenerations = pendingChatRequestGenerations.union(
            pendingSurfaceRequestGenerations)
        if let currentRequest = continuity.requestGeneration {
            pendingGenerations.insert(currentRequest)
        }
        let acceptedOperationIsActive = operationStatuses.values.contains {
            !$0.terminal && pendingGenerations.contains($0.requestGeneration)
        }
        return acceptedOperationIsActive || turnActive || asyncDetached
    }

    // MARK: session plumbing (never read by views — not observation-tracked)

    private let store: TokenStorage = {
        #if canImport(Security)
            KeychainTokenStore()
        #else
            InMemoryTokenStore()
        #endif
    }()
    @ObservationIgnored private var tokens: TokenSet?
    @ObservationIgnored private var ws: WSClient?
    @ObservationIgnored private var wsTask: Task<Void, Never>?
    @ObservationIgnored private var authSession: ASWebAuthenticationSession?
    @ObservationIgnored private var seqState: [String: Int] = [:]
    @ObservationIgnored private var attachSeq = 0
    @ObservationIgnored private var statusLifecycle = StatusLifecycleReducer()
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
    @ObservationIgnored private var llmPhaseTask: Task<Void, Never>?
    @ObservationIgnored private var llmWatchdogTask: Task<Void, Never>?
    @ObservationIgnored private var llmReconciliationRunning = false
    @ObservationIgnored var llmFirstLoginPhaseDelay = AppModel.llmFirstLoginPhaseDelayNanoseconds
    @ObservationIgnored var llmFirstLoginWatchdogDelay = AppModel.llmFirstLoginWatchdogNanoseconds
    @ObservationIgnored var llmOperationReconciler: ((String?, String) async -> LLMOperationReconciliation)?

    private let docMarker = "full write-up is on the canvas"
    private let maxTrail = 20
    private let timelineMutations: Set<String> = [
        "chat_message", "component_action", "table_paginate",
        "save_theme", "component_refine", "component_restore",
    ]
    private let conversationMutationActions: Set<String> = [
        "save_component", "delete_saved_component", "combine_components",
        "condense_components", "component_action", "component_refine",
        "component_restore", "table_paginate",
    ]

    static let llmFirstLoginPhaseDelayNanoseconds: UInt64 = 1_000_000_000
    static let llmFirstLoginWatchdogNanoseconds: UInt64 = 10_000_000_000

    /// Test seam: observes every outbound WS frame text (nil in production).
    @ObservationIgnored var outboundTap: ((String) -> Void)?

    /// Test-only override for the dedicated, current-connection voice path.
    /// Production always falls through to `WSClient.sendCurrentConnectionVoice`.
    @ObservationIgnored var currentConnectionVoiceSendOverride: ((String) -> Void)?

    var serverBase: URL {
        URL(string: serverBaseText) ?? URL(string: AstralConfig.serverBaseURL)!
    }

    var oidc: OIDCConfig? {
        guard let authority = URL(string: authorityText), !authorityText.isEmpty else { return nil }
        return OIDCConfig(authority: authority, clientId: clientId, redirectURI: redirectURI)
    }

    var rest: RestClient {
        RestClient(serverBase: serverBase) { [weak self] in
            await self?.freshAccessToken()
        }
    }

    // MARK: lifecycle

    override convenience init() {
        self.init(conversationResumeStore: ConversationResumeStore())
    }

    init(conversationResumeStore: ConversationResumeStore) {
        self.conversationResumeStore = conversationResumeStore
        let defaults = UserDefaults.standard
        let voiceDeviceKey = "astraldeep.voice.device-id.v1"
        if let stored = defaults.string(forKey: voiceDeviceKey),
            let parsed = UUID(uuidString: stored), parsed.uuidString.lowercased() == stored,
            stored[stored.index(stored.startIndex, offsetBy: 14)] == "4",
            "89ab".contains(stored[stored.index(stored.startIndex, offsetBy: 19)])
        {
            voiceDeviceId = stored
        } else {
            let fresh = UUID().uuidString.lowercased()
            defaults.set(fresh, forKey: voiceDeviceKey)
            voiceDeviceId = fresh
        }
        var storedBase = defaults.string(forKey: "serverBase") ?? ""
        var storedAuthority = defaults.string(forKey: "authority") ?? ""
        // The override keys hold USER edits only. Earlier builds seeded the
        // build-time DEFAULT into them on first launch, freezing the endpoint
        // for the whole install base — a stored value equal to the current
        // default is that seed (or a no-op edit), not an override: clear it so
        // a future xcconfig repoint reaches existing installs and their
        // paired watches.
        if storedBase == AstralConfig.serverBaseURL {
            defaults.removeObject(forKey: "serverBase")
            storedBase = ""
        }
        if storedAuthority == AstralConfig.keycloakAuthority {
            defaults.removeObject(forKey: "authority")
            storedAuthority = ""
        }
        serverBaseText = storedBase.isEmpty ? AstralConfig.serverBaseURL : storedBase
        authorityText = storedAuthority.isEmpty ? AstralConfig.keycloakAuthority : storedAuthority
        super.init()
        voice.setFrameSender { [weak self] text in
            self?.sendVoiceWire(text) ?? false
        }
        voice.setChatAdopter { [weak self] chatId in
            self?.adoptChat(chatId)
        }
        #if os(iOS)
            WatchOverrideSync.shared.activate()
            if !storedBase.isEmpty { WatchOverrideSync.shared.push(serverBaseText) }
        #endif
    }

    /// Bind the authenticated OIDC account to its opaque local locator. This
    /// is preference namespacing only; the server remains authoritative for
    /// ownership when the following registration/load request is handled.
    func bindConversationAccount(_ account: ConversationAccount) {
        if conversationAccount != account {
            continuity.clear()
            resetChatState()
        }
        conversationAccount = account
        activeChatId = conversationResumeStore.load(for: account)?.chatId
    }

    /// Build one reconnect registration and open its hydration fence before
    /// any welcome or transient frame can be reduced.
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
        #if os(macOS)
            let voiceDeviceKind = "macos"
        #else
            let voiceDeviceKind = "ios"
        #endif
        voice.installUIConnection(
            token: token, serverBase: serverBase, deviceId: voiceDeviceId,
            deviceKind: voiceDeviceKind, connectionGeneration: connection,
            visibleChatId: activeChatId)
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
        transientTurns = []
        transientCanvas = nil
        return continuity.beginConnection(generation)
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
        transientTurns = []
        transientCanvas = nil
        return true
    }

    var lastCommittedRenderRevision: UInt64 {
        continuity.lastCommittedRenderRevision
    }

    func bootstrap() async {
        guard let stored = store.load() else { return }
        tokens = stored.tokenSet
        // Enter the signed-in shell IMMEDIATELY: the WS dial starts now, and
        // the register frame waits on the (single-flight) token refresh
        // inside onConnect — so the IdP round trip and the socket handshake
        // run concurrently instead of back-to-back behind a blank sign-in
        // screen. Transient (offline) keeps the stored credentials: the
        // reconnect strip shows and the WS backoff loop registers once the
        // network returns. Credentials are wiped ONLY on a definitive IdP
        // rejection — never for being offline at launch.
        enterSignedIn(resumedSession: true)
        if case .rejected = await refreshOutcome() {
            await signOut(revokeRemote: false)  // the IdP already refused the credential
        }
    }

    // MARK: sign-in

    func signIn() {
        guard let oidc else {
            signInError = "Set the Keycloak realm URL first."
            return
        }
        signInError = nil
        let verifier = PKCE.makeVerifier()
        let state = PKCE.makeVerifier()
        let url = oidc.authorizeURL(state: state, challenge: PKCE.challenge(for: verifier))

        let session = ASWebAuthenticationSession(url: url, callbackURLScheme: AstralConfig.redirectScheme) {
            [weak self] callback, error in
            guard let self else { return }
            Task { @MainActor in
                if let error {
                    self.signInError = error.localizedDescription
                    return
                }
                guard let callback,
                    let items = URLComponents(url: callback, resolvingAgainstBaseURL: false)?.queryItems,
                    items.first(where: { $0.name == "state" })?.value == state,
                    let code = items.first(where: { $0.name == "code" })?.value
                else {
                    self.signInError = "Sign-in was cancelled."
                    return
                }
                await self.exchange(code: code, verifier: verifier, oidc: oidc)
            }
        }
        session.presentationContextProvider = self
        session.prefersEphemeralWebBrowserSession = false
        authSession = session
        session.start()
    }

    private func exchange(code: String, verifier: String, oidc: OIDCConfig) async {
        let request = NoStoreHTTP.request(
            url: oidc.tokenEndpoint,
            method: "POST",
            body: Data(oidc.tokenRequestBody(code: code, verifier: verifier).utf8),
            contentType: "application/x-www-form-urlencoded")
        do {
            let (data, response) = try await NoStoreHTTP.session.data(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            guard status == 200, let json = try? JSONValue.parse(data),
                let set = TokenSet(json: json)
            else {
                signInError = "Token exchange failed (HTTP \(status))."
                return
            }
            tokens = set
            store.save(StoredTokens(from: set))
            enterSignedIn(resumedSession: false)
        } catch {
            signInError = error.localizedDescription
        }
    }

    /// Ensure a live access token, classifying failures so callers can tell
    /// "the IdP revoked us" (wipe + interactive sign-in) from "we're offline"
    /// (keep credentials, retry later). SINGLE-FLIGHT: concurrent callers
    /// (the WS onConnect, REST tokenProvider, bootstrap validation) join one
    /// in-flight IdP round trip — two parallel grants with the same rotating
    /// refresh token can revoke the whole session at the IdP.
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
        guard let refresh = tokens?.refreshToken, let oidc else {
            return .rejected("no refresh token")
        }
        let generation = sessionGeneration
        let attempt = Task { await RefreshStrategy.direct(oidc).attempt(refreshToken: refresh) }
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

    /// The server refused our token. `refreshOutcome` short-circuits when the
    /// token isn't near expiry, which would reconnect with the SAME rejected
    /// credential forever — so join the in-flight refresh if one is running,
    /// otherwise FORCE a real IdP refresh; reconnect only if it produced a
    /// different token, else the session is dead server-side (revoked / hard
    /// cap) and we go to interactive sign-in.
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
            connectWS(resumed: true)
        case .ok, .rejected:
            await signOut()  // same token — the server will just refuse it again
        case .transient:
            break  // offline blip; the WS backoff loop keeps retrying
        }
    }

    private func enterSignedIn(resumedSession: Bool) {
        accountName = tokens?.displayName ?? ""
        if let account = tokens?.conversationAccount {
            bindConversationAccount(account)
        } else {
            conversationAccount = nil
            continuity.clear()
            resetChatState()
        }
        signedIn = true
        connectWS(resumed: resumedSession)
    }

    /// `revokeRemote: false` skips the server-side revocation round trip —
    /// used when the IdP has ALREADY refused the credential (nothing to
    /// revoke, and the call would only delay landing on the sign-in screen).
    func signOut(revokeRemote: Bool = true) async {
        // Snapshot the old credential and an authenticated revocation client,
        // then make local sign-out durable before the first suspension point.
        // A killed or frozen revocation request must not leave Keychain able
        // to resurrect the account that the user just signed out of.
        let access = tokens?.accessToken
        let refresh = tokens?.refreshToken
        let logoutClient = RestClient(serverBase: serverBase) { access }
        let socket = ws
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
        signedIn = false
        voice.close()
        continuity.clear()
        resetChatState()
        clearPendingOperationSubmissions()
        statusLifecycle.clear()
        operationStatuses = [:]
        agentLifecycles = [:]
        chromeMenu = nil
        mandatorySurface = false  // the next session re-gates server-side
        clearLLMFirstLoginOperation()
        agents = []
        history = []
        audit = []

        // Everything above is synchronous local teardown. Remote revocation
        // remains best-effort and uses only the captured old access token.
        await socket?.stop()
        if revokeRemote, let refresh {
            _ = try? await logoutClient.logout(clientId: clientId, refreshToken: refresh)
        }
    }

    /// Local account-removal seam (for MDM/account-management surfaces).
    func clearConversationForAccountRemoval() {
        if let account = conversationAccount {
            _ = conversationResumeStore.clear(.accountRemoval, for: account)
        }
        conversationAccount = nil
        continuity.clear()
        resetChatState()
        clearPendingOperationSubmissions()
        statusLifecycle.clear()
        operationStatuses = [:]
        agentLifecycles = [:]
        voice.close()
    }

    // MARK: WS

    @ObservationIgnored private var lastReportedViewport: (width: Int, height: Int)?

    private var device: DeviceDescriptor {
        #if os(macOS)
            let (w, h) = lastReportedViewport ?? (1280, 800)
            var descriptor = DeviceDescriptor.macos(viewportWidth: w, viewportHeight: h)
        #else
            let size = UIScreen.main.bounds.size
            let (w, h) = lastReportedViewport ?? (Int(size.width), Int(size.height))
            var descriptor = DeviceDescriptor.ios(viewportWidth: w, viewportHeight: h)
        #endif
        descriptor.deviceId = voiceDeviceId
        #if os(macOS)
            let audioAvailability = AppleVoicePermission.macOSHardwareAvailability(
                AppleVoicePermission.currentAudioRouteSnapshot())
            descriptor.hasMicrophone = audioAvailability.hasMicrophone
            descriptor.hasAudioOutput = audioAvailability.hasAudioOutput
        #else
            descriptor.hasMicrophone = AVCaptureDevice.default(for: .audio) != nil
            descriptor.hasAudioOutput = true
        #endif
        descriptor.microphonePermission = AppleVoicePermission.status
        descriptor.fullDuplex = true
        descriptor.voiceTransport = "livekit"
        // 066 capability envelope: additive fields, ignored by older servers.
        #if os(macOS)
            descriptor.reducedMotion =
                NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        #else
            descriptor.reducedMotion = UIAccessibility.isReduceMotionEnabled
        #endif
        return descriptor
    }

    /// FR-002/T030: report viewport changes (rotation, iPad Split View /
    /// Slide Over, macOS window resize) through the existing `update_device`
    /// action so ROTE re-derives the layout — the Android fold/rotation twin.
    func viewportChanged(width: Int, height: Int) {
        guard width > 0, height > 0 else { return }
        if let last = lastReportedViewport, last == (width, height) { return }
        let isFirst = lastReportedViewport == nil
        lastReportedViewport = (width, height)
        // The initial size rides on register_ui; only CHANGES re-report.
        guard signedIn, !isFirst else { return }
        let identity = ClientOperationIdentity.fresh()
        beginLocalOperationSubmission(
            identity: identity,
            action: "update_device",
            surface: "operation",
            chatId: nil,
            exposeStatus: false)
        rawSend(
            Outbound.updateDevice(
                sessionId: nil,
                device: device,
                submissionId: identity.submissionId,
                requestGeneration: identity.requestGeneration))
    }

    private func connectWS(resumed initialResumed: Bool) {
        wsTask?.cancel()
        if let previous = ws {
            Task { await previous.stop() }  // never leak a live socket loop
        }
        let client = WSClient(url: rest.webSocketURL)
        ws = client
        let resumeState = AppRegistrationResumeState(initial: initialResumed)
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

    /// Internal (not private) so XCTests can drive connection events.
    func handle(_ event: WSEvent) async {
        switch event {
        case .connected:
            connected = true
            everConnected = true
            // 055 continuity: register_ui resumed the server session, but the
            // server replays no turn frames on register — re-issue load_chat
            // so anything that finished while this socket was down (background
            // task, another device) re-hydrates the narrative and canvas.
            // No-op on first connect: activeChatId is never persisted.
            if continuity.connectionGeneration == nil {
                refreshActiveChat()
            }
            if let attempt = llmFirstLoginOperation,
                !attempt.isAuthoritativelyTerminal
            {
                Task { await self.reconcileLLMFirstLoginOperation() }
            }
        case .disconnected:
            connected = false
            voice.controlTransportDisconnected()
            clearPendingOperationSubmissions()
            turnActive = false
            pendingReplace = false
            pendingCanvas = []
            transientTurns = []
            transientCanvas = nil
            agentsLoading = false
            historyLoading = false
            auditLoading = false
            statusText = nil
        case .sendDropped(let total):
            bannerIsError = true
            errorBanner = "Not sent while offline (queue full: \(total) dropped)"
        case .queuedOperationDropped(let replay, let reason):
            localOperationSubmissions.removeValue(forKey: replay.identity.submissionId)
            if statusText == "Submitting…" {
                statusText =
                    localOperationSubmissions.values.contains(
                        where: localSubmissionShowsActivity) ? "Submitting…" : nil
            }
            bannerIsError = true
            errorBanner = "Not sent while offline: \(replay.action) (\(reason))"
        case .sendRejected(let action):
            // A malformed queued frame cannot be correlated safely. Clear the
            // local-only map; valid retained frames restore themselves before
            // replay on the next connection.
            clearPendingOperationSubmissions()
            bannerIsError = true
            errorBanner = "Not sent while offline: \(action) (invalid queued identity)"
        case .frame(let frame):
            handleFrame(frame)
        }
    }

    // MARK: reduce (port of AppViewModel.reduce)

    /// Internal (not private) so XCTests can drive frames through the reducer.
    func handleFrame(_ frame: InboundFrame) {
        voice.consume(frame)
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
                transientTurns = []
                transientCanvas = nil
            }
        case "ui_render":
            reduceUiRender(frame)
        case "ui_upsert":
            reduceUiUpsert(frame)
        case "chat_created":
            if let chatId = nestedChatId(frame) { adoptChat(chatId) }
        case "user_message_acked":
            if let chatId = nestedChatId(frame) { adoptChat(chatId) }
            turnActive = true
            pendingReplace = true
            pendingCanvas = []
            liveOpsThisTurn = false
        case "chat_loaded":
            if continuity.connectionGeneration == nil {
                reduceChatLoaded(frame)
            }
        case "chat_deleted":
            if let chatId = nestedChatId(frame) { clearConfirmedDeletion(chatId) }
        case "chat_status":
            reduceStatus(frame)
        case "agent_list":
            agents = Agent.list(from: frame.payload["agents"])
            agentsLoading = false
        case "history_list":
            history = (frame.payload["chats"]?.arrayValue ?? []).compactMap { ChatSummary(json: $0) }
            historyLoading = false
        case "ui_stream_data", "stream_data":
            if continuity.connectionGeneration == nil {
                applyCanvasOps(streamFrameToOps(frame, activeChat: activeChatId, seqState: &seqState))
            }
        case "stream_subscribed":
            // 055 mid-stream join: load_chat may already have re-hydrated the
            // streamed component — the placeholder must not blank it. Ops go
            // live to the visible canvas even mid-turn, so its ids are the
            // guard source (the same list applyCanvasOps mutates).
            if continuity.connectionGeneration == nil {
                applyCanvasOps(
                    subscribeAckOps(
                        frame,
                        existingIds: Set(canvas.compactMap(\.componentId))))
            }
        case "stream_error":
            if continuity.connectionGeneration != nil {
                transientTurns = []
                transientCanvas = nil
                statusText = nil
                bannerIsError = true
                errorBanner = frame.errorMessage
            } else {
                applyCanvasOps(streamErrorOps(frame))
            }
        case "chrome_menu":
            chromeMenu = ChromeMenuModel.fromJSON(frame.payload["model"])
        case "chrome_surface":
            reduceChromeSurface(frame)
        case "operation_status":
            reduceOperationStatus(frame)
            reduceLLMOperationStatus(frame)
        case "agent_lifecycle":
            reduceAgentLifecycle(frame)
        case "user_preferences":
            themeStore.applyPreferences(frame.payload)
        case "workspace_timeline_mode":
            timelineReadOnly = frame.payload["active"]?.boolValue ?? frame.payload["on"]?.boolValue ?? false
        case "error":
            let admissionRefused = reduceAdmissionRefusal(frame)
            let llmAdmissionRefused = reduceLLMAdmissionRefusal(frame)
            if !admissionRefused && !llmAdmissionRefused {
                reduceError(frame)
            }
        case "chat_step":
            let step = frame.payload["step"]
            let name = step?["name"]?.stringValue ?? step?["kind"]?.stringValue
            stepTrail = trailUpsert(stepTrail, stepLine(name: name, status: step?["status"]?.stringValue))
        case "tool_progress":
            let head = [frame.payload["tool_name"]?.stringValue, frame.payload["message"]?.stringValue]
                .compactMap { $0 }.joined(separator: ": ")
            // The wire `percentage` is a JSON number (Optional[int] server-side).
            // Bounds-checked: Int(Double) traps on out-of-range values, and no
            // inbound frame may crash the client (FR-003).
            let pct =
                frame.payload["percentage"]
                .flatMap { value -> String? in
                    if let s = value.stringValue { return s }
                    guard let n = value.numberValue, n.isFinite, n >= 0, n <= 999
                    else { return nil }
                    return String(Int(n))
                }
                .map { " (\($0)%)" } ?? ""
            let label = (head + pct).isEmpty ? "Working…" : head + pct
            stepTrail = trailUpsert(stepTrail, "• \(label)")
        case "task_started":
            if frameTargetsActiveChat(frame) {
                statusText = "Working in the background…"
                asyncDetached = true
            } else {
                bannerIsError = false
                errorBanner = "Background task started in another chat"
            }
        case "task_completed":
            if frameTargetsActiveChat(frame) {
                if continuity.connectionGeneration == nil {
                    commitTurn()
                    refreshActiveChat()
                } else {
                    turnActive = false
                    statusText = nil
                }
                bannerIsError = false
                errorBanner = "Background task finished"
            } else {
                bannerIsError = false
                errorBanner = "Background task finished in another chat"
            }
        case "notification":
            let text = [frame.payload["title"]?.stringValue, frame.payload["body"]?.stringValue]
                .compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: ": ")
            if !text.isEmpty {
                bannerIsError = frame.payload["level"]?.stringValue == "error"
                errorBanner = text
            }
            // 055 continuity: a delivery into the OPEN chat refreshes it in
            // place (scheduled-run output persists to history server-side).
            if let chatId = nestedChatId(frame), chatId == activeChatId {
                refreshActiveChat()
            }
        // 055 (US3): the eight workspace verb acks, promoted ignored → handled
        // (wire-contract §4). The server's follow-up ui_upsert/ui_render
        // fan-outs stay authoritative; these give the issuing socket immediate
        // feedback without waiting on them.
        case "component_saved":
            let title = frame.payload["component"]?["title"]?.stringValue ?? ""
            bannerIsError = false
            errorBanner = title.isEmpty ? "Component saved" : "Saved \(title)"
        case "component_save_error":
            bannerIsError = true
            errorBanner = frame.payload["error"]?.stringValue ?? "Couldn't save component"
        case "component_deleted":
            if continuity.connectionGeneration == nil,
                let cid = frame.payload["component_id"]?.stringValue,
                !cid.isEmpty
            {
                applyCanvasOps([UpsertOp(op: "remove", componentId: cid, component: nil)])
            }
        case "combine_status":
            statusText = frame.payload["message"]?.stringValue ?? frame.payload["status"]?.stringValue
        case "combine_error":
            statusText = nil
            bannerIsError = true
            errorBanner = frame.payload["error"]?.stringValue ?? "Couldn't combine components"
        case "components_combined", "components_condensed":
            statusText = nil
            if continuity.connectionGeneration == nil {
                applyCanvasOps(replacementOps(frame))
            }
        case "saved_components_list":
            // Accepted ack; there is no native saved-components surface to
            // refresh (browsing rides the server-driven chrome surface) — a
            // future surface would consume `payload["components"]` here.
            break
        case "auth_required":
            Task { await self.handleAuthRequired() }
        default:
            break
        }
    }

    private func reduceLLMOperationStatus(_ frame: InboundFrame) {
        guard let status = OperationStatus(frame: frame),
            status.action == "chrome_llm_save",
            status.surface == "llm_settings",
            continuity.connectionGeneration == status.connectionGeneration,
            var current = llmFirstLoginOperation,
            current.requestGeneration == status.requestGeneration,
            !current.isAuthoritativelyTerminal
        else { return }
        if let operationId = current.operationId, operationId != status.operationId {
            return
        }
        if let sequence = current.sequence, status.sequence <= sequence {
            return
        }
        current.operationId = status.operationId
        current.sequence = status.sequence
        current.phase = status.phase
        current.phaseVisible = current.phaseVisible || status.state != "accepted"

        let authoritativeState = LLMFirstLoginOperation.State(rawValue: status.state)
        guard let authoritativeState, authoritativeState != .submitting,
            authoritativeState != .unconfirmed
        else { return }
        if current.state == .unconfirmed && !status.terminal {
            current.label = "Unable to confirm; reconnecting"
            current.retryable = true
            llmFirstLoginOperation = current
            return
        }

        current.state = authoritativeState
        current.label = status.label
        current.retryable = status.retryable
        current.errorCode = status.error.objectValue?["code"]?.stringValue
        current.errorMessage = status.error.objectValue?["message"]?.stringValue
        current.isAuthoritativelyTerminal = status.terminal
        llmFirstLoginOperation = current
        if status.terminal {
            cancelLLMTimers()
            if authoritativeState == .completed {
                advanceAfterLLMCompletionIfNeeded()
            }
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
            let ownsActiveChatTurn = operationOwnsActiveChatTurn(
                action: status.action,
                requestGeneration: status.requestGeneration)
            clearLocalOperationSubmission(requestGeneration: status.requestGeneration)
            if ownsActiveChatTurn {
                settleActiveChatTurn(
                    requestGeneration: status.requestGeneration,
                    discardUncommitted: status.state != "completed")
            }
            if let message = status.error.objectValue?["message"]?.stringValue {
                bannerIsError = true
                errorBanner = message
                transientTurns = []
                transientCanvas = nil
            }
            // A terminal success is historical evidence, not live progress.
            // If another correlated operation is still active, project that
            // operation; otherwise remove the status row altogether.
            statusText = latestActiveOperationStatusText()
        } else {
            statusText = status.label
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
        return localOperationSubmissions.values.contains(where: localSubmissionShowsActivity)
            ? "Submitting…" : nil
    }

    private func reduceAgentLifecycle(_ frame: InboundFrame) {
        guard let lifecycle = AgentLifecycle(frame: frame),
            statusLifecycle.accept(lifecycle: lifecycle)
        else { return }
        agentLifecycles = statusLifecycle.agents
        bannerIsError = lifecycle.state == "failed"
        errorBanner = "\(lifecycle.agentId): \(lifecycle.label)"
    }

    @discardableResult
    private func reduceAdmissionRefusal(_ frame: InboundFrame) -> Bool {
        guard let refusal = AdmissionRefusal(frame: frame),
            let submission = localOperationSubmissions.removeValue(forKey: refusal.submissionId)
        else { return false }
        if operationOwnsActiveChatTurn(
            action: submission.action,
            requestGeneration: submission.requestGeneration)
        {
            settleActiveChatTurn(
                requestGeneration: submission.requestGeneration,
                discardUncommitted: true)
        }
        statusText = latestActiveOperationStatusText()
        bannerIsError = true
        errorBanner = refusal.message
        return true
    }

    @discardableResult
    private func reduceLLMAdmissionRefusal(_ frame: InboundFrame) -> Bool {
        guard let refusal = AdmissionRefusal(frame: frame),
            var current = llmFirstLoginOperation,
            current.submissionId == refusal.submissionId,
            current.operationId == nil,
            !current.isAuthoritativelyTerminal
        else { return false }
        current.state = refusal.retryable ? .retryable : .failed
        current.phase = refusal.code
        current.label = refusal.message
        current.retryable = refusal.retryable
        current.errorCode = refusal.code
        current.errorMessage = current.label
        current.phaseVisible = true
        current.isAuthoritativelyTerminal = true
        llmFirstLoginOperation = current
        cancelLLMTimers()
        return true
    }

    private func advanceAfterLLMCompletionIfNeeded() {
        guard var current = llmFirstLoginOperation,
            current.requiresAdvance,
            !current.didAdvance
        else { return }
        current.didAdvance = true
        llmFirstLoginOperation = current
        mandatorySurface = false
        if screen == .surface {
            screen = .chat
            pendingSurface = nil
            pendingSurfaceKey = ""
            pendingSurfaceParams = .object([:])
        }
    }

    private func cancelLLMTimers() {
        llmPhaseTask?.cancel()
        llmPhaseTask = nil
        llmWatchdogTask?.cancel()
        llmWatchdogTask = nil
    }

    private func clearLLMFirstLoginOperation() {
        cancelLLMTimers()
        llmFirstLoginOperation = nil
        llmReconciliationRunning = false
    }

    private func armLLMFirstLoginTimers(submissionId: String) {
        cancelLLMTimers()
        let phaseDelay = llmFirstLoginPhaseDelay
        llmPhaseTask = Task { [weak self] in
            do {
                try await Task.sleep(nanoseconds: phaseDelay)
            } catch {
                return
            }
            guard let self, var current = self.llmFirstLoginOperation,
                current.submissionId == submissionId,
                current.isLoading
            else { return }
            current.phaseVisible = true
            self.llmFirstLoginOperation = current
        }

        let watchdogDelay = llmFirstLoginWatchdogDelay
        llmWatchdogTask = Task { [weak self] in
            do {
                try await Task.sleep(nanoseconds: watchdogDelay)
            } catch {
                return
            }
            guard let self, var current = self.llmFirstLoginOperation,
                current.submissionId == submissionId,
                !current.isAuthoritativelyTerminal
            else { return }
            current.state = .unconfirmed
            current.phase = "awaiting_reconciliation"
            current.label = "Unable to confirm; reconnecting"
            current.retryable = true
            current.errorCode = "deadline_exceeded"
            current.errorMessage = "Unable to confirm; reconnecting"
            current.phaseVisible = true
            self.llmFirstLoginOperation = current
            self.llmPhaseTask?.cancel()
            self.llmPhaseTask = nil
            if self.connected {
                Task { await self.reconcileLLMFirstLoginOperation() }
            }
        }
    }

    func reconcileLLMFirstLoginOperation() async {
        guard !llmReconciliationRunning,
            let current = llmFirstLoginOperation,
            !current.isAuthoritativelyTerminal
        else { return }
        llmReconciliationRunning = true
        defer { llmReconciliationRunning = false }

        let result: LLMOperationReconciliation
        if let llmOperationReconciler {
            result = await llmOperationReconciler(current.operationId, current.submissionId)
        } else {
            do {
                let client = rest
                if let operationId = current.operationId {
                    if let operation = try await client.operation(id: operationId) {
                        result = .operation(operation)
                    } else {
                        result = .unavailable
                    }
                } else if let submission = try await client.operationSubmission(id: current.submissionId) {
                    result = .submission(submission)
                } else {
                    result = .unavailable
                }
            } catch {
                result = .unavailable
            }
        }

        guard let latest = llmFirstLoginOperation,
            latest.submissionId == current.submissionId,
            !latest.isAuthoritativelyTerminal
        else { return }
        switch result {
        case .operation(let operation):
            applyReconciledLLMOperation(operation)
        case .submission(.accepted(let operation)):
            applyReconciledLLMOperation(operation)
        case .submission(.refused(let code, let retryable, _)):
            var refused = latest
            refused.state = retryable ? .retryable : .failed
            refused.phase = code
            refused.label =
                retryable
                ? "The request was not accepted. Try again."
                : "The request was not accepted. Check the form."
            refused.retryable = retryable
            refused.errorCode = code
            refused.errorMessage = refused.label
            refused.phaseVisible = true
            refused.isAuthoritativelyTerminal = true
            llmFirstLoginOperation = refused
            cancelLLMTimers()
        case .unavailable:
            if latest.state == .unconfirmed {
                var unavailable = latest
                unavailable.label = "Unable to confirm; reconnecting"
                llmFirstLoginOperation = unavailable
            }
        }
    }

    private func applyReconciledLLMOperation(_ projection: OperationProjection) {
        guard projection.operationKind == "llm_credential_save",
            var current = llmFirstLoginOperation,
            projection.requestGeneration == current.requestGeneration,
            current.operationId == nil || current.operationId == projection.operationId,
            current.sequence == nil || projection.stateRevision > current.sequence!,
            !current.isAuthoritativelyTerminal
        else { return }
        current.operationId = projection.operationId
        current.sequence = projection.stateRevision
        current.phase = projection.phaseCode ?? projection.state
        let mapped: LLMFirstLoginOperation.State
        switch projection.state {
        case "queued":
            mapped = .accepted
        case "running":
            if current.phase == "validating_credentials" {
                mapped = .validating
            } else if current.phase == "saving_credentials" {
                mapped = .persisting
            } else {
                mapped = .running
            }
        case "completed":
            mapped = .completed
        case "failed":
            mapped = .failed
        case "cancelled":
            mapped = .cancelled
        case "retryable":
            mapped = .retryable
        default:
            return
        }
        let terminal = [.completed, .failed, .cancelled, .retryable].contains(mapped)
        if current.state != .unconfirmed || terminal {
            current.state = mapped
            current.label = projection.safeSummary ?? reconciledLLMLabel(for: mapped)
            current.retryable = mapped == .retryable
        }
        current.errorCode = projection.terminalCode
        current.errorMessage = terminal && mapped != .completed ? current.label : nil
        current.phaseVisible = current.phaseVisible || mapped != .accepted
        current.isAuthoritativelyTerminal = terminal
        llmFirstLoginOperation = current
        if terminal {
            cancelLLMTimers()
            if mapped == .completed {
                advanceAfterLLMCompletionIfNeeded()
            }
        }
    }

    private func reconciledLLMLabel(for state: LLMFirstLoginOperation.State) -> String {
        switch state {
        case .accepted:
            return "Accepted"
        case .validating:
            return "Checking your provider credentials…"
        case .persisting:
            return "Saving credentials…"
        case .running:
            return "Finishing provider setup…"
        case .completed:
            return "Provider setup complete"
        case .failed:
            return "Check your provider credentials"
        case .cancelled:
            return "Provider setup cancelled"
        case .retryable:
            return "Provider unavailable. Try again."
        case .submitting:
            return "Submitting…"
        case .unconfirmed:
            return "Unable to confirm; reconnecting"
        }
    }

    private func reduceConversationSnapshot(_ frame: InboundFrame) {
        guard let snapshot = ConversationSnapshot(frame: frame),
            continuity.apply(snapshot) == .applied
        else { return }

        // Persist the locator before publishing either half of the atomic
        // transcript+canvas replacement to observation.
        if let account = conversationAccount {
            _ = conversationResumeStore.save(chatId: snapshot.chatId, for: account)
        }
        let restoredTurns = snapshot.messages.map { message in
            var text = message.visibleText
            if !message.attachmentNames.isEmpty {
                let attachments = "📎 " + message.attachmentNames.joined(separator: ", ")
                text = text.isEmpty ? attachments : text + "\n" + attachments
            }
            return ChatTurn(
                id: message.messageId,
                role: message.role,
                text: text,
                components: message.components)
        }
        activeChatId = snapshot.chatId
        turns = restoredTurns
        canvas = snapshot.canvasComponents
        transientTurns = []
        transientCanvas = nil
        pendingCanvas = []
        canvasHistory = []
        viewingIndex = nil
        turnActive = false
        pendingReplace = false
        liveOpsThisTurn = false
        statusText = nil
        stepTrail = []
        asyncDetached = false
        pendingCommitRequestGeneration = nil
    }

    /// Feature 060 render frames are previews. A valid, strictly sequenced
    /// scope may update only the disposable overlay; committed state changes
    /// exclusively through a complete conversation_snapshot.
    /// A chat-target preview turn, shaped exactly like the committed path
    /// (`reduceUiRender`): the chat column is TEXT, so components are flattened
    /// into it and never carried alongside. Carrying both rendered every
    /// text-only answer twice — once as markdown, once as a duplicate card.
    private func chatPreviewTurn(
        _ components: [AstralComponent], frame: InboundFrame
    ) -> ChatTurn? {
        let text = flattenText(components)
        guard !text.isEmpty else { return nil }
        return ChatTurn(
            id: "preview-\(frame.payload["frame_sequence"]?.numberValue ?? 0)",
            role: "assistant",
            text: text,
            components: [])
    }

    /// Canvas-target preview components, filtered exactly like the committed
    /// path (`reduceUiRender`): reasoning, doc cards and skeletons are chat /
    /// loading artifacts and never belong on the canvas.
    private func canvasPreviewComponents(
        _ components: [AstralComponent]
    ) -> [AstralComponent] {
        components.filter {
            !isReasoning($0) && !isDocCard($0.componentId) && !isSkeleton($0)
        }
    }

    private func reduceTransient(_ frame: InboundFrame) {
        guard continuity.acceptTransient(frame) else { return }
        switch frame.name {
        case "ui_render", "ui_update":
            let components = frame.renderComponents
            if frame.renderTarget == "chat" {
                let pendingUsers = transientTurns.filter { $0.role == "user" }
                let response = chatPreviewTurn(components, frame: frame).map { [$0] } ?? []
                transientTurns = pendingUsers + response
            } else {
                transientCanvas = canvasPreviewComponents(components)
            }
        case "ui_append":
            let components = frame.renderComponents
            if frame.renderTarget == "chat" {
                if let turn = chatPreviewTurn(components, frame: frame) {
                    transientTurns.append(turn)
                }
            } else {
                transientCanvas =
                    (transientCanvas ?? canvas) + canvasPreviewComponents(components)
            }
        case "ui_upsert":
            transientCanvas = Canvas.apply(transientCanvas ?? canvas, frame.upsertOps)
        case "ui_stream_data":
            transientCanvas = Canvas.apply(
                transientCanvas ?? canvas,
                streamFrameToOps(frame, activeChat: activeChatId, seqState: &seqState))
        default:
            break
        }
    }

    private func adoptChat(_ chatId: String) {
        if let account = conversationAccount {
            _ = conversationResumeStore.save(chatId: chatId, for: account)
        }
        activeChatId = chatId
        voice.updateVisibleChatLocally(chatId)
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
        guard activeChatId == chatId else { return }
        clearContinuityChatKeepingConnection()
        resetChatState()
    }

    private func clearContinuityChatKeepingConnection() {
        let connection = continuity.connectionGeneration
        continuity.clear()
        if let connection {
            _ = continuity.beginConnection(connection)
        }
    }

    private func nestedChatId(_ frame: InboundFrame) -> String? {
        frame.payload["payload"]?["chat_id"]?.stringValue ?? frame.payload["chat_id"]?.stringValue
    }

    /// 055 cross-device continuity: does a background-task frame concern the
    /// chat currently open? A frame with no chat id targets the issuing
    /// socket (pre-fan-out servers) and counts as ours.
    private func frameTargetsActiveChat(_ frame: InboundFrame) -> Bool {
        guard let chatId = nestedChatId(frame), !chatId.isEmpty else { return true }
        return chatId == activeChatId
    }

    /// Re-issue load_chat for the open chat so the narrative and canvas pick
    /// up content produced off this socket (background task, another device).
    private func refreshActiveChat() {
        guard let chatId = activeChatId, !chatId.isEmpty else { return }
        let identity = ClientOperationIdentity.fresh()
        let request = identity.requestGeneration
        if continuity.connectionGeneration != nil {
            guard
                openConversationRequest(
                    chatId: chatId,
                    requestGeneration: request,
                    purpose: .hydration)
            else { return }
        }
        beginLocalOperationSubmission(
            identity: identity,
            action: "load_chat",
            surface: "chat",
            chatId: chatId)
        rawSend(
            Outbound.loadChat(
                sessionId: activeChatId,
                chatId: chatId,
                submissionId: identity.submissionId,
                requestGeneration: request))
    }

    private func reduceUiRender(_ frame: InboundFrame) {
        let comps = frame.renderComponents
        if frame.renderTarget == "chat" {
            let text = flattenText(comps)
            if text.isEmpty || text.range(of: docMarker, options: .caseInsensitive) != nil { return }
            appendTurn(role: "assistant", text: text)
            return
        }
        let reasoning = comps.filter { isReasoning($0) }
        let canvasComps = comps.filter { !isReasoning($0) && !isDocCard($0.componentId) && !isSkeleton($0) }
        for r in reasoning {
            let text = flattenText(r.children).isEmpty ? flattenText([r]) : flattenText(r.children)
            if !text.isEmpty { appendTurn(role: "reasoning", text: text) }
        }
        if pendingReplace {
            if canvasComps.isEmpty { return }
            pendingCanvas = Canvas.apply(pendingCanvas, renderToOps(canvasComps))
        } else {
            canvas = canvasComps
            pendingCanvas = []
        }
    }

    private func reduceUiUpsert(_ frame: InboundFrame) {
        if let chatId = frame.chatId, let active = activeChatId, chatId != active { return }
        let ops = frame.upsertOps
        for op in ops where op.op != "remove" && isDocCard(op.componentId) {
            if let comp = op.component {
                let text = flattenText([comp])
                if !text.isEmpty { appendTurn(role: "assistant", text: text) }
            }
        }
        let canvasOps = ops.filter { !isDocCard($0.componentId) && !isSkeleton($0.component) }
        applyCanvasOps(canvasOps)
    }

    private func reduceChatLoaded(_ frame: InboundFrame) {
        let chat = frame.payload["chat"]
        activeChatId = chat?["id"]?.stringValue ?? activeChatId
        if let account = conversationAccount, let activeChatId {
            _ = conversationResumeStore.save(chatId: activeChatId, for: account)
        }
        let messages = chat?["messages"]?.arrayValue ?? chat?["history"]?.arrayValue ?? []
        turns = messages.enumerated().map { index, m in
            let content = m["content"]?.stringValue ?? m["text"]?.stringValue ?? ""
            let role = m["role"]?.stringValue ?? (m["is_user"]?.boolValue == true ? "user" : "assistant")
            // Index-keyed ids: identical repeated messages must not collide
            // (duplicate Identifiable ids are undefined behavior in ForEach).
            return ChatTurn(id: "hist-\(index)", role: role, text: content)
        }
        canvas = []
        pendingCanvas = []
        canvasHistory = []
        viewingIndex = nil
        turnActive = false
        pendingReplace = false
        canvasLabel = ""
        pendingLabel = ""
        statusText = nil
        stepTrail = []
        asyncDetached = false
    }

    private func reduceChromeSurface(_ frame: InboundFrame) {
        let surfaceKey = frame.payload["surface_key"]?.stringValue ?? ""
        let title = frame.payload["title"]?.stringValue ?? ""
        let components = AstralComponent.list(from: frame.payload["components"])
        if surfaceKey.isEmpty && components.isEmpty {
            // The server's blank close instruction also lifts the 054
            // mandatory pin (save success → close, then welcome ui_render).
            mandatorySurface = false
            if screen == .surface {
                screen = .chat
                pendingSurface = nil
                pendingSurfaceKey = ""
                pendingSurfaceParams = .object([:])
            }
            return
        }
        if frame.surfaceMode == "mandatory" {
            // 054 first-run gate: accept the surface even though unsolicited
            // and pin it — goTo/newChat/openSurface and the top bar suppress
            // navigation until the server closes it; sign-out stays (FR-013).
            mandatorySurface = true
            screen = .surface
            pendingSurfaceKey = surfaceKey
            pendingSurfaceParams = .object([:])
            pendingSurface = SurfaceContent(surfaceKey: surfaceKey, title: title, components: components)
            return
        }
        if screen == .surface && pendingSurfaceKey == surfaceKey {
            pendingSurface = SurfaceContent(surfaceKey: surfaceKey, title: title, components: components)
            return
        }
        let text = [title, noticeText(components)].filter { !$0.isEmpty }.joined(separator: ": ")
        if !text.isEmpty {
            bannerIsError = true
            errorBanner = text
        }
    }

    private func reduceError(_ frame: InboundFrame) {
        let code = frame.payload["code"]?.stringValue
        let safeErrorClasses: Set<String> = [
            "auth_failed", "model_not_found", "transport_error",
            "contract_violation", "provider_unavailable", "other",
        ]
        let specificCode = frame.payload["error_class"]?.stringValue.flatMap {
            safeErrorClasses.contains($0) ? $0 : nil
        }
        let displayedCode = specificCode ?? code
        let message =
            frame.payload["message"]?.stringValue
            ?? frame.payload["payload"]?["message"]?.stringValue ?? "Something went wrong."
        errorBanner =
            (displayedCode != nil && displayedCode != "internal")
            ? "\(message) (\(displayedCode!))" : message
        bannerIsError = true
        turnActive = false
        pendingReplace = false
        pendingCanvas = []
        transientTurns = []
        transientCanvas = nil
        agentsLoading = false
        historyLoading = false
        auditLoading = false
        statusText = nil
        asyncDetached = false
        if code == "chat_not_found" || code == "conversation_not_found" {
            let deletedChat = nestedChatId(frame) ?? activeChatId
            if let deletedChat {
                clearConfirmedDeletion(deletedChat)
                errorBanner = (code != "internal") ? "\(message) (\(code!))" : message
                bannerIsError = true
            }
        }
    }

    private func reduceStatus(_ frame: InboundFrame) {
        let status = frame.payload["status"]?.stringValue
        let message = frame.payload["message"]?.stringValue
        let label = (message?.isEmpty == false) ? message : status
        switch status {
        case "done":
            if continuity.connectionGeneration == nil {
                commitTurn()
            } else {
                turnActive = false
                statusText = nil
                stepTrail = []
                asyncDetached = false
                // The turn is over, so no further canvas work is coming. A
                // committed snapshot always precedes `done` ("terminal status
                // follows the sole committed-state frame"), so releasing the
                // skeleton here cannot race one. A text-only turn produces no
                // canvas ops and no snapshot at all, and would otherwise latch
                // the skeleton forever — hiding a canvas that is already
                // correct (the previous components, or the welcome).
                pendingReplace = false
                liveOpsThisTurn = false
            }
        case "idle":
            turnActive = false
            statusText = nil
            stepTrail = []
            asyncDetached = false
        case "thinking", "executing", "fixing", "processing_async":
            turnActive = true
            statusText = label
        default:
            statusText = label
        }
    }

    private func commitTurn() {
        if !pendingReplace {
            turnActive = false
            statusText = nil
            stepTrail = []
            asyncDetached = false
            return
        }
        if pendingCanvas.isEmpty {
            // No buffered render — the live canvas (already carrying any ops
            // applied mid-turn) IS the committed state, minus any welcome
            // that resurrected mid-turn (055: `wel_` never survives a turn).
            canvas = canvas.dropWelcome()
            turnActive = false
            pendingReplace = false
            statusText = nil
            stepTrail = []
            asyncDetached = false
            return
        }
        // 055: `wel_` identities never enter the timeline; a welcome-only
        // canvas archives nothing.
        let archived = canvas.dropWelcome()
        if !archived.isEmpty {
            let label = canvasLabel.isEmpty ? "Canvas \(canvasHistory.count + 1)" : canvasLabel
            canvasHistory.append(CanvasSnapshot(label: label, components: archived))
        }
        canvas = pendingCanvas
        pendingCanvas = []
        canvasLabel = pendingLabel
        pendingLabel = ""
        turnActive = false
        pendingReplace = false
        statusText = nil
        stepTrail = []
        asyncDetached = false
    }

    /// Identity-keyed ops morph the VISIBLE canvas immediately, even while a
    /// replacing turn is armed — only full `ui_render` replaces buffer (the
    /// mid-turn clobber hazard 044 guards against). A buffered render still
    /// wins at commit, so mid-turn ops mirror into it and the committed state
    /// stays what it would have been under accumulate-then-commit.
    private func applyCanvasOps(_ ops: [UpsertOp]) {
        if ops.isEmpty { return }
        canvas = Canvas.apply(canvas, ops)
        if pendingReplace {
            liveOpsThisTurn = true
            if !pendingCanvas.isEmpty {
                pendingCanvas = Canvas.apply(pendingCanvas, ops)
            }
        }
    }

    // MARK: reduce helpers

    private func appendTurn(role: String, text: String) {
        turns.append(ChatTurn(id: "\(role)-\(turns.count)", role: role, text: text))
    }

    private func renderToOps(_ components: [AstralComponent]) -> [UpsertOp] {
        components.enumerated().map { i, c in
            let id = c.componentId ?? "xr-\(c.type)-\(i)"
            return UpsertOp(
                op: "upsert", componentId: id,
                component: c.componentId == nil ? c.withComponentId(id) : c)
        }
    }

    /// components_combined / components_condensed → canvas ops: remove the
    /// consumed ids, upsert each carried result. Results are saved-row shapes
    /// (`{id, component_data, …}`); the component dict rides in
    /// `component_data` and may not carry a workspace identity yet (the
    /// server stamps it in the reconcile ui_render that follows), so identity
    /// falls back to the fresh row id.
    private func replacementOps(_ frame: InboundFrame) -> [UpsertOp] {
        let removed = frame.payload["removed_ids"]?.arrayValue?.compactMap { $0.stringValue } ?? []
        var ops: [UpsertOp] = removed.map { UpsertOp(op: "remove", componentId: $0, component: nil) }
        for row in frame.payload["new_components"]?.arrayValue ?? [] {
            guard let comp = row["component_data"].flatMap({ AstralComponent(json: $0) }) else { continue }
            let cid = comp.componentId ?? row["id"]?.stringValue ?? "combined-\(ops.count)"
            ops.append(
                UpsertOp(
                    op: "upsert", componentId: cid,
                    component: comp.componentId == nil ? comp.withComponentId(cid) : comp))
        }
        return ops
    }

    private func stepLine(name: String?, status: String?) -> String {
        let icon: String
        switch status {
        case "completed": icon = "✓"
        case "errored": icon = "✗"
        default: icon = "•"
        }
        return "\(icon) \(name ?? "step")"
    }

    private func trailKey(_ line: String) -> String {
        let afterSpace = line.contains(" ") ? String(line[line.index(after: line.firstIndex(of: " ")!)...]) : line
        return afterSpace.replacingOccurrences(of: #"\s*\(\d+(\.\d+)?%\)$"#, with: "", options: .regularExpression)
    }

    private func trailUpsert(_ trail: [String], _ line: String) -> [String] {
        let key = trailKey(line)
        var next = trail
        if let idx = trail.lastIndex(where: { trailKey($0) == key }) {
            next[idx] = line
        } else {
            next.append(line)
        }
        return Array(next.suffix(maxTrail))
    }

    private func isReasoning(_ c: AstralComponent) -> Bool {
        c.type.lowercased() == "collapsible" && (c.raw["title"]?.stringValue ?? "").lowercased() == "reasoning"
    }

    private func isDocCard(_ id: String?) -> Bool { id?.hasPrefix("doc_") ?? false }

    private func isSkeleton(_ c: AstralComponent?) -> Bool { c?.type.lowercased() == "skeleton" }

    private func flattenText(_ components: [AstralComponent]) -> String {
        components.map { c in
            let own = c.raw["content"]?.stringValue ?? c.raw["text"]?.stringValue ?? ""
            return (own + "\n" + flattenText(c.children)).trimmingCharacters(in: .whitespacesAndNewlines)
        }.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func noticeText(_ components: [AstralComponent]) -> String {
        components.map { c in
            let own =
                c.raw["message"]?.stringValue ?? c.raw["content"]?.stringValue
                ?? c.raw["text"]?.stringValue ?? ""
            return (own + "\n" + noticeText(c.children)).trimmingCharacters(in: .whitespacesAndNewlines)
        }.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func parserNote(_ status: String?) -> String? {
        switch status {
        case "preparing": return "preparing reader…"
        case "pending_admin_approval": return "reader pending admin"
        case "unavailable": return "no reader yet"
        default: return nil
        }
    }

    private func resetChatState() {
        activeChatId = nil
        voice.updateVisibleChatLocally(nil)
        turns = []
        canvas = []
        transientTurns = []
        transientCanvas = nil
        pendingCanvas = []
        canvasHistory = []
        viewingIndex = nil
        turnActive = false
        pendingReplace = false
        canvasLabel = ""
        pendingLabel = ""
        staged = []
        statusText = nil
        errorBanner = nil
        stepTrail = []
        asyncDetached = false
        pendingCommitRequestGeneration = nil
    }

    private func beginLocalOperationSubmission(
        identity: ClientOperationIdentity,
        action: String,
        surface: String,
        chatId: String?,
        exposeStatus: Bool = true
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
        if exposeStatus { statusText = submission.label }
    }

    private func localSubmissionShowsActivity(_ submission: LocalOperationSubmission) -> Bool {
        submission.action != "update_device" && submission.action != "chrome_llm_save"
    }

    /// Rebind one exact retained UI event to this connection before its bytes
    /// leave the offline queue.
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
        if localSubmissionShowsActivity(submission) { statusText = submission.label }
        return true
    }

    private func clearLocalOperationSubmission(requestGeneration: String) {
        localOperationSubmissions = localOperationSubmissions.filter {
            $0.value.requestGeneration != requestGeneration
        }
    }

    private func operationOwnsActiveChatTurn(
        action: String,
        requestGeneration: String
    ) -> Bool {
        action == "chat_message"
            && (continuity.requestGeneration == requestGeneration
                || pendingCommitRequestGeneration == requestGeneration)
    }

    private func settleActiveChatTurn(
        requestGeneration: String,
        discardUncommitted: Bool
    ) {
        turnActive = false
        pendingReplace = false
        liveOpsThisTurn = false
        stepTrail = []
        asyncDetached = false
        // A successful operation status is only lifecycle evidence. The
        // authoritative conversation_snapshot may follow it, so keep the
        // visible transient result until that atomic commit replaces it.
        // Only a definitive failure/refusal is allowed to discard the overlay.
        if discardUncommitted {
            pendingCanvas = []
            transientTurns = []
            transientCanvas = nil
            if pendingCommitRequestGeneration == requestGeneration {
                pendingCommitRequestGeneration = nil
            }
        }
    }

    private func clearPendingOperationSubmissions() {
        let wasSubmitting = statusText == "Submitting…"
        localOperationSubmissions.removeAll()
        if wasSubmitting { statusText = nil }
    }

    private func operationSurface(action: String, payload: JSONValue) -> String {
        if let surface = payload["surface"]?.stringValue,
            surface.range(
                of: "^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
                options: .regularExpression) != nil
        {
            return surface
        }
        if action == "chat_message" || action == "load_chat" { return "chat" }
        return "operation"
    }

    private func operationChatId(action: String, payload: JSONValue) -> String? {
        if action == "chat_message" || action == "load_chat"
            || conversationMutationActions.contains(action)
        {
            return payload["chat_id"]?.stringValue ?? activeChatId
        }
        return nil
    }

    // MARK: actions

    func dismissBanner() { errorBanner = nil }

    func sendChat(_ text: String) {
        if timelineReadOnly { return }
        let ready = staged.filter { $0.state == "ready" && $0.attachmentId != nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty && ready.isEmpty { return }
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
        let bubble =
            ready.isEmpty
            ? text
            : (text + "\n📎 " + ready.map(\.filename).joined(separator: ", ")).trimmingCharacters(in: .whitespaces)
        if continuity.connectionGeneration == nil {
            appendTurn(role: "user", text: bubble)
        } else {
            transientTurns.append(
                ChatTurn(
                    id: "pending-user-\(UUID().uuidString.lowercased())",
                    role: "user",
                    text: bubble))
        }
        turnActive = true
        pendingReplace = true
        pendingCanvas = []
        liveOpsThisTurn = false
        // 055 uniform rule: purge the ephemeral welcome (`wel_` identities)
        // from the committed canvas at turn start — the server no longer
        // sends the blanking `ui_render []` (wire-contract §1). Continuity
        // mode keeps the welcome instead: there the canvas is replaced only by
        // a committed snapshot, and a turn that never produces one must leave
        // the run-examples screen exactly as it found it.
        if continuity.connectionGeneration == nil {
            canvas = canvas.dropWelcome()
        }
        pendingLabel = String((text.isEmpty ? (ready.first?.filename ?? "") : text).prefix(80))
        staged = []
        viewingIndex = nil
        statusText = nil
        errorBanner = nil
        stepTrail = []
        asyncDetached = false

        beginLocalOperationSubmission(
            identity: identity,
            action: "chat_message",
            surface: "chat",
            chatId: activeChatId)

        var payload: [String: JSONValue] = ["message": .string(text)]
        if let cid = activeChatId { payload["chat_id"] = .string(cid) }
        if !ready.isEmpty {
            payload["attachments"] = .array(
                ready.map { att in
                    .object([
                        "attachment_id": .string(att.attachmentId!),
                        "filename": .string(att.filename),
                        "category": .string(att.category),
                    ])
                })
        }
        rawSend(
            Outbound.uiEvent(
                action: "chat_message",
                sessionId: activeChatId,
                payload: .object(payload),
                submissionId: identity.submissionId,
                requestGeneration: request,
                snapshotPurpose: .commit))
    }

    func sendEvent(_ action: String, _ payload: JSONValue = .object([:])) {
        if action == "attach_existing" {
            if stageExistingAttachment(payload) {
                let filename = payload["filename"]?.stringValue ?? "file"
                screen = .chat
                bannerIsError = false
                errorBanner = "Attached \(filename) — it will be sent with your next message"
            }
            return
        }
        if timelineReadOnly && timelineMutations.contains(action) { return }
        if action == "chat_message" {
            turnActive = true
            pendingReplace = true
            pendingCanvas = []
            liveOpsThisTurn = false
            if continuity.connectionGeneration == nil {
                canvas = canvas.dropWelcome()  // 055: same turn-start purge as sendChat
            }
            viewingIndex = nil
            errorBanner = nil
            stepTrail = []
            asyncDetached = false
        }
        if action == "chat_message" {
            let identity = ClientOperationIdentity.fresh()
            let request = identity.requestGeneration
            if let chatId = activeChatId, continuity.connectionGeneration != nil {
                guard
                    openConversationRequest(
                        chatId: chatId,
                        requestGeneration: request,
                        purpose: .commit)
                else { return }
            } else if activeChatId == nil {
                pendingCommitRequestGeneration = request
            }
            beginLocalOperationSubmission(
                identity: identity,
                action: action,
                surface: "chat",
                chatId: activeChatId)
            rawSend(
                Outbound.uiEvent(
                    action: action,
                    sessionId: activeChatId,
                    payload: payload,
                    submissionId: identity.submissionId,
                    requestGeneration: request,
                    snapshotPurpose: .commit))
            return
        }
        if action == "load_chat", let chatId = payload["chat_id"]?.stringValue {
            let identity = ClientOperationIdentity.fresh()
            if continuity.connectionGeneration != nil {
                guard
                    openConversationRequest(
                        chatId: chatId,
                        requestGeneration: identity.requestGeneration,
                        purpose: .hydration)
                else { return }
            }
            beginLocalOperationSubmission(
                identity: identity,
                action: action,
                surface: "chat",
                chatId: chatId)
            rawSend(
                Outbound.loadChat(
                    sessionId: chatId,
                    chatId: chatId,
                    submissionId: identity.submissionId,
                    requestGeneration: identity.requestGeneration))
            return
        }

        let identity = ClientOperationIdentity.fresh()
        let chatId = operationChatId(action: action, payload: payload)
        beginLocalOperationSubmission(
            identity: identity,
            action: action,
            surface: operationSurface(action: action, payload: payload),
            chatId: chatId)
        rawSend(
            Outbound.uiEvent(
                action: action,
                sessionId: chatId,
                payload: payload,
                submissionId: identity.submissionId,
                requestGeneration: identity.requestGeneration))
    }

    /// Submit one server-authored ParamPicker action. Provider Save is the one
    /// specialization: its client UUIDs and local `submitting` projection are
    /// created synchronously before the socket send, while every other action
    /// keeps the generic event path.
    @discardableResult
    func submitParamPicker(
        action: String,
        fields: [String: JSONValue],
        payload: [String: JSONValue]
    ) -> Bool {
        var submittedPayload = payload
        submittedPayload["fields"] = .object(fields)
        guard action == "chrome_llm_save" else {
            emit(action, payload: submittedPayload)
            return true
        }

        if let current = llmFirstLoginOperation,
            !current.isAuthoritativelyTerminal
        {
            if current.state == .unconfirmed {
                Task { await self.reconcileLLMFirstLoginOperation() }
            } else if !current.phaseVisible {
                var focused = current
                focused.phaseVisible = true
                llmFirstLoginOperation = focused
            }
            return false
        }
        guard let connectionGeneration = continuity.connectionGeneration else {
            bannerIsError = true
            errorBanner = "Reconnect before saving your provider settings."
            return false
        }

        let identity = ClientOperationIdentity.fresh()
        let submissionId = identity.submissionId
        let requestGeneration = identity.requestGeneration
        llmFirstLoginOperation = LLMFirstLoginOperation(
            submissionId: submissionId,
            requestGeneration: requestGeneration,
            connectionGeneration: connectionGeneration,
            requiresAdvance: mandatorySurface && pendingSurfaceKey == "llm",
            operationId: nil,
            sequence: nil,
            state: .submitting,
            phase: "submitting",
            label: "Submitting…",
            retryable: false,
            errorCode: nil,
            errorMessage: nil,
            phaseVisible: false,
            isAuthoritativelyTerminal: false,
            didAdvance: false)
        armLLMFirstLoginTimers(submissionId: submissionId)

        beginLocalOperationSubmission(
            identity: identity,
            action: action,
            surface: "llm_settings",
            chatId: nil,
            exposeStatus: false)

        submittedPayload["surface"] = .string("llm_settings")
        rawSend(
            Outbound.uiEvent(
                action: action,
                sessionId: nil,
                payload: .object(submittedPayload),
                submissionId: submissionId,
                requestGeneration: requestGeneration))
        return true
    }

    /// Bridge for the component renderer's `emit(action, payload)` callback.
    func emit(_ action: String, payload: [String: JSONValue] = [:]) {
        sendEvent(action, .object(payload))
    }

    private func rawSend(_ text: String) {
        outboundTap?(text)
        Task { await ws?.send(text) }
    }

    /// Voice final transcripts pass through the same conversation fence and
    /// local operation bookkeeping as typed chat, while retries reuse the
    /// exact serialized frame and never duplicate the optimistic bubble.
    @discardableResult
    func sendVoiceWire(_ text: String) -> Bool {
        guard signedIn, connected,
            currentConnectionVoiceSendOverride != nil || ws != nil,
            VoiceCurrentConnectionFrame(frameText: text) != nil,
            let frame = InboundFrame.parse(text)
        else { return false }
        if frame.name == "ui_event", frame.payload["action"]?.stringValue == "chat_message",
            let submission = frame.payload["submission_id"]?.stringValue,
            let request = frame.payload["request_generation"]?.stringValue,
            let identity = ClientOperationIdentity(
                submissionId: submission, requestGeneration: request),
            let chatId = frame.payload["session_id"]?.stringValue
        {
            if localOperationSubmissions[submission] == nil {
                guard
                    continuity.connectionGeneration == nil
                        || openConversationRequest(
                            chatId: chatId, requestGeneration: request, purpose: .commit)
                else { return false }
                beginLocalOperationSubmission(
                    identity: identity, action: "chat_message", surface: "chat",
                    chatId: chatId)
                let message = frame.payload["payload"]?["message"]?.stringValue ?? ""
                if continuity.connectionGeneration == nil {
                    appendTurn(role: "user", text: message)
                } else {
                    transientTurns.append(
                        ChatTurn(
                            id: "pending-voice-\(submission)", role: "user",
                            text: message))
                }
                turnActive = true
                pendingReplace = true
                pendingCanvas = []
                liveOpsThisTurn = false
            }
        }
        outboundTap?(text)
        if let override = currentConnectionVoiceSendOverride {
            override(text)
        } else if let ws {
            Task { _ = await ws.sendCurrentConnectionVoice(text) }
        }
        return true
    }

    func activateVoice() async { await voice.activate() }

    func performVoiceControl(_ action: VoiceControlAction) async {
        await voice.perform(action)
    }

    func voiceSceneBecameActive() { voice.sceneBecameActive() }

    func voiceSceneBecameInactive() { voice.sceneBecameInactive() }

    func voiceAudioSessionInterrupted() { voice.audioSessionInterrupted() }

    func voiceAudioSessionInterruptionEnded() { voice.audioSessionInterruptionEnded() }

    func voiceAudioRouteChanged() { voice.audioRouteChanged() }

    func voiceAudioEngineConfigurationChanged() { voice.audioEngineConfigurationChanged() }

    func voiceSessionLocked() { voice.sessionLocked() }

    func voiceSessionUnlocked() { voice.sessionUnlocked() }

    func voiceApplicationWillTerminate() { voice.close() }

    // MARK: refine + export (055 US4/US5)

    /// 055 US4: component-scoped refine (wire-contract §3). The instruction
    /// comes from the context-menu sheet; an empty one never reaches the wire.
    func refineComponent(_ componentId: String, instruction: String) {
        let trimmed = instruction.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !componentId.isEmpty, !trimmed.isEmpty else { return }
        sendEvent(
            "component_refine",
            .object([
                "component_id": .string(componentId),
                "instruction": .string(trimmed),
            ]))
    }

    /// 055 US5: CSV export URL for one table component, chat-scoped per the
    /// REST contract; opened in the system browser (session-authed route).
    func exportComponentURL(_ componentId: String) -> URL? {
        guard let chatId = activeChatId, !chatId.isEmpty, !componentId.isEmpty else { return nil }
        let base = serverBase.appendingPathComponent("api/export/component/\(componentId).csv")
        var comps = URLComponents(url: base, resolvingAgainstBaseURL: false)
        comps?.queryItems = [URLQueryItem(name: "chat_id", value: chatId)]
        return comps?.url
    }

    /// 055 US5: self-contained HTML export of the current chat's canvas.
    func exportCanvasURL() -> URL? {
        guard let chatId = activeChatId, !chatId.isEmpty else { return nil }
        return serverBase.appendingPathComponent("api/export/canvas/\(chatId).html")
    }

    func newChat() {
        if mandatorySurface { return }  // 054: navigation pinned (sign-out only)
        if let account = conversationAccount {
            _ = conversationResumeStore.clear(.newChat, for: account)
        }
        seqState.removeAll()
        clearContinuityChatKeepingConnection()
        resetChatState()
        screen = .chat
        sendEvent("new_chat")
    }

    func openChat(_ chatId: String) {
        if let account = conversationAccount {
            guard conversationResumeStore.save(chatId: chatId, for: account) else { return }
        }
        activeChatId = chatId
        voice.updateVisibleChatLocally(chatId)
        let identity = ClientOperationIdentity.fresh()
        let request = identity.requestGeneration
        if continuity.connectionGeneration != nil {
            guard
                openConversationRequest(
                    chatId: chatId,
                    requestGeneration: request,
                    purpose: .hydration)
            else { return }
        }
        beginLocalOperationSubmission(
            identity: identity,
            action: "load_chat",
            surface: "chat",
            chatId: chatId)
        rawSend(
            Outbound.loadChat(
                sessionId: chatId,
                chatId: chatId,
                submissionId: identity.submissionId,
                requestGeneration: request))
        screen = .chat
        viewingIndex = nil
    }

    /// FR-011: history offers open AND delete (server-side via REST, like the
    /// Android/Windows twins), then refreshes the list.
    func deleteChat(_ chatId: String) {
        Task {
            guard (try? await rest.deleteChat(id: chatId)) == true else { return }
            history.removeAll { $0.id == chatId }
            clearConfirmedDeletion(chatId)
            sendEvent("get_history")
        }
    }

    func goTo(_ target: Screen) {
        if mandatorySurface { return }  // 054: navigation pinned (sign-out only)
        screen = target
        agentsLoading = target == .agents || agentsLoading
        historyLoading = target == .history || historyLoading
        auditLoading = target == .audit || auditLoading
        switch target {
        case .agents: sendEvent("discover_agents")
        case .history: sendEvent("get_history")
        case .audit: loadAudit()
        case .chat, .surface: break
        }
    }

    func openMenuItem(_ item: ChromeMenuItem) { openSurface(item.surface, params: item.params) }

    func openSurface(_ surface: String, params: JSONValue = .object([:])) {
        if mandatorySurface { return }  // 054: the pinned surface can't be replaced client-side
        switch surface {
        case "agents": goTo(.agents)
        case "audit": goTo(.audit)
        default:
            sendEvent("chrome_open", .object(["surface": .string(surface), "params": params]))
            screen = .surface
            pendingSurfaceKey = surface
            pendingSurfaceParams = params
            pendingSurface = nil
        }
    }

    func retryPendingSurface() {
        guard !pendingSurfaceKey.isEmpty else { return }
        sendEvent("chrome_open", .object(["surface": .string(pendingSurfaceKey), "params": pendingSurfaceParams]))
    }

    /// Dismiss the open settings surface — the native twin of web's
    /// `astral-modal-close` ✕ (`client.js closeModal()`) and Android's system
    /// Back. Both are LOCAL dismissals, so this sends no frame; the server
    /// keeps no per-socket surface state to release. Refused while the 054
    /// mandatory pin is set, exactly as web's `data-mandatory` card refuses
    /// every dismissal affordance (FR-013: sign-out stays the one escape).
    func closeSurface() {
        if mandatorySurface { return }
        guard screen == .surface else { return }
        screen = .chat
        pendingSurface = nil
        pendingSurfaceKey = ""
        pendingSurfaceParams = .object([:])
    }

    func setToolEnabled(_ agent: Agent, tool: String, enabled: Bool) {
        patchAgent(agent.id) { a in
            var perms = a.permissions
            perms[tool] = enabled
            var copy = a
            copy.permissions = perms
            return copy
        }
        let kind = agent.toolScopeMap[tool] ?? "tools:read"
        Task {
            _ = await rest.setToolPermission(agentId: agent.id, tool: tool, kind: kind, enabled: enabled)
            sendEvent("discover_agents")
        }
    }

    func setAgentEnabled(_ agent: Agent, enabled: Bool) {
        patchAgent(agent.id) { a in
            var copy = a
            copy.permissions = Dictionary(uniqueKeysWithValues: a.tools.map { ($0, enabled) })
            return copy
        }
        let kinds = agent.toolScopeMap.values.isEmpty ? Array(agent.scopes.keys) : Array(Set(agent.toolScopeMap.values))
        var scopes: [String: JSONValue] = [:]
        for k in kinds { scopes[k] = .bool(enabled) }
        var overrides: [String: JSONValue] = [:]
        for t in agent.tools { overrides[t] = .bool(enabled) }
        sendEvent(
            "set_agent_permissions",
            .object([
                "agent_id": .string(agent.id),
                "scopes": .object(scopes),
                "tool_overrides": .object(overrides),
            ]))
        sendEvent("discover_agents")
    }

    private func patchAgent(_ agentId: String, _ transform: (Agent) -> Agent) {
        agents = agents.map { $0.id == agentId ? transform($0) : $0 }
    }

    func enableRecommended() {
        sendEvent("enable_recommended_agents")
        sendEvent("discover_agents")
    }

    private func loadAudit() {
        Task {
            let events = await rest.audit()
            audit = events
            auditLoading = false
        }
    }

    func viewCanvasSnapshot(_ index: Int) {
        if canvasHistory.indices.contains(index) { viewingIndex = index }
    }

    func backToLiveCanvas() { viewingIndex = nil }

    // MARK: attachments

    func stageAttachment(filename: String, mimeType: String?, data: Data) {
        attachSeq += 1
        let uid = attachSeq
        staged.append(
            StagedAttachment(
                uid: uid, filename: filename, category: "file",
                attachmentId: nil, state: "uploading", note: nil))
        Task {
            let up = await rest.uploadAttachment(filename: filename, mimeType: mimeType, data: data)
            staged = staged.map { a in
                guard a.uid == uid else { return a }
                var copy = a
                if let up {
                    copy.attachmentId = up.attachmentId
                    copy.category = up.category
                    copy.state = "ready"
                    copy.note = parserNote(up.parserStatus)
                } else {
                    copy.state = "failed"
                    copy.note = "upload failed"
                }
                return copy
            }
        }
    }

    /// Stage a picked or dropped file URL (file importer, macOS drag-and-drop).
    func stageFile(url: URL) {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        guard let data = try? Data(contentsOf: url) else {
            bannerIsError = true
            errorBanner = "Couldn't read \(url.lastPathComponent)"
            return
        }
        let mime = UTType(filenameExtension: url.pathExtension)?.preferredMIMEType
        stageAttachment(filename: url.lastPathComponent, mimeType: mime, data: data)
    }

    func removeAttachment(_ uid: Int) {
        staged.removeAll { $0.uid == uid }
    }

    private func stageExistingAttachment(_ payload: JSONValue) -> Bool {
        guard let id = payload["attachment_id"]?.stringValue, !id.isEmpty else { return false }
        if staged.contains(where: { $0.attachmentId == id }) { return true }
        attachSeq += 1
        staged.append(
            StagedAttachment(
                uid: attachSeq,
                filename: payload["filename"]?.stringValue ?? "attachment",
                category: payload["category"]?.stringValue ?? "file",
                attachmentId: id, state: "ready", note: nil))
        return true
    }

    // MARK: connection label (parity with Android connectionStripLabel)

    var connectionStripLabel: String? {
        if !everConnected || connected { return nil }
        return "Reconnecting…"
    }
}

private actor AppRegistrationResumeState {
    private var next: Bool

    init(initial: Bool) {
        next = initial
    }

    func consume() -> Bool {
        let value = next
        next = true
        return value
    }
}

extension AppModel: ASWebAuthenticationPresentationContextProviding {
    nonisolated func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        MainActor.assumeIsolated {
            #if os(macOS)
                NSApplication.shared.mainWindow ?? ASPresentationAnchor()
            #else
                UIApplication.shared.connectedScenes
                    .compactMap { ($0 as? UIWindowScene)?.keyWindow }
                    .first ?? ASPresentationAnchor()
            #endif
        }
    }
}
