package com.personalailabs.astraldeep.app.transport

import android.util.Log
import com.personalailabs.astraldeep.core.protocol.ChatAttachment
import com.personalailabs.astraldeep.core.protocol.ConversationResume
import com.personalailabs.astraldeep.core.protocol.DeviceCapabilities
import com.personalailabs.astraldeep.core.protocol.Inbound
import com.personalailabs.astraldeep.core.protocol.VoicePlayoutEvent
import com.personalailabs.astraldeep.core.protocol.VoiceTranscript
import com.personalailabs.astraldeep.core.protocol.Wire
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.emitAll
import kotlinx.coroutines.flow.flow
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.UUID
import java.util.concurrent.TimeUnit

enum class ConnectionState { Connecting, Connected, Disconnected, AuthRequired }

/** The fixed purpose bound to one UUID4 request generation. */
enum class ConversationRequestPurpose { HYDRATION, COMMIT }

/**
 * Client-side equality fence opened before a registration/load/turn is sent.
 * A connection-only binding has null chat/request/purpose and is used when no
 * account-scoped resume locator exists.
 */
data class ConversationGenerationBinding(
    val connectionGeneration: String,
    val chatId: String?,
    val requestGeneration: String?,
    val purpose: ConversationRequestPurpose?,
)

/** Exact registration bytes paired with the fence that must be installed first. */
internal data class RegistrationAttempt(
    val binding: ConversationGenerationBinding,
    val frame: String,
)

/**
 * Client-owned identity for one outbound attempt. The server may replace the
 * local `submitting` projection only after it durably accepts this exact
 * request generation, while transport queueing preserves both UUIDs verbatim.
 */
data class LocalSubmission(
    val action: String,
    val chatId: String?,
    val submissionId: String,
    val requestGeneration: String,
)

/** One queued submission that could not be retained or safely replayed. */
data class QueuedSubmissionFailure(
    val submission: LocalSubmission,
    val reason: String,
)

/** Pure exponential backoff schedule (base·2^(attempt-1), capped). Unit-tested. */
fun backoffDelayMs(
    attempt: Int,
    baseMs: Long = 1_000L,
    maxMs: Long = 30_000L,
): Long {
    if (attempt <= 1) return baseMs
    val shift = (attempt - 1).coerceIn(0, 20)
    val raw = baseMs shl shift
    return if (raw <= 0L || raw > maxMs) maxMs else raw
}

/**
 * The WebSocket transport: connects to the orchestrator's `/ws`, sends
 * `register_ui` on open, decodes inbound frames via [Wire] into [Inbound], and
 * exposes them as a reconnecting cold [Flow]. [state] reflects the live
 * connection; reconnect uses [backoffDelayMs] (reset on each successful open).
 *
 * Outbound resilience (FR-012): frames sent while disconnected are queued (bounded)
 * and flushed on the next open, so user input is not lost across a blip; an
 * already-sent frame leaves the queue, so a reconnect does not resend it.
 */
