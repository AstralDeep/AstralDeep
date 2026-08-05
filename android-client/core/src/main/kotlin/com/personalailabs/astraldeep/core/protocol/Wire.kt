package com.personalailabs.astraldeep.core.protocol

import com.personalailabs.astraldeep.core.sdui.CanvasOp
import com.personalailabs.astraldeep.core.sdui.Component
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import java.time.Instant
import java.util.UUID

/**
 * The wire codec. Tolerant decode (ignore-unknown-keys / lenient) of inbound
 * frames into [Inbound] variants, and encoders for the outbound frames
 * (`register_ui`, `ui_event` + helpers). Pure — no Android, JVM-unit-tested.
 */
object Wire {
    private val json =
        Json {
            ignoreUnknownKeys = true
            isLenient = true
            explicitNulls = false
        }

    fun decode(raw: String): Inbound {
        val root =
            runCatching { json.parseToJsonElement(raw) as? JsonObject }.getOrNull()
                ?: return Inbound.Unknown("")
        val type = root.str("type").orEmpty()
        if (type in VOICE_MEDIA_TYPES && raw.toByteArray(Charsets.UTF_8).size > MAX_VOICE_MEDIA_BYTES) {
            return Inbound.Unknown(type)
        }
        return decode(root)
    }

    fun decode(root: JsonObject): Inbound =
        when (val type = root.str("type").orEmpty()) {
            "ui_render" -> uiRenderFromJson(root, type)
            "ui_upsert" -> uiUpsertFromJson(root, type)
            // The modern push system and the legacy poll system share the frame shape.
            "ui_stream_data", "stream_data" -> uiStreamDataFromJson(root, type)
            "stream_subscribed" ->
                Inbound.StreamSubscribed(root.str("stream_id"), root.str("tool_name"), root.str("component_id"))
            "stream_error" -> {
                val payload = root.obj("payload")
                Inbound.StreamErrorMsg(
                    requestAction = root.str("request_action"),
                    sessionId = root.str("session_id"),
                    streamId = payload?.str("stream_id"),
                    toolName = payload?.str("tool_name") ?: root.str("tool_name"),
                    error =
                        errorFromJson(payload)
                            ?: StreamError(code = root.str("error"), message = root.str("error")),
                )
            }
            "stream_unsubscribed" -> Inbound.StreamUnsubscribed(root.str("tool_name"))
            "chat_created" -> chatCreatedFromJson(root) ?: Inbound.Unknown(type)
            "user_message_acked" -> messageAckFromJson(root) ?: Inbound.Unknown(type)
            "composer_state" -> composerStateFromJson(root) ?: Inbound.Unknown(type)
            "voice_control_binding" ->
                voiceControlBindingFromJson(root)?.let(Inbound::VoiceControlBindingFrame) ?: Inbound.Unknown(type)
            "voice_session_state" ->
                voiceSessionStateFromJson(root)?.let(Inbound::VoiceSessionStateFrame) ?: Inbound.Unknown(type)
            "voice_turn_state" ->
                voiceTurnStateFromJson(root)?.let(Inbound::VoiceTurnStateFrame) ?: Inbound.Unknown(type)
            "voice_submission_rejected" ->
                voiceSubmissionRejectedFromJson(root)?.let(Inbound::VoiceSubmissionRejectedFrame)
                    ?: Inbound.Unknown(type)
            "voice_transcript" ->
                voiceTranscriptFromJson(root)?.let(Inbound::VoiceTranscriptFrame) ?: Inbound.Unknown(type)
            "voice_announcement_media" ->
                voiceAnnouncementFromJson(root)?.let(Inbound::VoiceAnnouncementMediaFrame) ?: Inbound.Unknown(type)
            "chat_loaded" -> Inbound.ChatLoaded(transcriptFromJson(root.obj("chat")))
            "conversation_snapshot" -> conversationSnapshotFromJson(root) ?: Inbound.Unknown(type)
            "conversation_commit_ready" -> conversationCommitReadyFromJson(root) ?: Inbound.Unknown(type)
            "agent_list" -> Inbound.AgentList(agentsFromJson(root.arr("agents")))
            "history_list" -> Inbound.HistoryList(chatsFromJson(root.arr("chats")))
            "chat_status" -> Inbound.ChatStatus(root.str("status"), root.str("message"))
            "operation_status" -> operationStatusFromJson(root) ?: Inbound.Unknown(type)
            "agent_lifecycle" -> agentLifecycleFromJson(root) ?: Inbound.Unknown(type)
            "chrome_render" -> Inbound.ChromeRender(root.str("region") ?: "modal", root.str("html").orEmpty())
            "chrome_menu" ->
                com.personalailabs.astraldeep.core.chrome.ChromeMenuModel.fromJson(root.obj("model"))
                    ?.let { Inbound.ChromeMenu(it) } ?: Inbound.Unknown(type)
            "chrome_surface" ->
                Inbound.ChromeSurface(
                    surfaceKey = root.str("surface_key").orEmpty(),
                    title = root.str("title").orEmpty(),
                    components = Component.listFromJson(root.arr("components")),
                    // Reserved delivery field (054): absent == "replace" (today's
                    // behavior); "mandatory" == the first-run LLM-setup gate.
                    mode = root.str("mode") ?: "replace",
                )
            "auth_required" -> Inbound.AuthRequired(root.str("reason"))
            // Server error replies arrive in three shapes: {code,message},
            // {payload:{message}}, {message} — normalize; never silent (FR-002).
            "error" ->
                admissionRefusalFromJson(root)
                    ?: Inbound.ErrorFrame(
                        code = root.str("code"),
                        message = root.str("message") ?: root.obj("payload")?.str("message") ?: "Something went wrong.",
                        chatId = root.str("chat_id"),
                        connectionGeneration = root.str("connection_generation"),
                        requestGeneration = root.str("request_generation"),
                        retryable = root.bool("retryable") ?: false,
                        submissionId = canonicalUuid4(root.str("submission_id")),
                        accepted = root.bool("accepted"),
                    )
            "chat_step" -> {
                val step = root.obj("step")
                Inbound.ChatStep(
                    id = step?.str("id"),
                    name = step?.str("name") ?: step?.str("kind"),
                    status = step?.str("status"),
                )
            }
            "tool_progress" -> {
                // Compose a short human label from whatever fields arrived (all
                // optional): "tool: message (pct%)".
                val head = listOfNotNull(root.str("tool_name"), root.str("message")).joinToString(": ")
                val pct = root.str("percentage")?.let { " ($it%)" }.orEmpty()
                Inbound.ToolProgress(label = (head + pct).ifBlank { "Working…" })
            }
            // Task frames nest their fields under `payload` (older emitters were flat).
            "task_started" ->
                Inbound.TaskStarted(
                    taskId = root.obj("payload")?.str("task_id") ?: root.str("task_id"),
                    chatId = root.obj("payload")?.str("chat_id") ?: root.str("chat_id"),
                )
            "task_completed" ->
                Inbound.TaskCompleted(
                    taskId = root.obj("payload")?.str("task_id") ?: root.str("task_id"),
                    chatId = root.obj("payload")?.str("chat_id") ?: root.str("chat_id"),
                )
            "notification" ->
                Inbound.Notification(
                    title = root.str("title"),
                    body = root.str("body"),
                    level = root.str("level"),
                    chatId = root.str("chat_id"),
                )
            // Stored preferences at boot ({preferences:{theme:{…}}}); the app folds
            // `theme` into the live palette (US5 restyle).
            "user_preferences" -> Inbound.UserPreferences(theme = root.obj("preferences")?.obj("theme"))
            // Read-only workspace timeline toggle ({active}); `on` is tolerated.
            "workspace_timeline_mode" ->
                Inbound.WorkspaceTimelineMode(active = root.bool("active") ?: root.bool("on") ?: false)
            // Workspace verb acks (055 US3, wire-contract §4).
            "component_saved" -> Inbound.ComponentSaved(title = root.obj("component")?.str("title"))
            "component_save_error" -> Inbound.ComponentSaveError(root.str("error"))
            "component_deleted" -> Inbound.ComponentDeleted(root.str("component_id"))
            "combine_status" -> Inbound.CombineStatus(root.str("status"), root.str("message"))
            "combine_error" -> Inbound.CombineError(root.str("error"))
            "components_combined", "components_condensed" ->
                Inbound.ComponentsReplaced(
                    removedIds = root.strList("removed_ids"),
                    newComponents = replacementsFromJson(root.arr("new_components")),
                )
            "saved_components_list" -> Inbound.SavedComponentsList(count = root.arr("components")?.size ?: 0)
            else -> Inbound.Unknown(type)
        }

    /** Validate the shared structured-v2 host advertisement without emitting it. */
    fun decodeAgentHostRegistration(raw: String): AgentHostRegistration? = parseObject(raw)?.let(::agentHostRegistrationFromJson)

    /** Validate the host acknowledgement that author-only Android deliberately ignores. */
    fun decodeAgentHostRegistered(raw: String): AgentHostRegistered? = parseObject(raw)?.let(::agentHostRegisteredFromJson)

