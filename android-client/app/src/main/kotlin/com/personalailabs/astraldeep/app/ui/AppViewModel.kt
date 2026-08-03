package com.personalailabs.astraldeep.app.ui

import android.util.Log
import androidx.compose.runtime.Immutable
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.personalailabs.astraldeep.app.auth.ConversationResumeStore
import com.personalailabs.astraldeep.app.auth.ConversationResumeStore.AccountIdentity
import com.personalailabs.astraldeep.app.auth.ConversationResumeStore.ClearReason
import com.personalailabs.astraldeep.app.rest.AstralRest
import com.personalailabs.astraldeep.app.rest.AuditEvent
import com.personalailabs.astraldeep.app.transport.ConnectionState
import com.personalailabs.astraldeep.app.transport.ConversationGenerationBinding
import com.personalailabs.astraldeep.app.transport.ConversationRequestPurpose
import com.personalailabs.astraldeep.app.transport.LocalSubmission
import com.personalailabs.astraldeep.app.transport.OrchestratorClient
import com.personalailabs.astraldeep.app.transport.QueuedSubmissionFailure
import com.personalailabs.astraldeep.app.ui.theme.ThemePalette
import com.personalailabs.astraldeep.app.ui.theme.themePaletteForSpec
import com.personalailabs.astraldeep.app.voice.VoiceMediaCapability
import com.personalailabs.astraldeep.app.voice.VoiceSessionController
import com.personalailabs.astraldeep.app.voice.VoiceUiState
import com.personalailabs.astraldeep.core.chrome.ChromeMenuModel
import com.personalailabs.astraldeep.core.chrome.MenuItem
import com.personalailabs.astraldeep.core.protocol.Agent
import com.personalailabs.astraldeep.core.protocol.ChatAttachment
import com.personalailabs.astraldeep.core.protocol.ChatSummary
import com.personalailabs.astraldeep.core.protocol.DeviceCapabilities
import com.personalailabs.astraldeep.core.protocol.Inbound
import com.personalailabs.astraldeep.core.protocol.ProtocolManifest
import com.personalailabs.astraldeep.core.protocol.VoiceControl
import com.personalailabs.astraldeep.core.sdui.Canvas
import com.personalailabs.astraldeep.core.sdui.CanvasOp
import com.personalailabs.astraldeep.core.sdui.Component
import com.personalailabs.astraldeep.core.streaming.streamErrorOps
import com.personalailabs.astraldeep.core.streaming.streamFrameToOps
import com.personalailabs.astraldeep.core.streaming.subscribeAckOps
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/** Canonical transcript part disposition used by the native message renderer. */
enum class ChatSegmentKind { TEXT, COMPONENTS, STRUCTURED, RECOVERY }

/** One ordered, semantically retained transcript part. */
@Immutable
data class ChatSegment(
    val kind: ChatSegmentKind,
    val text: String,
    val components: List<Component> = emptyList(),
    val structuredValue: JsonElement? = null,
)

/** Visible transcript turn with ordered semantic parts and retained attachments. */
@Immutable
data class ChatTurn(
    val role: String,
    val text: String,
    val segments: List<ChatSegment> = emptyList(),
    val attachments: List<JsonObject> = emptyList(),
    val messageId: String? = null,
    val createdAt: String? = null,
) {
    val hasVisibleContent: Boolean
        get() = text.isNotBlank() || segments.any { it.components.isNotEmpty() } || attachments.isNotEmpty()
}

/**
 * The top-level navigable surfaces. Settings is no longer a screen — it is the
 * server-driven dropdown from the top-bar gear (feature 042); items route to the
 * native Agents/Audit screens or, for any other surface, the SDUI [Surface] screen
 * (chrome_open → chrome_surface, rendered natively).
 */
enum class Screen { Chat, Agents, History, Audit, Surface }

/** A paperclip-staged upload chip (feature 031). */
@Immutable
data class StagedAttachment(
    val uid: Long,
    val filename: String,
    val category: String,
    val attachmentId: String?,
    /** "uploading" | "ready" | "failed" */
    val state: String,
    val note: String? = null,
)

/** A read-only snapshot of a past turn's finished canvas (client-side timeline). */
@Immutable
data class CanvasSnapshot(val label: String, val components: List<Component>)

@Immutable
data class UiState(
    val connection: ConnectionState = ConnectionState.Disconnected,
    val screen: Screen = Screen.Chat,
    val activeChatId: String? = null,
    /** Last complete server-owned transcript. Pending turns live separately. */
    val turns: List<ChatTurn> = emptyList(),
    val pendingTurns: List<ChatTurn> = emptyList(),
    // canvas lifecycle: identity-keyed ops morph the live canvas as they arrive
    // (even mid-turn — 055 live-op rule); only in-turn FULL renders buffer.
    val canvas: List<Component> = emptyList(),
    /** Disposable request-scoped preview. Null means show committed [canvas]. */
    val transientCanvas: List<Component>? = null,
    /** Spec 060 equality fence and per-chat committed revision. */
    val connectionGeneration: String? = null,
    val requestGeneration: String? = null,
    val requestChatId: String? = null,
    val requestPurpose: ConversationRequestPurpose? = null,
    val expectedCommitRenderRevision: ULong? = null,
    val lastCommittedRenderRevision: ULong = 0UL,
    val lastTransientFrameSequence: ULong = 0UL,
    val hydrationApplied: Boolean = false,
    val acceptedSnapshotId: String? = null,
    val acceptedSnapshot: Inbound.ConversationSnapshot? = null,
    /** Buffer built from a replacing turn's full renders; committed on `done`. */
    val pendingCanvas: List<Component> = emptyList(),
    /** Orchestrator is working this turn (drives the thin progress indicator). */
    val turnActive: Boolean = false,
    /** This turn will REPLACE the canvas on completion (a user chat turn). */
    val pendingReplace: Boolean = false,
    /**
     * Snapshot of the committed canvas at turn arming — the timeline archives
     * THIS at commit, since in-turn ops now morph [canvas] itself.
     */
    val preTurnCanvas: List<Component> = emptyList(),
    /**
     * An in-turn op has landed on the live canvas: clears the query skeleton
     * (first canvas content, matching the web) and makes the live canvas the
     * committed state when no full render was buffered.
     */
    val turnOpsApplied: Boolean = false,
    /** Label describing the current committed canvas (the prompt that made it). */
    val canvasLabel: String = "",
    /** Label for the in-flight replacing turn. */
    val pendingLabel: String = "",
    /** Previous turns' finished canvases, oldest→newest (read-only timeline). */
    val canvasHistory: List<CanvasSnapshot> = emptyList(),
    /** When non-null, the canvas area shows this history entry read-only. */
    val viewingIndex: Int? = null,
    // --- input / chrome ---
    val staged: List<StagedAttachment> = emptyList(),
    val statusText: String? = null,
    /** Transient dismissible banner (server errors, offline drops, notifications). */
    val banner: String? = null,
    /** Banner severity — "error" | "info" — drives the bar's styling. */
    val bannerKind: String = "error",
    /** The running turn's execution trail (chat_step/tool_progress lines), capped. */
    val stepTrail: List<String> = emptyList(),
    /** The turn detached into a background task (task_started) — UI can relax. */
    val asyncDetached: Boolean = false,
    /** True once this session has connected — gates the "Reconnecting…" strip. */
    val everConnected: Boolean = false,
    val agents: List<Agent> = emptyList(),
    /** Local-only `submitting` attempts, keyed by their request generation. */
    val pendingSubmissions: Map<String, LocalSubmission> = emptyMap(),
    /** Highest canonical projection retained for each durable operation. */
    val operationStatuses: Map<String, Inbound.OperationStatus> = emptyMap(),
    /** Highest `(lifecycleGeneration, stateRevision)` retained per agent. */
    val agentLifecycles: Map<String, Inbound.AgentLifecycle> = emptyMap(),
    val history: List<ChatSummary> = emptyList(),
    val audit: List<AuditEvent> = emptyList(),
    // Per-surface "fetching its data" flags → skeletons on the list screens.
    val agentsLoading: Boolean = false,
    val historyLoading: Boolean = false,
    val auditLoading: Boolean = false,
    // The server-owned chrome model (top bar + settings menu). Rendered verbatim
    // (already role-filtered by the server) — the client never hard-codes the menu.
    val chromeMenu: ChromeMenuModel? = null,
    /** The surface key the client asked to open — used to retry a stalled surface (T039). */
    val pendingSurfaceKey: String = "",
    /** The params the surface was opened with — retried verbatim so a stalled
     *  surface reopens in the same state (e.g. a specific tab), not its default. */
    val pendingSurfaceParams: JsonObject = JsonObject(emptyMap()),
    /** Feature 043 — the SDUI settings surface currently delivered (native render). */
    val pendingSurface: Inbound.ChromeSurface? = null,
    /** Live theme palette (feature 044 US5); null = the default brand dark scheme. */
    val themePalette: ThemePalette? = null,
    /** Feature 028/044 — the read-only workspace timeline is being viewed (mutations paused). */
    val timelineReadOnly: Boolean = false,
    /**
     * Feature 054 — a `mode:"mandatory"` chrome surface (the first-run LLM-setup
     * gate) is pinned: navigation and system Back are suppressed until the
     * server's blank-key close frame clears it. Sign-out stays enabled (FR-013).
     */
    val mandatorySurface: Boolean = false,
) {
    /** What the canvas area actually renders (a history entry, or the live canvas). */
    val visibleCanvas: List<Component>
        get() = viewingIndex?.let { canvasHistory.getOrNull(it)?.components } ?: (transientCanvas ?: canvas)

    /** Pending user/preview turns are overlays and never mutate committed transcript. */
    val visibleTurns: List<ChatTurn> get() = turns + pendingTurns

    val isViewingHistory: Boolean get() = viewingIndex != null

    /** Canonical work still active on this connection, newest update first. */
    val activeOperationStatus: Inbound.OperationStatus?
        get() =
            operationStatuses.values
                .asSequence()
                .filter {
                    !it.terminal &&
                        it.connectionGeneration == connectionGeneration &&
                        (it.chatId == null || it.chatId == activeChatId)
                }
                .maxWithOrNull(compareBy<Inbound.OperationStatus> { it.updatedAt }.thenBy { it.operationId })

    /**
     * Status copy belongs to active work, not to the settled chat. A local
     * submission remains visible until admission; afterward the canonical
     * non-terminal operation label wins. Retained terminals are reconciliation
     * evidence only and never revive idle chrome.
     */
    val hasActiveWork: Boolean
        get() = turnActive || pendingSubmissions.isNotEmpty() || activeOperationStatus != null

    val workingStatusText: String?
        get() =
            activeOperationStatus?.label
                ?: statusText?.takeIf { turnActive }
                ?: "Submitting…".takeIf { pendingSubmissions.isNotEmpty() }

    /**
     * Skeletons show from send until the turn's FIRST live canvas content lands
     * (identity-keyed ops apply immediately — 055 live rule, matching the web's
     * hide-on-first-content) or, for a turn with none, until `done` commits.
     */
    val showSkeleton: Boolean
        get() = pendingReplace && !turnOpsApplied && viewingIndex == null

    /**
     * Mutating affordances are locked while the read-only workspace timeline is
     * being viewed (T041) — the composer/send and component re-execution are
     * disabled until the live view is restored.
     */
    val mutationsLocked: Boolean get() = timelineReadOnly
}