class OrchestratorClient(
    private val url: String,
    private val client: OkHttpClient = defaultClient(),
    private val uuidFactory: () -> String = { UUID.randomUUID().toString() },
) {
    /** An outbound frame queued while offline (its `action` kept for the drop notice). */
    private data class Queued(
        val action: String,
        val frame: String,
        val submission: LocalSubmission,
        val request: ConversationRequest? = null,
    )

    /** Request identity can be queued before the next connection generation exists. */
    private data class ConversationRequest(
        val chatId: String?,
        val requestGeneration: String,
        val purpose: ConversationRequestPurpose,
    )

    @Volatile private var socket: WebSocket? = null

    @Volatile private var open = false

    @Volatile private var connectionGeneration: String? = null
    private val pending = ArrayDeque<Queued>()
    private val _state = MutableStateFlow(ConnectionState.Disconnected)
    val state: StateFlow<ConnectionState> = _state.asStateFlow()

    private val _dropped = MutableSharedFlow<String>(extraBufferCapacity = 8, onBufferOverflow = BufferOverflow.DROP_OLDEST)
    private val _queuedFailures =
        MutableSharedFlow<QueuedSubmissionFailure>(
            extraBufferCapacity = 8,
            onBufferOverflow = BufferOverflow.DROP_OLDEST,
        )

    /** The `action` of each frame dropped from the full offline queue — overflow is never silent (T014). */
    val dropped: SharedFlow<String> = _dropped.asSharedFlow()

    /** Identity-bearing failures let the UI settle the exact local projection. */
    val queuedFailures: SharedFlow<QueuedSubmissionFailure> = _queuedFailures.asSharedFlow()

    /**
     * Reconnecting inbound stream. Collect this for the life of the session.
     * [sessionId] is a provider, read at each (re)connect, so `register_ui`
     * always carries the chat active NOW — not the one open when the session
     * started (cross-device continuity, audit item 12).
     */
    fun stream(
        token: String,
        device: DeviceCapabilities,
        sessionId: () -> String? = { null },
        onGeneration: (ConversationGenerationBinding) -> Unit = {},
        onQueuedSubmission: (LocalSubmission) -> Unit = {},
    ): Flow<Inbound> =
        flow {
            generationObserver = onGeneration
            var attempt = 0
            while (true) {
                emitAll(
                    connectOnce(
                        token,
                        device,
                        sessionId,
                        onGeneration,
                        onQueuedSubmission,
                    ) { attempt = 0 },
                )
                _state.value = ConnectionState.Disconnected
                attempt += 1
                delay(backoffDelayMs(attempt))
            }
        }

    private fun connectOnce(
        token: String,
        device: DeviceCapabilities,
        sessionId: () -> String?,
        onGeneration: (ConversationGenerationBinding) -> Unit,
        onQueuedSubmission: (LocalSubmission) -> Unit,
        onOpen: () -> Unit,
    ): Flow<Inbound> =
        callbackFlow {
            _state.value = ConnectionState.Connecting
            val request = Request.Builder().url(url).build()
            val listener =
                object : WebSocketListener() {
                    override fun onOpen(
                        webSocket: WebSocket,
                        response: Response,
                    ) {
                        // register_ui MUST be the first frame on the socket:
                        // only after it is enqueued may the offline queue flush
                        // and Connected-reactive sends (the reconnect load_chat
                        // refresh) flow, or the server would refuse them as
                        // unregistered.
                        val registration = createRegistrationAttempt(token, device, sessionId())
                        connectionGeneration = registration.binding.connectionGeneration
                        // Install the equality fence before register_ui can produce
                        // a hydration response on this socket.
                        onGeneration(registration.binding)
                        webSocket.send(registration.frame)
                        open = true
                        onOpen()
                        flushPending(webSocket, onGeneration, onQueuedSubmission)
                        _state.value = ConnectionState.Connected
                    }

                    override fun onMessage(
                        webSocket: WebSocket,
                        text: String,
                    ) {
                        val msg = Wire.decode(text)
                        if (msg is Inbound.AuthRequired) _state.value = ConnectionState.AuthRequired
                        trySend(msg)
                    }

                    override fun onClosing(
                        webSocket: WebSocket,
                        code: Int,
                        reason: String,
                    ) {
                        open = false
                        connectionGeneration = null
                        webSocket.close(NORMAL_CLOSE, null)
                        close()
                    }

                    override fun onFailure(
                        webSocket: WebSocket,
                        t: Throwable,
                        response: Response?,
                    ) {
                        open = false
                        connectionGeneration = null
                        Log.w(TAG, "WebSocket failure: ${t.message}")
                        close()
                    }
                }
            socket = client.newWebSocket(request, listener)
            awaitClose {
                open = false
                connectionGeneration = null
                socket?.cancel()
            }
        }

    private fun flushPending(
        webSocket: WebSocket,
        onGeneration: (ConversationGenerationBinding) -> Unit,
        onQueuedSubmission: (LocalSubmission) -> Unit,
    ) {
        flushPending(onGeneration, onQueuedSubmission, webSocket::send)
    }

    /**
     * Re-install a queued frame's request and local-operation fences before
     * handing its exact bytes to the newly opened socket.
     */
    private fun flushPending(
        onGeneration: (ConversationGenerationBinding) -> Unit,
        onQueuedSubmission: (LocalSubmission) -> Unit,
        send: (String) -> Boolean,
    ) {
        synchronized(pending) {
            while (pending.isNotEmpty()) {
                val queued = pending.removeFirst()
                queued.request?.let { bindRequest(it, onGeneration) }
                onQueuedSubmission(queued.submission)
                if (!send(queued.frame)) {
                    pending.addFirst(queued)
                    break
                }
            }
        }
    }

    private fun enqueueOrSend(
        action: String,
        frame: String,
        submission: LocalSubmission,
        request: ConversationRequest? = null,
        onGeneration: (ConversationGenerationBinding) -> Unit = generationObserver,
    ) {
        val s = socket
        if (open && s != null) {
            request?.let { bindRequest(it, onGeneration) }
            // OkHttp returns false only when the frame was not accepted into
            // its outbound queue, so retaining it here cannot duplicate an
            // accepted send and closes the open/close race without data loss.
            if (s.send(frame)) return
        }
        if (!validQueuedIdentity(frame, submission)) {
            _queuedFailures.tryEmit(
                QueuedSubmissionFailure(submission, "invalid queued identity"),
            )
            return
        }
        val droppedActions = mutableListOf<String>()
        val droppedSubmissions = mutableListOf<LocalSubmission>()
        synchronized(pending) {
            pending.addLast(Queued(action, frame, submission, request))
            while (pending.size > MAX_QUEUE) {
                val dropped = pending.removeFirst()
                droppedActions.add(dropped.action)
                droppedSubmissions.add(dropped.submission)
            }
        }
        droppedActions.forEach { _dropped.tryEmit(it) }
        droppedSubmissions.forEach { dropped ->
            _queuedFailures.tryEmit(QueuedSubmissionFailure(dropped, "offline queue full"))
        }
    }

    fun sendChat(
        message: String,
        chatId: String?,
        attachments: List<ChatAttachment> = emptyList(),
        onSubmission: (LocalSubmission) -> Unit = {},
    ): LocalSubmission {
        val submission = newSubmission("chat_message", chatId)
        // Install the local-only projection before queueing or socket I/O. A
        // fast accepted frame must never race ahead of its correlation map.
        onSubmission(submission)
        val request = conversationRequest(submission, ConversationRequestPurpose.COMMIT)
        enqueueOrSend(
            "chat_message",
            Wire.encodeChatMessage(
                message = message,
                chatId = chatId,
                attachments = attachments,
                requestGeneration = submission.requestGeneration,
                submissionId = submission.submissionId,
            ),
            submission,
            request,
        )
        return submission
    }

    /**
     * Submit one proof-bearing final ASR transcript through the ordinary chat
     * dispatcher. Unlike typed input this is deliberately never placed in the
     * generic offline queue: the voice controller owns exactly one bounded,
     * unacknowledged final and replays those same immutable identifiers only
     * after a fresh UI binding is installed.
     */
    fun sendVoiceTranscript(
        transcript: VoiceTranscript,
        expectedConnectionGeneration: String,
        onSubmission: (LocalSubmission) -> Unit = {},
    ): Boolean {
        val currentSocket = socket
        if (!open || currentSocket == null || connectionGeneration != expectedConnectionGeneration) return false
        val submission =
            LocalSubmission(
                action = "chat_message",
                chatId = transcript.chatId,
                submissionId = transcript.submissionId,
                requestGeneration = transcript.requestGeneration,
            )
        val frame =
            runCatching { Wire.encodeVoiceChatMessage(transcript, expectedConnectionGeneration) }
                .getOrNull() ?: return false
        val request = conversationRequest(submission, ConversationRequestPurpose.COMMIT)
        bindRequest(request, generationObserver)
        onSubmission(submission)
        return currentSocket.send(frame)
    }

    /**
     * Report a local render observation only on its still-current UI socket.
     * Playout evidence is intentionally not queued or replayed after reconnect.
     */
    fun sendVoicePlayoutEvent(value: VoicePlayoutEvent): Boolean {
        val currentSocket = socket
        if (
            !open || currentSocket == null ||
            connectionGeneration != value.connectionGeneration
        ) {
            return false
        }
        val frame = runCatching { Wire.encodeVoicePlayoutEvent(value) }.getOrNull() ?: return false
        return currentSocket.send(frame)
    }

    /**
     * Create the initial chat for an explicit voice tap with the strict C1
     * correlation envelope. This handshake is live-only and never enters the
     * generic offline message queue.
     */
    fun createChatForVoice(): LocalSubmission? {
        val currentSocket = socket
        val currentGeneration = connectionGeneration
        if (!open || currentSocket == null || currentGeneration == null) return null
        val submission = newSubmission("new_chat", null)
        val frame =
            Wire.encodeCorrelatedVoiceNewChat(
                connectionGeneration = currentGeneration,
                submissionId = submission.submissionId,
                requestGeneration = submission.requestGeneration,
            )
        return submission.takeIf { currentSocket.send(frame) }
    }

    /** Current registered UI generation, exposed only for voice equality fencing. */
    fun currentConnectionGeneration(): String? = connectionGeneration.takeIf { open }

    fun sendEvent(
        action: String,
        sessionId: String?,
        payload: JsonObject = JsonObject(emptyMap()),
        onSubmission: (LocalSubmission) -> Unit = {},
    ): LocalSubmission {
        val payloadChat = (payload["chat_id"] as? JsonPrimitive)?.contentOrNull
        val submission = newSubmission(action, payloadChat ?: sessionId)
        // See sendChat: local acknowledgement is synchronous and precedes
        // both the offline queue and any live WebSocket send.
        onSubmission(submission)
        val request =
            when (action) {
                "load_chat" -> {
                    conversationRequest(submission, ConversationRequestPurpose.HYDRATION)
                }
                "chat_message" -> conversationRequest(submission, ConversationRequestPurpose.COMMIT)
                else -> null
            }
        enqueueOrSend(
            action,
            Wire.encodeUiEvent(
                action = action,
                sessionId = sessionId,
                payload = payload,
                requestGeneration = submission.requestGeneration,
                submissionId = submission.submissionId,
            ),
            submission,
            request,
        )
        return submission
    }

    /**
     * Build one connection attempt. Production calls this immediately inside
     * `onOpen`; tests use it to prove locator/generation registration bytes.
     */
    internal fun createRegistrationAttempt(
        token: String,
        device: DeviceCapabilities,
        activeChatId: String?,
    ): RegistrationAttempt {
        val connection = newUuid4()
        val request = activeChatId?.let { newUuid4() }
        val binding =
            ConversationGenerationBinding(
                connectionGeneration = connection,
                chatId = activeChatId,
                requestGeneration = request,
                purpose = request?.let { ConversationRequestPurpose.HYDRATION },
            )
        return RegistrationAttempt(
            binding = binding,
            frame =
                Wire.encodeRegisterUi(
                    token = token,
                    sessionId = activeChatId,
                    device = device,
                    connectionGeneration = connection,
                    resume = request?.let { ConversationResume(activeChatId!!, it) },
                ),
        )
    }

    @Volatile
    private var generationObserver: (ConversationGenerationBinding) -> Unit = {}

    /** Install the observer used by sends that happen outside the stream call stack. */
    internal fun observeConversationGenerations(observer: (ConversationGenerationBinding) -> Unit) {
        generationObserver = observer
    }

    private fun conversationRequest(
        submission: LocalSubmission,
        purpose: ConversationRequestPurpose,
    ) =
        ConversationRequest(
            chatId = submission.chatId,
            requestGeneration = submission.requestGeneration,
            purpose = purpose,
        )

    private fun newSubmission(
        action: String,
        chatId: String?,
    ): LocalSubmission {
        val submissionId = newUuid4()
        val requestGeneration = newUuid4()
        require(requestGeneration != submissionId) {
            "submission and request generation must be distinct UUID4 values"
        }
        return LocalSubmission(
            action = action,
            chatId = chatId,
            submissionId = submissionId,
            requestGeneration = requestGeneration,
        )
    }

    private fun bindRequest(
        request: ConversationRequest,
        observer: (ConversationGenerationBinding) -> Unit,
    ) {
        val connection = connectionGeneration ?: return
        observer(
            ConversationGenerationBinding(
                connectionGeneration = connection,
                chatId = request.chatId,
                requestGeneration = request.requestGeneration,
                purpose = request.purpose,
            ),
        )
    }

    private fun newUuid4(): String {
        val value = uuidFactory()
        val parsed = runCatching { UUID.fromString(value) }.getOrNull()
        require(parsed?.version() == 4 && parsed.toString() == value) { "uuidFactory must return canonical UUID4" }
        return value
    }

    /** Validate the replay metadata against the exact serialized UI event. */
    internal fun validQueuedIdentity(
        frame: String,
        submission: LocalSubmission,
    ): Boolean {
        val root =
            runCatching { Json.parseToJsonElement(frame).jsonObject }
                .getOrNull() ?: return false
        val payload = root["payload"]?.let { runCatching { it.jsonObject }.getOrNull() } ?: return false
        val action = (root["action"] as? JsonPrimitive)?.contentOrNull ?: return false
        val topSubmission = (root["submission_id"] as? JsonPrimitive)?.contentOrNull
        val topRequest = (root["request_generation"] as? JsonPrimitive)?.contentOrNull
        val payloadSubmission = (payload["submission_id"] as? JsonPrimitive)?.contentOrNull
        val payloadRequest = (payload["request_generation"] as? JsonPrimitive)?.contentOrNull
        if (
            (root["type"] as? JsonPrimitive)?.contentOrNull != "ui_event" ||
            action != submission.action ||
            !SNAKE_CASE.matches(action) ||
            canonicalUuid4(topSubmission) == null ||
            canonicalUuid4(topRequest) == null ||
            topSubmission != submission.submissionId ||
            topRequest != submission.requestGeneration ||
            payloadSubmission != topSubmission ||
            payloadRequest != topRequest
        ) {
            return false
        }
        val explicitChat = (payload["chat_id"] as? JsonPrimitive)?.contentOrNull
        if (explicitChat != null && canonicalUuid4(explicitChat) == null) return false
        val sessionChat =
            (root["session_id"] as? JsonPrimitive)?.contentOrNull
                ?.takeIf { canonicalUuid4(it) != null }
        val chatId = explicitChat ?: sessionChat
        return submission.chatId == chatId &&
            (submission.chatId == null || canonicalUuid4(submission.chatId) != null)
    }

    /** Deterministic reconnect seam used by JVM tests without a real socket. */
    internal fun replayPendingForTest(
        connectionGeneration: String,
        onGeneration: (ConversationGenerationBinding) -> Unit,
        onQueuedSubmission: (LocalSubmission) -> Unit,
        send: (String) -> Boolean,
    ) {
        require(canonicalUuid4(connectionGeneration) != null)
        this.connectionGeneration = connectionGeneration
        flushPending(onGeneration, onQueuedSubmission, send)
    }

    /** Install a deterministic open socket for the send-failure race test. */
    internal fun installOpenSocketForTest(webSocket: WebSocket) {
        socket = webSocket
        open = true
    }

    /** The actions currently queued offline — a test seam to assert what was (not) sent. */
    internal fun pendingActions(): List<String> = synchronized(pending) { pending.map { it.action } }

    /** Raw queued frames for protocol tests; never exposed by the shipping API. */
    internal fun pendingFrames(): List<String> = synchronized(pending) { pending.map { it.frame } }

    companion object {
        private const val TAG = "OrchestratorClient"
        private const val NORMAL_CLOSE = 1000
        private const val MAX_QUEUE = 64
        private val SNAKE_CASE = Regex("^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")

        private fun canonicalUuid4(value: String?): String? {
            val parsed = runCatching { UUID.fromString(value) }.getOrNull()
            return value?.takeIf { parsed?.version() == 4 && parsed.toString() == it }
        }

        private fun defaultClient(): OkHttpClient =
            OkHttpClient.Builder()
                .pingInterval(20, TimeUnit.SECONDS)
                .readTimeout(0, TimeUnit.MILLISECONDS) // streaming socket
                .build()
    }
}