    /** Parse the immutable candidate capability map; malformed/missing data stays unknown. */
    fun decodeCandidateCapabilityMap(raw: String): CandidateCapabilityMap? = parseObject(raw)?.let(::candidateCapabilityMapFromJson)

    fun decodeCandidateCapabilityMap(root: JsonObject): CandidateCapabilityMap? = candidateCapabilityMapFromJson(root)

    // ---- outbound encoders ----

    fun encodeRegisterUi(
        token: String,
        sessionId: String?,
        device: DeviceCapabilities,
        connectionGeneration: String? = null,
        resume: ConversationResume? = null,
    ): String {
        require(connectionGeneration == null || canonicalUuid4(connectionGeneration) != null) {
            "connectionGeneration must be a canonical UUID4"
        }
        if (resume != null) {
            require(connectionGeneration != null) { "resume requires connectionGeneration" }
            require(resume.schemaVersion == 1) { "resume schemaVersion must be 1" }
            require(canonicalUuid4(resume.activeChatId) != null) { "resume activeChatId must be a canonical UUID4" }
            require(canonicalUuid4(resume.requestGeneration) != null) {
                "resume requestGeneration must be a canonical UUID4"
            }
        }
        return buildJsonObject {
            put("type", "register_ui")
            put("token", token)
            putJsonArray("capabilities") {
                add("render")
                add("stream")
                if (device.hasMicrophone && device.hasAudioOutput) add("voice")
            }
            put("session_id", sessionId)
            device.deviceId?.let { put("device_id", it) }
            putJsonObject("device") {
                put("device_type", device.deviceType)
                put("screen_width", device.screenWidth)
                put("screen_height", device.screenHeight)
                put("viewport_width", device.viewportWidth)
                put("viewport_height", device.viewportHeight)
                put("pixel_ratio", device.pixelRatio)
                put("has_touch", device.hasTouch)
                put("has_microphone", device.hasMicrophone)
                put("has_audio_output", device.hasAudioOutput)
                put("microphone_permission", device.microphonePermission)
                put("full_duplex", device.fullDuplex)
                put("voice_transport", device.voiceTransport)
                putJsonArray("supported_types") { device.supportedTypes.forEach { add(it) } }
            }
            put("resumed", false)
            if (connectionGeneration != null) put("connection_generation", connectionGeneration)
            if (resume != null) {
                putJsonObject("resume") {
                    put("schema_version", resume.schemaVersion)
                    put("active_chat_id", resume.activeChatId)
                    put("request_generation", resume.requestGeneration)
                }
            }
        }.toString()
    }

    fun encodeUiEvent(
        action: String,
        sessionId: String?,
        payload: JsonObject = JsonObject(emptyMap()),
        requestGeneration: String? = null,
        submissionId: String? = null,
    ): String {
        require(requestGeneration == null || canonicalUuid4(requestGeneration) != null) {
            "requestGeneration must be a canonical UUID4"
        }
        require(submissionId == null || canonicalUuid4(submissionId) != null) {
            "submissionId must be a canonical UUID4"
        }
        val identifiedPayload =
            buildJsonObject {
                payload.forEach(::put)
                if (submissionId != null) put("submission_id", submissionId)
                if (requestGeneration != null) put("request_generation", requestGeneration)
            }
        return buildJsonObject {
            put("type", "ui_event")
            put("action", action)
            put("session_id", sessionId)
            if (submissionId != null) put("submission_id", submissionId)
            if (requestGeneration != null) put("request_generation", requestGeneration)
            put("payload", identifiedPayload)
        }.toString()
    }

    fun encodeChatMessage(
        message: String,
        chatId: String?,
        attachments: List<ChatAttachment> = emptyList(),
        requestGeneration: String? = null,
        submissionId: String? = null,
    ): String =
        encodeUiEvent(
            action = "chat_message",
            sessionId = chatId,
            payload =
                buildJsonObject {
                    put("message", message)
                    if (chatId != null) put("chat_id", chatId)
                    if (attachments.isNotEmpty()) {
                        putJsonArray("attachments") {
                            attachments.forEach { a ->
                                add(
                                    buildJsonObject {
                                        put("attachment_id", a.attachmentId)
                                        put("filename", a.filename)
                                        put("category", a.category)
                                    },
                                )
                            }
                        }
                    }
                },
            requestGeneration = requestGeneration,
            submissionId = submissionId,
        )

    /**
     * Build the ordinary `chat_message` used for a final voice transcript.
     * The proof-bearing origin is copied verbatim; no voice-only dispatch exists.
     */
    fun encodeVoiceChatMessage(
        transcript: VoiceTranscript,
        connectionGeneration: String,
    ): String {
        require(canonicalUuid4(connectionGeneration) != null) { "connectionGeneration must be a canonical UUID4" }
        require(transcript.final && transcript.text.isNotBlank()) { "only a non-empty final transcript may be submitted" }
        val origin = requireNotNull(transcript.originOrNull()) { "final transcript proof is incomplete" }
        return buildJsonObject {
            put("type", "ui_event")
            put("action", "chat_message")
            put("session_id", transcript.chatId)
            put("connection_generation", connectionGeneration)
            put("submission_id", transcript.submissionId)
            put("request_generation", transcript.requestGeneration)
            putJsonObject("payload") {
                put("message", transcript.text)
                put("chat_id", transcript.chatId)
                put("connection_generation", connectionGeneration)
                put("submission_id", transcript.submissionId)
                put("request_generation", transcript.requestGeneration)
                put("snapshot_purpose", "commit")
                putJsonObject("voice_origin") {
                    put("schema_version", origin.schemaVersion)
                    put("session_id", origin.sessionId)
                    put("generation", origin.generation)
                    put("media_grant_revision", origin.mediaGrantRevision)
                    put("turn_id", origin.turnId)
                    put("client_turn_id", origin.clientTurnId)
                    put("chat_context_revision", origin.chatContextRevision)
                    put("source_participant_identity", origin.sourceParticipantIdentity)
                    put("detected_language", origin.detectedLanguage)
                    put("text_digest_sha256", origin.textDigestSha256)
                    put("transcript_proof", origin.transcriptProof)
                    put("proof_expires_at", origin.proofExpiresAt)
                }
            }
        }.toString()
    }

    /** Strict correlated new-chat handshake used only to bootstrap explicit voice activation. */
    fun encodeCorrelatedVoiceNewChat(
        connectionGeneration: String,
        submissionId: String,
        requestGeneration: String,
    ): String {
        require(canonicalUuid4(connectionGeneration) != null)
        require(canonicalUuid4(submissionId) != null)
        require(canonicalUuid4(requestGeneration) != null)
        return buildJsonObject {
            put("type", "ui_event")
            put("action", "new_chat")
            put("schema_version", VOICE_SCHEMA_VERSION)
            put("connection_generation", connectionGeneration)
            put("submission_id", submissionId)
            put("request_generation", requestGeneration)
            putJsonObject("payload") {
                put("schema_version", VOICE_SCHEMA_VERSION)
                put("connection_generation", connectionGeneration)
                put("submission_id", submissionId)
                put("request_generation", requestGeneration)
            }
        }.toString()
    }

    /** Encode one content-free observation from the matched local audio renderer. */
    fun encodeVoicePlayoutEvent(value: VoicePlayoutEvent): String {
        require(canonicalUuid4(value.deviceId) != null)
        require(canonicalUuid4(value.connectionGeneration) != null)
        require(canonicalUuid4(value.sessionId) != null)
        require(value.generation > 0 && value.mediaGrantRevision > 0)
        require(canonicalUuid4(value.announcementId) != null)
        require(value.announcementSequence > 0)
        require(value.turnId == null || canonicalUuid4(value.turnId) != null)
        require(value.kind in VOICE_ANNOUNCEMENT_KINDS)
        require(value.quantumRole in VOICE_QUANTUM_ROLES)
        require(value.quantumIndex in 0..31)
        require(value.phase in VOICE_PLAYOUT_PHASES)
        require(value.clientSequence >= 0)
        require(runCatching { Instant.parse(value.observedAt) }.isSuccess)
        when (value.quantumRole) {
            "single" -> {
                require(value.kind != "result" && value.quantumIndex == 0)
                require(value.resultReservedSamplesAfter == null)
            }
            "result_opening" -> {
                require(value.kind == "result" && value.quantumIndex == 0)
                require(value.resultReservedSamplesAfter in 1..36_000)
            }
            "result_continuation" -> {
                require(value.kind == "result" && value.quantumIndex in 1..31)
                require(value.resultReservedSamplesAfter in 1..720_000)
            }
        }
        require((value.kind == "greeting") == (value.turnId == null))
        return buildJsonObject {
            put("type", "voice_playout_event")
            put("schema_version", VOICE_SCHEMA_VERSION)
            put("device_id", value.deviceId)
            put("connection_generation", value.connectionGeneration)
            put("session_id", value.sessionId)
            put("generation", value.generation)
            put("media_grant_revision", value.mediaGrantRevision)
            put("announcement_id", value.announcementId)
            put("announcement_sequence", value.announcementSequence)
            put("turn_id", value.turnId)
            put("kind", value.kind)
            put("quantum_role", value.quantumRole)
            put("quantum_index", value.quantumIndex)
            value.resultReservedSamplesAfter?.let { put("result_reserved_samples_after", it) }
            put("phase", value.phase)
            put("client_sequence", value.clientSequence)
            put("observed_at", value.observedAt)
        }.toString()
    }