/**
 * The `ui_event` actions refused while the read-only workspace-timeline snapshot
 * is active (T041). Covers the real mutation entry points reachable from rendered
 * components — chat send, component actions (incl. the 055 refine/restore verbs),
 * table pagination, and theme saves.
 * Navigation (chrome_open, load_chat, discover_agents, …) and the timeline-exit
 * action stay allowed so the user is never trapped. Pure → unit-tested.
 */
internal fun isTimelineMutation(action: String): Boolean = action in TIMELINE_MUTATIONS

/** A delayed or foreign no-chat voice response must never switch the visible conversation. */
internal fun isExpectedVoiceChatCreation(
    state: UiState,
    pending: LocalSubmission,
    message: Inbound.ChatCreated,
): Boolean =
    state.activeChatId == null &&
        message.chatId != null &&
        message.fromMessage == false &&
        message.connectionGeneration == state.connectionGeneration &&
        message.submissionId == pending.submissionId &&
        message.requestGeneration == pending.requestGeneration

/** Only session acquisition needs a chat-binding preflight before its REST action. */
internal fun voiceControlNeedsChatPreflight(action: String): Boolean =
    action == "voice_session_start" || action == "voice_session_takeover"

private val TIMELINE_MUTATIONS =
    setOf("chat_message", "component_action", "component_refine", "component_restore", "table_paginate", "save_theme")

/**
 * Owns the connection + derived UI state. Folds each [Inbound] into [state] and
 * sends chat/events out. Identity-keyed canvas ops (`ui_upsert`, streaming)
 * apply to the LIVE canvas immediately — even mid-turn — so the originating
 * Spec 060 conversation frames keep server snapshots as the sole committed
 * transcript/canvas publication. Scoped render/upsert/stream frames update only
 * a disposable preview; stale generations never cross the equality fence. The
 * legacy reducer remains available for a no-generation compatibility session.
 */
class AppViewModel(
    private val client: OrchestratorClient,
    private val rest: AstralRest,
    private val resumeStore: ConversationResumeStore? = null,
    private val voiceController: VoiceSessionController? = null,
) : ViewModel() {
    private data class PendingVoiceActivation(
        val action: String,
        val capability: VoiceMediaCapability,
        val submission: LocalSubmission,
    )

    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()
    private val unavailableVoice = MutableStateFlow(VoiceUiState.Unavailable)
    val voiceState: StateFlow<VoiceUiState> = voiceController?.state ?: unavailableVoice.asStateFlow()

    private var session: Job? = null
    private var snapshotTimeout: Job? = null
    private var token: String? = null
    private var device: DeviceCapabilities? = null
    private var account: AccountIdentity? = null
    private var attachSeq: Long = 0
    private val seqState = mutableMapOf<String, Int>()
    private var pendingVoiceActivation: PendingVoiceActivation? = null

    init {
        voiceController?.setTranscriptSubmitter { transcript, connectionGeneration ->
            client.sendVoiceTranscript(transcript, connectionGeneration) { submission ->
                _state.update { current ->
                    if (current.pendingSubmissions.containsKey(submission.requestGeneration)) {
                        projectLocalSubmission(current, submission)
                    } else {
                        val armed = armTurn(current)
                        projectLocalSubmission(
                            armed.copy(
                                pendingTurns = armed.pendingTurns + ChatTurn("user", transcript.text),
                                pendingLabel = transcript.text.take(80),
                            ),
                            submission,
                        )
                    }
                }
            }
        }
        voiceController?.setPlayoutReporter(client::sendVoicePlayoutEvent)
    }

    /** Begin (or restart) the session with a bearer token + device caps. */
    fun start(
        token: String,
        device: DeviceCapabilities,
    ) {
        this.token = token
        this.device = device
        val nextAccount = ConversationResumeStore.accountFromAccessToken(token)
        val previousAccount = account
        if (previousAccount != null && nextAccount != null && previousAccount != nextAccount) {
            resumeStore?.clear(previousAccount, ClearReason.ACCOUNT_SWITCH_OR_REMOVAL)
        }
        account = nextAccount
        val locatedChat = nextAccount?.let { resumeStore?.load(it)?.chatId }
        if (previousAccount != nextAccount) {
            _state.value =
                UiState(
                    activeChatId = locatedChat,
                    themePalette = _state.value.themePalette,
                )
        } else if (_state.value.activeChatId == null && locatedChat != null) {
            _state.value = _state.value.copy(activeChatId = locatedChat)
        }
        session?.cancel()
        snapshotTimeout?.cancel()
        seqState.clear()
        client.observeConversationGenerations(::installConversationGeneration)
        session =
            viewModelScope.launch {
                launch {
                    client
                        .stream(
                            token = token,
                            device = device,
                            sessionId = { _state.value.activeChatId },
                            onGeneration = ::installConversationGeneration,
                            onQueuedSubmission = ::installQueuedSubmission,
                        ).collect { msg ->
                            voiceController?.handleInbound(msg)
                            val before = _state.value
                            val retryChat = snapshotRetryTarget(before, msg)
                            val after = reduceWithPersistence(before, msg)
                            _state.value = after
                            handleVoiceAfterReduction(before, after, msg)
                            when {
                                retryChat != null -> {
                                    snapshotTimeout?.cancel()
                                    _state.update { current ->
                                        current.copy(statusText = "Conversation restore failed; retrying…")
                                    }
                                    requestChatRefresh(retryChat)
                                }
                                msg is Inbound.ConversationCommitReady &&
                                    after.requestGeneration == msg.requestGeneration &&
                                    after.expectedCommitRenderRevision == msg.renderRevision -> {
                                    scheduleSnapshotTimeout(
                                        ConversationGenerationBinding(
                                            connectionGeneration = msg.connectionGeneration,
                                            chatId = msg.chatId,
                                            requestGeneration = msg.requestGeneration,
                                            purpose = ConversationRequestPurpose.COMMIT,
                                        ),
                                        msg.renderRevision,
                                    )
                                }
                                msg is Inbound.ConversationSnapshot && after !== before &&
                                    after.lastCommittedRenderRevision == msg.renderRevision -> {
                                    snapshotTimeout?.cancel()
                                }
                            }
                            // Cross-device continuity (audit item 12): a background
                            // result landing in the OPEN chat re-issues load_chat so
                            // narrative + canvas refresh without a manual reopen.
                            continuityReloadTarget(before, msg)?.let(::requestChatRefresh)
                        }
                }
                launch {
                    client.state.collect { c ->
                        _state.update { current -> reduceConnectionState(current, c) }
                        if (c == ConnectionState.Disconnected) {
                            snapshotTimeout?.cancel()
                            voiceController?.connectionLost()
                        }
                    }
                }
                launch {
                    // A frame dropped from the full offline queue is never silent
                    // (T014): tell the user which action was lost.
                    client.dropped.collect { action ->
                        _state.value =
                            _state.value.copy(
                                banner = "Not sent while offline: $action (queue full)",
                                bannerKind = "error",
                            )
                    }
                }
                launch {
                    client.queuedFailures.collect { failure ->
                        _state.update { current -> reduceQueuedFailure(current, failure) }
                    }
                }
            }
    }

    /** Dismiss the transient banner (the ✕ on the banner bar). */
    fun dismissBanner() {
        _state.value = _state.value.copy(banner = null)
    }

    fun sendChat(text: String) {
        val s = _state.value
        // Viewing the read-only timeline: refuse a new turn (mutations paused, T041).
        if (s.timelineReadOnly) return
        val ready = s.staged.filter { it.state == "ready" && it.attachmentId != null }
        if (text.isBlank() && ready.isEmpty()) return
        val bubble =
            if (ready.isEmpty()) {
                text
            } else {
                (text + "\n📎 " + ready.joinToString(", ") { it.filename }).trim()
            }
        _state.value =
            armTurn(s).copy(
                pendingTurns = s.pendingTurns + ChatTurn("user", bubble),
                pendingLabel = (text.ifBlank { ready.firstOrNull()?.filename ?: "" }).take(80),
                staged = emptyList(),
            )
        val attachments = ready.map { ChatAttachment(it.attachmentId!!, it.filename, it.category) }
        client.sendChat(text, _state.value.activeChatId, attachments) { submission ->
            _state.update { current -> projectLocalSubmission(current, submission) }
        }
    }

    /** Execute only a visible, enabled server-owned voice composer control. */
    fun invokeVoiceControl(
        control: VoiceControl,
        capability: VoiceMediaCapability,
    ) {
        val controller = voiceController ?: return
        if (!control.visible || !control.enabled || _state.value.timelineReadOnly) return
        if (control.action in setOf("voice_session_start", "voice_session_takeover") && _state.value.activeChatId == null) {
            controller.awaitingChat()
            val submission = client.createChatForVoice()
            if (submission == null) {
                controller.activationFailed("network_interrupted", "Voice needs a live connection. You can keep typing.")
            } else {
                pendingVoiceActivation = PendingVoiceActivation(control.action, capability, submission)
            }
            return
        }
        if (voiceControlNeedsChatPreflight(control.action)) {
            _state.value.activeChatId?.let(controller::updateVisibleChatLocally)
        }
        viewModelScope.launch {
            when (control.action) {
                "voice_session_start" -> controller.activate(capability)
                "voice_session_takeover" -> controller.takeOver(capability)
                "voice_session_end" -> controller.end()
                "voice_microphone_set" -> controller.setMicrophoneEnabled(!control.pressed)
                "voice_speech_stop" -> controller.stopSpeech()
                "voice_speech_mute_set" -> controller.setSpeechMuted(!control.pressed)
                "voice_visible_chat_update" -> _state.value.activeChatId?.let(controller::updateVisibleChatLocally)
                "voice_sensitive_recap_request" ->
                    _state.update {
                        it.copy(
                            banner = "Sensitive spoken recap consent is not available on this build.",
                            bannerKind = "info",
                        )
                    }
            }
        }
    }

    /** Permission/no-device failures still use the same controller feedback surface. */
    fun reportVoiceCapability(capability: VoiceMediaCapability) {
        val controller = voiceController ?: return
        viewModelScope.launch { controller.activate(capability) }
    }

    private fun handleVoiceAfterReduction(
        before: UiState,
        after: UiState,
        message: Inbound,
    ) {
        val controller = voiceController ?: return
        if (before.activeChatId != after.activeChatId) controller.updateVisibleChatLocally(after.activeChatId)
        val pending = pendingVoiceActivation ?: return
        if (
            message is Inbound.ChatCreated &&
            before.activeChatId != null &&
            message.connectionGeneration == before.connectionGeneration &&
            message.submissionId == pending.submission.submissionId &&
            message.requestGeneration == pending.submission.requestGeneration
        ) {
            pendingVoiceActivation = null
            controller.activationFailed(
                "chat_context_unavailable",
                "Voice start was cancelled because you changed conversations.",
            )
            return
        }
        if (
            message !is Inbound.ChatCreated || message.chatId == null || message.chatId != after.activeChatId ||
            message.fromMessage != false || message.connectionGeneration != after.connectionGeneration ||
            message.submissionId != pending.submission.submissionId ||
            message.requestGeneration != pending.submission.requestGeneration
        ) {
            return
        }
        pendingVoiceActivation = null
        viewModelScope.launch {
            when (pending.action) {
                "voice_session_start" -> controller.activate(pending.capability)
                "voice_session_takeover" -> controller.takeOver(pending.capability)
            }
        }
    }

    fun sendEvent(
        action: String,
        payload: JsonObject = JsonObject(emptyMap()),
    ) {
        // `attach_existing` is a CLIENT-LOCAL action (ui_protocol.json
        // client_local_actions): the attachments library's "Attach" button stages
        // the already-uploaded file as a chip HERE — it is never forwarded to the
        // server (mirrors the web paperclip "Choose from your files", T047).
        if (action == "attach_existing") {
            if (stageExistingAttachment(payload)) {
                // The web modal CLOSES on Attach; the native twin returns to the
                // chat so the staged chip is immediately visible in the composer.
                // Staying on the surface gave no feedback at all — the button
                // read as dead, and backing out via "+ New" wiped the chip.
                val filename = (payload["filename"] as? JsonPrimitive)?.contentOrNull ?: "file"
                _state.value =
                    _state.value.copy(
                        screen = Screen.Chat,
                        banner = "Attached $filename — it will be sent with your next message",
                        bannerKind = "info",
                    )
            }
            return
        }
        // Viewing the read-only timeline: refuse mutating events (T041); navigation
        // and the timeline-exit action still flow so the user is never trapped.
        if (_state.value.timelineReadOnly && isTimelineMutation(action)) return
        // A rendered control that submits a chat turn (e.g. an example card) goes
        // through sendEvent, not sendChat — mirror the optimistic turn-start so the
        // canvas shows the skeleton the instant it's tapped, not only once the
        // server acks the turn.
        if (action == "chat_message") {
            _state.value = armTurn(_state.value)
        }
        client.sendEvent(action, _state.value.activeChatId, payload) { submission ->
            _state.update { current -> projectLocalSubmission(current, submission) }
        }
    }

    /** Install the local-only status before any authoritative operation frame is reduced. */
    internal fun projectLocalSubmission(
        s: UiState,
        submission: LocalSubmission,
    ): UiState =
        s.copy(
            pendingSubmissions = s.pendingSubmissions + (submission.requestGeneration to submission),
            statusText = "Submitting…",
        )

    /** Restore an exact queued projection before the transport replays it. */
    private fun installQueuedSubmission(submission: LocalSubmission) {
        _state.update { current -> projectLocalSubmission(current, submission) }
    }

    /** Settle only the projection whose queued bytes were visibly discarded. */
    internal fun reduceQueuedFailure(
        s: UiState,
        failure: QueuedSubmissionFailure,
    ): UiState {
        val retained =
            s.pendingSubmissions.filterValues {
                it.submissionId != failure.submission.submissionId
            }
        val ownsCurrentChatTurn =
            failure.submission.action == "chat_message" &&
                (
                    failure.submission.requestGeneration == s.requestGeneration ||
                        (
                            s.requestGeneration == null &&
                                s.pendingSubmissions.keys.lastOrNull() == failure.submission.requestGeneration
                        )
                )
        return s.copy(
            pendingSubmissions = retained,
            statusText =
                when {
                    !ownsCurrentChatTurn && s.turnActive -> s.statusText
                    retained.isNotEmpty() -> "Submitting…"
                    else -> null
                },
            banner = "Not sent while offline: ${failure.submission.action} (${failure.reason})",
            bannerKind = "error",
            turnActive = if (ownsCurrentChatTurn) false else s.turnActive,
            pendingReplace = if (ownsCurrentChatTurn) false else s.pendingReplace,
            pendingCanvas = if (ownsCurrentChatTurn) emptyList() else s.pendingCanvas,
            preTurnCanvas = if (ownsCurrentChatTurn) emptyList() else s.preTurnCanvas,
            turnOpsApplied = if (ownsCurrentChatTurn) false else s.turnOpsApplied,
            transientCanvas = if (ownsCurrentChatTurn) null else s.transientCanvas,
            pendingTurns = if (ownsCurrentChatTurn) emptyList() else s.pendingTurns,
            lastTransientFrameSequence = if (ownsCurrentChatTurn) 0UL else s.lastTransientFrameSequence,
            stepTrail = if (ownsCurrentChatTurn) emptyList() else s.stepTrail,
            asyncDetached = if (ownsCurrentChatTurn) false else s.asyncDetached,
        )
    }

    /**
     * Optimistic turn-start arming shared by [sendChat] and the rendered-control
     * `chat_message` path: purge the turn-scoped welcome components from the
     * committed canvas (feature 055 uniform rule — see [dropWelcome]) and
     * snapshot it as [UiState.preTurnCanvas] for the timeline, since in-turn ops
     * now morph the live canvas. `internal` so the JVM unit test can drive it.
     */
    internal fun armTurn(s: UiState): UiState {
        val live = s.canvas.dropWelcome()
        return s.copy(
            canvas = live,
            preTurnCanvas = live,
            turnOpsApplied = false,
            turnActive = true,
            pendingReplace = true,
            pendingCanvas = emptyList(),
            viewingIndex = null,
            banner = null,
            stepTrail = emptyList(),
            asyncDetached = false,
        )
    }

    /** Start a fresh conversation (clears the canvas, timeline, and transcript). */
    fun newChat() {
        if (!clearResumeLocator(ClearReason.EXPLICIT_NEW_CHAT)) {
            _state.value =
                _state.value.copy(
                    banner = "Could not start a new chat because the current selection was not saved.",
                    bannerKind = "error",
                )
            return
        }
        snapshotTimeout?.cancel()
        seqState.clear()
        _state.value =
            _state.value.copy(
                activeChatId = null,
                turns = emptyList(),
                pendingTurns = emptyList(),
                canvas = emptyList(),
                transientCanvas = null,
                pendingCanvas = emptyList(),
                preTurnCanvas = emptyList(),
                turnOpsApplied = false,
                canvasHistory = emptyList(),
                viewingIndex = null,
                turnActive = false,
                pendingReplace = false,
                canvasLabel = "",
                pendingLabel = "",
                staged = emptyList(),
                statusText = null,
                banner = null,
                stepTrail = emptyList(),
                asyncDetached = false,
                requestGeneration = null,
                requestChatId = null,
                requestPurpose = null,
                expectedCommitRenderRevision = null,
                lastCommittedRenderRevision = 0UL,
                lastTransientFrameSequence = 0UL,
                hydrationApplied = false,
                acceptedSnapshotId = null,
                acceptedSnapshot = null,
            )
        voiceController?.updateVisibleChatLocally(null)
        sendEvent("new_chat")
    }

    // --- attachments (paperclip, feature 031) -------------------------------

    /** Stage + upload a picked file; the chip flips uploading→ready/failed. */
    fun stageAttachment(
        filename: String,
        mimeType: String?,
        bytes: ByteArray,
    ) {
        val t = token ?: return
        val uid = ++attachSeq
        _state.value =
            _state.value.copy(
                staged = _state.value.staged + StagedAttachment(uid, filename, "file", null, "uploading"),
            )
        viewModelScope.launch {
            val up = runCatching { rest.uploadAttachment(t, filename, mimeType, bytes) }.getOrNull()
            _state.value =
                _state.value.copy(
                    staged =
                        _state.value.staged.map { a ->
                            when {
                                a.uid != uid -> a
                                up == null -> a.copy(state = "failed", note = "upload failed")
                                else ->
                                    a.copy(
                                        attachmentId = up.attachmentId,
                                        category = up.category,
                                        state = "ready",
                                        note = parserNote(up.parserStatus),
                                    )
                            }
                        },
                )
        }
    }

    fun removeAttachment(uid: Long) {
        _state.value = _state.value.copy(staged = _state.value.staged.filterNot { it.uid == uid })
    }

    // --- read-only canvas timeline (US "previous canvases") -----------------

    fun viewCanvasSnapshot(index: Int) {
        if (index in _state.value.canvasHistory.indices) {
            _state.value = _state.value.copy(viewingIndex = index)
        }
    }

    fun backToLiveCanvas() {
        _state.value = _state.value.copy(viewingIndex = null)
    }

    // --- US4 surfaces -------------------------------------------------------

    /** Switch surface and lazily fetch its data (flagging it loading for a skeleton). */
    fun goTo(screen: Screen) {
        _state.value =
            _state.value.copy(
                screen = screen,
                agentsLoading = screen == Screen.Agents || _state.value.agentsLoading,
                historyLoading = screen == Screen.History || _state.value.historyLoading,
                auditLoading = screen == Screen.Audit || _state.value.auditLoading,
            )
        when (screen) {
            Screen.Agents -> sendEvent("discover_agents")
            Screen.History -> sendEvent("get_history")
            Screen.Audit -> loadAudit()
            Screen.Chat -> Unit
            Screen.Surface -> Unit
        }
    }

    /**
     * Route a settings-menu item (from the server-owned model) to its surface.
     * The menu structure itself always matches the web exactly.
     */
    fun openMenuItem(item: MenuItem) = openSurface(item.surface, item.params)

    /**
     * Open a chrome surface by key — from a settings-menu item OR a top-bar action
     * (pulse/timeline, T037). Native Agents/Audit screens where they exist,
     * otherwise request the SDUI surface (chrome_open) and render it natively when
     * the chrome_surface frame arrives (feature 043).
     */
    fun openSurface(
        surface: String,
        params: JsonObject = JsonObject(emptyMap()),
    ) {
        when (surface) {
            "agents" -> goTo(Screen.Agents)
            "audit" -> goTo(Screen.Audit)
            else -> {
                sendEvent(
                    "chrome_open",
                    buildJsonObject {
                        put("surface", surface)
                        put("params", params)
                    },
                )
                _state.value =
                    _state.value.copy(
                        screen = Screen.Surface,
                        pendingSurfaceKey = surface,
                        pendingSurfaceParams = params,
                        pendingSurface = null,
                    )
            }
        }
    }

    /** Re-request the pending SDUI surface after a load timeout (T039 retry). */
    fun retryPendingSurface() {
        val st = _state.value
        if (st.pendingSurfaceKey.isNotBlank()) {
            sendEvent(
                "chrome_open",
                buildJsonObject {
                    put("surface", st.pendingSurfaceKey)
                    put("params", st.pendingSurfaceParams)
                },
            )
        }
    }

    /**
     * Stage an already-uploaded attachment as a ready chip (feature 031, T047) from
     * the attachments library's `attach_existing {attachment_id, filename,
     * category}` — no re-upload, no server frame. Returns whether the chip is
     * staged after the call: `true` for newly staged AND for an already-staged
     * duplicate (the user's intent — "use this file" — is satisfied either way,
     * so the caller still navigates back to the composer); `false` only for a
     * malformed payload (blank id), which stages nothing.
     */
    private fun stageExistingAttachment(payload: JsonObject): Boolean {
        val id = (payload["attachment_id"] as? JsonPrimitive)?.contentOrNull?.takeIf { it.isNotBlank() } ?: return false
        if (_state.value.staged.any { it.attachmentId == id }) return true
        val filename = (payload["filename"] as? JsonPrimitive)?.contentOrNull ?: "attachment"
        val category = (payload["category"] as? JsonPrimitive)?.contentOrNull ?: "file"
        _state.value =
            _state.value.copy(
                staged = _state.value.staged + StagedAttachment(++attachSeq, filename, category, id, "ready"),
            )
        return true
    }

    /**
     * Apply a theme spec locally (feature 044 US5) — from a `theme_apply` component,
     * an interactive `color_picker`, or the local echo of `save_theme`.
     * Recomposition restyles the whole app; the server persists it in parallel, so
     * this is a pure UI mirror (fail-safe: a bad hex leaves the palette unchanged).
     */
    fun applyTheme(spec: JsonObject) {
        _state.value = _state.value.copy(themePalette = themePaletteForSpec(_state.value.themePalette, spec))
    }

    fun openChat(chatId: String) {
        if (!persistActiveChat(chatId)) {
            _state.value =
                _state.value.copy(
                    banner = "Could not save the selected conversation.",
                    bannerKind = "error",
                )
            return
        }
        val switching = _state.value.activeChatId != chatId
        _state.value =
            _state.value.copy(
                activeChatId = chatId,
                screen = Screen.Chat,
                viewingIndex = null,
                lastCommittedRenderRevision = if (switching) 0UL else _state.value.lastCommittedRenderRevision,
                transientCanvas = null,
                pendingTurns = emptyList(),
            )
        voiceController?.updateVisibleChatLocally(chatId)
        requestChatRefresh(chatId, locatorAlreadyPersisted = true)
    }

    /** Re-issue load_chat under a fresh hydration UUID4 after persisting its locator. */
    private fun requestChatRefresh(
        chatId: String,
        locatorAlreadyPersisted: Boolean = false,
    ) {
        if (!locatorAlreadyPersisted && !persistActiveChat(chatId)) {
            _state.value =
                _state.value.copy(
                    banner = "Could not save the selected conversation.",
                    bannerKind = "error",
                )
            return
        }
        sendEvent("load_chat", buildJsonObject { put("chat_id", chatId) })
    }

    /** Enable/disable a single tool of an agent (REST per-(tool,kind) write), then refresh. */
    fun setToolEnabled(
        agent: Agent,
        tool: String,
        enabled: Boolean,
    ) {
        patchAgent(agent.id) { it.copy(permissions = it.permissions + (tool to enabled)) } // optimistic
        val t = token ?: return
        val kind = agent.toolScopeMap[tool] ?: "tools:read"
        viewModelScope.launch {
            runCatching { rest.setToolPermission(t, agent.id, tool, kind, enabled) }
            sendEvent("discover_agents")
        }
    }

    /** Master toggle: enable/disable all of an agent's tools at once (WS scopes + overrides). */
    fun setAgentEnabled(
        agent: Agent,
        enabled: Boolean,
    ) {
        patchAgent(agent.id) { a -> a.copy(permissions = a.tools.associateWith { enabled }) } // optimistic
        val kinds = agent.toolScopeMap.values.toSet().ifEmpty { agent.scopes.keys }
        sendEvent(
            "set_agent_permissions",
            buildJsonObject {
                put("agent_id", agent.id)
                putJsonObject("scopes") { kinds.forEach { put(it, enabled) } }
                putJsonObject("tool_overrides") { agent.tools.forEach { put(it, enabled) } }
            },
        )
        sendEvent("discover_agents")
    }

    /**
     * Optimistically update one agent so a toggle responds instantly; the
     * subsequent discover_agents refresh reconciles with the server truth.
     */
    private fun patchAgent(
        agentId: String,
        transform: (Agent) -> Agent,
    ) {
        _state.value =
            _state.value.copy(
                agents = _state.value.agents.map { if (it.id == agentId) transform(it) else it },
            )
    }

    fun enableRecommended() {
        sendEvent("enable_recommended_agents")
        sendEvent("discover_agents")
    }

    private fun loadAudit() {
        val t = token
        if (t == null) {
            _state.value = _state.value.copy(auditLoading = false)
            return
        }
        viewModelScope.launch {
            val events = runCatching { rest.audit(t) }.getOrDefault(emptyList())
            _state.value = _state.value.copy(audit = events, auditLoading = false)
        }
    }

    // --- reducer ------------------------------------------------------------

    /**
     * Fold transport state without letting a dead socket leave either a turn
     * skeleton or a client-only submission projection running indefinitely.
     */
    internal fun reduceConnectionState(
        s: UiState,
        connection: ConnectionState,
    ): UiState =
        when (connection) {
            ConnectionState.Disconnected ->
                s.copy(
                    connection = connection,
                    turnActive = false,
                    pendingReplace = false,
                    pendingCanvas = emptyList(),
                    preTurnCanvas = emptyList(),
                    turnOpsApplied = false,
                    transientCanvas = null,
                    pendingTurns = emptyList(),
                    connectionGeneration = null,
                    requestGeneration = null,
                    requestChatId = null,
                    requestPurpose = null,
                    expectedCommitRenderRevision = null,
                    hydrationApplied = false,
                    acceptedSnapshotId = null,
                    acceptedSnapshot = null,
                    lastTransientFrameSequence = 0UL,
                    agentsLoading = false,
                    historyLoading = false,
                    auditLoading = false,
                    pendingSubmissions = emptyMap(),
                    statusText = null,
                )
            ConnectionState.Connected -> s.copy(connection = connection, everConnected = true)
            else -> s.copy(connection = connection)
        }

    /** Fold one inbound frame into state. `internal` so the JVM unit test can drive it. */
    internal fun reduce(
        s: UiState,
        msg: Inbound,
    ): UiState =
        when (msg) {
            is Inbound.UiRender -> reduceUiRender(s, msg)
            is Inbound.UiUpsert -> reduceUiUpsert(s, msg)
            is Inbound.ChatCreated -> {
                val pendingVoice = pendingVoiceActivation?.submission
                if (pendingVoice != null && !isExpectedVoiceChatCreation(s, pendingVoice, msg)) {
                    s
                } else {
                    bindAcknowledgedChat(s, msg.chatId)
                }
            }
            is Inbound.UserMessageAcked ->
                // The origin's optimistic arm normally ran already (sendChat / the
                // chat_message ui_event); arming here too covers an acked turn that
                // skipped it, without resetting an armed turn's pre-turn snapshot
                // or already-applied live ops.
                if (msg.chatId != null && s.requestChatId != null && msg.chatId != s.requestChatId) {
                    s
                } else {
                    bindAcknowledgedChat(if (s.pendingReplace) s else armTurn(s), msg.chatId)
                }
            is Inbound.ChatLoaded ->
                if (s.connectionGeneration == null) reduceLegacyChatLoaded(s, msg) else s
            is Inbound.ConversationSnapshot -> reduceConversationSnapshot(s, msg)
            is Inbound.ConversationCommitReady -> reduceConversationCommitReady(s, msg)
            is Inbound.ChatStatus -> reduceStatus(s, msg)
            is Inbound.AgentList -> s.copy(agents = msg.agents, agentsLoading = false)
            is Inbound.HistoryList -> s.copy(history = msg.chats, historyLoading = false)
            is Inbound.UiStreamData -> reduceUiStreamData(s, msg)
            is Inbound.StreamSubscribed ->
                if (hasGenerationScopedConversation(s)) {
                    s
                } else {
                    applyCanvasOps(s, subscribeAckOps(msg, canvasIds(s)))
                }
            is Inbound.StreamErrorMsg ->
                if (hasGenerationScopedConversation(s)) {
                    s.copy(
                        banner = msg.error.message ?: msg.error.code ?: "Stream error",
                        bannerKind = "error",
                    )
                } else {
                    applyCanvasOps(s, streamErrorOps(msg))
                }
            is Inbound.ChromeMenu -> s.copy(chromeMenu = msg.model)
            is Inbound.ChromeSurface ->
                when {
                    // The documented CLOSE instruction (chrome_close, workspace-
                    // timeline view/live, the 054 gate unlock): a blank key with no
                    // components pops the surface screen back to the chat it was
                    // opened over, so the user is never stuck on a stale surface
                    // hiding the canvas — and always releases the mandatory pin.
                    msg.surfaceKey.isBlank() && msg.components.isEmpty() ->
                        if (s.screen == Screen.Surface) {
                            s.copy(
                                screen = Screen.Chat,
                                pendingSurface = null,
                                pendingSurfaceKey = "",
                                pendingSurfaceParams = JsonObject(emptyMap()),
                                mandatorySurface = false,
                            )
                        } else {
                            s.copy(mandatorySurface = false)
                        }
                    // A mandatory surface (the 054 first-run LLM-setup gate) is
                    // ACCEPTED even though unsolicited: show it and pin it — the
                    // scaffold suppresses navigation/Back until the server's blank
                    // close frame (above) clears the pin. Sign-out stays enabled.
                    msg.mode == "mandatory" ->
                        s.copy(
                            screen = Screen.Surface,
                            pendingSurface = msg,
                            pendingSurfaceKey = msg.surfaceKey,
                            pendingSurfaceParams = JsonObject(emptyMap()),
                            mandatorySurface = true,
                        )
                    // The surface the user is currently awaiting.
                    s.screen == Screen.Surface && s.pendingSurfaceKey == msg.surfaceKey ->
                        s.copy(pendingSurface = msg)
                    // A mismatched key (chrome error notices arrive keyed "error":
                    // unknown action, admin-denied, handler failures) must not yank
                    // the user to Screen.Surface with the wrong content — but it is
                    // never a SILENT drop either (FR-002): surface its alert text
                    // through the banner, mirroring Inbound.ErrorFrame.
                    else -> {
                        val text =
                            listOf(msg.title, noticeText(msg.components))
                                .filter { it.isNotBlank() }
                                .joinToString(": ")
                        if (text.isBlank()) s else s.copy(banner = text, bannerKind = "error")
                    }
                }
            // Stored preferences at boot: fold `theme` into the live palette so the
            // app opens in the user's saved theme (US5 restyle).
            is Inbound.UserPreferences -> s.copy(themePalette = themePaletteForSpec(s.themePalette, msg.theme))
            // Read-only workspace timeline toggled: lock/unlock mutations (T041).
            is Inbound.WorkspaceTimelineMode -> s.copy(timelineReadOnly = msg.active)
            // A server error reply is never silent (FR-002). Only the strict
            // refusal variant can settle the exact local submission it names.
            is Inbound.AdmissionRefusal -> reduceAdmissionRefusal(s, msg)
            is Inbound.ErrorFrame -> reduceErrorFrame(s, msg)
            is Inbound.ChatStep ->
                s.copy(stepTrail = trailUpsert(s.stepTrail, stepLine(msg)))
            is Inbound.ToolProgress ->
                s.copy(stepTrail = trailUpsert(s.stepTrail, "• ${msg.label}"))
            is Inbound.OperationStatus -> reduceOperationStatus(s, msg)
            is Inbound.AgentLifecycle -> reduceAgentLifecycle(s, msg)
            // The turn detached into a background task: keep the turn alive but let
            // the UI relax — results will arrive when the task completes. A task in
            // ANOTHER chat (started on another device, audit item 12) must not touch
            // this chat's turn state — unobtrusive banner only. A null chat_id
            // (legacy flat frame) is treated as the open chat, as before.
            is Inbound.TaskStarted ->
                if (forOpenChat(msg.chatId, s)) {
                    s.copy(statusText = "Working in the background…", asyncDetached = true)
                } else {
                    s.copy(banner = "Background task started in another chat", bannerKind = "info")
                }
            is Inbound.TaskCompleted ->
                if (forOpenChat(msg.chatId, s)) {
                    val settled =
                        if (s.connectionGeneration == null) {
                            commitTurn(s)
                        } else {
                            s.copy(statusText = null, asyncDetached = false)
                        }
                    settled.copy(banner = "Background task finished", bannerKind = "info")
                } else {
                    // The banner layer has no tap action — point at History instead.
                    s.copy(banner = "Background task finished in another chat — open it from History", bannerKind = "info")
                }
            is Inbound.Notification -> {
                val text =
                    listOfNotNull(
                        msg.title?.takeIf { it.isNotBlank() },
                        msg.body?.takeIf { it.isNotBlank() },
                    ).joinToString(": ")
                if (text.isBlank()) {
                    s
                } else {
                    s.copy(banner = text, bannerKind = if (msg.level == "error") "error" else "info")
                }
            }
            // 055 (US3): the eight workspace verb acks, promoted ignored → handled
            // (wire-contract §4). The server's follow-up ui_upsert/ui_render
            // fan-outs stay authoritative; these give the issuing socket immediate
            // identity-keyed reconcile + feedback without waiting on them.
            is Inbound.ComponentSaved ->
                s.copy(
                    banner = msg.title?.takeIf { it.isNotBlank() }?.let { "Saved $it" } ?: "Component saved",
                    bannerKind = "info",
                )
            is Inbound.ComponentSaveError ->
                s.copy(banner = msg.error ?: "Couldn't save component", bannerKind = "error")
            is Inbound.ComponentDeleted ->
                if (hasGenerationScopedConversation(s)) {
                    s
                } else {
                    msg.componentId?.takeIf { it.isNotBlank() }
                        ?.let { applyCanvasOps(s, listOf(CanvasOp("remove", it))) } ?: s
                }
            is Inbound.CombineStatus -> s.copy(statusText = msg.message ?: msg.status)
            is Inbound.CombineError ->
                s.copy(statusText = null, banner = msg.error ?: "Couldn't combine components", bannerKind = "error")
            is Inbound.ComponentsReplaced -> {
                if (hasGenerationScopedConversation(s)) {
                    s.copy(statusText = null)
                } else {
                    val ops =
                        msg.removedIds.map { CanvasOp("remove", it) } +
                            msg.newComponents.mapNotNull { c -> c.id?.let { CanvasOp("upsert", it, c) } }
                    applyCanvasOps(s.copy(statusText = null), ops)
                }
            }
            is Inbound.SavedComponentsList -> {
                // Accepted ack; no native saved-components surface exists to
                // refresh (browsing rides the server-driven chrome surfaces) —
                // logged so it is never a silent drop (FR-002).
                Log.i(TAG, "saved_components_list acked (${msg.count} components)")
                s
            }
            is Inbound.Unknown -> {
                // A deliberately-ignored frame (parity matrix) is a quiet drop; a
                // truly unclassified type warns so drift is visible (FR-001).
                if (ProtocolManifest.isClassified(msg.type)) {
                    Log.i(TAG, "ignored frame type=${msg.type}")
                } else {
                    Log.w(TAG, "unhandled frame type=${msg.type}")
                }
                s
            }
            else -> s
        }

    /** Install a connection/request equality fence without changing committed surfaces. */
    internal fun bindConversationGeneration(
        s: UiState,
        binding: ConversationGenerationBinding,
    ): UiState {
        val switchingChats =
            binding.chatId != null && s.activeChatId != null && binding.chatId != s.activeChatId
        return s.copy(
            activeChatId = binding.chatId ?: s.activeChatId,
            connectionGeneration = binding.connectionGeneration,
            requestGeneration = binding.requestGeneration,
            requestChatId = binding.chatId,
            requestPurpose = binding.purpose,
            expectedCommitRenderRevision = null,
            lastCommittedRenderRevision = if (switchingChats) 0UL else s.lastCommittedRenderRevision,
            lastTransientFrameSequence = 0UL,
            transientCanvas = null,
            pendingTurns = if (binding.purpose == ConversationRequestPurpose.HYDRATION) emptyList() else s.pendingTurns,
            hydrationApplied = false,
            acceptedSnapshotId = null,
            acceptedSnapshot = null,
        )
    }

    /** Install a fence and arm the bounded hydration wait before bytes are sent. */
    private fun installConversationGeneration(binding: ConversationGenerationBinding) {
        _state.update { current -> bindConversationGeneration(current, binding) }
        val currentToken = token
        val currentDevice = device
        val deviceId = currentDevice?.deviceId
        if (currentToken != null && deviceId != null) {
            voiceController?.installUiConnection(
                token = currentToken,
                deviceId = deviceId,
                connectionGeneration = binding.connectionGeneration,
                visibleChatId = binding.chatId ?: _state.value.activeChatId,
            )
        }
        snapshotTimeout?.cancel()
        if (binding.purpose == ConversationRequestPurpose.HYDRATION) {
            scheduleSnapshotTimeout(binding)
        }
    }

    /**
     * Preserve the last committed surfaces when one complete snapshot does not
     * arrive in time, then retry this exact chat under a newly generated fence.
     */
    private fun scheduleSnapshotTimeout(
        binding: ConversationGenerationBinding,
        expectedCommitRevision: ULong? = null,
    ) {
        val chatId = binding.chatId ?: return
        val requestGeneration = binding.requestGeneration ?: return
        snapshotTimeout?.cancel()
        snapshotTimeout =
            viewModelScope.launch {
                delay(SNAPSHOT_TIMEOUT_MS)
                val current = _state.value
                val stillWaiting =
                    current.activeChatId == chatId &&
                        current.connectionGeneration == binding.connectionGeneration &&
                        current.requestGeneration == requestGeneration &&
                        current.requestPurpose == binding.purpose &&
                        current.expectedCommitRenderRevision == expectedCommitRevision &&
                        !(binding.purpose == ConversationRequestPurpose.HYDRATION && current.hydrationApplied)
                if (!stillWaiting) return@launch
                _state.value =
                    current.copy(
                        statusText = "Conversation restore timed out; retrying…",
                        transientCanvas = null,
                        pendingTurns = emptyList(),
                        lastTransientFrameSequence = 0UL,
                    )
                requestChatRefresh(chatId)
            }
    }

    /** Exact-scope retry classification; foreign/stale errors are inert. */
    internal fun snapshotRetryTarget(
        s: UiState,
        msg: Inbound,
    ): String? {
        val error = msg as? Inbound.ErrorFrame ?: return null
        if (error.code != SNAPSHOT_RETRYABLE_CODE || !error.retryable) return null
        val chatId = error.chatId ?: return null
        return chatId.takeIf {
            s.requestPurpose != null &&
                s.requestGeneration != null &&
                chatId == s.activeChatId &&
                error.connectionGeneration == s.connectionGeneration &&
                error.requestGeneration == s.requestGeneration
        }
    }

    /**
     * Persist newly acknowledged chat identity before exposing it and clear a
     * locator only for an owner-scoped, generation-matching definitive miss.
     */
    private fun reduceWithPersistence(
        s: UiState,
        msg: Inbound,
    ): UiState {
        if (msg is Inbound.ErrorFrame && isDefinitiveCurrentChatMiss(s, msg)) {
            if (!clearResumeLocator(ClearReason.CONFIRMED_DELETION)) {
                return s.copy(
                    banner = "Conversation was removed, but local recovery state could not be cleared.",
                    bannerKind = "error",
                )
            }
            return clearConversationState(s).copy(
                banner = "Conversation not found.",
                bannerKind = "error",
            )
        }

        // Reduction is pure: validate every scope/revision first, then commit the
        // locator synchronously before the candidate state becomes observable.
        val candidate = reduce(s, msg)
        val acknowledgedChat =
            when (msg) {
                is Inbound.ChatCreated ->
                    msg.chatId?.takeIf { candidate.activeChatId == it && candidate !== s }
                is Inbound.UserMessageAcked ->
                    msg.chatId?.takeIf { candidate.activeChatId == it && candidate !== s }
                is Inbound.ConversationSnapshot ->
                    msg.chatId.takeIf {
                        candidate !== s &&
                            candidate.activeChatId == msg.chatId &&
                            candidate.lastCommittedRenderRevision == msg.renderRevision
                    }
                else -> null
            }
        if (acknowledgedChat != null && !persistActiveChat(acknowledgedChat)) {
            return s.copy(
                banner = "Could not save the active conversation; the update was not applied.",
                bannerKind = "error",
            )
        }
        return candidate
    }

    private fun persistActiveChat(chatId: String): Boolean {
        val owner = account ?: return resumeStore == null
        val store = resumeStore ?: return true
        return store.save(owner, chatId)
    }

    private fun clearResumeLocator(reason: ClearReason): Boolean {
        val owner = account ?: return resumeStore == null
        val store = resumeStore ?: return true
        return store.clear(owner, reason)
    }

    /** Synchronous explicit-sign-out hook used before credentials are removed. */
    fun clearConversationForSignOut(): Boolean {
        voiceController?.logout()
        return clearResumeLocator(ClearReason.DEFINITIVE_SIGN_OUT)
    }

    private fun isDefinitiveCurrentChatMiss(
        s: UiState,
        error: Inbound.ErrorFrame,
    ): Boolean =
        error.code in DEFINITIVE_CHAT_MISS_CODES &&
            error.chatId == s.activeChatId &&
            error.connectionGeneration == s.connectionGeneration &&
            error.requestGeneration == s.requestGeneration &&
            !error.retryable

    private fun clearConversationState(s: UiState): UiState =
        s.copy(
            activeChatId = null,
            turns = emptyList(),
            pendingTurns = emptyList(),
            canvas = emptyList(),
            transientCanvas = null,
            pendingCanvas = emptyList(),
            preTurnCanvas = emptyList(),
            turnOpsApplied = false,
            canvasHistory = emptyList(),
            viewingIndex = null,
            turnActive = false,
            pendingReplace = false,
            canvasLabel = "",
            pendingLabel = "",
            statusText = null,
            stepTrail = emptyList(),
            asyncDetached = false,
            requestGeneration = null,
            requestChatId = null,
            requestPurpose = null,
            expectedCommitRenderRevision = null,
            lastCommittedRenderRevision = 0UL,
            lastTransientFrameSequence = 0UL,
            hydrationApplied = false,
            acceptedSnapshotId = null,
            acceptedSnapshot = null,
        )

    private fun bindAcknowledgedChat(
        s: UiState,
        chatId: String?,
    ): UiState {
        if (chatId == null) return s
        if (s.requestChatId != null && s.requestChatId != chatId) return s
        return s.copy(activeChatId = chatId, requestChatId = chatId)
    }

    /** Open a supplied commit fence only for a future revision on this socket/chat. */
    private fun reduceConversationCommitReady(
        s: UiState,
        ready: Inbound.ConversationCommitReady,
    ): UiState {
        if (
            ready.schemaVersion != 1 ||
            ready.chatId != s.activeChatId ||
            ready.connectionGeneration != s.connectionGeneration ||
            ready.renderRevision <= s.lastCommittedRenderRevision
        ) {
            Log.i(TAG, "conversation_commit_ready ignored: stale or foreign scope")
            return s
        }
        return s.copy(
            requestGeneration = ready.requestGeneration,
            requestChatId = ready.chatId,
            requestPurpose = ConversationRequestPurpose.COMMIT,
            expectedCommitRenderRevision = ready.renderRevision,
            lastTransientFrameSequence = 0UL,
            transientCanvas = null,
            hydrationApplied = false,
            acceptedSnapshotId = null,
            acceptedSnapshot = null,
        )
    }

    /** Purpose-aware, all-or-nothing committed snapshot reducer. */
    private fun reduceConversationSnapshot(
        s: UiState,
        snapshot: Inbound.ConversationSnapshot,
    ): UiState {
        val expectedPurpose =
            when (s.requestPurpose) {
                ConversationRequestPurpose.HYDRATION -> "hydration"
                ConversationRequestPurpose.COMMIT -> "commit"
                null -> null
            }
        if (
            snapshot.schemaVersion != 1 ||
            snapshot.chatId != s.activeChatId ||
            (s.requestChatId != null && snapshot.chatId != s.requestChatId) ||
            snapshot.connectionGeneration != s.connectionGeneration ||
            snapshot.requestGeneration != s.requestGeneration ||
            snapshot.snapshotPurpose != expectedPurpose
        ) {
            Log.i(TAG, "conversation snapshot ignored: wrong scope or purpose")
            return s
        }
        if (
            s.requestPurpose == ConversationRequestPurpose.COMMIT &&
            s.expectedCommitRenderRevision == null
        ) {
            Log.w(TAG, "conversation snapshot ignored: missing commit-ready prelude")
            return s
        }
        if (
            s.expectedCommitRenderRevision != null &&
            snapshot.renderRevision != s.expectedCommitRenderRevision
        ) {
            Log.w(TAG, "conversation snapshot ignored: commit-ready revision mismatch")
            return s
        }
        if (snapshot.renderRevision < s.lastCommittedRenderRevision) {
            Log.i(TAG, "stale_frame_ignored")
            return s
        }
        if (snapshot.renderRevision == s.lastCommittedRenderRevision) {
            if (
                s.requestPurpose == ConversationRequestPurpose.HYDRATION &&
                !s.hydrationApplied
            ) {
                return applyConversationSnapshot(s, snapshot)
            }
            if (s.hydrationApplied && s.acceptedSnapshotId == snapshot.snapshotId) {
                if (s.acceptedSnapshot == snapshot) return s
                Log.w(TAG, "revision_conflict: snapshot identity content changed")
                return s
            }
            if (s.hydrationApplied) Log.w(TAG, "revision_conflict") else Log.i(TAG, "unexpected_equal_commit")
            return s
        }
        return applyConversationSnapshot(s, snapshot)
    }

    private fun applyConversationSnapshot(
        s: UiState,
        snapshot: Inbound.ConversationSnapshot,
    ): UiState {
        val transcript = decodeTranscript(snapshot.transcript)
        if (transcript == null) {
            Log.w(TAG, "conversation snapshot rejected: semantic transcript decode failed")
            return s
        }
        val hydration = s.requestPurpose == ConversationRequestPurpose.HYDRATION
        return s.copy(
            activeChatId = snapshot.chatId,
            turns = transcript,
            pendingTurns = emptyList(),
            canvas = snapshot.canvas.components,
            transientCanvas = null,
            pendingCanvas = emptyList(),
            preTurnCanvas = emptyList(),
            turnOpsApplied = false,
            canvasHistory = emptyList(),
            viewingIndex = null,
            turnActive = false,
            pendingReplace = false,
            canvasLabel = "",
            pendingLabel = "",
            statusText = null,
            stepTrail = emptyList(),
            asyncDetached = false,
            lastCommittedRenderRevision = snapshot.renderRevision,
            lastTransientFrameSequence = 0UL,
            hydrationApplied = hydration,
            acceptedSnapshotId = if (hydration) snapshot.snapshotId else null,
            acceptedSnapshot = if (hydration) snapshot else null,
            requestGeneration = if (hydration) s.requestGeneration else null,
            requestChatId = if (hydration) snapshot.chatId else null,
            requestPurpose = if (hydration) s.requestPurpose else null,
            expectedCommitRenderRevision = null,
        )
    }

    private fun decodeTranscript(messages: List<JsonObject>): List<ChatTurn>? =
        messages.map { message -> decodeTranscriptMessage(message) ?: return null }

    private fun decodeTranscriptMessage(message: JsonObject): ChatTurn? {
        val messageId = message.string("message_id") ?: return null
        val role = message.string("role") ?: return null
        val createdAt = message.string("created_at") ?: return null
        val rawParts = message["parts"] as? JsonArray ?: return null
        val rawAttachments = message["attachments"] as? JsonArray ?: return null
        if (rawParts.isEmpty() || rawAttachments.any { it !is JsonObject }) return null
        val segments = rawParts.map { decodeTranscriptPart(it as? JsonObject ?: return null) }
        return ChatTurn(
            role = role,
            text = segments.joinToString("\n") { it.text }.trim(),
            segments = segments,
            attachments = rawAttachments.map { it.jsonObject },
            messageId = messageId,
            createdAt = createdAt,
        )
    }

    private fun decodeTranscriptPart(part: JsonObject): ChatSegment {
        val recovery =
            ChatSegment(
                kind = ChatSegmentKind.RECOVERY,
                text = RECOVERY_MESSAGE,
            )
        return when (part.string("type")) {
            "text" -> {
                val text = part.string("text")
                if (text.isNullOrBlank()) recovery else ChatSegment(ChatSegmentKind.TEXT, text)
            }
            "structured" -> {
                val plain = part.string("plain_text")
                if (plain.isNullOrBlank() || !part.containsKey("value")) {
                    recovery
                } else {
                    ChatSegment(
                        kind = ChatSegmentKind.STRUCTURED,
                        text = plain,
                        structuredValue = part["value"],
                    )
                }
            }
            "components" -> {
                val values = part["components"] as? JsonArray
                if (values == null || values.isEmpty() || values.any { it !is JsonObject }) {
                    recovery
                } else {
                    val components = Component.listFromJson(values)
                    val text =
                        flattenSemanticComponentText(components).ifBlank {
                            components.joinToString(", ") { "[${it.type.ifBlank { "component" }}]" }
                        }
                    ChatSegment(ChatSegmentKind.COMPONENTS, text, components)
                }
            }
            "recovery" -> {
                val message = part.string("message")
                ChatSegment(ChatSegmentKind.RECOVERY, message?.takeIf { it.isNotBlank() } ?: RECOVERY_MESSAGE)
            }
            else -> recovery
        }
    }

    private fun JsonObject.string(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.contentOrNull

    private fun flattenSemanticComponentText(components: List<Component>): String =
        components.joinToString("\n") { component ->
            val own =
                SEMANTIC_TEXT_KEYS.mapNotNull { key ->
                    (component.attributes[key] as? JsonPrimitive)?.contentOrNull
                }.joinToString(" ")
            listOf(own, flattenSemanticComponentText(component.children))
                .filter { it.isNotBlank() }
                .joinToString("\n")
        }.trim()

    private fun reduceErrorFrame(
        s: UiState,
        error: Inbound.ErrorFrame,
    ): UiState {
        val banner =
            if (error.code != null && error.code != "internal") {
                "${error.message} (${error.code})"
            } else {
                error.message
            }
        return s.copy(
            banner = banner,
            bannerKind = "error",
            turnActive = false,
            pendingReplace = false,
            pendingCanvas = emptyList(),
            preTurnCanvas = emptyList(),
            turnOpsApplied = false,
            transientCanvas = null,
            pendingTurns = emptyList(),
            lastTransientFrameSequence = 0UL,
            agentsLoading = false,
            historyLoading = false,
            auditLoading = false,
            statusText = null,
            asyncDetached = false,
        )
    }

    private fun reduceAdmissionRefusal(
        s: UiState,
        refusal: Inbound.AdmissionRefusal,
    ): UiState {
        val pending =
            s.pendingSubmissions.entries.firstOrNull { (_, submission) ->
                submission.submissionId == refusal.submissionId
            } ?: return s
        val chatSubmission = pending.value.action == "chat_message"
        val ownsCurrentChatTurn =
            chatSubmission &&
                (
                    pending.key == s.requestGeneration ||
                        (s.requestGeneration == null && s.pendingSubmissions.keys.lastOrNull() == pending.key)
                )
        val retained = s.pendingSubmissions - pending.key
        return s.copy(
            banner = "${refusal.message} (${refusal.code})",
            bannerKind = "error",
            pendingSubmissions = retained,
            statusText =
                when {
                    !ownsCurrentChatTurn && s.turnActive -> s.statusText
                    retained.isNotEmpty() -> "Submitting…"
                    else -> null
                },
            turnActive = if (ownsCurrentChatTurn) false else s.turnActive,
            pendingReplace = if (ownsCurrentChatTurn) false else s.pendingReplace,
            pendingCanvas = if (ownsCurrentChatTurn) emptyList() else s.pendingCanvas,
            preTurnCanvas = if (ownsCurrentChatTurn) emptyList() else s.preTurnCanvas,
            turnOpsApplied = if (ownsCurrentChatTurn) false else s.turnOpsApplied,
            transientCanvas = if (ownsCurrentChatTurn) null else s.transientCanvas,
            pendingTurns = if (ownsCurrentChatTurn) emptyList() else s.pendingTurns,
            lastTransientFrameSequence = if (ownsCurrentChatTurn) 0UL else s.lastTransientFrameSequence,
            stepTrail = if (ownsCurrentChatTurn) emptyList() else s.stepTrail,
            asyncDetached = if (ownsCurrentChatTurn) false else s.asyncDetached,
        )
    }

    private fun reduceOperationStatus(
        s: UiState,
        status: Inbound.OperationStatus,
    ): UiState {
        if (status.connectionGeneration != s.connectionGeneration) {
            return s
        }
        val pendingOperation = s.pendingSubmissions[status.requestGeneration]
        val inScope =
            if (status.chatId != null) {
                status.chatId == s.activeChatId &&
                    (
                        status.requestGeneration == s.requestGeneration ||
                            pendingOperation?.action == status.action
                    )
            } else {
                pendingOperation != null && pendingOperation.action == status.action
            }
        if (!inScope) return s
        val current = s.operationStatuses[status.operationId]
        if (current != null && (current.terminal || status.sequence <= current.sequence)) {
            return s
        }
        val visible = status.error?.message ?: status.label
        val retained = s.operationStatuses + (status.operationId to status)
        val pending =
            if (status.terminal) {
                s.pendingSubmissions - status.requestGeneration
            } else {
                s.pendingSubmissions
            }
        val chatOperation = status.action == "chat_message"
        val ownsCurrentChatTurn = chatOperation && status.requestGeneration == s.requestGeneration
        // Completion can race ahead of the authoritative conversation snapshot.
        // Keep the disposable answer/canvas overlay visible across that ordering;
        // the snapshot remains the only event allowed to replace it atomically.
        // Failed/cancelled/retryable terminals still discard the optimistic turn.
        val discardsCurrentChatPreview = ownsCurrentChatTurn && status.state != "completed"
        return if (status.terminal) {
            val errorNotice =
                status.error?.let { error ->
                    "${error.message} (${error.code})"
                }
            s.copy(
                operationStatuses = retained,
                pendingSubmissions = pending,
                // A successful terminal is retained canonically above, but it
                // is not ongoing work and must not leave an idle "Completed"
                // row behind. Non-success terminals use the prominent banner.
                statusText =
                    when {
                        !ownsCurrentChatTurn && s.turnActive -> s.statusText
                        pending.isNotEmpty() -> "Submitting…"
                        else -> null
                    },
                banner = errorNotice ?: s.banner,
                bannerKind = if (errorNotice != null) "error" else s.bannerKind,
                turnActive = if (ownsCurrentChatTurn) false else s.turnActive,
                // The operation is no longer busy, so retire the loading
                // skeleton even while its already-rendered overlay awaits the
                // authoritative snapshot.
                pendingReplace = if (ownsCurrentChatTurn) false else s.pendingReplace,
                pendingCanvas = if (discardsCurrentChatPreview) emptyList() else s.pendingCanvas,
                preTurnCanvas = if (discardsCurrentChatPreview) emptyList() else s.preTurnCanvas,
                turnOpsApplied = if (discardsCurrentChatPreview) false else s.turnOpsApplied,
                transientCanvas = if (discardsCurrentChatPreview) null else s.transientCanvas,
                pendingTurns = if (discardsCurrentChatPreview) emptyList() else s.pendingTurns,
                lastTransientFrameSequence =
                    if (discardsCurrentChatPreview) 0UL else s.lastTransientFrameSequence,
                stepTrail = if (ownsCurrentChatTurn) emptyList() else s.stepTrail,
                asyncDetached = if (ownsCurrentChatTurn) false else s.asyncDetached,
            )
        } else {
            s.copy(
                operationStatuses = retained,
                pendingSubmissions = pending,
                statusText = visible,
                turnActive = if (ownsCurrentChatTurn) true else s.turnActive,
            )
        }
    }

    private fun reduceAgentLifecycle(
        s: UiState,
        lifecycle: Inbound.AgentLifecycle,
    ): UiState {
        val current = s.agentLifecycles[lifecycle.agentId]
        if (
            current != null &&
                (
                    lifecycle.lifecycleGeneration < current.lifecycleGeneration ||
                        (
                            lifecycle.lifecycleGeneration == current.lifecycleGeneration &&
                                lifecycle.stateRevision <= current.stateRevision
                        )
                )
        ) {
            return s
        }
        return s.copy(
            agentLifecycles = s.agentLifecycles + (lifecycle.agentId to lifecycle),
            banner = "${lifecycle.agentId}: ${lifecycle.label}",
            bannerKind = if (lifecycle.state == "failed") "error" else "info",
        )
    }

    private fun transientScopeMatches(
        s: UiState,
        scope: com.personalailabs.astraldeep.core.protocol.TransientFrameScope,
    ): Boolean =
        scope.chatId == s.activeChatId &&
            scope.connectionGeneration == s.connectionGeneration &&
            scope.requestGeneration == s.requestGeneration &&
            scope.baseRenderRevision == s.lastCommittedRenderRevision &&
            scope.frameSequence > s.lastTransientFrameSequence

    private fun hasGenerationScopedConversation(s: UiState): Boolean = s.connectionGeneration != null && s.activeChatId != null

    private fun reduceUiRender(
        s: UiState,
        msg: Inbound.UiRender,
    ): UiState {
        val scope = msg.scope
        if (scope != null) {
            if (!transientScopeMatches(s, scope)) return s
            if (msg.target == "chat") {
                val text = flattenText(msg.components)
                return s.copy(
                    pendingTurns =
                        if (text.isBlank()) s.pendingTurns else s.pendingTurns + ChatTurn("assistant", text),
                    lastTransientFrameSequence = scope.frameSequence,
                )
            }
            return s.copy(
                transientCanvas = msg.components.filterNot(::isSkeleton),
                lastTransientFrameSequence = scope.frameSequence,
                turnOpsApplied = s.turnOpsApplied || s.pendingReplace,
            )
        }
        // An active 060 conversation never lets an unscoped compatibility frame
        // mutate committed surfaces. A no-chat welcome remains a valid global UI.
        if (s.connectionGeneration != null && s.activeChatId != null) return s
        return reduceLegacyUiRender(s, msg)
    }

    private fun reduceLegacyUiRender(
        s: UiState,
        msg: Inbound.UiRender,
    ): UiState =
        if (msg.target == "chat") {
            val text = flattenText(msg.components)
            if (text.isBlank() || text.contains(DOC_ON_CANVAS_MARKER, ignoreCase = true)) {
                s
            } else {
                s.copy(turns = s.turns + ChatTurn("assistant", text))
            }
        } else {
            val (reasoning, rest0) = msg.components.partition(::isReasoning)
            val canvasComps = rest0.filterNot { isDocCard(it.id) || isSkeleton(it) }
            val reasoningTurns =
                reasoning.mapNotNull { component ->
                    flattenText(component.children).ifBlank { flattenText(listOf(component)) }
                        .takeIf { it.isNotBlank() }
                        ?.let { ChatTurn("reasoning", it) }
                }
            val next = if (reasoningTurns.isEmpty()) s else s.copy(turns = s.turns + reasoningTurns)
            if (next.pendingReplace) {
                if (canvasComps.isEmpty()) {
                    next
                } else {
                    next.copy(pendingCanvas = Canvas.apply(next.pendingCanvas, renderToOps(canvasComps)))
                }
            } else {
                next.copy(canvas = canvasComps, pendingCanvas = emptyList())
            }
        }

    private fun reduceUiUpsert(
        s: UiState,
        msg: Inbound.UiUpsert,
    ): UiState {
        val scope = msg.scope
        if (scope != null) {
            if (!transientScopeMatches(s, scope)) return s
            val preview = Canvas.apply(s.transientCanvas ?: s.canvas, msg.ops)
            return s.copy(
                transientCanvas = preview,
                lastTransientFrameSequence = scope.frameSequence,
                turnOpsApplied = s.turnOpsApplied || s.pendingReplace,
            )
        }
        if (s.connectionGeneration != null && s.activeChatId != null) return s
        if (msg.chatId != null && s.activeChatId != null && msg.chatId != s.activeChatId) return s
        val docTurns =
            msg.ops.mapNotNull { op ->
                if (op.op != "remove" && isDocCard(op.componentId)) {
                    op.component?.let { flattenText(listOf(it)) }
                        ?.takeIf { it.isNotBlank() }
                        ?.let { ChatTurn("assistant", it) }
                } else {
                    null
                }
            }
        val canvasOps = msg.ops.filterNot { isDocCard(it.componentId) || isSkeleton(it.component) }
        val next = if (docTurns.isEmpty()) s else s.copy(turns = s.turns + docTurns)
        return applyCanvasOps(next, canvasOps)
    }

    private fun reduceUiStreamData(
        s: UiState,
        msg: Inbound.UiStreamData,
    ): UiState {
        val scope = msg.scope
        val ops = streamFrameToOps(msg, s.activeChatId, seqState)
        if (scope != null) {
            if (!transientScopeMatches(s, scope)) return s
            return s.copy(
                transientCanvas = Canvas.apply(s.transientCanvas ?: s.canvas, ops),
                lastTransientFrameSequence = scope.frameSequence,
                turnOpsApplied = s.turnOpsApplied || (s.pendingReplace && ops.isNotEmpty()),
            )
        }
        if (s.connectionGeneration != null && s.activeChatId != null) return s
        return applyCanvasOps(s, ops)
    }

    private fun reduceLegacyChatLoaded(
        s: UiState,
        msg: Inbound.ChatLoaded,
    ): UiState =
        s.copy(
            activeChatId = msg.chat.id ?: s.activeChatId,
            turns = msg.chat.messages.map { ChatTurn(it.role, it.content) },
            canvas = emptyList(),
            pendingCanvas = emptyList(),
            preTurnCanvas = emptyList(),
            turnOpsApplied = false,
            canvasHistory = emptyList(),
            viewingIndex = null,
            turnActive = false,
            pendingReplace = false,
            canvasLabel = "",
            pendingLabel = "",
            statusText = null,
            stepTrail = emptyList(),
            asyncDetached = false,
        )

    /**
     * A task frame targets the open chat. Foreign only when BOTH ids are known
     * and differ — a null frame chat_id (legacy flat shape) and a not-yet-acked
     * `activeChatId` (first turn) both count as ours, mirroring the UiUpsert
     * drop guard.
     */
    private fun forOpenChat(
        chatId: String?,
        s: UiState,
    ): Boolean = chatId == null || s.activeChatId == null || chatId == s.activeChatId

    /**
     * The chat to re-issue load_chat for after folding [msg] — a background task
     * or scheduler notification that landed in the OPEN chat refreshes it in
     * place (cross-device continuity, audit item 12); anything else (a different
     * chat, or no chat named) reloads nothing. Pure → unit-tested; the send
     * itself happens in [start]'s collect loop.
     */
    internal fun continuityReloadTarget(
        s: UiState,
        msg: Inbound,
    ): String? =
        when (msg) {
            is Inbound.TaskCompleted -> msg.chatId?.takeIf { it == s.activeChatId }
            is Inbound.Notification -> msg.chatId?.takeIf { it == s.activeChatId }
            else -> null
        }

    /** Web-parity step line: ✓ completed · ✗ errored · • otherwise, then the name. */
    private fun stepLine(step: Inbound.ChatStep): String {
        val icon =
            when (step.status) {
                "completed" -> "✓"
                "errored" -> "✗"
                else -> "•"
            }
        return "$icon ${step.name ?: "step"}"
    }

    /** The trail-line identity: the text sans glyph and sans a trailing percent. */
    private fun trailKey(line: String): String = line.substringAfter(" ").replace(TRAIL_PCT, "")

    /**
     * Append a trail line, updating in place when the same step/tool advances
     * (mirrors the web's per-step element update); bounded to [MAX_TRAIL].
     */
    private fun trailUpsert(
        trail: List<String>,
        line: String,
    ): List<String> {
        val key = trailKey(line)
        val idx = trail.indexOfLast { trailKey(it) == key }
        val next = if (idx >= 0) trail.toMutableList().also { it[idx] = line } else trail + line
        return next.takeLast(MAX_TRAIL)
    }

    /**
     * Ids already on the LIVE canvas — the list [applyCanvasOps] targets, i.e.
     * what the user sees — the subscribe-ack placeholder guard, so a device
     * joining mid-stream never blanks retained content under the same identity
     * (055).
     */
    private fun canvasIds(s: UiState): Set<String> = s.canvas.mapNotNullTo(HashSet()) { it.id }

    /**
     * Apply identity-keyed ops (ui_upsert, streaming, workspace verb acks) to
     * the LIVE canvas — even while a replacing turn is armed (055 live-op rule):
     * the origin morphs in place exactly like co-viewing devices, and the first
     * in-turn op clears the query skeleton. Only full renders buffer mid-turn.
     */
    private fun applyCanvasOps(
        s: UiState,
        ops: List<CanvasOp>,
    ): UiState {
        if (ops.isEmpty()) return s
        return s.copy(
            canvas = Canvas.apply(s.canvas, ops),
            turnOpsApplied = s.turnOpsApplied || s.pendingReplace,
        )
    }

    /**
     * Convert a bare `ui_render` component list into in-place upsert ops. A
     * component keeps its own id; an id-less overlay (the reasoning collapsible)
     * gets a STABLE synthetic id by type+position so repeated pushes update it in
     * place instead of duplicating — and it never collides with the round's
     * real component ids.
     */
    private fun renderToOps(components: List<Component>): List<CanvasOp> =
        components.mapIndexed { i, c ->
            val id = c.id ?: "xr-${c.type}-$i"
            CanvasOp(op = "upsert", componentId = id, component = if (c.id == null) c.copy(id = id) else c)
        }

    private fun reduceStatus(
        s: UiState,
        msg: Inbound.ChatStatus,
    ): UiState {
        val label = msg.message?.takeIf { it.isNotBlank() } ?: msg.status
        return when (msg.status) {
            "done", "idle", "completed" ->
                if (s.connectionGeneration == null) {
                    commitTurn(s)
                } else {
                    // The following conversation_snapshot is the sole committed
                    // publication. Status completion cannot advance either surface.
                    s.copy(turnActive = false, statusText = null, stepTrail = emptyList())
                }
            "thinking", "executing", "fixing", "processing_async" ->
                s.copy(turnActive = true, statusText = label)
            "info" ->
                label?.takeIf { it.isNotBlank() }
                    ?.let {
                        s.copy(
                            statusText = if (s.turnActive) s.statusText else null,
                            banner = it,
                            bannerKind = "info",
                        )
                    }
                    ?: s
            // Unknown states cannot assert that work is active. They also must
            // not erase the latest valid progress for a turn that is still live.
            else -> if (s.hasActiveWork) s else s.copy(statusText = null)
        }
    }

    /**
     * A turn finished (`chat_status done`). For a replacing turn that produced
     * canvas content, the committed state is what the user is looking at — the
     * live canvas (in-turn ops applied as they arrived, 055 live rule) with the
     * buffered full render, when one arrived, winning per identity on top — and
     * the pre-turn snapshot goes onto the timeline. A text-only turn leaves the
     * canvas untouched (never blank it) apart from the welcome purge (see
     * [dropWelcome]).
     */
    private fun commitTurn(s: UiState): UiState {
        if (!s.pendingReplace) {
            return s.copy(turnActive = false, statusText = null, stepTrail = emptyList(), asyncDetached = false)
        }
        if (s.pendingCanvas.isEmpty() && !s.turnOpsApplied) {
            // Text-only turn: keep the canvas — minus welcome (belt-and-braces;
            // the arming purge already dropped it).
            return s.copy(
                canvas = s.canvas.dropWelcome(),
                preTurnCanvas = emptyList(),
                turnActive = false,
                pendingReplace = false,
                statusText = null,
                stepTrail = emptyList(),
                asyncDetached = false,
            )
        }
        // The buffered render merges ONTO the live canvas (render wins per
        // identity, live-only components survive) — a partial overlay render
        // must never drop the round's already-applied upserts.
        val live = s.canvas.dropWelcome()
        val committed = if (s.pendingCanvas.isEmpty()) live else Canvas.apply(live, renderToOps(s.pendingCanvas))
        // Welcome components never enter the timeline; a welcome-only pre-turn
        // canvas archives nothing (the "Canvas 1" leak regression).
        val archived = s.preTurnCanvas.dropWelcome()
        val newHistory =
            if (archived.isNotEmpty()) {
                s.canvasHistory +
                    CanvasSnapshot(
                        label = s.canvasLabel.ifBlank { "Canvas ${s.canvasHistory.size + 1}" },
                        components = archived,
                    )
            } else {
                s.canvasHistory
            }
        return s.copy(
            canvas = committed,
            pendingCanvas = emptyList(),
            preTurnCanvas = emptyList(),
            turnOpsApplied = false,
            canvasHistory = newHistory,
            canvasLabel = s.pendingLabel,
            pendingLabel = "",
            turnActive = false,
            pendingReplace = false,
            statusText = null,
            stepTrail = emptyList(),
            asyncDetached = false,
        )
    }

    /** A model "Reasoning" collapsible (routed to the chat, not the canvas). */
    private fun isReasoning(c: Component): Boolean =
        c.type.equals("collapsible", ignoreCase = true) &&
            ((c.attributes["title"] as? JsonPrimitive)?.contentOrNull ?: "")
                .equals("Reasoning", ignoreCase = true)

    /** The narrative doc card the server promotes long answers into (id "doc_…"). */
    private fun isDocCard(id: String?): Boolean = id != null && id.startsWith("doc_")

    /** A `skeleton` loading placeholder — stray in a finished canvas, so dropped. */
    private fun isSkeleton(c: Component?): Boolean = c != null && c.type.equals("skeleton", ignoreCase = true)

    /**
     * Turn-scoped welcome components (feature 055 uniform rule): identities are
     * "wel_"-prefixed, purged at turn start and never archived. Unconditional —
     * when the server flag is off the welcome arrives id-less, so this is a no-op.
     */
    private fun List<Component>.dropWelcome(): List<Component> = filterNot { it.id?.startsWith("wel_") == true }

    private fun flattenText(components: List<Component>): String =
        components.joinToString("\n") { c ->
            val own =
                (c.attributes["content"] as? JsonPrimitive)?.contentOrNull
                    ?: (c.attributes["text"] as? JsonPrimitive)?.contentOrNull
                    ?: ""
            (own + "\n" + flattenText(c.children)).trim()
        }.trim()

    /** Notice text from a chrome_surface's components — Alerts carry `message`. */
    private fun noticeText(components: List<Component>): String =
        components.joinToString("\n") { c ->
            val own =
                (c.attributes["message"] as? JsonPrimitive)?.contentOrNull
                    ?: (c.attributes["content"] as? JsonPrimitive)?.contentOrNull
                    ?: (c.attributes["text"] as? JsonPrimitive)?.contentOrNull
                    ?: ""
            (own + "\n" + noticeText(c.children)).trim()
        }.trim()

    private fun parserNote(status: String?): String? =
        when (status) {
            "preparing" -> "preparing reader…"
            "pending_admin_approval" -> "reader pending admin"
            "unavailable" -> "no reader yet"
            else -> null
        }

    companion object {
        private const val TAG = "AppViewModel"

        private const val SNAPSHOT_TIMEOUT_MS = 5_000L
        private const val SNAPSHOT_RETRYABLE_CODE = "snapshot_retryable"
        private const val RECOVERY_MESSAGE = "A saved response could not be displayed."
        private val DEFINITIVE_CHAT_MISS_CODES = setOf("chat_not_found", "chat_deleted")
        private val SEMANTIC_TEXT_KEYS =
            listOf("content", "text", "message", "label", "title", "value", "caption")

        /** The step trail is a live glance, not a log — keep only the tail. */
        private const val MAX_TRAIL = 20

        /** A trailing " (40%)"/" (40.5%)" progress suffix (stripped for trail identity). */
        private val TRAIL_PCT = Regex("""\s*\(\d+(\.\d+)?%\)$""")

        // The server pairs a canvas doc card with a "…full write-up is on the
        // canvas" lead in the chat. On mobile we route the full answer to the chat
        // instead, so that paired lead is suppressed to avoid duplication.
        private const val DOC_ON_CANVAS_MARKER = "full write-up is on the canvas"

        fun factory(
            client: OrchestratorClient,
            rest: AstralRest,
            resumeStore: ConversationResumeStore? = null,
            voiceController: VoiceSessionController? = null,
        ) = viewModelFactory {
            initializer { AppViewModel(client, rest, resumeStore, voiceController) }
        }
    }

    override fun onCleared() {
        voiceController?.close()
        super.onCleared()
    }
}