    // ---- feature 060 strict wire models ----

    private data class ScopeDecode(
        val valid: Boolean,
        val scope: TransientFrameScope?,
    )

    private data class ExplicitNullable<out T>(val value: T?)

    private const val VOICE_SCHEMA_VERSION = "1"
    private const val MAX_VOICE_MEDIA_BYTES = 12 * 1024

    private val VOICE_ANNOUNCEMENT_KINDS =
        setOf(
            "greeting", "acknowledgement", "progress", "waiting", "result",
            "sensitive_notice", "failure", "refusal", "cancellation",
        )
    private val VOICE_QUANTUM_ROLES = setOf("single", "result_opening", "result_continuation")
    private val VOICE_PLAYOUT_PHASES = setOf("started", "finished", "interrupted")

    // 066 T023: bounded canonical text-part variants (backend CANONICAL_TEXT_PART_VARIANTS twin).
    private val CANONICAL_TEXT_PART_VARIANTS = setOf("caption")

    private val snakeCase = Regex("^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
    private val lowerSha256 = Regex("^[0-9a-f]{64}$")
    private val languageTag = Regex("^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
    private val opaqueId = Regex("^[A-Za-z0-9._:-]+$")
    private val VOICE_MEDIA_TYPES = setOf("voice_transcript", "voice_announcement_media")
    private val VOICE_STATES =
        setOf(
            "off", "unavailable", "connecting", "greeting", "listening", "speech_detected",
            "transcribing", "acknowledging", "processing", "waiting_on_user", "speaking_progress",
            "speaking_result", "muted", "suspended", "reconnecting", "error", "ended",
        )
    private val INACTIVE_VOICE_STATES = setOf("off", "unavailable", "suspended", "reconnecting", "error", "ended")
    private val ACTIVE_VOICE_STATES =
        setOf(
            "connecting", "greeting", "listening", "speech_detected", "transcribing", "acknowledging",
            "processing", "waiting_on_user", "speaking_progress", "speaking_result", "muted",
            "reconnecting", "error",
        )
    private val VOICE_REASONS =
        setOf(
            "ready", "feature_disabled", "authentication_required", "permission_not_determined",
            "permission_denied", "permission_restricted", "no_microphone", "no_audio_output",
            "media_unavailable", "worker_unavailable", "asr_unavailable", "tts_unavailable",
            "voice_unavailable", "output_language_unsupported", "capacity_exhausted", "takeover_required",
            "idle_expired", "backgrounded", "audio_interrupted", "chat_context_unavailable", "auth_expired",
            "network_interrupted", "media_error", "speech_error", "stale_generation", "ended_by_user",
            "internal_error",
        )
    private val VOICE_ACTIONS =
        setOf(
            "voice_session_start",
            "voice_session_takeover",
            "voice_session_end",
            "voice_microphone_set",
            "voice_speech_stop",
            "voice_speech_mute_set",
            "voice_visible_chat_update",
            "voice_sensitive_recap_request",
        )
    private val SPOKEN_OUTPUT_POLICIES = setOf("pending", "full_recap", "english_lifecycle_only")
    private val OUTPUT_REASONS = setOf("language_pending", "ready", "output_language_unsupported")
    private val TURN_STATES =
        setOf(
            "recognizing", "submitting", "accepted", "processing", "waiting_on_user", "succeeded", "failed",
            "refused", "cancelled", "abandoned",
        )
    private val REJECTION_REASONS =
        setOf(
            "capacity_exhausted",
            "chat_unavailable",
            "invalid_binding",
            "invalid_proof",
            "proof_expired",
            "permission_denied",
            "stale_session",
            "malformed_final",
        )
    private val ANNOUNCEMENT_KINDS =
        setOf(
            "greeting", "acknowledgement", "progress", "waiting", "result", "sensitive_notice", "failure",
            "refusal", "cancellation",
        )
    private val QUANTUM_ROLES = setOf("single", "result_opening", "result_continuation")
    private val VOICE_TRANSPORTS = setOf("livekit", "watch_pcm_websocket")
    private val VOICE_BINDING_PATTERN = Regex("^[A-Za-z0-9._~-]+$")
    private val strictSemVer =
        Regex(
            "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)" +
                "(?:-[0-9A-Za-z-]+(?:\\.[0-9A-Za-z-]+)*)?" +
                "(?:\\+[0-9A-Za-z-]+(?:\\.[0-9A-Za-z-]+)*)?$",
        )
    private val operationFlags =
        mapOf(
            "accepted" to (false to false),
            "validating" to (false to false),
            "persisting" to (false to false),
            "running" to (false to false),
            "completed" to (true to false),
            "failed" to (true to false),
            "cancelled" to (true to false),
            "retryable" to (true to true),
        )
    private val operationErrorCodes =
        setOf(
            "invalid_input",
            "validation_failed",
            "provider_unavailable",
            "network_unavailable",
            "deadline_exceeded",
            "capacity_exceeded",
            "queue_wait_expired",
            "registration_timeout",
            "disconnected",
            "cancelled_by_user",
            "operation_failed",
            "conflict",
            "incompatible_runtime",
            "agent_offline",
            "stale_generation",
        )
    private val admissionRefusalCodes =
        setOf(
            "capacity_exceeded",
            "registration_required",
            "registration_timeout",
            "idempotency_conflict",
            "connection_closing",
            "service_draining",
            "invalid_input",
            "registration_queue_full",
            "operation_failed",
        )
    private val agentLifecycleReasonCodes =
        setOf(
            "invalid_host_registration",
            "runtime_contract_unsupported",
            "runtime_lock_mismatch",
            "bundle_digest_mismatch",
            "bundle_install_failed",
            "child_start_failed",
            "child_registration_timeout",
            "child_exited",
            "child_hung",
            "host_lost",
            "agent_offline",
            "agent_deleted",
            "stale_runtime_generation",
            "revision_promotion_failed",
            "inventory_required",
            "process_cleanup_timeout",
        )

    private fun uiRenderFromJson(
        root: JsonObject,
        type: String,
    ): Inbound {
        val decodedScope = root.transientScope()
        if (!decodedScope.valid) return Inbound.Unknown(type)
        return Inbound.UiRender(
            target = root.str("target") ?: "canvas",
            components = Component.listFromJson(root.arr("components")),
            scope = decodedScope.scope,
        )
    }

    private fun uiUpsertFromJson(
        root: JsonObject,
        type: String,
    ): Inbound {
        val decodedScope = root.transientScope()
        if (!decodedScope.valid) return Inbound.Unknown(type)
        return Inbound.UiUpsert(
            chatId = root.str("chat_id"),
            ops = opsFromJson(root.arr("ops")),
            scope = decodedScope.scope,
        )
    }

    private fun uiStreamDataFromJson(
        root: JsonObject,
        type: String,
    ): Inbound {
        val decodedScope = root.transientScope()
        if (!decodedScope.valid) return Inbound.Unknown(type)
        return Inbound.UiStreamData(
            streamId = root.str("stream_id"),
            sessionId = root.str("session_id"),
            seq = root.int("seq"),
            components = Component.listFromJson(root.arr("components")),
            terminal = root.bool("terminal") ?: false,
            error = errorFromJson(root.obj("error")),
            toolName = root.str("tool_name"),
            componentId = root.str("component_id"),
            scope = decodedScope.scope,
        )
    }

    private fun JsonObject.transientScope(): ScopeDecode {
        val fenceFields =
            listOf(
                "connection_generation",
                "request_generation",
                "base_render_revision",
                "frame_sequence",
            )
        if (fenceFields.none(::containsKey)) return ScopeDecode(valid = true, scope = null)
        val chatId = canonicalUuid4(strictString("chat_id")) ?: return ScopeDecode(false, null)
        val connection =
            canonicalUuid4(strictString("connection_generation")) ?: return ScopeDecode(false, null)
        val request = canonicalUuid4(strictString("request_generation")) ?: return ScopeDecode(false, null)
        val baseRevision = strictULong("base_render_revision") ?: return ScopeDecode(false, null)
        val sequence = strictULong("frame_sequence") ?: return ScopeDecode(false, null)
        return ScopeDecode(
            valid = true,
            scope =
                TransientFrameScope(
                    chatId = chatId,
                    connectionGeneration = connection,
                    requestGeneration = request,
                    baseRenderRevision = baseRevision,
                    frameSequence = sequence,
                ),
        )
    }

    private fun conversationSnapshotFromJson(root: JsonObject): Inbound.ConversationSnapshot? {
        if (
            !root.hasExactKeys(
                "type",
                "schema_version",
                "snapshot_id",
                "chat_id",
                "connection_generation",
                "request_generation",
                "snapshot_purpose",
                "render_revision",
                "committed_at",
                "transcript",
                "canvas",
            )
        ) {
            return null
        }
        if (root.strictString("type") != "conversation_snapshot" || root.strictULong("schema_version") != 1UL) {
            return null
        }
        val snapshotId = canonicalUuid4(root.strictString("snapshot_id")) ?: return null
        val chatId = canonicalUuid4(root.strictString("chat_id")) ?: return null
        val connection = canonicalUuid4(root.strictString("connection_generation")) ?: return null
        val request = canonicalUuid4(root.strictString("request_generation")) ?: return null
        val purpose = root.strictString("snapshot_purpose")?.takeIf { it == "hydration" || it == "commit" } ?: return null
        val renderRevision = root.strictULong("render_revision") ?: return null
        val committedAt = root.strictString("committed_at")?.takeIf(::isRfc3339Utc) ?: return null
        val transcript = root.arr("transcript")?.let(::canonicalTranscript) ?: return null
        val canvasObject = root.obj("canvas") ?: return null
        if (!canvasObject.hasExactKeys("target", "components") || canvasObject.strictString("target") != "canvas") {
            return null
        }
        val componentArray = canvasObject.arr("components") ?: return null
        if (!canonicalNativeComponents(componentArray)) return null
        return Inbound.ConversationSnapshot(
            schemaVersion = 1,
            snapshotId = snapshotId,
            chatId = chatId,
            connectionGeneration = connection,
            requestGeneration = request,
            snapshotPurpose = purpose,
            renderRevision = renderRevision,
            committedAt = committedAt,
            transcript = transcript,
            canvas = SnapshotCanvas(target = "canvas", components = Component.listFromJson(componentArray)),
        )
    }

    private fun conversationCommitReadyFromJson(root: JsonObject): Inbound.ConversationCommitReady? {
        if (
            !root.hasExactKeys(
                "type",
                "schema_version",
                "chat_id",
                "connection_generation",
                "request_generation",
                "render_revision",
            ) ||
            root.strictString("type") != "conversation_commit_ready" ||
            root.strictULong("schema_version") != 1UL
        ) {
            return null
        }
        val chatId = canonicalUuid4(root.strictString("chat_id")) ?: return null
        val connection = canonicalUuid4(root.strictString("connection_generation")) ?: return null
        val request = canonicalUuid4(root.strictString("request_generation")) ?: return null
        val revision = root.strictULong("render_revision") ?: return null
        return Inbound.ConversationCommitReady(
            schemaVersion = 1,
            chatId = chatId,
            connectionGeneration = connection,
            requestGeneration = request,
            renderRevision = revision,
        )
    }

    private fun canonicalTranscript(array: JsonArray): List<JsonObject>? {
        val messages = mutableListOf<JsonObject>()
        for (element in array) {
            val message = element as? JsonObject ?: return null
            if (!canonicalTranscriptMessage(message)) return null
            messages += message
        }
        return messages
    }

    private fun canonicalTranscriptMessage(message: JsonObject): Boolean {
        if (!message.hasExactKeys("message_id", "role", "created_at", "parts", "attachments")) return false
        if (message.strictString("message_id").isNullOrEmpty()) return false
        if (message.strictString("role") !in setOf("user", "assistant", "system", "tool")) return false
        if (message.strictString("created_at")?.let(::isRfc3339Utc) != true) return false
        val attachments = message.arr("attachments") ?: return false
        if (attachments.any { it !is JsonObject }) return false
        val parts = message.arr("parts")?.takeIf { it.isNotEmpty() } ?: return false
        return parts.all { part -> (part as? JsonObject)?.let(::canonicalTranscriptPart) == true }
    }

    // 066 T023: the bounded caption shape a text part may additionally take
    // (mirrors backend CANONICAL_TEXT_PART_VARIANTS).
    private fun boundedTextVariantShape(part: JsonObject): Boolean =
        part.hasExactKeys("type", "text", "variant") &&
            part.strictString("variant") in CANONICAL_TEXT_PART_VARIANTS

    private fun canonicalTranscriptPart(part: JsonObject): Boolean =
        when (part.strictString("type")) {
            "text" ->
                (part.hasExactKeys("type", "text") || boundedTextVariantShape(part)) &&
                    part.strictString("text") != null
            "components" -> {
                val components = part.arr("components")
                part.hasExactKeys("type", "components") &&
                    components != null &&
                    canonicalNativeComponents(components)
            }
            "structured" ->
                part.hasExactKeys("type", "value", "plain_text") && part.strictString("plain_text") != null
            "recovery" ->
                part.hasExactKeys("type", "code", "message") &&
                    !part.strictString("code").isNullOrEmpty() &&
                    !part.strictString("message").isNullOrEmpty()
            else -> false
        }

    /** Native semantic snapshots never accept web-only presentation authority. */
    private fun canonicalNativeComponents(components: JsonArray): Boolean =
        components.all { element ->
            val component = element as? JsonObject ?: return@all false
            val type = component.strictString("type")
            if (type.isNullOrBlank() || "_presentation" in component) return@all false
            val children = component["children"]
            if (children is JsonArray && !canonicalNativeComponents(children)) return@all false
            if (children is JsonObject && !canonicalNativeComponents(JsonArray(listOf(children)))) return@all false
            val content = component["content"]
            when {
                content is JsonObject && content.strictString("type") != null ->
                    canonicalNativeComponents(JsonArray(listOf(content)))
                content is JsonArray &&
                    content.isNotEmpty() &&
                    content.all { (it as? JsonObject)?.strictString("type") != null } ->
                    canonicalNativeComponents(content)
                else -> true
            }
        }

    private fun operationStatusFromJson(root: JsonObject): Inbound.OperationStatus? {
        if (
            !root.hasExactKeys(
                "type",
                "operation_id",
                "action",
                "surface",
                "chat_id",
                "connection_generation",
                "request_generation",
                "sequence",
                "state",
                "phase",
                "label",
                "terminal",
                "retryable",
                "error",
                "retry_after_ms",
                "updated_at",
            ) || root.strictString("type") != "operation_status"
        ) {
            return null
        }
        val operationId = canonicalUuid4(root.strictString("operation_id")) ?: return null
        val action = root.strictString("action")?.takeIf(::isSnakeCase) ?: return null
        val surface = root.strictString("surface")?.takeIf(::isSnakeCase) ?: return null
        val chatId = root.explicitNullableUuid("chat_id") ?: return null
        val connection = canonicalUuid4(root.strictString("connection_generation")) ?: return null
        val request = canonicalUuid4(root.strictString("request_generation")) ?: return null
        val sequence = root.strictULong("sequence") ?: return null
        val state = root.strictString("state") ?: return null
        val phase = root.strictString("phase")?.takeIf(::isSnakeCase) ?: return null
        val label = root.strictString("label")?.takeIf { it.isNotBlank() } ?: return null
        val terminal = root.strictBoolean("terminal") ?: return null
        val retryable = root.strictBoolean("retryable") ?: return null
        if (operationFlags[state] != (terminal to retryable)) return null

        val errorElement = root["error"] ?: return null
        val requiresError = state == "failed" || state == "cancelled" || state == "retryable"
        val error =
            if (requiresError) {
                val value = errorElement as? JsonObject ?: return null
                if (!value.hasExactKeys("code", "message")) return null
                val code = value.strictString("code")?.takeIf(operationErrorCodes::contains) ?: return null
                val message = value.strictString("message")?.takeIf { it.isNotBlank() } ?: return null
                OperationStatusError(code = code, message = message)
            } else {
                if (errorElement !is JsonNull) return null
                null
            }

        val retryAfterElement = root["retry_after_ms"] ?: return null
        val retryAfter =
            if (retryAfterElement is JsonNull) {
                null
            } else {
                if (state != "retryable") return null
                root.strictULong("retry_after_ms") ?: return null
            }
        val updatedAt = root.strictString("updated_at")?.takeIf(::isRfc3339Utc) ?: return null
        return Inbound.OperationStatus(
            operationId = operationId,
            action = action,
            surface = surface,
            chatId = chatId.value,
            connectionGeneration = connection,
            requestGeneration = request,
            sequence = sequence,
            state = state,
            phase = phase,
            label = label,
            terminal = terminal,
            retryable = retryable,
            error = error,
            retryAfterMs = retryAfter,
            updatedAt = updatedAt,
        )
    }

    private fun admissionRefusalFromJson(root: JsonObject): Inbound.AdmissionRefusal? {
        if (
            !root.hasExactKeys(
                "type",
                "submission_id",
                "accepted",
                "code",
                "message",
                "retryable",
                "retry_after_ms",
            ) || root.strictString("type") != "error" || root.strictBoolean("accepted") != false
        ) {
            return null
        }
        val submissionId = canonicalUuid4(root.strictString("submission_id")) ?: return null
        val code = root.strictString("code")?.takeIf(admissionRefusalCodes::contains) ?: return null
        val message = root.strictString("message")?.takeIf { it.isNotBlank() } ?: return null
        val retryable = root.strictBoolean("retryable") ?: return null
        val retryAfterElement = root["retry_after_ms"] ?: return null
        val retryAfter =
            if (retryAfterElement is JsonNull) {
                null
            } else {
                if (!retryable) return null
                root.strictULong("retry_after_ms") ?: return null
            }
        return Inbound.AdmissionRefusal(
            submissionId = submissionId,
            code = code,
            message = message,
            retryable = retryable,
            retryAfterMs = retryAfter,
        )
    }

    private fun agentLifecycleFromJson(root: JsonObject): Inbound.AgentLifecycle? {
        if (
            !root.hasExactKeys(
                "type",
                "agent_id",
                "revision_id",
                "runtime_instance_id",
                "lifecycle_generation",
                "state_revision",
                "state",
                "reason_code",
                "label",
                "updated_at",
            ) || root.strictString("type") != "agent_lifecycle"
        ) {
            return null
        }
        val agentId = root.strictString("agent_id")?.takeIf { it.isNotBlank() } ?: return null
        val revisionId = root.explicitNullableUuid("revision_id") ?: return null
        val runtimeId = root.explicitNullableUuid("runtime_instance_id") ?: return null
        val lifecycleGeneration = root.strictULong("lifecycle_generation") ?: return null
        val stateRevision = root.strictULong("state_revision") ?: return null
        val state =
            root.strictString("state")
                ?.takeIf { it in setOf("starting", "online", "updating", "failed", "offline") }
                ?: return null
        if (state in setOf("starting", "online", "updating") && (revisionId.value == null || runtimeId.value == null)) {
            return null
        }
        val reasonCode = root.explicitNullableString("reason_code") ?: return null
        if (reasonCode.value != null && reasonCode.value !in agentLifecycleReasonCodes) return null
        val label = root.strictString("label")?.takeIf { it.isNotBlank() } ?: return null
        val updatedAt = root.strictString("updated_at")?.takeIf(::isRfc3339Utc) ?: return null
        return Inbound.AgentLifecycle(
            agentId = agentId,
            revisionId = revisionId.value,
            runtimeInstanceId = runtimeId.value,
            lifecycleGeneration = lifecycleGeneration,
            stateRevision = stateRevision,
            state = state,
            reasonCode = reasonCode.value,
            label = label,
            updatedAt = updatedAt,
        )
    }

    private fun agentHostRegistrationFromJson(root: JsonObject): AgentHostRegistration? {
        if (
            !root.hasExactKeys(
                "host_id",
                "supported_runtime_contract_versions",
                "runtime_lock_sha256",
                "platform",
                "client_version",
            )
        ) {
            return null
        }
        val hostId = canonicalUuid4(root.strictString("host_id")) ?: return null
        val versions =
            root.positiveSortedVersions("supported_runtime_contract_versions")?.takeIf { it.isNotEmpty() }
                ?: return null
        val digest = root.strictString("runtime_lock_sha256")?.takeIf(lowerSha256::matches) ?: return null
        val platform = root.strictString("platform")?.takeIf { it == "windows" || it == "macos" } ?: return null
        val clientVersion = root.strictString("client_version")?.takeIf(strictSemVer::matches) ?: return null
        return AgentHostRegistration(hostId, versions, digest, platform, clientVersion)
    }

    private fun agentHostRegisteredFromJson(root: JsonObject): AgentHostRegistered? {
        if (
            !root.hasExactKeys("type", "host_id", "host_session_id", "inventory_required", "accepted_at") ||
            root.strictString("type") != "agent_host_registered"
        ) {
            return null
        }
        val hostId = canonicalUuid4(root.strictString("host_id")) ?: return null
        val hostSessionId = canonicalUuid4(root.strictString("host_session_id")) ?: return null
        val inventoryRequired = root.strictBoolean("inventory_required") ?: return null
        val acceptedAt = root.strictString("accepted_at")?.takeIf(::isRfc3339Utc) ?: return null
        return AgentHostRegistered(hostId, hostSessionId, inventoryRequired, acceptedAt)
    }

    private fun candidateCapabilityMapFromJson(root: JsonObject): CandidateCapabilityMap? {
        if (!root.hasExactKeys("capabilities")) return null
        val capabilities = root.obj("capabilities")?.takeIf { it.hasExactKeys("personal_agent_host") } ?: return null
        val hosts = capabilities.obj("personal_agent_host")?.takeIf { it.hasExactKeys("macos") } ?: return null
        val macos = hosts.obj("macos") ?: return null
        if (!macos.hasExactKeys("supported", "runtime_contract_versions", "source_feature")) return null
        val supported = macos.strictBoolean("supported") ?: return null
        val versions = macos.positiveSortedVersions("runtime_contract_versions") ?: return null
        val source = macos.explicitNullableString("source_feature") ?: return null
        if (supported) {
            if (2 !in versions || source.value != "059") return null
        } else if (versions.isNotEmpty() || source.value != null) {
            return null
        }
        return CandidateCapabilityMap(
            macosPersonalAgentHost =
                PersonalAgentHostCapability(
                    supported = supported,
                    runtimeContractVersions = versions,
                    sourceFeature = source.value,
                ),
        )
    }

    // ---- feature 065 conversational-voice contract ----

    private fun chatCreatedFromJson(root: JsonObject): Inbound.ChatCreated? {
        if (root["schema_version"] == null) {
            return Inbound.ChatCreated(root.obj("payload")?.str("chat_id") ?: root.str("chat_id"))
        }
        if (!root.hasExactKeys("type", "schema_version", "connection_generation", "submission_id", "request_generation", "payload")) {
            return null
        }
        if (root.strictString("schema_version") != VOICE_SCHEMA_VERSION) return null
        val connection = canonicalUuid4(root.strictString("connection_generation")) ?: return null
        val submission = canonicalUuid4(root.strictString("submission_id")) ?: return null
        val request = canonicalUuid4(root.strictString("request_generation")) ?: return null
        val payload = root.obj("payload") ?: return null
        if (!payload.hasExactKeys(
                "schema_version",
                "chat_id",
                "from_message",
                "connection_generation",
                "submission_id",
                "request_generation",
            )
        ) {
            return null
        }
        val chatId = canonicalUuid4(payload.strictString("chat_id")) ?: return null
        val fromMessage = payload.strictBoolean("from_message") ?: return null
        if (
            payload.strictString("schema_version") != VOICE_SCHEMA_VERSION ||
            payload.strictString("connection_generation") != connection ||
            payload.strictString("submission_id") != submission ||
            payload.strictString("request_generation") != request
        ) {
            return null
        }
        return Inbound.ChatCreated(chatId, connection, submission, request, fromMessage)
    }

    private fun messageAckFromJson(root: JsonObject): Inbound.UserMessageAcked? {
        if (root["schema_version"] == null) {
            return Inbound.UserMessageAcked(
                chatId = root.obj("payload")?.str("chat_id") ?: root.str("chat_id"),
                messageId = root.obj("payload")?.str("message_id") ?: root.str("message_id"),
            )
        }
        if (!root.hasExactKeys(
                "type",
                "schema_version",
                "chat_id",
                "message_id",
                "submission_id",
                "request_generation",
                "connection_generation",
                "voice_turn_id",
            )
        ) {
            return null
        }
        if (root.strictString("schema_version") != VOICE_SCHEMA_VERSION) return null
        val chatId = canonicalUuid4(root.strictString("chat_id")) ?: return null
        val messageId = root.strictPositiveInt("message_id")?.toString() ?: return null
        val submission = canonicalUuid4(root.strictString("submission_id")) ?: return null
        val request = canonicalUuid4(root.strictString("request_generation")) ?: return null
        val connection = canonicalUuid4(root.strictString("connection_generation")) ?: return null
        val voiceTurn = root.explicitNullableUuid("voice_turn_id") ?: return null
        return Inbound.UserMessageAcked(chatId, messageId, submission, request, connection, voiceTurn.value)
    }

    private fun composerStateFromJson(root: JsonObject): Inbound.ComposerState? {
        if (!root.hasExactKeys("type", "schema_version", "revision", "connection_generation", "voice")) return null
        if (root.strictString("schema_version") != VOICE_SCHEMA_VERSION) return null
        val revision = root.strictNonNegativeInt("revision") ?: return null
        val connection = canonicalUuid4(root.strictString("connection_generation")) ?: return null
        val voice = root.obj("voice")?.let(::voiceComposerModelFromJson) ?: return null
        return Inbound.ComposerState(revision, connection, voice)
    }

    private fun voiceComposerModelFromJson(root: JsonObject): VoiceComposerModel? {
        val required =
            setOf(
                "available",
                "state",
                "speech_muted",
                "microphone_enabled",
                "foreground_active",
                "reason",
                "output_locale",
                "chat_context_revision",
                "applied_chat_context_revision",
                "chat_context_synced",
                "controls",
            )
        val optional =
            setOf(
                "message",
                "session_id",
                "generation",
                "media_grant_revision",
                "visible_chat_id",
                "foreground_turn_id",
                "owner_device",
                "idle_expires_at",
            )
        if (!root.hasRequiredAndOptional(required, optional)) return null
        val available = root.strictBoolean("available") ?: return null
        val state = root.strictString("state")?.takeIf { it in VOICE_STATES } ?: return null
        val speechMuted = root.strictBoolean("speech_muted") ?: return null
        val microphoneEnabled = root.strictBoolean("microphone_enabled") ?: return null
        val foregroundActive = root.strictBoolean("foreground_active") ?: return null
        val reason = root.strictString("reason")?.takeIf { it in VOICE_REASONS } ?: return null
        if (root.strictString("output_locale") != "en-US") return null
        val contextRevision = root.explicitNullablePositiveInt("chat_context_revision") ?: return null
        val appliedRevision = root.explicitNullablePositiveInt("applied_chat_context_revision") ?: return null
        val contextSynced = root.strictBoolean("chat_context_synced") ?: return null
        val message = root.optionalBoundedString("message", 240) ?: return null
        val sessionId = root.optionalNullableUuid("session_id") ?: return null
        val generation = root.optionalNullablePositiveInt("generation") ?: return null
        val grantRevision = root.optionalNullablePositiveInt("media_grant_revision") ?: return null
        val visibleChat = root.optionalNullableUuid("visible_chat_id") ?: return null
        val foregroundTurn = root.optionalNullableUuid("foreground_turn_id") ?: return null
        val idleExpiry = root.optionalNullableTimestamp("idle_expires_at") ?: return null
        val owner = root.optionalOwnerDevice("owner_device") ?: return null
        val controls = root.arr("controls")?.map { (it as? JsonObject)?.let(::voiceControlFromJson) ?: return null } ?: return null
        if (controls.isEmpty() || controls.size > 12) return null
        if (!foregroundActive && (microphoneEnabled || state !in INACTIVE_VOICE_STATES)) return null
        if (foregroundActive && state !in ACTIVE_VOICE_STATES) return null
        return VoiceComposerModel(
            available,
            state,
            speechMuted,
            microphoneEnabled,
            foregroundActive,
            reason,
            "en-US",
            message.value,
            contextRevision.value,
            appliedRevision.value,
            contextSynced,
            sessionId.value,
            generation.value,
            grantRevision.value,
            visibleChat.value,
            foregroundTurn.value,
            owner.value,
            idleExpiry.value,
            controls,
        )
    }

    private fun voiceControlFromJson(root: JsonObject): VoiceControl? {
        if (!root.hasExactKeys("key", "action", "label", "icon", "visible", "enabled", "pressed", "busy")) return null
        val key = root.strictString("key")?.takeIf(::isOpaqueId) ?: return null
        val action = root.strictString("action")?.takeIf { it in VOICE_ACTIONS } ?: return null
        val label = root.strictString("label")?.takeIf { it.isNotEmpty() && it.length <= 80 } ?: return null
        val icon = root.strictString("icon")?.takeIf { it.isNotEmpty() && it.length <= 64 } ?: return null
        return VoiceControl(
            key,
            action,
            label,
            icon,
            root.strictBoolean("visible") ?: return null,
            root.strictBoolean("enabled") ?: return null,
            root.strictBoolean("pressed") ?: return null,
            root.strictBoolean("busy") ?: return null,
        )
    }

    private fun voiceControlBindingFromJson(root: JsonObject): VoiceControlBinding? {
        if (!root.hasExactKeys("type", "schema_version", "device_id", "connection_generation", "binding_id", "binding", "expires_at")) {
            return null
        }
        if (root.strictString("schema_version") != VOICE_SCHEMA_VERSION) return null
        val binding =
            root.strictString("binding")
                ?.takeIf { it.length in 32..512 && VOICE_BINDING_PATTERN.matches(it) }
                ?: return null
        return VoiceControlBinding(
            canonicalUuid4(root.strictString("device_id")) ?: return null,
            canonicalUuid4(root.strictString("connection_generation")) ?: return null,
            canonicalUuid4(root.strictString("binding_id")) ?: return null,
            binding,
            root.strictString("expires_at")?.takeIf(::isRfc3339Utc) ?: return null,
        )
    }

    private fun voiceSessionStateFromJson(root: JsonObject): VoiceSessionState? {
        val required =
            setOf(
                "type", "schema_version", "session_id", "connection_generation", "generation",
                "media_grant_revision", "visible_chat_id", "chat_context_revision",
                "applied_chat_context_revision", "chat_context_synced", "state", "speech_muted",
                "microphone_enabled", "foreground_active", "reason", "occurred_at",
            )
        if (!root.hasRequiredAndOptional(required, setOf("message"))) return null
        if (root.strictString("schema_version") != VOICE_SCHEMA_VERSION) return null
        val state = root.strictString("state")?.takeIf { it in VOICE_STATES } ?: return null
        val foreground = root.strictBoolean("foreground_active") ?: return null
        val microphone = root.strictBoolean("microphone_enabled") ?: return null
        if (!foreground && (microphone || state !in setOf("suspended", "reconnecting", "error", "ended"))) return null
        if (foreground && state !in ACTIVE_VOICE_STATES) return null
        return VoiceSessionState(
            canonicalUuid4(root.strictString("session_id")) ?: return null,
            canonicalUuid4(root.strictString("connection_generation")) ?: return null,
            root.strictPositiveInt("generation") ?: return null,
            root.strictPositiveInt("media_grant_revision") ?: return null,
            canonicalUuid4(root.strictString("visible_chat_id")) ?: return null,
            root.strictPositiveInt("chat_context_revision") ?: return null,
            (root.explicitNullablePositiveInt("applied_chat_context_revision") ?: return null).value,
            root.strictBoolean("chat_context_synced") ?: return null,
            state,
            root.strictBoolean("speech_muted") ?: return null,
            microphone,
            foreground,
            root.strictString("reason")?.takeIf { it in VOICE_REASONS } ?: return null,
            (root.optionalBoundedString("message", 240) ?: return null).value,
            root.strictString("occurred_at")?.takeIf(::isRfc3339Utc) ?: return null,
        )
    }

    private fun voiceTurnStateFromJson(root: JsonObject): VoiceTurnState? {
        val required =
            setOf(
                "type", "schema_version", "session_id", "connection_generation", "generation",
                "media_grant_revision", "turn_id", "client_turn_id", "submission_id",
                "request_generation", "chat_id", "chat_context_revision", "detected_language",
                "spoken_output_policy", "output_reason", "state", "foreground",
                "sensitive_result_pending", "sequence", "occurred_at",
            )
        if (!root.hasRequiredAndOptional(required, setOf("result_id", "message", "speech_outcome"))) return null
        if (root.strictString("schema_version") != VOICE_SCHEMA_VERSION) return null
        val language = root.explicitNullableLanguage("detected_language") ?: return null
        val policy = root.strictString("spoken_output_policy")?.takeIf { it in SPOKEN_OUTPUT_POLICIES } ?: return null
        val outputReason = root.strictString("output_reason")?.takeIf { it in OUTPUT_REASONS } ?: return null
        val turnState = root.strictString("state")?.takeIf { it in TURN_STATES } ?: return null
        when {
            language.value == null && (policy != "pending" || outputReason != "language_pending") -> return null
            language.value?.startsWith("en") == true && (policy != "full_recap" || outputReason != "ready") -> return null
            language.value != null && !language.value.startsWith("en") &&
                (policy != "english_lifecycle_only" || outputReason != "output_language_unsupported") -> return null
        }
        if (turnState == "recognizing" && language.value != null) return null
        if (turnState !in setOf("recognizing", "abandoned") && language.value == null) return null
        val speechOutcome =
            if ("speech_outcome" in root) {
                VoiceSpeechOutcome.fromWireValue(root.strictString("speech_outcome")) ?: return null
            } else {
                null
            }
        if (speechOutcome != null && turnState != "succeeded") return null
        return VoiceTurnState(
            canonicalUuid4(root.strictString("session_id")) ?: return null,
            canonicalUuid4(root.strictString("connection_generation")) ?: return null,
            root.strictPositiveInt("generation") ?: return null,
            root.strictPositiveInt("media_grant_revision") ?: return null,
            canonicalUuid4(root.strictString("turn_id")) ?: return null,
            canonicalUuid4(root.strictString("client_turn_id")) ?: return null,
            canonicalUuid4(root.strictString("submission_id")) ?: return null,
            canonicalUuid4(root.strictString("request_generation")) ?: return null,
            canonicalUuid4(root.strictString("chat_id")) ?: return null,
            root.strictPositiveInt("chat_context_revision") ?: return null,
            language.value,
            policy,
            outputReason,
            turnState,
            root.strictBoolean("foreground") ?: return null,
            root.strictBoolean("sensitive_result_pending") ?: return null,
            root.strictNonNegativeInt("sequence") ?: return null,
            speechOutcome,
            (root.optionalNullableOpaqueId("result_id") ?: return null).value,
            (root.optionalBoundedString("message", 240) ?: return null).value,
            root.strictString("occurred_at")?.takeIf(::isRfc3339Utc) ?: return null,
        )
    }

    private fun voiceSubmissionRejectedFromJson(root: JsonObject): VoiceSubmissionRejected? {
        val required =
            setOf(
                "type", "schema_version", "session_id", "connection_generation", "generation",
                "media_grant_revision", "turn_id", "client_turn_id", "submission_id",
                "request_generation", "chat_id", "reason", "retry_policy", "occurred_at",
            )
        if (!root.hasRequiredAndOptional(required, setOf("message"))) return null
        if (root.strictString("schema_version") != VOICE_SCHEMA_VERSION) return null
        return VoiceSubmissionRejected(
            canonicalUuid4(root.strictString("session_id")) ?: return null,
            canonicalUuid4(root.strictString("connection_generation")) ?: return null,
            root.strictPositiveInt("generation") ?: return null,
            root.strictPositiveInt("media_grant_revision") ?: return null,
            canonicalUuid4(root.strictString("turn_id")) ?: return null,
            canonicalUuid4(root.strictString("client_turn_id")) ?: return null,
            canonicalUuid4(root.strictString("submission_id")) ?: return null,
            canonicalUuid4(root.strictString("request_generation")) ?: return null,
            canonicalUuid4(root.strictString("chat_id")) ?: return null,
            root.strictString("reason")?.takeIf { it in REJECTION_REASONS } ?: return null,
            root.strictString("retry_policy")?.takeIf { it in setOf("explicit_user_retry", "none") } ?: return null,
            (root.optionalBoundedString("message", 240) ?: return null).value,
            root.strictString("occurred_at")?.takeIf(::isRfc3339Utc) ?: return null,
        )
    }

    private fun voiceTranscriptFromJson(root: JsonObject): VoiceTranscript? {
        val required =
            setOf(
                "type", "schema_version", "session_id", "generation", "turn_id", "client_turn_id",
                "submission_id", "request_generation", "chat_id", "chat_context_revision",
                "media_grant_revision", "sequence", "final", "text", "detected_language",
                "source_participant_identity",
            )
        val proofFields = setOf("text_digest_sha256", "transcript_proof", "proof_expires_at")
        if (!root.hasRequiredAndOptional(required, proofFields)) return null
        if (root.strictString("schema_version") != VOICE_SCHEMA_VERSION) return null
        val final = root.strictBoolean("final") ?: return null
        val text = root.strictString("text")?.takeIf { it.length <= 8000 } ?: return null
        val language = root.explicitNullableLanguage("detected_language") ?: return null
        val digest = root.strictString("text_digest_sha256")
        val proof = root.strictString("transcript_proof")
        val proofExpiry = root.strictString("proof_expires_at")
        if (final) {
            if (text.isBlank() || language.value == null || digest?.matches(lowerSha256) != true ||
                proof?.matches(lowerSha256) != true || proofExpiry?.let(::isRfc3339Utc) != true
            ) {
                return null
            }
        } else if (proofFields.any(root::containsKey)) {
            return null
        }
        return VoiceTranscript(
            canonicalUuid4(root.strictString("session_id")) ?: return null,
            root.strictPositiveInt("generation") ?: return null,
            canonicalUuid4(root.strictString("turn_id")) ?: return null,
            canonicalUuid4(root.strictString("client_turn_id")) ?: return null,
            canonicalUuid4(root.strictString("submission_id")) ?: return null,
            canonicalUuid4(root.strictString("request_generation")) ?: return null,
            canonicalUuid4(root.strictString("chat_id")) ?: return null,
            root.strictPositiveInt("chat_context_revision") ?: return null,
            root.strictPositiveInt("media_grant_revision") ?: return null,
            root.strictNonNegativeInt("sequence") ?: return null,
            final,
            text,
            language.value,
            digest,
            proof,
            proofExpiry,
            root.strictString("source_participant_identity")?.takeIf(::isOpaqueId) ?: return null,
        )
    }

    private fun voiceAnnouncementFromJson(root: JsonObject): VoiceAnnouncementMedia? {
        val required =
            setOf(
                "type", "schema_version", "session_id", "generation", "media_grant_revision",
                "announcement_id", "announcement_sequence", "turn_id", "kind", "quantum_role",
                "quantum_index", "transport", "worker_identity", "sample_rate_hz", "duration_samples",
            )
        val optional =
            setOf(
                "result_reserved_samples_after",
                "track_sid",
                "track_name",
                "first_media_sequence",
                "last_media_sequence",
            )
        if (!root.hasRequiredAndOptional(required, optional)) return null
        if (root.strictString("schema_version") != VOICE_SCHEMA_VERSION) return null
        val turn = root.explicitNullableUuid("turn_id") ?: return null
        val kind = root.strictString("kind")?.takeIf { it in ANNOUNCEMENT_KINDS } ?: return null
        val role = root.strictString("quantum_role")?.takeIf { it in QUANTUM_ROLES } ?: return null
        val index = root.strictNonNegativeInt("quantum_index")?.takeIf { it <= 31 } ?: return null
        val transport = root.strictString("transport")?.takeIf { it in VOICE_TRANSPORTS } ?: return null
        val duration = root.strictPositiveInt("duration_samples")?.takeIf { it <= 96_000 } ?: return null
        val reserved = root.optionalPositiveInt("result_reserved_samples_after", 720_000) ?: return null
        val trackSid = root.optionalOpaqueId("track_sid") ?: return null
        val trackName = root.optionalOpaqueId("track_name") ?: return null
        val firstSequence = root.optionalNonNegativeInt("first_media_sequence") ?: return null
        val lastSequence = root.optionalNonNegativeInt("last_media_sequence") ?: return null
        if ((kind == "greeting") != (turn.value == null)) return null
        when (role) {
            "single" -> if (kind == "result" || index != 0 || reserved.value != null) return null
            "result_opening" -> {
                val reservedSamples = reserved.value ?: return null
                if (kind != "result" || index != 0 || duration > 36_000 || reservedSamples !in 1..36_000) return null
            }
            "result_continuation" ->
                if (kind != "result" || index !in 1..31 || reserved.value == null) return null
        }
        when (transport) {
            "livekit" ->
                if (
                    trackSid.value == null ||
                    trackName.value == null ||
                    firstSequence.value != null ||
                    lastSequence.value != null
                ) {
                    return null
                }
            "watch_pcm_websocket" -> {
                val first = firstSequence.value ?: return null
                val last = lastSequence.value ?: return null
                if (trackSid.value != null || trackName.value != null || last < first) return null
            }
        }
        return VoiceAnnouncementMedia(
            canonicalUuid4(root.strictString("session_id")) ?: return null,
            root.strictPositiveInt("generation") ?: return null,
            root.strictPositiveInt("media_grant_revision") ?: return null,
            canonicalUuid4(root.strictString("announcement_id")) ?: return null,
            root.strictPositiveInt("announcement_sequence") ?: return null,
            turn.value,
            kind,
            role,
            index,
            transport,
            root.strictString("worker_identity")?.takeIf(::isOpaqueId) ?: return null,
            root.strictPositiveInt("sample_rate_hz")?.takeIf { it == 24_000 } ?: return null,
            duration,
            reserved.value,
            trackSid.value,
            trackName.value,
        )
    }

    private fun parseObject(raw: String): JsonObject? = runCatching { json.parseToJsonElement(raw) as? JsonObject }.getOrNull()

    private fun JsonObject.hasExactKeys(vararg keys: String): Boolean = this.keys == keys.toSet()

    private fun JsonObject.hasRequiredAndOptional(
        required: Set<String>,
        optional: Set<String>,
    ): Boolean = keys.containsAll(required) && keys.all { it in required || it in optional }

    private fun JsonObject.strictString(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.content

    private fun JsonObject.strictBoolean(key: String): Boolean? = (this[key] as? JsonPrimitive)?.takeIf { !it.isString }?.booleanOrNull

    private fun JsonObject.strictPositiveInt(key: String): Int? =
        (this[key] as? JsonPrimitive)?.takeIf { !it.isString }?.intOrNull?.takeIf { it > 0 }

    private fun JsonObject.strictNonNegativeInt(key: String): Int? =
        (this[key] as? JsonPrimitive)?.takeIf { !it.isString }?.intOrNull?.takeIf { it >= 0 }

    private fun JsonObject.strictULong(key: String): ULong? =
        (this[key] as? JsonPrimitive)?.takeIf { !it.isString }?.content?.toULongOrNull()

    private fun JsonObject.explicitNullableString(key: String): ExplicitNullable<String>? {
        val element = this[key] ?: return null
        if (element is JsonNull) return ExplicitNullable(null)
        return strictString(key)?.let(::ExplicitNullable)
    }

    private fun JsonObject.explicitNullableUuid(key: String): ExplicitNullable<String>? {
        val element = this[key] ?: return null
        if (element is JsonNull) return ExplicitNullable(null)
        return canonicalUuid4(strictString(key))?.let(::ExplicitNullable)
    }

    private fun JsonObject.explicitNullablePositiveInt(key: String): ExplicitNullable<Int>? {
        val element = this[key] ?: return null
        if (element is JsonNull) return ExplicitNullable(null)
        return strictPositiveInt(key)?.let(::ExplicitNullable)
    }

    private fun JsonObject.explicitNullableLanguage(key: String): ExplicitNullable<String>? {
        val element = this[key] ?: return null
        if (element is JsonNull) return ExplicitNullable(null)
        return strictString(key)?.takeIf { it.length in 2..32 && languageTag.matches(it) }?.let(::ExplicitNullable)
    }

    private fun JsonObject.optionalBoundedString(
        key: String,
        maxLength: Int,
    ): ExplicitNullable<String>? {
        if (!containsKey(key)) return ExplicitNullable(null)
        return strictString(key)?.takeIf { it.length <= maxLength }?.let(::ExplicitNullable)
    }

    private fun JsonObject.optionalNullableUuid(key: String): ExplicitNullable<String>? {
        val element = this[key] ?: return ExplicitNullable(null)
        if (element is JsonNull) return ExplicitNullable(null)
        return canonicalUuid4(strictString(key))?.let(::ExplicitNullable)
    }

    private fun JsonObject.optionalNullablePositiveInt(key: String): ExplicitNullable<Int>? {
        val element = this[key] ?: return ExplicitNullable(null)
        if (element is JsonNull) return ExplicitNullable(null)
        return strictPositiveInt(key)?.let(::ExplicitNullable)
    }

    private fun JsonObject.optionalNullableTimestamp(key: String): ExplicitNullable<String>? {
        val element = this[key] ?: return ExplicitNullable(null)
        if (element is JsonNull) return ExplicitNullable(null)
        return strictString(key)?.takeIf(::isRfc3339Utc)?.let(::ExplicitNullable)
    }

    private fun JsonObject.optionalOwnerDevice(key: String): ExplicitNullable<VoiceOwnerDevice>? {
        val element = this[key] ?: return ExplicitNullable(null)
        if (element is JsonNull) return ExplicitNullable(null)
        val owner = element as? JsonObject ?: return null
        if (!owner.hasRequiredAndOptional(setOf("device_id", "device_kind", "generation"), setOf("device_label"))) return null
        val label = owner.optionalBoundedString("device_label", 80) ?: return null
        return ExplicitNullable(
            VoiceOwnerDevice(
                deviceId = canonicalUuid4(owner.strictString("device_id")) ?: return null,
                deviceKind =
                    owner.strictString("device_kind")
                        ?.takeIf { it in setOf("web", "windows", "android", "ios", "macos", "watchos") }
                        ?: return null,
                deviceLabel = label.value,
                generation = owner.strictPositiveInt("generation") ?: return null,
            ),
        )
    }

    private fun JsonObject.optionalNullableOpaqueId(key: String): ExplicitNullable<String>? {
        val element = this[key] ?: return ExplicitNullable(null)
        if (element is JsonNull) return ExplicitNullable(null)
        return strictString(key)?.takeIf(::isOpaqueId)?.let(::ExplicitNullable)
    }

    private fun JsonObject.optionalPositiveInt(
        key: String,
        maximum: Int,
    ): ExplicitNullable<Int>? {
        if (!containsKey(key)) return ExplicitNullable(null)
        return strictPositiveInt(key)?.takeIf { it <= maximum }?.let(::ExplicitNullable)
    }

    private fun JsonObject.optionalNonNegativeInt(key: String): ExplicitNullable<Int>? {
        if (!containsKey(key)) return ExplicitNullable(null)
        return strictNonNegativeInt(key)?.let(::ExplicitNullable)
    }

    private fun JsonObject.optionalOpaqueId(key: String): ExplicitNullable<String>? {
        if (!containsKey(key)) return ExplicitNullable(null)
        return strictString(key)?.takeIf(::isOpaqueId)?.let(::ExplicitNullable)
    }

    private fun JsonObject.positiveSortedVersions(key: String): List<Int>? {
        val values = arr(key) ?: return null
        val versions =
            values.map { element ->
                val primitive = (element as? JsonPrimitive)?.takeIf { !it.isString } ?: return null
                val value = primitive.content.toULongOrNull() ?: return null
                if (value == 0UL || value > Int.MAX_VALUE.toULong()) return null
                value.toInt()
            }
        return versions.takeIf { it == it.distinct().sorted() }
    }

    private fun canonicalUuid4(value: String?): String? {
        if (value == null) return null
        val parsed = runCatching { UUID.fromString(value) }.getOrNull() ?: return null
        return value.takeIf { parsed.version() == 4 && parsed.toString() == value }
    }

    private fun isRfc3339Utc(value: String): Boolean = value.endsWith("Z") && runCatching { Instant.parse(value) }.isSuccess

    private fun isSnakeCase(value: String): Boolean = snakeCase.matches(value)

    private fun isOpaqueId(value: String): Boolean = value.length in 1..128 && opaqueId.matches(value)

    // ---- legacy-compatible helpers ----

    private fun JsonObject.str(key: String): String? = (this[key] as? JsonPrimitive)?.contentOrNull

    private fun JsonObject.int(key: String): Int? = (this[key] as? JsonPrimitive)?.intOrNull

    private fun JsonObject.bool(key: String): Boolean? = (this[key] as? JsonPrimitive)?.booleanOrNull

    private fun JsonObject.arr(key: String): JsonArray? = this[key] as? JsonArray

    private fun JsonObject.obj(key: String): JsonObject? = this[key] as? JsonObject

    private fun JsonObject.boolMap(key: String): Map<String, Boolean> =
        (this[key] as? JsonObject)?.entries
            ?.associate { (k, v) -> k to ((v as? JsonPrimitive)?.booleanOrNull ?: false) } ?: emptyMap()

    private fun JsonObject.strMap(key: String): Map<String, String> =
        (this[key] as? JsonObject)?.entries
            ?.associate { (k, v) -> k to ((v as? JsonPrimitive)?.contentOrNull ?: "") } ?: emptyMap()

    private fun JsonObject.strList(key: String): List<String> =
        (this[key] as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull } ?: emptyList()

    private fun opsFromJson(arr: JsonArray?): List<CanvasOp> =
        arr?.mapNotNull { el ->
            val o = el as? JsonObject ?: return@mapNotNull null
            val cid = o.str("component_id") ?: return@mapNotNull null
            CanvasOp(
                op = o.str("op") ?: "upsert",
                componentId = cid,
                component = o.obj("component")?.let { Component.fromJson(it) },
            )
        } ?: emptyList()

    // components_combined/condensed results are saved-row shapes ({id,
    // component_data, …}); the primitive dict rides in `component_data` and may
    // not carry a workspace identity yet (the reconcile ui_render that follows
    // stamps it), so identity falls back to the fresh row id.
    private fun replacementsFromJson(arr: JsonArray?): List<Component> =
        arr?.mapIndexedNotNull { i, el ->
            val row = el as? JsonObject ?: return@mapIndexedNotNull null
            val data = row.obj("component_data") ?: return@mapIndexedNotNull null
            val comp = Component.fromJson(data)
            if (comp.id != null) comp else comp.copy(id = row.str("id") ?: "combined-$i")
        } ?: emptyList()

    private fun errorFromJson(o: JsonObject?): StreamError? =
        o?.let {
            StreamError(
                code = it.str("code"),
                message = it.str("message"),
                retryable = it.bool("retryable") ?: false,
                phase = it.str("phase"),
            )
        }

    private fun agentsFromJson(arr: JsonArray?): List<Agent> =
        arr?.mapNotNull { el ->
            val o = el as? JsonObject ?: return@mapNotNull null
            val id = o.str("id") ?: return@mapNotNull null
            val permissions = o.boolMap("permissions")
            // `tools` is a list of {name, description} (send_agent_list) OR plain
            // strings (dashboard); fall back to the permission keys.
            val toolObjs = (o["tools"] as? JsonArray)?.mapNotNull { it as? JsonObject }.orEmpty()
            val tools: List<String>
            val toolDescriptions: Map<String, String>
            if (toolObjs.isNotEmpty()) {
                tools = toolObjs.mapNotNull { it.str("name") }
                toolDescriptions =
                    toolObjs.mapNotNull { t -> t.str("name")?.let { it to t.str("description").orEmpty() } }.toMap()
            } else {
                tools = o.strList("tools").ifEmpty { permissions.keys.toList() }
                toolDescriptions = o.strMap("tool_descriptions")
            }
            Agent(
                id = id,
                name = o.str("name") ?: id,
                description = o.str("description").orEmpty(),
                isPublic = o.bool("is_public") ?: false,
                scopes = o.boolMap("scopes"),
                tools = tools,
                toolDescriptions = toolDescriptions,
                permissions = permissions,
                toolScopeMap = o.strMap("tool_scope_map"),
            )
        } ?: emptyList()

    private fun chatsFromJson(arr: JsonArray?): List<ChatSummary> =
        arr?.mapNotNull { el ->
            val o = el as? JsonObject ?: return@mapNotNull null
            val id = o.str("id") ?: return@mapNotNull null
            ChatSummary(id, o.str("title").orEmpty())
        } ?: emptyList()

    private fun transcriptFromJson(o: JsonObject?): ChatTranscript {
        if (o == null) return ChatTranscript(null, emptyList())
        val msgsArr = (o["messages"] as? JsonArray) ?: (o["history"] as? JsonArray)
        val msgs =
            msgsArr?.mapNotNull { el ->
                val m = el as? JsonObject ?: return@mapNotNull null
                val content = m.str("content") ?: m.str("text") ?: ""
                val role = m.str("role") ?: if (m.bool("is_user") == true) "user" else "assistant"
                ChatTurn(role, content)
            } ?: emptyList()
        return ChatTranscript(o.str("id"), msgs)
    }
}
